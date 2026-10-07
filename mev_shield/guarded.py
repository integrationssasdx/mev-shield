"""双预算守卫计划：python -m mev_shield.guarded [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验与
fee 降序 / hash 升序基线之上，把风险约束隔离计划（riskplan）的移除
预算与安全重排计划（reorder）的位置变化预算合并为一份双预算守卫
计划：既移除高风险交易，又在受限的位置变化内重排保留集合。既有入口
的输入、输出与退出码均保持不变。

输入在统一决策入口的根对象上新增两个必需字段：

- ``isolationLimit``：最多移除笔数，非负 JSON 整数（排除布尔值）；
- ``maxMoves``：相对 fee 降序、hash 升序基线可改变位置的交易数上限，
  非负 JSON 整数（排除布尔值）。

校验先完整执行统一决策入口的全部既有校验（含 ``BAD_SLIPPAGE_MODE``），
再依次校验 ``isolationLimit``、``maxMoves``；缺失、类型错误、布尔值
或小于零依次唯一返回 ``BAD_ISOLATION_LIMIT``、``BAD_MOVE_LIMIT``，
旧错误一律优先。

合法性（保留集合的最终顺序）：

- 保留交易逐笔通过滑点与价格上下文检查（同 riskplan；market 口径缺
  token 正数参考价的交易不可保留）；
- 保留集合中 sim 为 revert 的占比不超过 ``rollbackLimit``，空集合
  占比视为 0；
- 同一 ``from`` 的保留交易 nonce 在最终顺序上严格递增；
- 顺序执行与统一决策入口相同的相邻三段夹子判定，无任何夹子证据。

择优目标依次为：保留 fee 总和最高、保留笔数最多、位置变化数（最终
下标与基线下标不同的保留交易数）最少、各保留交易最终下标与基线下标
差的绝对值和最小、被移除交易按输入位置形成的 hash 序列字典序最小、
最终位置对应输入下标序列的字典序最小。枚举全部预算内子集与 nonce
合法交错求全局最优，不做逐笔贪心；结果确定，相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``、``selectedOrder``、
``removed``、``moved``、``keptFee``、``removedFee``、``movedCount``、
``displacement``、``requiredMoves``、``evidence``、``blockers``、
``result``、``feasible``、``isolationLimit``、``maxMoves``。

- ``removed`` 按输入位置列出 ``hash`` / ``at`` / ``RISK_REMOVED``，
  ``moved`` 按最终位置列出 ``hash`` / ``from`` / ``to`` /
  ``REORDERED``，均去重；
- ``evidence`` 为完整基线上的全部夹子证据（按起始位置升序、同位按
  victim hash 升序）；``blockers`` 为基线统一决策的原因码，按
  riskplan 固定顺序去重；
- ``requiredMoves`` 为只计 ``isolationLimit`` 与合法性（不计
  ``maxMoves``）的最少位置变化数，无合法集合时为 0。

结果码：

- 双预算均满足：``result`` 为 ``OK``，``feasible`` 为 true；
- 预算内无任何合法保留集合：``result`` 为
  ``ISOLATION_LIMIT_EXCEEDED``；
- ``requiredMoves`` 超过 ``maxMoves``：``result`` 为
  ``MOVE_LIMIT_EXCEEDED``；
- 后两者 ``feasible`` 为 false，``selectedOrder`` / ``removed`` /
  ``moved`` 为空，``keptFee`` / ``removedFee`` / ``movedCount`` /
  ``displacement`` 为 0，超限时 ``requiredMoves`` 仍保留实测最小值；
  均为正常结论：退出 0，stderr 不写码；
- 输入校验失败退出 2、stderr 写唯一原因码，stdout 保持同形且
  ``result`` 为 ``INPUT_ERROR``。
"""

import json
import sys

from . import bounded
from . import core
from . import decision
from . import reorder
from . import riskplan
from .cli import parse_args

# 输入错误码：isolationLimit / maxMoves 缺失、类型错误、布尔值或小于零
BAD_ISOLATION_LIMIT = bounded.BAD_ISOLATION_LIMIT
BAD_MOVE_LIMIT = reorder.BAD_MOVE_LIMIT

# 结果码
OK = "OK"
ISOLATION_LIMIT_EXCEEDED = "ISOLATION_LIMIT_EXCEEDED"
MOVE_LIMIT_EXCEEDED = "MOVE_LIMIT_EXCEEDED"
INPUT_ERROR = "INPUT_ERROR"

# 移除 / 重排原因码：与 riskplan、reorder 保持一致
RISK_REMOVED = riskplan.RISK_REMOVED
REORDERED = reorder.REORDERED

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE），再依次
    校验必需的 isolationLimit、maxMoves（非负 JSON 整数，排除布尔值），
    非法依次抛 BAD_ISOLATION_LIMIT、BAD_MOVE_LIMIT。
    """
    req = decision.parse_request(raw)
    # 统一决策校验已通过，raw 必为合法 JSON 对象
    data = json.loads(raw)
    isolation_limit = data.get("isolationLimit")
    if not decision._is_nonneg_int(isolation_limit):
        raise decision.DecisionError(BAD_ISOLATION_LIMIT)
    max_moves = data.get("maxMoves")
    if not decision._is_nonneg_int(max_moves):
        raise decision.DecisionError(BAD_MOVE_LIMIT)
    req["isolationLimit"] = isolation_limit
    req["maxMoves"] = max_moves
    return req


def error_result(ident):
    """输入错误结果：列表为空，数值为 0，result 为 INPUT_ERROR。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "selectedOrder": [],
        "removed": [],
        "moved": [],
        "keptFee": 0,
        "removedFee": 0,
        "movedCount": 0,
        "displacement": 0,
        "requiredMoves": 0,
        "evidence": [],
        "blockers": [],
        "result": INPUT_ERROR,
        "feasible": False,
        "isolationLimit": 0,
        "maxMoves": 0,
    }


def _lanes(selected):
    """保留集合的发送者道：道内 nonce 升序，交错即全部 nonce 合法排列。"""
    lanes = {}
    for tx in selected:
        lanes.setdefault(tx["from"], []).append(tx)
    for lane in lanes.values():
        lane.sort(key=lambda tx: tx["nonce"])
    return list(lanes.values())


def _search(lanes, baseline_pos, input_pos, n, move_cap):
    """枚举发送者道的全部交错，返回 (best_key, best_order)。

    与 reorder._search 相同的增量夹子检查与比较键（位置变化数、位移
    绝对值和、最终位置的输入下标序列）；move_cap 为位置变化数上限
    （含），None 表示不设上限。无任何合法交错时返回 (None, None)。
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
            if p >= 2 and reorder._sandwich_triple(order[p - 2], order[p - 1], tx):
                continue
            base_at = baseline_pos[tx["hash"]]
            step = base_at != p
            if move_cap is not None and moved + step > move_cap:
                continue
            cursors[lane_at] = cursor + 1
            order.append(tx)
            moved += step
            disp += abs(base_at - p)
            dfs()
            disp -= abs(base_at - p)
            moved -= step
            order.pop()
            cursors[lane_at] = cursor

    dfs()
    return best_key, best_order


def _subsets(req, baseline, keepable):
    """产出预算内且通过子集级合法性（价格上下文 / 滑点 / 回滚占比）的保留集合。"""
    rollback_limit = req["rollbackLimit"]
    n = len(baseline)
    min_kept = max(0, n - req["isolationLimit"])
    for mask in range(1 << n):
        if bin(mask).count("1") < min_kept:
            continue
        selected = [baseline[i] for i in range(n) if (mask >> i) & 1]
        if any(tx["hash"] not in keepable for tx in selected):
            continue
        if not riskplan._rollback_ok(selected, rollback_limit):
            continue
        yield selected


def _failure(req, baseline_hashes, evidence, blockers, result, required_moves):
    """预算内无解结果：列表为空，金额与移动统计为 0，保留 requiredMoves。"""
    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "selectedOrder": [],
        "removed": [],
        "moved": [],
        "keptFee": 0,
        "removedFee": 0,
        "movedCount": 0,
        "displacement": 0,
        "requiredMoves": required_moves,
        "evidence": evidence,
        "blockers": blockers,
        "result": result,
        "feasible": False,
        "isolationLimit": req["isolationLimit"],
        "maxMoves": req["maxMoves"],
    }


def guard(req):
    """枚举预算内全部子集与合法交错求全局最优双预算守卫计划。"""
    txs = req["transactions"]
    isolation_limit = req["isolationLimit"]
    max_moves = req["maxMoves"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]
    baseline_pos = {tx["hash"]: at for at, tx in enumerate(baseline)}
    input_pos = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )
    blockers = riskplan._blockers(req)
    keepable = riskplan._keepable(req)

    total_fee = sum(tx["fee"] for tx in txs)

    # 第一遍：requiredMoves 为只计 isolationLimit 与合法性的最少位置
    # 变化数。已探明的最小值作为上限（减一）剪枝，只寻找更优解。
    required_moves = None
    for selected in _subsets(req, baseline, keepable):
        cap = None if required_moves is None else required_moves - 1
        if cap is not None and cap < 0:
            continue
        key, _order = _search(
            _lanes(selected), baseline_pos, input_pos, len(selected), cap)
        if key is not None:
            required_moves = key[0]

    if required_moves is None:
        # 预算内无任何合法保留集合：正常结论，退出 0，不写 stderr
        return _failure(req, baseline_hashes, evidence, blockers,
                        ISOLATION_LIMIT_EXCEEDED, 0)
    if required_moves > max_moves:
        # 最少位置变化超出 maxMoves：超限留 requiredMoves
        return _failure(req, baseline_hashes, evidence, blockers,
                        MOVE_LIMIT_EXCEEDED, required_moves)

    # 第二遍：双预算（移除笔数与位置变化数均不超限）内择优。比较键：
    # fee 总和最大、笔数最多、位置变化数最少、位移和最小、被移除 hash
    # 序列（按输入位置）字典序最小、最终位置输入下标序列字典序最小。
    best_key = None
    best_order = None
    best_kept = None
    for selected in _subsets(req, baseline, keepable):
        kept_fee = sum(tx["fee"] for tx in selected)
        kept_count = len(selected)
        if best_key is not None and (-kept_fee, -kept_count) > best_key[:2]:
            continue
        key, order = _search(
            _lanes(selected), baseline_pos, input_pos, kept_count, max_moves)
        if key is None:
            continue
        kept_set = set(tx["hash"] for tx in selected)
        removed_seq = tuple(
            tx["hash"] for tx in txs if tx["hash"] not in kept_set)
        plan_key = (-kept_fee, -kept_count, key[0], key[1], removed_seq, key[2])
        if best_key is None or plan_key < best_key:
            best_key = plan_key
            best_order = order
            best_kept = kept_set

    kept_fee = -best_key[0]
    removed = [
        {"hash": tx["hash"], "at": at, "reason": RISK_REMOVED}
        for at, tx in enumerate(txs)
        if tx["hash"] not in best_kept
    ]
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
    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "selectedOrder": [tx["hash"] for tx in best_order],
        "removed": removed,
        "moved": moved,
        "keptFee": kept_fee,
        "removedFee": total_fee - kept_fee,
        "movedCount": len(moved),
        "displacement": sum(abs(entry["to"] - entry["from"]) for entry in moved),
        "requiredMoves": required_moves,
        "evidence": evidence,
        "blockers": blockers,
        "result": OK,
        "feasible": True,
        "isolationLimit": isolation_limit,
        "maxMoves": max_moves,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并依次追加 isolationLimit、maxMoves 校验；
    error_code 非空时退出码 2。预算内无解或超限是正常结论，
    error_code 为 None。
    """
    try:
        req = parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident), exc.code
    return guard(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次双预算守卫计划，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码；
    预算内无解或超限退出 0，不写 stderr。
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
