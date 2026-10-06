"""安全重排计划：python -m mev_shield.reorder [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验、
fee 降序与 hash 升序基线、相邻三段夹子证据与 nonce 依赖之上，给出
一份只调整顺序、不删除交易的安全重排计划。既有入口的输入、输出与
退出码均保持不变。

输入在统一决策入口的根对象上新增必需字段：

- ``maxMoves``：可改变基线位置的交易数上限，非负 JSON 整数（排除
  布尔值）。缺失、类型错误、布尔值或小于零均返回 ``BAD_MOVE_LIMIT``；
  该校验排在统一决策入口全部既有校验（含 ``BAD_SLIPPAGE_MODE``）
  之后，旧错误一律优先。

合法性（safeOrder 为输入交易的一个排列，不增加、丢失或重复）：

- 同一 ``from`` 的交易 nonce 在该顺序上严格递增；
- 顺序执行与统一决策入口相同的相邻三段夹子判定，无任何夹子证据。

基线仍按 fee 降序、hash 升序。择优目标依次为：位置变化数（最终
下标与基线下标不同的交易笔数）最小、各交易最终下标与基线下标差的
绝对值和最小、最终位置对应输入下标序列的字典序最小。枚举全部排列
求全局最优，不做逐笔贪心；结果确定，相同输入逐字一致。滑点、价格
上下文与回滚沿用既有校验，但不参与合法性判定，不影响可行性。

输出字段固定：``id``、``baselineOrder``（基线 hash 顺序）、
``safeOrder``（最优安全顺序）、``moved``（位置变化的交易，按最终
位置升序列出 ``hash``、``from``（基线下标）、``to``（最终下标）与
固定原因 ``REORDERED``）、``movedCount``、``displacement``、
``evidence``（完整基线的全部夹子证据，按起始位置升序、同位按
victim hash 升序）、``blockers``（基线统一决策问题，仅按
SANDWICH_DETECTED、NONCE_ORDER_VIOLATION 顺序列出）、``result``
（``OK`` / ``MOVE_LIMIT_EXCEEDED`` / ``SAFE_ORDER_NOT_FOUND`` /
``INPUT_ERROR``）、``feasible``、``maxMoves``。

结果判定：最优方案的位置变化数不超过 ``maxMoves`` 时 ``result`` 为
``OK`` 且 ``feasible`` 为 true；超限时为 ``MOVE_LIMIT_EXCEEDED``
且 ``feasible`` 为 false，仍输出最优方案；没有任何合法顺序时为
``SAFE_ORDER_NOT_FOUND``，``safeOrder`` 与 ``moved`` 为空、
``movedCount`` 与 ``displacement`` 为 0。无方案或超限均为正常
结论：退出 0，stderr 不写码。输入校验失败退出 2，stderr 写唯一
原因码，stdout 保持同形且 ``result`` 为 ``INPUT_ERROR``。
"""

import json
import sys
from itertools import permutations

from . import core
from . import decision
from .cli import parse_args

# 输入错误码：maxMoves 缺失、类型错误、布尔值或小于零
BAD_MOVE_LIMIT = "BAD_MOVE_LIMIT"

# 结果结论码
RESULT_OK = "OK"
RESULT_MOVE_LIMIT = "MOVE_LIMIT_EXCEEDED"
RESULT_NOT_FOUND = "SAFE_ORDER_NOT_FOUND"
RESULT_INPUT_ERROR = "INPUT_ERROR"

# 移动记录原因码：重排计划中位置变化的交易统一记此原因
REORDERED = "REORDERED"

# blockers 固定顺序：统一决策原因码中与顺序合法性相关的两类
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
        "result": RESULT_INPUT_ERROR,
        "feasible": False,
        "maxMoves": 0,
    }


def _blockers(req):
    """基线统一决策的原因码去重，仅保留顺序相关两类，按固定顺序输出。"""
    reasons = set(decision.decide(req)["reasons"])
    return [code for code in _BLOCKER_ORDER if code in reasons]


def reorder(req):
    """枚举全部排列求全局最优安全重排计划，返回结果字典。"""
    txs = req["transactions"]
    limit = req["maxMoves"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )
    blockers = _blockers(req)

    n = len(baseline)
    input_at = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 枚举基线位置的全部排列（perm[f] 为最终位置 f 上的基线下标）。
    # 比较键：位置变化数最小、位移绝对值和最小、最终位置对应输入下标
    # 序列字典序最小。排列数量随交易数阶乘增长，与既有入口的指数级
    # 枚举约定一致，面向单批次小规模输入。
    best_key = None
    best_perm = None
    for perm in permutations(range(n)):
        moves = 0
        displacement = 0
        for final_at, base_at in enumerate(perm):
            if base_at != final_at:
                moves += 1
                displacement += abs(final_at - base_at)
        # 已不可能优于当前最优（前两级比较键更大）时跳过合法性检查
        if best_key is not None and (moves, displacement) > best_key[:2]:
            continue
        ordered = [baseline[base_at] for base_at in perm]
        if not decision.nonce_order_satisfied(ordered):
            continue
        if decision.detect_sandwich_evidence(ordered):
            continue
        key = (
            moves,
            displacement,
            tuple(input_at[tx["hash"]] for tx in ordered),
        )
        if best_key is None or key < best_key:
            best_key = key
            best_perm = perm

    if best_perm is None:
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
            "result": RESULT_NOT_FOUND,
            "feasible": False,
            "maxMoves": limit,
        }

    safe_order = [baseline[base_at]["hash"] for base_at in best_perm]
    moved = [
        {
            "hash": baseline[base_at]["hash"],
            "from": base_at,
            "to": final_at,
            "reason": REORDERED,
        }
        for final_at, base_at in enumerate(best_perm)
        if base_at != final_at
    ]
    moves, displacement = best_key[0], best_key[1]
    feasible = moves <= limit
    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "safeOrder": safe_order,
        "moved": moved,
        "movedCount": moves,
        "displacement": displacement,
        "evidence": evidence,
        "blockers": blockers,
        "result": RESULT_OK if feasible else RESULT_MOVE_LIMIT,
        "feasible": feasible,
        "maxMoves": limit,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并追加 maxMoves 校验；error_code 非空时
    退出码 2。无合法顺序或超出 maxMoves 是正常结论，error_code 为
    None。
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
    无合法顺序或超出 maxMoves 退出 0，不写 stderr。
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
