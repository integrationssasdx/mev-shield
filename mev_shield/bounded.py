"""预算约束隔离计划：python -m mev_shield.bounded [--input IN] [--output OUT]

在最小隔离计划（``python -m mev_shield.mitigation``）的输入、校验、
fee 降序 hash 升序基线、相邻三段夹子规则与同 from nonce 严格递增
约束之上，新增最多移除笔数预算 ``isolationLimit``。既有入口的输入、
输出与退出码均保持不变。

输入同统一决策入口，根对象新增必需 ``isolationLimit``：最多移除笔数，
必须是非负 JSON 整数（排除布尔值）。缺失、类型错误、布尔值或小于
零一律返回 ``BAD_ISOLATION_LIMIT``，该校验排在统一决策入口全部既有
校验（含 ``BAD_SLIPPAGE_MODE``）之后。

枚举保留集合（按基线相对顺序排列），合法性与最小隔离计划一致
（无夹子证据、同 from nonce 严格递增），且被移除笔数不超过
``isolationLimit``。择优目标依次为：保留 fee 总和最高、保留笔数
最多、被移除交易按输入位置形成的 hash 序列字典序最小；空集虽恒
合法，但其移除数为全部笔数，可能超出预算——此时预算内无合法解。

输出字段固定：``id``、``baselineOrder``、``selectedOrder``、
``removed``、``keptFee``、``removedFee``、``evidence``、``feasible``、
``isolationLimit``。预算内无解时退出 0、``feasible`` 为 false、
``selectedOrder`` 与 ``removed`` 为空、两个 fee 为 0，``evidence``
仍取完整基线。
"""

import json
import sys

from . import core
from . import decision
from .cli import parse_args

# 新增输入错误码：隔离预算缺失、类型错误、布尔值或小于零
BAD_ISOLATION_LIMIT = "BAD_ISOLATION_LIMIT"

# 沿用最小隔离计划的移除原因码
SANDWICH_REMOVED = "SANDWICH_REMOVED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回带 isolationLimit 的规范化请求；失败抛 DecisionError。

    统一决策入口的全部校验（含 BAD_SLIPPAGE_MODE）先执行，之后才
    校验 isolationLimit，故任何既有错误码都优先于 BAD_ISOLATION_LIMIT。
    isolationLimit 必需存在，且为非负 JSON 整数（排除布尔值）。
    """
    req = decision.parse_request(raw)
    data = json.loads(raw)
    limit = data.get("isolationLimit")
    if not decision._is_nonneg_int(limit):
        raise decision.DecisionError(BAD_ISOLATION_LIMIT)
    req["isolationLimit"] = limit
    return req


def error_result(ident):
    """输入错误结果：四个列表为空，fees 为 0，feasible 为 false，预算记 0。"""
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


def _infeasible(ident, baseline_hashes, evidence, limit):
    """预算内无合法解：选择与移除列表为空、fee 为 0，证据仍取完整基线。"""
    return {
        "id": ident,
        "baselineOrder": baseline_hashes,
        "selectedOrder": [],
        "removed": [],
        "keptFee": 0,
        "removedFee": 0,
        "evidence": evidence,
        "feasible": False,
        "isolationLimit": limit,
    }


def _legal(selected):
    """保留集合（已按基线相对顺序排列）是否合法：nonce 严格递增且无夹子。"""
    if not decision.nonce_order_satisfied(selected):
        return False
    return not decision.detect_sandwich_evidence(selected)


def isolate(req):
    """在 isolationLimit 预算内枚举子集求全局最优隔离计划，返回结果字典。"""
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

    # 枚举基线顺序的全部子集（mask 第 i 位保留 baseline[i]），只考虑
    # 移除数 n-popcount(mask) 不超过预算的保留集合。
    # 比较键：fee 总和最大、笔数最多、被移除 hash 序列（按输入位置）
    # 字典序最小。预算内可能一个合法解都没有（含空集也超预算）。
    best_key = None
    best_kept = ()
    for mask in range(1 << n):
        kept_count = bin(mask).count("1")
        if n - kept_count > limit:
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
        return _infeasible(req["id"], baseline_hashes, evidence, limit)

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

    输入校验与错误码沿用统一决策入口，新增 BAD_ISOLATION_LIMIT
    排在最后；error_code 非空时退出码 2。预算不足是正常结果（退出 0，
    feasible 为 false），不返回错误码。
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
    预算内无可行解为正常结果，退出 0 且不写 stderr。
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
