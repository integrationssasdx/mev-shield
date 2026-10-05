"""最小隔离计划：python -m mev_shield.mitigation [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入语义之上，从
交易包的全部子集中选出一个合法保留集合：先按既有 fee 降序、hash 升序
生成基线顺序，再对保留集合（相对顺序与基线一致）执行与统一入口完全
相同的相邻三段夹子判定；仅当无夹子且同发送者保留交易 nonce 严格递增
时集合合法。

优化目标依次为：保留 fee 总和最高、保留笔数最多、被移除交易按输入
位置形成的 hash 序列字典序最小。全局枚举（分支限界），不逐笔贪心；
移除交易可能使原先不相邻的交易重新相邻并形成新夹子，故合法性按
保留序列上的任意连续三笔判定。

输出字段固定：``id``、``baselineOrder``、``selectedOrder``、``removed``、
``keptFee``、``removedFee``、``evidence``；相同输入逐字一致。
"""

import sys

from . import core
from .cli import parse_args
from . import decision

# 移除原因码
SANDWICH_REMOVED = "SANDWICH_REMOVED"

_EXIT_OK = 0
_EXIT_ERROR = 2

# 固定输出键顺序
RESULT_KEYS = (
    "id",
    "baselineOrder",
    "selectedOrder",
    "removed",
    "keptFee",
    "removedFee",
    "evidence",
)
REMOVED_KEYS = ("hash", "at", "reason")


def _is_structural_triple(t0, t1, t2):
    """与 decision.detect_sandwich_evidence 相同的夹子谓词，但不要求相邻。

    用于判断保留序列中任意连续三笔（在基线顺序中可能原本不相邻）是否
    构成夹子；移除中间交易后新相邻的三笔同样据此判定。
    """
    if t0["sim"] != "success" or t1["sim"] != "success" or t2["sim"] != "success":
        return False
    # 首尾同一发送者（攻击者），受害交易发送者不同
    if t0["from"] != t2["from"] or t0["from"] == t1["from"]:
        return False
    if not (t0["token"] == t1["token"] == t2["token"]):
        return False
    # 前置腿与受害者同向，后置腿反向
    if t0["side"] != t1["side"] or t0["side"] == t2["side"]:
        return False
    p0, p1, p2 = t0["price"], t1["price"], t2["price"]
    if t0["side"] == "buy":
        # 正向：受害者买在被推高的价格，后置卖出时价格自高点回落
        return p1 > p0 and p2 < p1
    # 反向：受害者卖在被压低的价格，后置买回时价格自低点回升
    return p1 < p0 and p2 > p1


def _sandwich_triples(ordered):
    """枚举基线顺序上任意 a<b<c 的结构性夹子三元组（位置集合）。"""
    triples = set()
    n = len(ordered)
    for a in range(n - 2):
        t0 = ordered[a]
        for b in range(a + 1, n - 1):
            t1 = ordered[b]
            for c in range(b + 1, n):
                if _is_structural_triple(t0, t1, ordered[c]):
                    triples.add((a, b, c))
    return triples


def optimal_isolation(ordered, input_position):
    """返回最优隔离方案 (kept_positions, removed_positions)，位置均指基线位置。

    input_position[p] 为基线位置 p 的交易在原输入中的位置。

    在全部子集上精确枚举（DFS + 分支限界 + 同态去重），目标依次为：
    保留 fee 总和最高 -> 保留笔数最多 -> 被移除交易按输入位置形成的
    hash 序列字典序最小。
    """
    n = len(ordered)

    # 基线位置按输入位置排序：叶节点据此生成"按输入位置的移除 hash 序列"
    positions_by_input = sorted(range(n), key=lambda p: input_position[p])

    triples = _sandwich_triples(ordered)

    senders = sorted({tx["from"] for tx in ordered})
    sender_index = {sender: i for i, sender in enumerate(senders)}

    suffix_fee = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_fee[i] = suffix_fee[i + 1] + ordered[i]["fee"]

    # 最优方案；首次下探尽量保留（fee 非负，保留优先即强初始解）
    best = {"fee": -1, "count": -1, "removed": None, "kept": None}

    # 同状态去重：(下一位置, 倒数第二保留位置, 倒数第一保留位置,
    # 各发送者最近保留 nonce) -> 已达到的最大 (fee, count)。
    # 仅在 fee 或 count 严格更优时剪枝；二者相同则历史保留集合不同，
    # 仍可能产出字典序更优的移除序列，不得剪枝。
    memo = {}

    # 各发送者扫描过程中最近一笔保留交易的 nonce（-1 表示尚无保留）
    last_nonce = [-1] * len(senders)
    kept = []

    def removed_sequence(mask):
        return tuple(
            ordered[p]["hash"]
            for p in positions_by_input
            if (mask >> p) & 1
        )

    def dfs(i, k2, k1, cur_fee, cur_count, mask):
        # 分支限界：剩余交易全部保留时的 fee / 笔数上界
        ub_fee = cur_fee + suffix_fee[i]
        if ub_fee < best["fee"]:
            return
        if ub_fee == best["fee"] and cur_count + (n - i) < best["count"]:
            return

        last_key = tuple(last_nonce)
        key = (i, k2, k1, last_key)
        prev = memo.get(key)
        if prev is not None:
            prev_fee, prev_count = prev
            if prev_fee > cur_fee or (prev_fee == cur_fee and prev_count > cur_count):
                return
        memo[key] = (cur_fee, cur_count)

        if i == n:
            removed = removed_sequence(mask)
            if (
                cur_fee > best["fee"]
                or (cur_fee == best["fee"] and cur_count > best["count"])
                or (
                    cur_fee == best["fee"]
                    and cur_count == best["count"]
                    and (best["removed"] is None or removed < best["removed"])
                )
            ):
                best["fee"] = cur_fee
                best["count"] = cur_count
                best["removed"] = removed
                best["kept"] = kept[:]
            return

        tx = ordered[i]
        s = sender_index[tx["from"]]

        # 先尝试保留：nonce 须严格大于该发送者上一笔保留交易，且新的
        # 连续三笔（k2, k1, i）不构成夹子
        prev = last_nonce[s]
        keep_allowed = (prev == -1 or tx["nonce"] > prev) and not (
            k2 != -1 and (k2, k1, i) in triples
        )
        if keep_allowed:
            last_nonce[s] = tx["nonce"]
            kept.append(i)
            dfs(i + 1, k1, i, cur_fee + tx["fee"], cur_count + 1, mask)
            kept.pop()
            last_nonce[s] = prev

        # 再尝试移除
        dfs(i + 1, k2, k1, cur_fee, cur_count, mask | (1 << i))

    dfs(0, -1, -1, 0, 0, 0)

    kept_positions = best["kept"]
    kept_set = set(kept_positions)
    removed_positions = [p for p in range(n) if p not in kept_set]
    return kept_positions, removed_positions


def plan(req):
    """对规范化请求求最小隔离计划，返回固定字段结果字典。"""
    txs = req["transactions"]

    # 输入位置（hash 已在入口校验中保证唯一）
    input_at = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 基线顺序：fee 降序、hash 升序
    ordered = decision.order_transactions(txs)
    baseline_order = [tx["hash"] for tx in ordered]

    # 基线各位置对应的输入位置
    input_position = [input_at[tx["hash"]] for tx in ordered]

    # 基线顺序上的全部夹子证据：按起始位置升序、同位按 victim hash 升序
    evidence = decision.detect_sandwich_evidence(ordered)
    evidence.sort(key=lambda entry: (entry["at"][0], entry["victim"]))

    kept_positions, removed_positions = optimal_isolation(ordered, input_position)

    selected_order = [ordered[p]["hash"] for p in kept_positions]

    # removed 按输入位置列出
    removed_entries = [
        {
            "hash": ordered[p]["hash"],
            "at": input_at[ordered[p]["hash"]],
            "reason": SANDWICH_REMOVED,
        }
        for p in removed_positions
    ]
    removed_entries.sort(key=lambda entry: entry["at"])

    kept_fee = sum(ordered[p]["fee"] for p in kept_positions)
    removed_fee = sum(ordered[p]["fee"] for p in removed_positions)

    return {
        "id": req["id"],
        "baselineOrder": baseline_order,
        "selectedOrder": selected_order,
        "removed": removed_entries,
        "keptFee": kept_fee,
        "removedFee": removed_fee,
        "evidence": evidence,
    }


def error_result(ident, code):
    """输入错误结果：四个列表为空，fee 合计为 0；原因码只走 stderr。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "selectedOrder": [],
        "removed": [],
        "keptFee": 0,
        "removedFee": 0,
        "evidence": [],
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    校验语义、优先级与错误码完全沿用统一决策入口；error_code 非空
    表示输入校验失败（退出码 2）。
    """
    try:
        req = decision.parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident, exc.code), exc.code
    return plan(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次最小隔离计划，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码。
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
        payload = serialize(error_result("", core.BAD_ARGS)).encode("utf-8")
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
        result, prior_code = error_result("", core.INPUT_IO), core.INPUT_IO
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
