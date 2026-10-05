"""预算约束隔离计划：python -m mev_shield.bounded [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验与
相邻三段夹子规则之上，从交易包的全部子集中选出一个合法的隔离保留
集合，且被移除笔数不超过批次级预算 ``isolationLimit``。既有入口的
输入、输出与退出码均保持不变。

输入在统一决策入口的根对象上新增必需字段：

- ``isolationLimit``：最多移除笔数，非负 JSON 整数（排除布尔值）。
  缺失、类型错误、布尔值或小于零均返回 ``BAD_ISOLATION_LIMIT``；
  该校验排在统一决策入口全部既有校验（含 ``BAD_SLIPPAGE_MODE``）
  之后，旧错误一律优先。

合法性（保留集合按基线顺序的相对顺序排列）：

- 顺序执行与统一决策入口相同的相邻三段夹子判定，无任何夹子证据；
- 同一 ``from`` 的保留交易 nonce 在该顺序上严格递增；
- 被移除笔数不超过 ``isolationLimit``。

择优目标依次为：保留 fee 总和最高、保留笔数最多、被移除交易按输入
位置形成的 hash 序列字典序最小。枚举全部预算内子集求全局最优，不做
逐笔贪心；结果确定，相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``（全部交易的 fee 降序、
hash 升序基线顺序）、``selectedOrder``（最优保留集合顺序）、
``removed``（按输入位置列出 hash / at / SANDWICH_REMOVED）、
``keptFee``、``removedFee``、``evidence``（baselineOrder 上的全部
夹子证据，按起始位置升序、同位按 victim hash 升序）、``feasible``、
``isolationLimit``。

预算内无解不是输入错误：退出 0，``feasible`` 为 false，
``selectedOrder`` 与 ``removed`` 为空，``keptFee`` 与 ``removedFee``
为 0，``baselineOrder`` 与 ``evidence`` 仍取完整基线，stderr 不写码。
"""

import json
import sys

from . import core
from . import decision
from .cli import parse_args

# 输入错误码：isolationLimit 缺失、类型错误、布尔值或小于零
BAD_ISOLATION_LIMIT = "BAD_ISOLATION_LIMIT"

# 移除原因码：隔离计划中的被移除交易统一记此原因
SANDWICH_REMOVED = "SANDWICH_REMOVED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE），再校验
    必需的 isolationLimit：非负 JSON 整数（排除布尔值），否则抛
    BAD_ISOLATION_LIMIT。
    """
    req = decision.parse_request(raw)
    # 统一决策校验已通过，raw 必为合法 JSON 对象
    limit = json.loads(raw).get("isolationLimit")
    if not decision._is_nonneg_int(limit):
        raise decision.DecisionError(BAD_ISOLATION_LIMIT)
    req["isolationLimit"] = limit
    return req


def error_result(ident):
    """输入错误结果：四个列表为空，keptFee / removedFee / isolationLimit 为 0。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "selectedOrder": [],
        "removed": [],
        "keptFee": 0,
        "removedFee": 0,
        "evidence": [],
        "feasible": False,
        "isolationLimit": 0,
    }


def _legal(selected):
    """保留集合（已按基线相对顺序排列）是否合法：nonce 严格递增且无夹子。"""
    if not decision.nonce_order_satisfied(selected):
        return False
    return not decision.detect_sandwich_evidence(selected)


def isolate(req):
    """枚举预算内全部子集求全局最优隔离计划，返回结果字典。"""
    txs = req["transactions"]
    limit = req["isolationLimit"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )

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
        if not _legal(selected):
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
            "feasible": False,
            "isolationLimit": limit,
        }

    kept_fee = -best_key[0]
    kept_set = set(best_kept)
    removed = [
        {"hash": tx["hash"], "at": at, "reason": SANDWICH_REMOVED}
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
        "feasible": True,
        "isolationLimit": limit,
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验沿用统一决策入口并追加 isolationLimit 校验；error_code
    非空时退出码 2。预算内无解是正常结论，error_code 为 None。
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
    """执行一次预算约束隔离计划，返回退出码。缓冲区参数用于测试注入。

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
