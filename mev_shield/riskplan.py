"""风险约束隔离计划：python -m mev_shield.riskplan [--input IN] [--output OUT]

在预算约束隔离计划（``python -m mev_shield.bounded``）的输入、校验与
基线规则之上，保留集合追加滑点与回滚风险约束。既有入口的输入、输出
与退出码均保持不变。

输入与校验同 bounded：先执行统一决策入口的全部校验（含
``BAD_SLIPPAGE_MODE``），再校验必需的 ``isolationLimit``（非负 JSON
整数，排除布尔值）；缺失、类型错误、布尔值或小于零均返回
``BAD_ISOLATION_LIMIT``，旧错误一律优先。

合法性（保留集合按基线顺序的相对顺序排列，移除笔数不超过
``isolationLimit``）：

- 同一 ``from`` 的保留交易 nonce 在该顺序上严格递增，且顺序执行
  与统一决策入口相同的相邻三段夹子判定，无任何夹子证据；
- 按 ``slippageMode`` 检查每笔保留交易的滑点：base 口径用
  ``basePrice``，market 口径用 ``market.prices`` 中同 token 正数
  参考价，偏离率不得超过 ``maxSlippage``；market 口径缺 token
  参考价的交易不可保留；
- 保留集合中 sim 为 revert 的占比不得超过 ``rollbackLimit``，
  空集合占比记 0。

择优目标依次为：保留 fee 总和最高、保留笔数最多、被移除交易按输入
位置形成的 hash 序列字典序最小。枚举全部预算内子集求全局最优，不做
逐笔贪心；结果确定，相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``（全部交易的 fee 降序、
hash 升序基线顺序）、``selectedOrder``（最优保留集合顺序）、
``removed``（按输入位置列出 hash / at / RISK_REMOVED）、
``keptFee``、``removedFee``、``evidence``（baselineOrder 上的全部
夹子证据，按起始位置升序、同位按 victim hash 升序）、``blockers``
（基线统一决策原因码去重，固定顺序 SANDWICH_DETECTED、
SLIPPAGE_EXCEEDED、PRICE_CONTEXT_MISSING、NONCE_ORDER_VIOLATION、
ROLLBACK_LIMIT_EXCEEDED）、``feasible``、``isolationLimit``。

预算内无解不是输入错误：退出 0，``feasible`` 为 false，
``selectedOrder`` 与 ``removed`` 为空，``keptFee`` 与 ``removedFee``
为 0，基线字段（baselineOrder / evidence / blockers /
isolationLimit）仍取完整基线，stderr 不写码。
"""

import sys

from . import bounded
from . import core
from . import decision
from .cli import parse_args

# 输入错误码：isolationLimit 缺失、类型错误、布尔值或小于零
BAD_ISOLATION_LIMIT = bounded.BAD_ISOLATION_LIMIT

# 移除原因码：风险约束隔离计划中的被移除交易统一记此原因
RISK_REMOVED = "RISK_REMOVED"

# blockers 固定顺序：基线统一决策原因码按此顺序去重输出
_BLOCKER_ORDER = (
    decision.SANDWICH_DETECTED,
    decision.SLIPPAGE_EXCEEDED,
    decision.PRICE_CONTEXT_MISSING,
    decision.NONCE_ORDER_VIOLATION,
    decision.ROLLBACK_LIMIT_EXCEEDED,
)

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    校验与 bounded 完全一致：先完整执行统一决策入口的全部校验，再
    校验必需的 isolationLimit（非负 JSON 整数，排除布尔值），否则
    抛 BAD_ISOLATION_LIMIT。
    """
    return bounded.parse_request(raw)


def error_result(ident):
    """输入错误结果：列表为空，费用与 isolationLimit 为 0。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "selectedOrder": [],
        "removed": [],
        "keptFee": 0,
        "removedFee": 0,
        "evidence": [],
        "blockers": [],
        "feasible": False,
        "isolationLimit": 0,
    }


def _slippage_ok(tx, req):
    """单笔保留交易是否满足滑点约束；market 口径缺参考价不可保留。"""
    if req["slippageMode"] == decision.SLIPPAGE_MODE_MARKET:
        reference = req["market"]["prices"].get(tx["token"])
        if reference is None:
            return False
    else:
        reference = req["basePrice"]
    dev = abs(tx["price"] - reference) / reference
    return dev <= req["maxSlippage"]


def _legal(selected, req):
    """保留集合（已按基线相对顺序排列）是否合法。

    nonce 严格递增、无相邻三段夹子证据、逐笔滑点不超限、revert
    占比不超过 rollbackLimit（空集合占比记 0）。
    """
    if not decision.nonce_order_satisfied(selected):
        return False
    if decision.detect_sandwich_evidence(selected):
        return False
    for tx in selected:
        if not _slippage_ok(tx, req):
            return False
    if selected:
        reverts = sum(1 for tx in selected if tx["sim"] == "revert")
        if reverts / len(selected) > req["rollbackLimit"]:
            return False
    return True


def isolate(req):
    """枚举预算内全部子集求全局最优风险约束隔离计划，返回结果字典。"""
    txs = req["transactions"]
    limit = req["isolationLimit"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )

    # blockers：基线统一决策原因码去重，按固定顺序输出
    found = set(decision.decide(req)["reasons"])
    blockers = [code for code in _BLOCKER_ORDER if code in found]

    n = len(baseline)
    total_fee = sum(tx["fee"] for tx in txs)

    # 枚举基线顺序中移除笔数不超过 isolationLimit 的全部子集
    # （mask 第 i 位保留 baseline[i]）。比较键：fee 总和最大、笔数最多、
    # 被移除 hash 序列（按输入位置）字典序最小。预算内可能无任何合法
    # 子集，此时 best_key 保持 None。
    best_key = None
    best_kept = ()
    min_kept = max(0, n - limit)
    for mask in range(1 << n):
        if bin(mask).count("1") < min_kept:
            continue
        selected = [baseline[i] for i in range(n) if (mask >> i) & 1]
        if not _legal(selected, req):
            continue
        kept_hashes = [tx["hash"] for tx in selected]
        kept_set = set(kept_hashes)
        kept_fee = sum(tx["fee"] for tx in selected)
        removed_seq = [
            tx["hash"] for tx in txs if tx["hash"] not in kept_set
        ]
        key = (-kept_fee, -len(kept_hashes), removed_seq)
        if best_key is None or key < best_key:
            best_key = key
            best_kept = kept_hashes

    if best_key is None:
        # 预算内无解：正常结论，退出 0，不写 stderr
        return {
            "id": req["id"],
            "baselineOrder": baseline_hashes,
            "selectedOrder": [],
            "removed": [],
            "keptFee": 0,
            "removedFee": 0,
            "evidence": evidence,
            "blockers": blockers,
            "feasible": False,
            "isolationLimit": limit,
        }

    kept_fee = -best_key[0]
    kept_set = set(best_kept)
    removed = [
        {"hash": tx["hash"], "at": at, "reason": RISK_REMOVED}
        for at, tx in enumerate(txs)
        if tx["hash"] not in kept_set
    ]
    return {
        "id": req["id"],
        "baselineOrder": baseline_hashes,
        "selectedOrder": list(best_kept),
        "removed": removed,
        "keptFee": kept_fee,
        "removedFee": total_fee - kept_fee,
        "evidence": evidence,
        "blockers": blockers,
        "feasible": True,
        "isolationLimit": limit,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用 bounded（统一决策校验 + isolationLimit 校验）；
    error_code 非空时退出码 2。预算内无解是正常结论，error_code
    为 None。
    """
    try:
        req = parse_request(raw)
    except decision.DecisionError as exc:
        ident, _count = decision._peek(raw)
        return error_result(ident), exc.code
    return isolate(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次风险约束隔离计划，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码；
    预算内无解退出 0，不写 stderr。
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
