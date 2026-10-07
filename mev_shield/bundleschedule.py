"""不可拆分交易捆绑的联合排程：python -m mev_shield.bundleschedule

在多区块排程计划（``python -m mev_shield.schedule``）的输入、校验、
窗口与期限定义之上，把候选交易按非空 ``bundle`` 字段分成不可拆分
捆绑：每个捆绑只能整组进入一个区块并在该块内占据连续段，段内顺序
固定为输入相对位置。既有入口的输入、输出与退出码均保持不变。

- 每笔交易新增必需的非空字符串字段 ``bundle``；同值交易组成一个
  捆绑，段内顺序取输入相对位置，捆绑初始次序取首笔位置。
- ``block``、``scheduleBlocks``、``blockCapacity`` 与交易级
  ``deadline`` 完全沿用多区块排程的定义与错误码（``BAD_BLOCK``、
  ``BAD_SCHEDULE_WINDOW``、``BAD_BLOCK_CAPACITY``、
  ``BAD_DEADLINE``）；新增的捆绑校验 ``BAD_BUNDLE_ID`` 排在这些
  窗口字段校验之后，统一决策入口与窗口字段的既有错误一律优先。

排程规则：

- 任一成员 ``deadline < block`` 则整组过期，不参与排程与夹子判定；
  否则捆绑只能进入所有成员期限允许（不晚于各自 deadline，无期限
  可进任意窗口区块）且容量足以整组容纳的区块；
- 每个已排捆绑在其区块内占连续段、段内顺序固定为输入相对位置；
  全局顺序按区块升序、块内按段顺序拼接，覆盖全部已选交易；
- 全局顺序上同一 ``from`` 的 nonce 严格递增，且按统一决策的相邻
  三段规则无任何夹子证据。

择优目标依次为：排程交易数最多、排程 fee 总和最高、``totalDelay``
（各交易所在区块减 ``block`` 之和）最小、全局顺序对应输入下标序列
字典序最小。枚举捆绑的全部区块 / 段位置与跳过选择求全局最优，不做
逐笔贪心；结果确定，相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``（全部交易的 fee 降序、
hash 升序基线顺序）、``blocks``（窗口每个区块的 ``block`` /
``order``）、``unscheduled``（按捆绑首笔位置列出 ``bundle`` /
``at`` / ``hashes`` / ``reason``，原因仅 ``DEADLINE_EXPIRED``、
``CAPACITY_EXCEEDED``、``BUNDLE_SKIPPED``）、``scheduledFee``、
``unscheduledFee``、``totalDelay``、``evidence``（未过期基线子序列
上的全部夹子证据，按起始位置升序、同位按 victim hash 升序）、
``feasible``。

- ``feasible`` 仅在全部捆绑排入时为 true；否则为 false，仍输出
  最优部分排程，退出 0，stderr 不写码；
- ``bundle`` 缺失或不是非空字符串时退出 2、stderr 写
  ``BAD_BUNDLE_ID``，stdout 保持同形：列表为空、数值为 0、
  ``feasible`` 为 false；既有校验与窗口字段错误均优先。
"""

import json
import sys
from itertools import permutations, product

from . import core
from . import decision
from . import schedule
from .cli import parse_args

# 输入错误码：bundle 缺失或不是非空字符串
BAD_BUNDLE_ID = "BAD_BUNDLE_ID"

# 未排程原因码：整组过期、整组成员数超过单块容量、最优方案未选中
DEADLINE_EXPIRED = core.DEADLINE_EXPIRED
CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
BUNDLE_SKIPPED = "BUNDLE_SKIPPED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行多区块排程计划的全部校验（统一决策入口校验加
    block / scheduleBlocks / blockCapacity / deadline），再校验每笔
    交易必需的非空字符串 bundle，否则抛 BAD_BUNDLE_ID。
    """
    req = schedule.parse_request(raw)
    # 统一决策与窗口字段校验已通过，raw 必为合法 JSON 对象且交易均为对象
    data = json.loads(raw)

    bundles = []
    for tx in data["transactions"]:
        bundle = tx.get("bundle")
        if not isinstance(bundle, str) or not bundle:
            raise decision.DecisionError(BAD_BUNDLE_ID)
        bundles.append(bundle)
    for parsed, bundle in zip(req["transactions"], bundles):
        parsed["bundle"] = bundle
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
    """按 bundle 值聚合交易，返回按首笔位置升序的捆绑列表。

    每个捆绑为 dict：id 为 bundle 值，members 为成员交易（保持输入
    相对顺序），first 为首笔输入位置，size 为成员数，fee 为成员 fee
    之和。同值交易按首次出现聚合成一个捆绑，首笔位置随首次出现单调
    递增，故返回次序即捆绑初始次序。
    """
    grouped = {}
    order = []
    for at, tx in enumerate(txs):
        bundle = tx["bundle"]
        if bundle not in grouped:
            grouped[bundle] = {
                "id": bundle,
                "members": [],
                "first": at,
                "size": 0,
                "fee": 0,
            }
            order.append(bundle)
        entry = grouped[bundle]
        entry["members"].append(tx)
        entry["size"] += 1
        entry["fee"] += tx["fee"]
    return [grouped[bundle] for bundle in order]


def _bundle_internally_valid(bundle):
    """捆绑自身是否可能合法：段内顺序固定且永不可被后续插入打断。

    段内同 from nonce 必须递增；段内相邻三笔不得已成夹子（段不可拆分，
    该相邻关系无法被后续段插入破坏）。
    """
    members = bundle["members"]
    if not decision.nonce_order_satisfied(members):
        return False
    if decision.detect_sandwich_evidence(members):
        return False
    return True


def _search(bundles, window, capacity, latest_offset, input_pos):
    """枚举全部捆绑排程方案，返回 (best_key, best_segments)。

    latest_offset[b] 为第 b 个捆绑可进的最大区块偏移；bundles 按首笔
    位置升序。best_segments[off] 为最优方案中该块的段顺序（捆绑编号
    元组）。

    DFS 只枚举每个捆绑跳过或整组进入哪个允许区块（不枚举块内段位置），
    与既有多区块排程枚举规模一致；到达叶节点后再枚举各块段排列并校验。
    对任一区块，段顺序按首笔输入位置升序即得到字典序最小的交易下标
    序列（每段序列以其首笔位置开头），故段排列按该序优先，首个合法
    组合即该分配下的字典序最优方案。

    比较键：排程笔数最多、fee 总和最高、totalDelay 最小、全局顺序
    对应输入下标序列字典序最小。枚举保持完整（不做逐笔贪心，最优不
    变），辅以仅剪枝的精确优化：DFS 按大体量 / 紧期限优先；先注入合法
    贪心初始当前最优；笔数 / fee 上界剪枝；跨块 nonce 相对次序已冲突
    （不依赖块内段排列）时立即剪枝。空排程恒合法，故 best_segments
    不会为 None。
    """
    m = len(bundles)
    sizes = [bundle["size"] for bundle in bundles]
    fees = [bundle["fee"] for bundle in bundles]
    firsts = [bundle["first"] for bundle in bundles]
    internally_valid = [_bundle_internally_valid(bundle) for bundle in bundles]

    # DFS 顺序不改变可达分配集合，仅让约束最强的捆绑先定型：体量降序、
    # 最晚偏移升序、首笔位置升序
    dfs_order = sorted(
        range(m), key=lambda b: (-sizes[b], latest_offset[b], firsts[b]))

    # 后缀笔数与后缀 fee（按 DFS 顺序）：笔数上界持平最优时用于剪枝
    suffix_count = [0] * (m + 1)
    suffix_fee = [0] * (m + 1)
    for d in range(m - 1, -1, -1):
        b = dfs_order[d]
        suffix_count[d] = suffix_count[d + 1] + sizes[b]
        suffix_fee[d] = suffix_fee[d + 1] + fees[b]

    # assigned[off] 为该块已分配的捆绑编号集合；loads 为各块占用笔数
    assigned = [set() for _ in range(window)]
    loads = [0] * window
    best_key = None
    best_segments = None

    def arrange_valid(segments_per_block):
        """对给定段排列校验全局 nonce 与夹子，合法返回全局交易序列。"""
        ordered = []
        for off in range(window):
            for b in segments_per_block[off]:
                ordered.extend(bundles[b]["members"])
        if not decision.nonce_order_satisfied(ordered):
            return None
        if decision.detect_sandwich_evidence(ordered):
            return None
        return ordered

    def adopt(key, segments_per_block):
        nonlocal best_key, best_segments
        if best_key is None or key < best_key:
            best_key = key
            best_segments = [tuple(seg) for seg in segments_per_block]

    def sorted_arrangement():
        """各块段均按首笔位置升序：该分配下全局字典序最小的段排列。"""
        return [tuple(sorted(assigned[off], key=lambda b: firsts[b]))
                for off in range(window)]

    def accept_arrangement(arrangement):
        """合法段排列：计算比较键并更新最优，返回是否合法。"""
        ordered = arrange_valid(arrangement)
        if ordered is None:
            return False
        fee = sum(tx["fee"] for tx in ordered)
        seq = tuple(input_pos[tx["hash"]] for tx in ordered)
        key = (
            -len(ordered),
            -fee,
            sum(loads[off] * off for off in range(window)),
            seq,
        )
        adopt(key, arrangement)
        return True

    def block_nonce_feasible(seg_ids):
        """是否存在段排列使该块内同发送者 nonce 严格递增。

        段不可拆分，故同一块内含同一发送者的两个段只能整体定先后：
        各自发送者 nonce 范围 (lo, hi) 互不重叠才有唯一可行先后
        （范围交叠则任何段排列都无法严格递增）。这些段间先后约束构成
        有向图，不同发送者给出的约束可能成环；无环才存在可行段排列。
        """
        by_sender = {}
        for b in seg_ids:
            for sender, (lo, hi) in sender_ranges[b].items():
                by_sender.setdefault(sender, []).append((b, lo, hi))
        edges = {b: set() for b in seg_ids}
        for entries in by_sender.values():
            for i, (bi, loi, hii) in enumerate(entries):
                for bj, loj, hij in entries[i + 1:]:
                    if hii < loj:
                        edges[bi].add(bj)
                    elif hij < loi:
                        edges[bj].add(bi)
                    else:
                        # 范围交叠：段不可交错，无任何可行排列
                        return False
        # Kahn 拓扑判环
        indegree = {b: 0 for b in seg_ids}
        for outs in edges.values():
            for b in outs:
                indegree[b] += 1
        queue = [b for b in seg_ids if indegree[b] == 0]
        seen = 0
        while queue:
            b = queue.pop()
            seen += 1
            for nxt in edges[b]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        return seen == len(seg_ids)

    def evaluate_leaf():
        """求该分配下字典序最小的合法段组合并更新最优。

        每块段顺序按首笔位置升序即块内字典序最小序列（段首成员即该段
        最小输入下标，任意相邻逆序交换都会在首位变大），故各块同时取
        最小的组合若合法即为全局最优，绝大多数叶节点在此直接返回；
        不合法时先判定各块是否存在满足 nonce 的段排列（段间先后约束
        无环），彻底无解的叶节点跳过；否则按 itertools.product 序枚举
        （区块越早变化越慢），首个同时满足 nonce 与夹子的组合即全局
        字典序最小。
        """
        minimal = sorted_arrangement()
        if accept_arrangement(minimal):
            return
        if not all(block_nonce_feasible(seg) for seg in minimal):
            return
        options = [list(permutations(seg)) for seg in minimal]
        for arrangement in product(*options):
            if accept_arrangement(arrangement):
                return

    # 跨块 nonce 必要性（与块内段排列无关）：维护各块各发送者
    # (min_nonce, max_nonce)。每个捆绑各发送者的范围预先算好一次。
    sender_ranges = []
    for bundle in bundles:
        rng = {}
        for tx in bundle["members"]:
            sender = tx["from"]
            nonce = tx["nonce"]
            cur = rng.get(sender)
            rng[sender] = (
                nonce if cur is None else min(cur[0], nonce),
                nonce if cur is None else max(cur[1], nonce),
            )
        sender_ranges.append(rng)
    range_by_block = [{} for _ in range(window)]

    def try_commit(b, off):
        """若 b 可放入 off 块则提交并返回还原信息；跨块 nonce 冲突返回 None。

        跨块检查只看必要条件（与块内段排列无关）：同发送者在 off 之前
        最近的已占用区块，其最大 nonce 必须小于合并后本块最小 nonce；
        本块最大 nonce 必须小于 off 之后最近已占用区块的最小 nonce。
        中间空块不改变跨块先后关系，故跳过空块直接比较最近占用块。
        范围只会随后续放入扩大，冲突一旦出现即永久冲突，不会误剪。
        """
        rng = range_by_block[off]
        merged = []
        for sender, (lo, hi) in sender_ranges[b].items():
            cur = rng.get(sender)
            new_lo = lo if cur is None else min(cur[0], lo)
            new_hi = hi if cur is None else max(cur[1], hi)
            earlier = off - 1
            while earlier >= 0 and sender not in range_by_block[earlier]:
                earlier -= 1
            if earlier >= 0 and range_by_block[earlier][sender][1] >= new_lo:
                return None
            later = off + 1
            while later < window and sender not in range_by_block[later]:
                later += 1
            if later < window and new_hi >= range_by_block[later][sender][0]:
                return None
            merged.append((sender, cur, new_lo, new_hi))
        loads[off] += sizes[b]
        assigned[off].add(b)
        saved = []
        for sender, cur, new_lo, new_hi in merged:
            saved.append((sender, cur))
            rng[sender] = (new_lo, new_hi)
        return saved

    def rollback(b, off, saved):
        loads[off] -= sizes[b]
        assigned[off].discard(b)
        rng = range_by_block[off]
        for sender, cur in saved:
            if cur is None:
                del rng[sender]
            else:
                rng[sender] = cur

    def greedy_incumbent():
        """构造一个合法贪心方案作为初始当前最优；只影响剪枝强度。"""
        trial_loads = [0] * window
        trial_assigned = [[] for _ in range(window)]
        for b in dfs_order:
            if not internally_valid[b]:
                continue
            size = sizes[b]
            for off in range(latest_offset[b] + 1):
                if trial_loads[off] + size > capacity:
                    continue
                trial_assigned[off].append(b)
                seg = [sorted(trial_assigned[o], key=lambda x: firsts[x])
                       for o in range(window)]
                if arrange_valid(seg) is not None:
                    trial_loads[off] += size
                    break
                trial_assigned[off].pop()
        seg = [tuple(sorted(trial_assigned[off], key=lambda x: firsts[x]))
               for off in range(window)]
        ordered = arrange_valid(seg)
        if ordered is None:
            return None
        fee = sum(tx["fee"] for tx in ordered)
        return (
            (
                -len(ordered),
                -fee,
                sum(trial_loads[off] * off for off in range(window)),
                tuple(input_pos[tx["hash"]] for tx in ordered),
            ),
            seg,
        )

    greedy = greedy_incumbent()
    if greedy is not None:
        adopt(greedy[0], greedy[1])
    else:
        # 空排程恒合法
        adopt((0, 0, 0, ()), [() for _ in range(window)])

    def dfs(d, placed, fee_so_far):
        if d == m:
            evaluate_leaf()
            return
        # 笔数上界已低于最优，或持平最优时 fee 上界已低于最优
        if placed + suffix_count[d] < -best_key[0]:
            return
        if (placed + suffix_count[d] == -best_key[0]
                and fee_so_far + suffix_fee[d] < -best_key[1]):
            return

        b = dfs_order[d]
        size = sizes[b]
        # 选择一：整组跳过
        dfs(d + 1, placed, fee_so_far)
        # 选择二：整组进入期限允许、容量足够的任一区块；块内段排列在
        # 叶节点统一枚举。自身段内顺序已不合法的捆绑放入任何位置都永
        # 久不合法，只能跳过。
        if not internally_valid[b]:
            return
        for off in range(latest_offset[b] + 1):
            if loads[off] + size > capacity:
                continue
            saved = try_commit(b, off)
            if saved is None:
                continue
            dfs(d + 1, placed + size, fee_so_far + fees[b])
            rollback(b, off, saved)

    dfs(0, 0, 0)
    return best_key, best_segments


def bundle_schedule(req):
    """枚举全部捆绑排程方案求全局最优，返回结果字典。"""
    txs = req["transactions"]
    block = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]
    last = block + window - 1

    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]
    input_pos = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 过期交易：deadline 小于起始区块。任一成员过期则整组过期，过期
    # 成员不参与排程与夹子判定
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

    # 捆绑初始次序取首笔位置；逐捆绑判定整组过期与期限允许的最晚偏移
    bundles = _build_bundles(txs)
    expired_ids = set()
    latest = {}
    for bundle in bundles:
        members = bundle["members"]
        if any(input_pos[tx["hash"]] in expired for tx in members):
            expired_ids.add(bundle["id"])
            continue
        # 可进入的最晚区块：取所有成员期限的最紧约束；无期限以窗口末
        # 为限。整组未过期时各成员 deadline 均不早于 block，故最晚偏移
        # 非负，至少首块可进。
        horizon = last
        for tx in members:
            deadline = tx["deadline"]
            if deadline is not None and deadline < horizon:
                horizon = deadline
        latest[bundle["id"]] = horizon - block

    candidates = [bundle for bundle in bundles if bundle["id"] not in expired_ids]
    latest_offsets = [latest[bundle["id"]] for bundle in candidates]

    best_key, best_segments = _search(
        candidates, window, capacity, latest_offsets, input_pos)

    # 最优方案直接以各块段顺序（候选捆绑编号）返回；展开段为交易顺序
    scheduled_set = set()
    blocks = []
    for off, seg in enumerate(best_segments):
        order = []
        for b in seg:
            scheduled_set.add(b)
            order.extend(tx["hash"] for tx in candidates[b]["members"])
        blocks.append({"block": block + off, "order": order})

    scheduled_hashes = {
        tx["hash"]
        for b in scheduled_set
        for tx in candidates[b]["members"]
    }
    scheduled_ids = {candidates[b]["id"] for b in scheduled_set}
    scheduled_fee = sum(
        tx["fee"] for tx in txs if tx["hash"] in scheduled_hashes)
    total_fee = sum(tx["fee"] for tx in txs)

    # 未排程捆绑按首笔位置列出：整组过期优先，其次整组超过区块容量
    # （体量相对单块容量的固有属性），其余为最优方案未选中
    unscheduled = []
    for bundle in bundles:
        if bundle["id"] in scheduled_ids:
            continue
        hashes = [tx["hash"] for tx in bundle["members"]]
        if bundle["id"] in expired_ids:
            reason = DEADLINE_EXPIRED
        elif bundle["size"] > capacity:
            reason = CAPACITY_EXCEEDED
        else:
            reason = BUNDLE_SKIPPED
        unscheduled.append(
            {
                "bundle": bundle["id"],
                "at": bundle["first"],
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
        "feasible": not expired_ids
        and len(scheduled_set) == len(candidates),
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用多区块排程计划并追加 bundle 字段校验；error_code
    非空时退出码 2。捆绑未能全部排入是正常结论，error_code 为 None。
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
