"""多区块排程计划：python -m mev_shield.schedule [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验、
fee 降序 / hash 升序基线、相邻三段夹子证据与 nonce 依赖之上，把
未过期交易排入一个多区块窗口：窗口为 ``block`` 到
``block + scheduleBlocks - 1``，每个区块至多容纳 ``blockCapacity``
笔交易。既有入口的输入、输出与退出码均保持不变。

输入在统一决策入口的根对象上新增字段：

- ``block``：窗口起始区块，必需，非负 JSON 整数（排除布尔值）；
- ``scheduleBlocks``：窗口区块数，必需，正 JSON 整数（排除布尔值）；
- ``blockCapacity``：每区块容量，必需，正 JSON 整数（排除布尔值）；
- 交易级 ``deadline``：可选，最后可执行区块，非负 JSON 整数（排除
  布尔值）；缺失表示无期限，可进窗口内任意区块。

四项校验依次返回 ``BAD_BLOCK``、``BAD_SCHEDULE_WINDOW``、
``BAD_BLOCK_CAPACITY``、``BAD_DEADLINE``，排在统一决策入口全部既有
校验（含 ``BAD_SLIPPAGE_MODE``）之后，旧错误一律优先。

排程规则：

- ``deadline < block`` 的交易过期：不参与排程，也不参与夹子判定；
- 未过期交易至多进一个不晚于自身 ``deadline`` 的窗口区块，无
  ``deadline`` 可进任意窗口区块；
- 各块内按 fee 降序、hash 升序，跨块按区块升序拼接得
  ``scheduledOrder``；该顺序上同一 ``from`` 的 nonce 严格递增，且
  按统一决策的相邻三段规则无任何夹子证据。

择优目标依次为：排程笔数最多、排程 fee 总和最高、``totalDelay``
（各交易所在区块减 ``block`` 之和）最小、``scheduledOrder`` 对应
输入下标序列字典序最小。枚举全部排程方案求全局最优，不做逐笔贪心；
结果确定，相同输入逐字一致。

输出字段固定：``id``、``block``、``scheduleBlocks``、
``blockCapacity``、``baselineOrder``（全部交易的 fee 降序、hash
升序基线顺序）、``blocks``（窗口每个区块的 ``block`` / ``order``）、
``scheduledOrder``、``unscheduled``（按输入位置列出 ``hash`` /
``at`` / ``reason``，原因仅 ``DEADLINE_EXPIRED`` 与
``SCHEDULE_SKIPPED``）、``scheduledFee``、``unscheduledFee``、
``totalDelay``、``evidence``（未过期基线子序列上的全部夹子证据，
按起始位置升序、同位按 victim hash 升序）、``feasible``。

- ``feasible`` 仅在未过期交易全部排程时为 true；否则为 false，
  仍输出最优部分排程，退出 0，stderr 不写码；
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形：
  列表为空、数值为 0、``feasible`` 为 false。
"""

import json
import sys

from . import core
from . import decision
from .cli import parse_args

# 输入错误码：排程字段缺失、类型错误、布尔值或越界
BAD_BLOCK = "BAD_BLOCK"
BAD_SCHEDULE_WINDOW = "BAD_SCHEDULE_WINDOW"
BAD_BLOCK_CAPACITY = "BAD_BLOCK_CAPACITY"
BAD_DEADLINE = core.BAD_DEADLINE

# 未排程原因码：过期交易与最优方案未选中的交易
DEADLINE_EXPIRED = core.DEADLINE_EXPIRED
SCHEDULE_SKIPPED = "SCHEDULE_SKIPPED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def _is_positive_int(value):
    # JSON 正整数；bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE），再依次
    校验 block（非负整数）、scheduleBlocks（正整数）、blockCapacity
    （正整数）与各交易可选 deadline（非负整数），分别抛 BAD_BLOCK、
    BAD_SCHEDULE_WINDOW、BAD_BLOCK_CAPACITY、BAD_DEADLINE。
    """
    req = decision.parse_request(raw)
    # 统一决策校验已通过，raw 必为合法 JSON 对象且 transactions 均为对象
    data = json.loads(raw)

    block = data.get("block")
    if not decision._is_nonneg_int(block):
        raise decision.DecisionError(BAD_BLOCK)
    window = data.get("scheduleBlocks")
    if not _is_positive_int(window):
        raise decision.DecisionError(BAD_SCHEDULE_WINDOW)
    capacity = data.get("blockCapacity")
    if not _is_positive_int(capacity):
        raise decision.DecisionError(BAD_BLOCK_CAPACITY)

    deadlines = []
    for tx in data["transactions"]:
        if "deadline" not in tx:
            deadlines.append(None)
            continue
        deadline = tx["deadline"]
        if not decision._is_nonneg_int(deadline):
            raise decision.DecisionError(BAD_DEADLINE)
        deadlines.append(deadline)

    req["block"] = block
    req["scheduleBlocks"] = window
    req["blockCapacity"] = capacity
    for parsed, deadline in zip(req["transactions"], deadlines):
        parsed["deadline"] = deadline
    return req


def error_result(ident):
    """输入错误结果：列表为空，数值为 0，feasible 为 false。"""
    return {
        "id": ident,
        "block": 0,
        "scheduleBlocks": 0,
        "blockCapacity": 0,
        "baselineOrder": [],
        "blocks": [],
        "scheduledOrder": [],
        "unscheduled": [],
        "scheduledFee": 0,
        "unscheduledFee": 0,
        "totalDelay": 0,
        "evidence": [],
        "feasible": False,
    }


def _search(candidates, latest, window, capacity, input_pos):
    """枚举全部排程方案，返回 (best_key, best_assign)。

    candidates 按输入位置升序；latest[i] 为第 i 笔可进的最大区块偏移
    （窗口内）。assign[i] 为区块偏移或 -1（不排程）。比较键：排程笔数
    最多、fee 总和最高、totalDelay 最小、scheduledOrder 输入下标序列
    字典序最小；以当前最优的笔数与 fee 上界剪枝。空排程恒合法，故
    best_assign 不会为 None。
    """
    m = len(candidates)
    # 后缀 fee 和：笔数上界持平最优时用于 fee 上界剪枝
    suffix_fee = [0] * (m + 1)
    for i in range(m - 1, -1, -1):
        suffix_fee[i] = suffix_fee[i + 1] + candidates[i]["fee"]

    loads = [0] * window
    assign = [-1] * m
    best_key = None
    best_assign = None

    def evaluate():
        """由 assign 还原 scheduledOrder 并校验，合法则返回比较键。"""
        per_block = [[] for _ in range(window)]
        delay = 0
        for i, off in enumerate(assign):
            if off < 0:
                continue
            per_block[off].append(candidates[i])
            delay += off
        ordered = []
        for off in range(window):
            per_block[off].sort(key=lambda tx: (-tx["fee"], tx["hash"]))
            ordered.extend(per_block[off])
        if not decision.nonce_order_satisfied(ordered):
            return None
        if decision.detect_sandwich_evidence(ordered):
            return None
        fee = sum(tx["fee"] for tx in ordered)
        seq = tuple(input_pos[tx["hash"]] for tx in ordered)
        return (-len(ordered), -fee, delay, seq)

    def dfs(i, placed, fee_so_far):
        nonlocal best_key, best_assign
        if i == m:
            key = evaluate()
            if key is not None and (best_key is None or key < best_key):
                best_key = key
                best_assign = list(assign)
            return
        if best_key is not None:
            # 笔数上界已低于最优，或持平最优时 fee 上界已低于最优
            if placed + (m - i) < -best_key[0]:
                return
            if (placed + (m - i) == -best_key[0]
                    and fee_so_far + suffix_fee[i] < -best_key[1]):
                return
        # 选择一：不排程
        dfs(i + 1, placed, fee_so_far)
        # 选择二：排入不晚于 latest[i] 的任一有余量区块
        for off in range(latest[i] + 1):
            if loads[off] >= capacity:
                continue
            loads[off] += 1
            assign[i] = off
            dfs(i + 1, placed + 1, fee_so_far + candidates[i]["fee"])
            assign[i] = -1
            loads[off] -= 1

    dfs(0, 0, 0)
    return best_key, best_assign


def schedule(req):
    """枚举全部排程方案求全局最优多区块排程，返回结果字典。"""
    txs = req["transactions"]
    block = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]
    last = block + window - 1

    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]
    input_pos = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 过期交易：deadline 小于起始区块；不参与排程与夹子判定
    expired = set()
    for at, tx in enumerate(txs):
        deadline = tx["deadline"]
        if deadline is not None and deadline < block:
            expired.add(at)

    # 证据取自未过期交易构成的基线子序列；起始位置唯一，victim 仅作
    # 稳定次序兜底
    active_baseline = [
        tx for tx in baseline if input_pos[tx["hash"]] not in expired
    ]
    evidence = sorted(
        decision.detect_sandwich_evidence(active_baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )

    # 候选交易按输入位置升序；latest 为各自可进的最大区块偏移
    candidates = [tx for at, tx in enumerate(txs) if at not in expired]
    latest = []
    for tx in candidates:
        deadline = tx["deadline"]
        horizon = last if deadline is None else min(deadline, last)
        latest.append(horizon - block)

    best_key, best_assign = _search(
        candidates, latest, window, capacity, input_pos)

    # 由最优方案还原各块顺序与 scheduledOrder
    per_block = [[] for _ in range(window)]
    for i, off in enumerate(best_assign):
        if off >= 0:
            per_block[off].append(candidates[i])
    blocks = []
    scheduled_order = []
    for off in range(window):
        per_block[off].sort(key=lambda tx: (-tx["fee"], tx["hash"]))
        order = [tx["hash"] for tx in per_block[off]]
        blocks.append({"block": block + off, "order": order})
        scheduled_order.extend(order)

    scheduled_set = set(scheduled_order)
    scheduled_fee = sum(tx["fee"] for tx in txs if tx["hash"] in scheduled_set)
    total_fee = sum(tx["fee"] for tx in txs)
    unscheduled = []
    for at, tx in enumerate(txs):
        if at in expired:
            reason = DEADLINE_EXPIRED
        elif tx["hash"] not in scheduled_set:
            reason = SCHEDULE_SKIPPED
        else:
            continue
        unscheduled.append({"hash": tx["hash"], "at": at, "reason": reason})

    return {
        "id": req["id"],
        "block": block,
        "scheduleBlocks": window,
        "blockCapacity": capacity,
        "baselineOrder": baseline_hashes,
        "blocks": blocks,
        "scheduledOrder": scheduled_order,
        "unscheduled": unscheduled,
        "scheduledFee": scheduled_fee,
        "unscheduledFee": total_fee - scheduled_fee,
        "totalDelay": best_key[2],
        "evidence": evidence,
        "feasible": len(scheduled_order) == len(candidates),
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并追加排程字段校验；error_code 非空时
    退出码 2。未过期交易未能全部排程是正常结论，error_code 为 None。
    """
    try:
        req = parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident), exc.code
    return schedule(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次多区块排程，返回退出码。缓冲区参数用于测试注入。

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
