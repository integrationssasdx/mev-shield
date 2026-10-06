"""安全重排计划：python -m mev_shield.reorder [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验、
fee 降序 / hash 升序基线、相邻三段夹子证据与 nonce 依赖之上，只调整
交易顺序（不删除任何交易）给出一个无夹子的安全重排计划。既有入口的
输入、输出与退出码均保持不变。

输入在统一决策入口的根对象上新增必需字段：

- ``maxMoves``：可改变基线位置的交易数上限，非负 JSON 整数（排除
  布尔值）。缺失、类型错误、布尔值或小于零均返回 ``BAD_MOVE_LIMIT``；
  该校验排在统一决策入口全部既有校验（含 ``BAD_SLIPPAGE_MODE``）
  之后，旧错误一律优先。

合法性（``safeOrder`` 恰好排列输入交易，不增加、丢失或重复）：

- 同一 ``from`` 的 nonce 在最终顺序上严格递增；
- 顺序执行与统一决策入口相同的相邻三段夹子判定，无任何夹子证据。

滑点、价格上下文与回滚沿用统一决策入口的既有校验，但不参与可行性
判定。基线仍按 fee 降序、hash 升序。择优目标依次为：位置变化数
（最终下标与基线下标不同的交易数）最小、各交易最终下标与基线下标
差的绝对值和最小、最终位置对应输入下标序列的字典序最小。按发送者
道枚举全部 nonce 合法交错求全局最优，不做逐笔贪心；结果确定，相同
输入逐字一致。

输出字段固定：``id``、``baselineOrder``（基线 hash 顺序）、
``safeOrder``（最优安全顺序）、``moved``（按最终位置升序列出
``hash`` / ``from`` / ``to`` 与固定原因 ``REORDERED``）、
``movedCount``、``displacement``、``evidence``（baselineOrder 上的
全部夹子证据，按起始位置升序、同位按 victim hash 升序）、
``blockers``（基线问题，仅 SANDWICH_DETECTED、NONCE_ORDER_VIOLATION，
顺序固定）、``result``、``feasible``、``maxMoves``。

- 最少变化数不超过 ``maxMoves``：``result`` 为 ``OK``，``feasible``
  为 true；
- 超过 ``maxMoves``：``result`` 为 ``MOVE_LIMIT_EXCEEDED``，
  ``feasible`` 为 false，仍输出最优方案；
- 没有任何合法顺序：``result`` 为 ``SAFE_ORDER_NOT_FOUND``，
  ``safeOrder`` 与 ``moved`` 为空，``movedCount`` 与 ``displacement``
  为 0；
- 无方案或超限均为正常结论：退出 0，stderr 不写码；
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形且
  ``result`` 为 ``INPUT_ERROR``。
"""

import json
import sys

from . import core
from . import decision
from .cli import parse_args

# 输入错误码：maxMoves 缺失、类型错误、布尔值或小于零
BAD_MOVE_LIMIT = "BAD_MOVE_LIMIT"

# 结果码
OK = "OK"
MOVE_LIMIT_EXCEEDED = "MOVE_LIMIT_EXCEEDED"
SAFE_ORDER_NOT_FOUND = "SAFE_ORDER_NOT_FOUND"
INPUT_ERROR = "INPUT_ERROR"

# 重排原因码：最终位置与基线位置不同的交易统一记此原因
REORDERED = "REORDERED"

# blockers 固定顺序：基线问题只列夹子证据与 nonce 顺序两类
_BLOCKER_ORDER = (
    decision.SANDWICH_DETECTED,
    decision.NONCE_ORDER_VIOLATION,
)

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE），再校验
    必需的 maxMoves：非负 JSON 整数（排除布尔值），否则抛
    BAD_MOVE_LIMIT。
    """
    req = decision.parse_request(raw)
    # 统一决策校验已通过，raw 必为合法 JSON 对象
    limit = json.loads(raw).get("maxMoves")
    if not decision._is_nonneg_int(limit):
        raise decision.DecisionError(BAD_MOVE_LIMIT)
    req["maxMoves"] = limit
    return req


def error_result(ident):
    """输入错误结果：列表为空，数值为 0，result 为 INPUT_ERROR。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "safeOrder": [],
        "moved": [],
        "movedCount": 0,
        "displacement": 0,
        "evidence": [],
        "blockers": [],
        "result": INPUT_ERROR,
        "feasible": False,
        "maxMoves": 0,
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


def _search(lanes, baseline_pos, input_pos, n):
    """枚举发送者道的全部交错，返回 (best_key, best_order)。

    道内按 nonce 升序排列，故每个交错天然满足同一 from 的 nonce 严格
    递增；追加交易时增量检查新完成的相邻三元组，保证无夹子证据。
    比较键：位置变化数、位移绝对值和、最终位置的输入下标序列；以
    当前最优键剪枝（两项数值沿路径单调不减）。
    """
    best_key = None
    best_order = None
    order = []
    cursors = [0] * len(lanes)
    moved = 0
    disp = 0

    def dfs():
        nonlocal best_key, best_order, moved, disp
        p = len(order)
        if p == n:
            seq = tuple(input_pos[tx["hash"]] for tx in order)
            key = (moved, disp, seq)
            if best_key is None or key < best_key:
                best_key = key
                best_order = list(order)
            return
        if best_key is not None:
            if moved > best_key[0]:
                return
            if moved == best_key[0] and disp > best_key[1]:
                return
        for lane_at, lane in enumerate(lanes):
            cursor = cursors[lane_at]
            if cursor >= len(lane):
                continue
            tx = lane[cursor]
            # 增量夹子检查：新完成的相邻三元组 (p-2, p-1, p)
            if p >= 2 and _sandwich_triple(order[p - 2], order[p - 1], tx):
                continue
            base_at = baseline_pos[tx["hash"]]
            cursors[lane_at] = cursor + 1
            order.append(tx)
            moved += base_at != p
            disp += abs(base_at - p)
            dfs()
            disp -= abs(base_at - p)
            moved -= base_at != p
            order.pop()
            cursors[lane_at] = cursor

    dfs()
    return best_key, best_order


def reorder(req):
    """枚举全部 nonce 合法交错求全局最优安全重排，返回结果字典。"""
    txs = req["transactions"]
    limit = req["maxMoves"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]
    baseline_pos = {tx["hash"]: at for at, tx in enumerate(baseline)}
    input_pos = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )
    blockers = []
    if evidence:
        blockers.append(decision.SANDWICH_DETECTED)
    if not decision.nonce_order_satisfied(baseline):
        blockers.append(decision.NONCE_ORDER_VIOLATION)

    # 发送者道：道内 nonce 升序，交错即全部 nonce 合法排列
    lanes = {}
    for tx in txs:
        lanes.setdefault(tx["from"], []).append(tx)
    for lane in lanes.values():
        lane.sort(key=lambda tx: tx["nonce"])

    _best_key, best_order = _search(
        list(lanes.values()), baseline_pos, input_pos, len(txs))

    if best_order is None:
        # 无任何合法顺序：正常结论，退出 0，不写 stderr
        return {
            "id": req["id"],
            "baselineOrder": baseline_hashes,
            "safeOrder": [],
            "moved": [],
            "movedCount": 0,
            "displacement": 0,
            "evidence": evidence,
            "blockers": blockers,
            "result": SAFE_ORDER_NOT_FOUND,
            "feasible": False,
            "maxMoves": limit,
        }

    moved = [
        {
            "hash": tx["hash"],
            "from": baseline_pos[tx["hash"]],
            "to": at,
            "reason": REORDERED,
        }
        for at, tx in enumerate(best_order)
        if baseline_pos[tx["hash"]] != at
    ]
    moved_count = len(moved)
    displacement = sum(abs(entry["to"] - entry["from"]) for entry in moved)
    feasible = moved_count <= limit
    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "safeOrder": [tx["hash"] for tx in best_order],
        "moved": moved,
        "movedCount": moved_count,
        "displacement": displacement,
        "evidence": evidence,
        "blockers": blockers,
        "result": OK if feasible else MOVE_LIMIT_EXCEEDED,
        "feasible": feasible,
        "maxMoves": limit,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并追加 maxMoves 校验；error_code 非空时
    退出码 2。超限或无方案是正常结论，error_code 为 None。
    """
    try:
        req = parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident), exc.code
    return reorder(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次安全重排计划，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码；
    超限或无方案退出 0，不写 stderr。
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
