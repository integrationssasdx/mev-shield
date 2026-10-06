"""多区块排程：python -m mev_shield.schedule [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验、
fee 降序 / hash 升序基线、相邻三段夹子证据与 nonce 依赖之上，把候选
交易排进从 ``block`` 起的若干区块。命令行参数与标准输入 / 输出均沿用
统一决策入口，既有各入口的输入、输出与退出码不变。

输入在统一决策交易包上新增（根对象前三个必需字段，交易级 ``deadline``
可选）：

- ``block``：窗口起始区块，非负 JSON 整数（排除布尔值），否则
  ``BAD_BLOCK``；
- ``scheduleBlocks``：窗口区块数，正 JSON 整数，否则
  ``BAD_SCHEDULE_WINDOW``；
- ``blockCapacity``：每区块容量上限，正 JSON 整数，否则
  ``BAD_BLOCK_CAPACITY``；
- 交易级 ``deadline``：最后可进入的区块，非负 JSON 整数（排除
  布尔值），否则 ``BAD_DEADLINE``。

四项校验排在统一决策入口全部既有校验（含 ``BAD_SLIPPAGE_MODE``）
之后，依次为 ``BAD_BLOCK``、``BAD_SCHEDULE_WINDOW``、
``BAD_BLOCK_CAPACITY``、``BAD_DEADLINE``，旧错误一律优先。

窗口为 ``block`` .. ``block + scheduleBlocks - 1``。未过期交易至多
进入一个不晚于 ``deadline`` 的区块；无 ``deadline`` 可进任意窗口区块。
``deadline < block`` 即过期：排除出排程，也不参与基线夹子判定。

排程合法性：

- 每个区块内 fee 降序、hash 升序；跨区块按区块升序拼成
  ``scheduledOrder``；
- 同一 ``from`` 的 nonce 在 ``scheduledOrder`` 上严格递增；
- ``scheduledOrder`` 按统一决策的相邻三段规则无任何夹子证据；
- 每区块交易数不超过 ``blockCapacity``。

择优目标依次为：排程笔数最多、排程 fee 总和最高、``totalDelay``
（各交易所在区块减 ``block`` 的偏移之和）最小、``scheduledOrder``
对应输入下标序列的字典序最小。枚举全部合法部分排程求全局最优，不做
逐笔贪心；结果确定，相同输入逐字一致。

输出字段固定：``id``、``block``、``scheduleBlocks``、
``blockCapacity``、``baselineOrder``（未过期交易的 fee 降序、hash
升序基线）、``blocks``（按窗口区块升序，每块列 ``blockHeight`` /
``order`` / ``fee``）、``scheduledOrder``、``unscheduled``（按输入
位置列出 ``hash`` / ``at`` / ``reason``，仅用 ``DEADLINE_EXPIRED``、
``SCHEDULE_SKIPPED``）、``scheduledFee``、``unscheduledFee``、
``totalDelay``、``evidence``（未过期基线上的全部夹子证据）、
``feasible``。未过期交易全部排程时 ``feasible`` 为 true，否则为
false 并给出最优部分排程。

输入错误退出 2、stderr 写唯一码，stdout 保持同形：列表为空、数值为
0、``feasible`` 为 false。无完整排程是正常结论，退出 0 且不写码。
"""

import json
import sys

from . import core
from . import decision
from .cli import parse_args

# 输入错误码：排在统一决策入口全部既有校验之后
BAD_BLOCK = "BAD_BLOCK"
BAD_SCHEDULE_WINDOW = "BAD_SCHEDULE_WINDOW"
BAD_BLOCK_CAPACITY = "BAD_BLOCK_CAPACITY"
BAD_DEADLINE = "BAD_DEADLINE"

# 未排程原因码
SCHEDULE_SKIPPED = "SCHEDULE_SKIPPED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE），再依次
    校验 block（非负整数）、scheduleBlocks（正整数）、blockCapacity
    （正整数）与交易级 deadline（非负整数），布尔值一律排除。
    """
    req = decision.parse_request(raw)
    # 统一决策校验已通过，raw 必为合法 JSON 对象
    data = json.loads(raw)

    block = data.get("block")
    if not decision._is_nonneg_int(block):
        raise decision.DecisionError(BAD_BLOCK)

    window = data.get("scheduleBlocks")
    if not decision._is_nonneg_int(window) or window < 1:
        raise decision.DecisionError(BAD_SCHEDULE_WINDOW)

    capacity = data.get("blockCapacity")
    if not decision._is_nonneg_int(capacity) or capacity < 1:
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
    for tx, deadline in zip(req["transactions"], deadlines):
        tx["deadline"] = deadline

    req["block"] = block
    req["scheduleBlocks"] = window
    req["blockCapacity"] = capacity
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


def _sandwich_triple(t0, t1, t2):
    """与 decision.detect_sandwich_evidence 一致的相邻三元组判定。"""
    if t0["sim"] != "success" or t1["sim"] != "success" or t2["sim"] != "success":
        return False
    if t0["from"] != t2["from"] or t0["from"] == t1["from"]:
        return False
    if not (t0["token"] == t1["token"] == t2["token"]):
        return False
    if t0["side"] != t1["side"] or t0["side"] == t2["side"]:
        return False
    p0, p1, p2 = t0["price"], t1["price"], t2["price"]
    if t0["side"] == "buy":
        return p1 > p0 and p2 < p1
    return p1 < p0 and p2 > p1


def schedule(req):
    """枚举全部合法部分排程求全局最优，返回结果字典。

    枚举方式：scheduledOrder 是各区块基线子序列按区块偏移拼接，故
    逐区块在基线上取子序列（每笔可放入本块或留给后续区块）。放入本
    块的交易接在 scheduledOrder 末尾，前驱（更早区块全部交易与本块
    已放交易）此时完全确定，可增量检查 nonce 严格递增与相邻三段
    夹子（含跨区块边界的三元组）。
    """
    txs = req["transactions"]
    start = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]

    expired = {
        at
        for at, tx in enumerate(txs)
        if tx["deadline"] is not None and tx["deadline"] < start
    }
    active = [at for at in range(len(txs)) if at not in expired]
    input_at = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 基线固定取未过期交易：fee 降序、hash 升序；证据取未过期基线
    baseline = decision.order_transactions([txs[at] for at in active])
    n = len(baseline)
    baseline_hashes = [tx["hash"] for tx in baseline]
    base_input = [input_at[tx["hash"]] for tx in baseline]
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )

    # 最优解不会使用第 n 个及以后的区块偏移：至多 n 笔交易时，若用到
    # 更晚区块，必存在更早空块，整体前移保持顺序与约束且 totalDelay
    # 更小（deadline 只要求不晚、夹子只看 scheduledOrder 相邻关系）。
    eff_window = min(window, n)

    all_mask = (1 << n) - 1
    assign = [-1] * n          # baseline 下标 -> 区块偏移（-1 为未排程）
    taken = [0] * eff_window   # 各区块已放交易按基线相对顺序
    order = []                 # scheduledOrder 前缀（baseline 下标，按追加序）
    last_nonce = {}            # 前缀中各 from 的最后 nonce
    placed_idx = []            # scheduledOrder 对应输入下标序列（追加序）

    # 各区块可放位掩码：deadline 不早于该区块高度（无 deadline 任意）
    eligible = [0] * (eff_window + 1)
    for s in range(eff_window):
        mask = 0
        height = start + s
        for j, tx in enumerate(baseline):
            if tx["deadline"] is None or tx["deadline"] >= height:
                mask |= 1 << j
        eligible[s] = mask

    count = 0
    fee_sum = 0
    delay_sum = 0
    best_key = None
    best_assign = None

    def valid_append(slot_idx, order_slots, last):
        t = baseline[slot_idx]
        if last.get(t["from"], -1) >= t["nonce"]:
            return False
        if len(order_slots) >= 2 and _sandwich_triple(
            baseline[order_slots[-2]], baseline[order_slots[-1]], t
        ):
            return False
        return True

    # 贪心初始解：按区块扫描，能放即放（本块优先天然压低 delay），
    # 尽早给出强 incumbent 供后续剪枝
    avail0 = all_mask
    assign0 = [-1] * n
    order0 = []
    last0 = {}
    counts0 = [0] * eff_window
    for s in range(eff_window):
        scan0 = 0
        while counts0[s] < capacity:
            bits = avail0 & eligible[s] & ~((1 << scan0) - 1)
            chosen = -1
            b = bits
            while b:
                lb = b & -b
                j = lb.bit_length() - 1
                if valid_append(j, order0, last0):
                    chosen = j
                    break
                b &= b - 1
            if chosen < 0:
                break
            t = baseline[chosen]
            assign0[chosen] = s
            counts0[s] += 1
            avail0 &= ~(1 << chosen)
            order0.append(chosen)
            last0[t["from"]] = t["nonce"]
            scan0 = chosen + 1
    g_count = sum(1 for x in assign0 if x >= 0)
    g_fee = sum(baseline[j]["fee"] for j, x in enumerate(assign0) if x >= 0)
    g_delay = sum(x for x in assign0 if x >= 0)
    g_seq = tuple(base_input[j] for j in order0)
    best_key = (-g_count, -g_fee, g_delay, g_seq)
    best_assign = tuple(assign0)

    def dfs(slot, scan, available):
        """在第 slot 块的基线上扫描；available 为本块及以后可放的位集。"""
        nonlocal best_key, best_assign, count, fee_sum, delay_sum

        if slot == eff_window:
            key = (-count, -fee_sum, delay_sum, tuple(placed_idx))
            if key < best_key:
                best_key = key
                best_assign = tuple(assign)
            return

        # 本块高度之后才到期的交易才能放入本块或后续区块
        available &= eligible[slot]

        # 待决笔数上界受剩余容量（本块 + 后续区块）限制
        room = capacity - taken[slot] + (eff_window - 1 - slot) * capacity
        max_add = min(available.bit_count(), room)
        target = -best_key[0]
        need = target - count
        if max_add < need:
            # 笔数无法追平当前最优
            return
        if max_add == need and need > 0:
            # 必须把待决中最高的 need 笔全部排上方可追平笔数：fee 上界
            # 取待决交易（基线即 fee 降序）中最高 need 笔
            bound_fee = 0
            bits = available
            for _ in range(need):
                k = (bits & -bits).bit_length() - 1
                bound_fee += baseline[k]["fee"]
                bits &= bits - 1
            if fee_sum + bound_fee < -best_key[1]:
                return
            # delay 下界：本块至多再放 cap-taken 笔（各计 slot），
            # 其余必进入更晚区块（各至少 slot+1）
            here = min(need, capacity - taken[slot])
            min_added_delay = here * slot + (need - here) * (slot + 1)
            if delay_sum + min_added_delay > best_key[2]:
                return
            # fee 与 delay 的乐观上界都只能追平最优时，字典序前缀
            # 已更大的分支不可能更优
            if (
                fee_sum + bound_fee == -best_key[1]
                and delay_sum + min_added_delay == best_key[2]
            ):
                prefix = tuple(placed_idx)
                if prefix > best_key[3][:len(prefix)]:
                    return

        # 本块基线上下一个不小于 scan 的可放位置
        rest = available & ~((1 << scan) - 1)
        if rest == 0 or taken[slot] == capacity:
            # 本块子序列结束（或已满）：剩余位全部交由下一区块
            dfs(slot + 1, 0, available)
            return

        j = (rest & -rest).bit_length() - 1
        tx = baseline[j]

        # 选项一：baseline[j] 放入本块（接在 scheduledOrder 末尾）。
        # 放置分支优先：尽早得到笔数多、delay 小的 incumbent。
        if (
            last_nonce.get(tx["from"], -1) < tx["nonce"]
            and not (
                len(order) >= 2
                and _sandwich_triple(
                    baseline[order[-2]], baseline[order[-1]], tx
                )
            )
        ):
            sender = tx["from"]
            prev = last_nonce.get(sender)
            assign[j] = slot
            taken[slot] += 1
            order.append(j)
            last_nonce[sender] = tx["nonce"]
            placed_idx.append(base_input[j])
            count += 1
            fee_sum += tx["fee"]
            delay_sum += slot

            dfs(slot, j + 1, available & ~(1 << j))

            delay_sum -= slot
            fee_sum -= tx["fee"]
            count -= 1
            placed_idx.pop()
            order.pop()
            taken[slot] -= 1
            assign[j] = -1
            if prev is None:
                del last_nonce[sender]
            else:
                last_nonce[sender] = prev

        # 选项二：baseline[j] 留给后续区块（最终可能跳过）
        dfs(slot, j + 1, available)

    dfs(0, 0, all_mask)

    placed_base = {j: off for j, off in enumerate(best_assign) if off >= 0}
    placed_set = {base_input[j] for j in placed_base}

    blocks = []
    scheduled_hashes = []
    for off in range(window):
        slots = [j for j in placed_base if placed_base[j] == off]
        order_hashes = [baseline[j]["hash"] for j in slots]
        scheduled_hashes.extend(order_hashes)
        blocks.append(
            {
                "blockHeight": start + off,
                "order": order_hashes,
                "fee": sum(baseline[j]["fee"] for j in slots),
            }
        )

    unscheduled = []
    for at, tx in enumerate(txs):
        if at in expired:
            unscheduled.append(
                {"hash": tx["hash"], "at": at, "reason": core.DEADLINE_EXPIRED}
            )
        elif at not in placed_set:
            unscheduled.append(
                {"hash": tx["hash"], "at": at, "reason": SCHEDULE_SKIPPED}
            )

    scheduled_fee = sum(txs[at]["fee"] for at in placed_set)
    total_fee = sum(tx["fee"] for tx in txs)
    total_delay = sum(best_assign[j] for j in placed_base)

    return {
        "id": req["id"],
        "block": start,
        "scheduleBlocks": window,
        "blockCapacity": capacity,
        "baselineOrder": baseline_hashes,
        "blocks": blocks,
        "scheduledOrder": scheduled_hashes,
        "unscheduled": unscheduled,
        "scheduledFee": scheduled_fee,
        "unscheduledFee": total_fee - scheduled_fee,
        "totalDelay": total_delay,
        "evidence": evidence,
        "feasible": len(placed_set) == len(active),
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并追加排程字段校验；error_code 非空时
    退出码 2。无法完整排程是正常结论，error_code 为 None。
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
    无完整排程退出 0，不写 stderr。
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
