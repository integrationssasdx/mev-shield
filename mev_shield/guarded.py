"""统一隔离、预算与安全重排：python -m mev_shield.guarded [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验、
fee 降序 / hash 升序基线、相邻三段夹子证据与 nonce 依赖之上，把风险
约束隔离（``mev_shield.riskplan``）与安全重排（``mev_shield.reorder``）
合并为一次决策：先按预算移除风险交易，再在保留集合内调整顺序，给出
无夹子的安全顺序。既有入口的输入、输出与退出码均保持不变。

输入在统一决策入口的根对象上新增两个必需字段：

- ``isolationLimit``：最多移除笔数，非负 JSON 整数（排除布尔值）。
  缺失、类型错误、布尔值或小于零均返回 ``BAD_ISOLATION_LIMIT``；
- ``maxMoves``：最终顺序相对基线的位置变化数上限，非负 JSON 整数
  （排除布尔值）。缺失、类型错误、布尔值或小于零均返回
  ``BAD_MOVE_LIMIT``。

两者均排在统一决策入口全部既有校验（含 ``BAD_SLIPPAGE_MODE``）之后，
旧错误一律优先；两个字段同时非法时先报 ``BAD_ISOLATION_LIMIT``。

保留集合合法性（沿用风险约束隔离）：

- 同一 ``from`` 的保留交易 nonce 在最终顺序上严格递增；
- 顺序执行与统一决策入口相同的相邻三段夹子判定，无任何夹子证据；
- 逐笔滑点与价格上下文检查：``slippageMode`` 为 base 时用
  ``basePrice``、为 market 时用 ``market.prices`` 中同 token 的正数
  参考价，绝对偏离率不得超过 ``maxSlippage``；market 模式缺 token
  正数参考价的交易不可保留；
- 保留集合中 sim 为 revert 的占比不得超过 ``rollbackLimit``，空集合
  占比视为 0；
- 被移除笔数不超过 ``isolationLimit``。

最终顺序必须恰好排列保留交易：枚举移除预算内的全部保留子集，再枚举
其发送者道的全部 nonce 合法交错，筛选无夹子且通过风险检查者。
``requiredMoves`` 为其中最终顺序相对基线位置变化数的最小值（无可行
集合时为 0），只取决于 ``isolationLimit`` 与合法性，与 ``maxMoves``
无关。

择优目标依次为：保留 fee 总和最高、保留笔数最多、位置变化数最少、
各位移绝对值和最小、被移除交易按输入位置形成的 hash 序列字典序最小、
最终顺序对应输入下标序列字典序最小。枚举全部方案求全局最优，不做
逐笔贪心；结果确定，相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``、``selectedOrder``、
``removed``（按输入位置列出 hash / at / RISK_REMOVED，去重）、
``moved``（按最终位置升序列出 hash / from / to / REORDERED，去重）、
``keptFee``、``removedFee``、``movedCount``、``displacement``、
``requiredMoves``、``evidence``（完整基线的全部夹子证据，按起始位置
升序、同位按 victim hash 升序）、``blockers``（基线统一决策原因码
去重，顺序同 riskplan）、``result``、``feasible``、``isolationLimit``、
``maxMoves``。

- 双预算同时满足：``result`` 为 ``OK``、``feasible`` 为 true；
- 隔离预算内无合法保留集合：``result`` 为 ``ISOLATION_LIMIT_EXCEEDED``；
- 最少变化数超过 ``maxMoves``：``result`` 为 ``MOVE_LIMIT_EXCEEDED``。
  后两者 ``feasible`` 为 false，列表为空、金额与移动统计为 0；
  超移动预算时 ``requiredMoves`` 仍给出实测最少变化数。

无方案或超限都是正常结论：退出 0，stderr 不写码；输入校验失败退出
2、stderr 写唯一原因码，stdout 保持同形（列表为空、数值为 0）。
"""

import json
import sys

from . import core
from . import decision
from . import riskplan
from .cli import parse_args

# 输入错误码：依次为 isolationLimit、maxMoves（旧校验一律优先）
BAD_ISOLATION_LIMIT = riskplan.BAD_ISOLATION_LIMIT
BAD_MOVE_LIMIT = "BAD_MOVE_LIMIT"

# 移除 / 重排原因码
RISK_REMOVED = riskplan.RISK_REMOVED
REORDERED = "REORDERED"

# 结果码
OK = "OK"
ISOLATION_LIMIT_EXCEEDED = "ISOLATION_LIMIT_EXCEEDED"
MOVE_LIMIT_EXCEEDED = "MOVE_LIMIT_EXCEEDED"
INPUT_ERROR = "INPUT_ERROR"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE），再依次
    校验必需的 isolationLimit 与 maxMoves：均为非负 JSON 整数（排除
    布尔值），否则依次抛 BAD_ISOLATION_LIMIT、BAD_MOVE_LIMIT。
    """
    req = decision.parse_request(raw)
    # 统一决策校验已通过，raw 必为合法 JSON 对象
    data = json.loads(raw)
    if not decision._is_nonneg_int(data.get("isolationLimit")):
        raise decision.DecisionError(BAD_ISOLATION_LIMIT)
    if not decision._is_nonneg_int(data.get("maxMoves")):
        raise decision.DecisionError(BAD_MOVE_LIMIT)
    req["isolationLimit"] = data["isolationLimit"]
    req["maxMoves"] = data["maxMoves"]
    return req


def error_result(ident):
    """输入错误结果：列表为空，金额与移动统计为 0，预算回显为 0。"""
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


def _best_interleaving(kept, baseline_pos, input_pos):
    """枚举保留集合的全部 nonce 合法交错，返回最优安全顺序的比较键。

    道内按 nonce 升序排列，每个交错天然满足同一 from 的 nonce 严格
    递增；追加交易时增量检查新完成的相邻三元组，保证无夹子证据。
    比较键（在保留集合固定时）：位置变化数、位移绝对值和、最终位置
    的输入下标序列。无合法交错时返回 None。
    """
    lanes_map = {}
    for tx in kept:
        lanes_map.setdefault(tx["from"], []).append(tx)
    lanes = list(lanes_map.values())
    for lane in lanes:
        lane.sort(key=lambda tx: tx["nonce"])

    n = len(kept)
    best_key = None
    order = []
    cursors = [0] * len(lanes)
    moved = 0
    disp = 0

    def dfs():
        nonlocal best_key, moved, disp
        p = len(order)
        if p == n:
            seq = tuple(input_pos[tx["hash"]] for tx in order)
            key = (moved, disp, seq)
            if best_key is None or key < best_key:
                best_key = key
            return
        # moved 沿路径单调不减，disp 同样单调不减：超过当前最优可剪枝
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
    return best_key


def plan(req):
    """枚举预算内全部子集与全部安全交错求全局最优，返回结果字典。"""
    txs = req["transactions"]
    isolation_limit = req["isolationLimit"]
    move_limit = req["maxMoves"]
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
    rollback_limit = req["rollbackLimit"]

    n = len(baseline)
    total_fee = sum(tx["fee"] for tx in txs)

    # 枚举基线顺序中移除笔数不超过 isolationLimit 的全部保留子集
    # （mask 第 i 位保留 baseline[i]）。对每个风险合法子集枚举其全部
    # nonce 合法、无夹子的交错，再按六级目标选全局最优。两条轨道：
    # min_required 为所有合法方案的最少位置变化数（忽略 maxMoves）；
    # best_* 仅在位置变化数不超过 maxMoves 的方案中择优。
    best_key = None
    best_kept = ()
    min_required = None
    min_kept_count = max(0, n - isolation_limit)
    for mask in range(1 << n):
        if bin(mask).count("1") < min_kept_count:
            continue
        selected = [baseline[i] for i in range(n) if (mask >> i) & 1]
        # 风险合法性：逐笔可保留、revert 占比不超限。nonce 严格递增由
        # 道内 nonce 升序的交错结构保证，无夹子由增量三元组检查保证
        # （空集合恒安全）；故这里不按基线相对顺序预判 nonce，基线
        # 倒序的同 from 交易仍可靠重排修复。
        if any(tx["hash"] not in keepable for tx in selected):
            continue
        if not riskplan._rollback_ok(selected, rollback_limit):
            continue

        inter = _best_interleaving(selected, baseline_pos, input_pos)
        if inter is None:
            continue
        moves, displacement, _seq = inter

        # requiredMoves：忽略 maxMoves 的最少位置变化数
        if min_required is None or moves < min_required:
            min_required = moves

        # 超过 maxMoves 的方案不参与最终方案择优
        if moves > move_limit:
            continue

        kept_hashes = [tx["hash"] for tx in selected]
        kept_set = set(kept_hashes)
        kept_fee = sum(tx["fee"] for tx in selected)
        removed_seq = [
            tx["hash"] for tx in txs if tx["hash"] not in kept_set
        ]
        # 比较键：保留 fee、笔数、位置变化数、位移和、被移除 hash 序列、
        # 最终顺序输入下标序列
        key = (
            -kept_fee,
            -len(kept_hashes),
            moves,
            displacement,
            removed_seq,
            _seq,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_kept = kept_hashes

    if min_required is None:
        # 隔离预算内无任何合法保留集合：正常结论，退出 0，不写 stderr
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
            "requiredMoves": 0,
            "evidence": evidence,
            "blockers": blockers,
            "result": ISOLATION_LIMIT_EXCEEDED,
            "feasible": False,
            "isolationLimit": isolation_limit,
            "maxMoves": move_limit,
        }

    required_moves = min_required
    if best_key is None:
        # 存在合法保留集合，但最少位置变化数也超过 maxMoves：保留
        # requiredMoves，其余列表 / 金额 / 移动统计置空置零
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
            "result": MOVE_LIMIT_EXCEEDED,
            "feasible": False,
            "isolationLimit": isolation_limit,
            "maxMoves": move_limit,
        }

    kept_fee = -best_key[0]
    kept_set = set(best_kept)
    moved_count = best_key[2]
    displacement = best_key[3]
    final_seq = best_key[5]
    selected_order = [txs[i]["hash"] for i in final_seq]

    # removed 按输入位置去重列出；moved 按最终位置升序去重列出
    removed = [
        {"hash": tx["hash"], "at": at, "reason": RISK_REMOVED}
        for at, tx in enumerate(txs)
        if tx["hash"] not in kept_set
    ]
    moved = [
        {
            "hash": txs[i]["hash"],
            "from": baseline_pos[txs[i]["hash"]],
            "to": to_at,
            "reason": REORDERED,
        }
        for to_at, i in enumerate(final_seq)
        if baseline_pos[txs[i]["hash"]] != to_at
    ]
    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "selectedOrder": selected_order,
        "removed": removed,
        "moved": moved,
        "keptFee": kept_fee,
        "removedFee": total_fee - kept_fee,
        "movedCount": moved_count,
        "displacement": displacement,
        "requiredMoves": required_moves,
        "evidence": evidence,
        "blockers": blockers,
        "result": OK,
        "feasible": True,
        "isolationLimit": isolation_limit,
        "maxMoves": move_limit,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并依次追加 isolationLimit、maxMoves 校验；
    error_code 非空时退出码 2。隔离无解或移动超限是正常结论，
    error_code 为 None。
    """
    try:
        req = parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident), exc.code
    return plan(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次统一隔离重排决策，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码；
    隔离无解或移动超限退出 0，不写 stderr。
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
