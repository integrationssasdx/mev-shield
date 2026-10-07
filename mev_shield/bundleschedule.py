"""不可拆分交易捆绑的联合排程：python -m mev_shield.bundleschedule

在统一决策入口（``python -m mev_shield.decision``）与多区块排程
（``python -m mev_shield.schedule``）的输入、校验、fee 降序 / hash
升序基线、相邻三段夹子证据与 nonce 依赖之上，把带非空 ``bundle``
标识的交易按同值标识组成不可拆分捆绑，整组排入多区块窗口。既有
入口的输入、输出与退出码均保持不变。

输入在多区块排程计划的字段（``block`` / ``scheduleBlocks`` /
``blockCapacity`` 与交易级可选 ``deadline``）之上，要求每笔交易携带
非空字符串 ``bundle``；缺失或非非空字符串返回 ``BAD_BUNDLE_ID``。
该校验排在多区块排程四项校验之后，统一决策入口全部既有校验（含
``BAD_SLIPPAGE_MODE``）一律优先。

捆绑规则：

- 同 ``bundle`` 值的交易组成一个捆绑，段内顺序取输入相对位置；
  捆绑初始次序取首笔交易的输入位置；
- 每个捆绑只能整组进入一个区块并占一个连续段，段内顺序不变；
  全局顺序按区块、段顺序拼接，恰好覆盖已选交易；
- 任一成员的 ``deadline`` 早于起始区块则整组过期；否则只能进入
  全部成员期限允许且剩余容量足以容纳整组的区块；
- 最终全局顺序中同一 ``from`` 的 nonce 严格递增，且按统一决策的
  相邻三段规则无任何夹子证据。

择优目标依次为：排程交易笔数最多、排程 fee 总和最高、
``totalDelay``（各交易所在区块减 ``block`` 之和）最小、全局顺序
对应输入下标序列字典序最小。枚举全部捆绑排程方案求全局最优，不做
逐笔贪心；相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``、``blocks``（窗口每个区块的
``block`` / ``order``）、``unscheduled``（按捆绑首笔位置列出
``bundle`` / ``at`` / ``hashes`` / ``reason``，原因仅
``DEADLINE_EXPIRED``、``CAPACITY_EXCEEDED``、``BUNDLE_SKIPPED``）、
``scheduledFee``、``unscheduledFee``、``totalDelay``、``evidence``
（未过期基线子序列上的全部夹子证据，按起始位置升序、同位按 victim
hash 升序）、``feasible``。

- ``feasible`` 仅在全部捆绑排入时为 true；否则为 false，仍输出最优
  部分排程，退出 0，stderr 不写码；
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形：
  列表为空、数值为 0、``feasible`` 为 false。
"""

import json
import sys

from . import core
from . import decision
from . import schedule
from .cli import parse_args

# 输入错误码：交易 bundle 缺失或非非空字符串
BAD_BUNDLE_ID = "BAD_BUNDLE_ID"

# 未排程原因码：整组过期 / 无任何区块可容纳整组 / 最优方案未选
DEADLINE_EXPIRED = core.DEADLINE_EXPIRED
CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
BUNDLE_SKIPPED = "BUNDLE_SKIPPED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先执行多区块排程计划的全部校验（统一决策校验 + block /
    scheduleBlocks / blockCapacity / deadline），再逐笔校验 bundle：
    必须为非空字符串，否则抛 BAD_BUNDLE_ID。
    """
    req = schedule.parse_request(raw)
    # 排程校验已通过，raw 必为合法 JSON 对象且 transactions 均为对象
    data = json.loads(raw)

    bundles = []
    for tx in data["transactions"]:
        bid = tx.get("bundle")
        if not isinstance(bid, str) or not bid:
            raise decision.DecisionError(BAD_BUNDLE_ID)
        bundles.append(bid)
    for parsed, bid in zip(req["transactions"], bundles):
        parsed["bundle"] = bid
    return req


def error_result(ident):
    """输入错误结果：列表为空，数值为 0，feasible 为 false。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "blocks": [],
        "unscheduled": [],
        "scheduledFee": 0,
        "unscheduledFee": 0,
        "totalDelay": 0,
        "evidence": [],
        "feasible": False,
    }


def _build_bundles(txs):
    """按输入顺序构造捆绑：成员保持输入相对位置，捆绑按首笔位置排序。

    返回 (groups, group_of)：group_of[at] 为该位置交易所属捆绑下标。
    """
    index = {}
    groups = []
    group_of = [0] * len(txs)
    for at, tx in enumerate(txs):
        bid = tx["bundle"]
        if bid not in index:
            index[bid] = len(groups)
            groups.append({"bundle": bid, "first": at, "members": []})
        g = index[bid]
        groups[g]["members"].append(at)
        group_of[at] = g
    groups.sort(key=lambda grp: grp["first"])
    # 排序后重建 输入位置 -> 捆绑下标 映射
    group_of = [0] * len(txs)
    for gi, grp in enumerate(groups):
        for at in grp["members"]:
            group_of[at] = gi
    return groups, group_of


def _search(groups, txs, block, window, capacity, expired_groups, input_pos):
    """枚举全部捆绑排程方案，返回 (best_key, best_assign)。

    groups 按首笔位置升序；assign[g] 为区块偏移或 -1（整组不排）。
    一个区块内各捆绑段按首笔位置升序排列，段内成员按输入位置排列。
    比较键：排程笔数最多、fee 总和最高、totalDelay 最小、全局顺序
    对应输入下标序列字典序最小。以当前最优的笔数与 fee 上界剪枝。
    空排程恒合法，故 best_assign 不会为 None。
    """
    g = len(groups)

    # 每个捆绑：成员、大小、fee 与可进的最大区块偏移（窗口内）
    members = [grp["members"] for grp in groups]
    sizes = [len(m) for m in members]
    fees = [sum(txs[at]["fee"] for at in m) for m in members]
    last = block + window - 1
    latest = []
    for gi in range(g):
        horizon = last
        for at in members[gi]:
            deadline = txs[at]["deadline"]
            if deadline is not None and deadline < horizon:
                horizon = deadline
        latest.append(horizon - block)

    # 后缀 fee 和：笔数上界持平最优时用于 fee 上界剪枝
    suffix_count = [0] * (g + 1)
    suffix_fee = [0] * (g + 1)
    for gi in range(g - 1, -1, -1):
        suffix_count[gi] = suffix_count[gi + 1] + sizes[gi]
        suffix_fee[gi] = suffix_fee[gi + 1] + fees[gi]

    loads = [0] * window
    assign = [-1] * g
    best_key = None
    best_assign = None

    def evaluate():
        """由 assign 还原全局顺序并校验，合法则返回比较键。"""
        per_block = [[] for _ in range(window)]
        delay = 0
        for gi, off in enumerate(assign):
            if off < 0:
                continue
            # 区块内段按捆绑首笔位置升序（即 groups 次序），段内保持
            # 输入相对位置
            per_block[off].extend(txs[at] for at in members[gi])
            delay += off * sizes[gi]
        ordered = []
        for off in range(window):
            ordered.extend(per_block[off])
        if not decision.nonce_order_satisfied(ordered):
            return None
        if decision.detect_sandwich_evidence(ordered):
            return None
        fee = sum(tx["fee"] for tx in ordered)
        seq = tuple(input_pos[tx["hash"]] for tx in ordered)
        return (-len(ordered), -fee, delay, seq)

    def dfs(gi, placed, fee_so_far):
        nonlocal best_key, best_assign
        if gi == g:
            key = evaluate()
            if key is not None and (best_key is None or key < best_key):
                best_key = key
                best_assign = list(assign)
            return
        if best_key is not None:
            # 笔数上界已低于最优，或持平最优时 fee 上界已低于最优
            if placed + suffix_count[gi] < -best_key[0]:
                return
            if (placed + suffix_count[gi] == -best_key[0]
                    and fee_so_far + suffix_fee[gi] < -best_key[1]):
                return
        # 选择一：整组不排程
        dfs(gi + 1, placed, fee_so_far)
        # 选择二：整组排入不晚于 latest[gi] 且剩余容量足够的区块
        if gi not in expired_groups:
            size = sizes[gi]
            for off in range(latest[gi] + 1):
                if loads[off] + size > capacity:
                    continue
                loads[off] += size
                assign[gi] = off
                dfs(gi + 1, placed + size, fee_so_far + fees[gi])
                assign[gi] = -1
                loads[off] -= size

    dfs(0, 0, 0)
    return best_key, best_assign


def bundle_schedule(req):
    """枚举全部捆绑排程方案求全局最优，返回结果字典。"""
    txs = req["transactions"]
    block = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]

    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]
    input_pos = {tx["hash"]: at for at, tx in enumerate(txs)}

    groups, group_of = _build_bundles(txs)

    # 整组过期：任一成员 deadline 早于起始区块；该组不参与排程与证据
    expired_members = set()
    expired_groups = set()
    for at, tx in enumerate(txs):
        deadline = tx["deadline"]
        if deadline is not None and deadline < block:
            expired_members.add(at)
            expired_groups.add(group_of[at])

    # 证据取自未过期交易构成的基线子序列；起始位置唯一，victim 仅作
    # 稳定次序兜底
    active_baseline = [
        tx for tx in baseline if input_pos[tx["hash"]] not in expired_members
    ]
    evidence = sorted(
        decision.detect_sandwich_evidence(active_baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )

    best_key, best_assign = _search(
        groups, txs, block, window, capacity, expired_groups, input_pos)

    # 由最优方案还原各块顺序：区块内段按捆绑首笔位置升序拼接
    placed_groups = [[] for _ in range(window)]
    scheduled_set = set()
    for gi, off in enumerate(best_assign):
        if off < 0:
            continue
        placed_groups[off].append(gi)
    blocks = []
    for off in range(window):
        order_hashes = []
        for gi in placed_groups[off]:
            for at in groups[gi]["members"]:
                order_hashes.append(txs[at]["hash"])
                scheduled_set.add(txs[at]["hash"])
        blocks.append({"block": block + off, "order": order_hashes})

    scheduled_fee = sum(tx["fee"] for tx in txs if tx["hash"] in scheduled_set)
    total_fee = sum(tx["fee"] for tx in txs)

    # 未排程捆绑按首笔位置排列；原因优先级：整组过期 > 超容量 > 其余
    unscheduled = []
    all_placed = True
    for gi, grp in enumerate(groups):
        if best_assign[gi] >= 0:
            continue
        all_placed = False
        member_at = grp["members"]
        hashes = [txs[at]["hash"] for at in member_at]
        if gi in expired_groups:
            reason = DEADLINE_EXPIRED
        else:
            last = block + window - 1
            horizon = last
            for at in member_at:
                deadline = txs[at]["deadline"]
                if deadline is not None and deadline < horizon:
                    horizon = deadline
            size = len(member_at)
            fits = any(
                horizon >= block + off and size <= capacity
                for off in range(window)
            )
            reason = CAPACITY_EXCEEDED if not fits else BUNDLE_SKIPPED
        unscheduled.append(
            {
                "bundle": grp["bundle"],
                "at": grp["first"],
                "hashes": hashes,
                "reason": reason,
            }
        )

    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "blocks": blocks,
        "unscheduled": unscheduled,
        "scheduledFee": scheduled_fee,
        "unscheduledFee": total_fee - scheduled_fee,
        "totalDelay": best_key[2],
        "evidence": evidence,
        "feasible": all_placed,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用多区块排程计划并追加 bundle 校验；error_code 非空时
    退出码 2。捆绑未能全部排程是正常结论，error_code 为 None。
    """
    try:
        req = parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident), exc.code
    return bundle_schedule(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次捆绑联合排程，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码；
    部分排程（feasible 为 false）退出 0，不写 stderr。
    """
    if argv is None:
        argv = sys.argv[1:]
    if stdin_buffer is None:
        stdin_buffer = sys.stdin.buffer
    if stdout_buffer is None:
        stdout_buffer = sys.stdout.buffer
    if stderr is None:
        stderr = sys.stderr

    opts = parse_args(argv)
    if opts is None:
        # 参数不可信，错误 JSON 写标准输出；stdout 也不可写时仅留 stderr。
        payload = serialize(error_result("")).encode("utf-8")
        try:
            stdout_buffer.write(payload)
            stdout_buffer.flush()
        except OSError:
            pass
        _stderr(core.BAD_ARGS, stderr)
        return _EXIT_ERROR

    try:
        if opts["input"] is None:
            raw = stdin_buffer.read()
        else:
            with open(opts["input"], "rb") as f:
                raw = f.read()
    except OSError:
        result, prior_code = error_result(""), core.INPUT_IO
    else:
        result, prior_code = process(raw)

    payload = serialize(result).encode("utf-8")
    exit_code = _EXIT_ERROR if prior_code is not None else _EXIT_OK

    try:
        if opts["output"] is None:
            stdout_buffer.write(payload)
            stdout_buffer.flush()
        else:
            with open(opts["output"], "wb") as f:
                f.write(payload)
    except OSError:
        # 输出不可写：仅向 stderr 写 code（已有更高优先级错误码时保留之）
        _stderr(prior_code if prior_code is not None else core.OUTPUT_IO, stderr)
        return _EXIT_ERROR

    if prior_code is not None:
        _stderr(prior_code, stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(run())
