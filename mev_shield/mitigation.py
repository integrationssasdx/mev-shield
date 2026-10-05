"""最小隔离计划：python -m mev_shield.mitigation [--input IN] [--output OUT]

在统一决策入口（``python -m mev_shield.decision``）的输入、校验与
相邻三段夹子规则之上，从交易包的全部子集中选出一个合法的最小隔离
保留集合。既有两个入口的输入、输出与退出码均保持不变。

合法性（保留集合按基线顺序的相对顺序排列）：

- 顺序执行与统一决策入口相同的相邻三段夹子判定，无任何夹子证据；
- 同一 ``from`` 的保留交易 nonce 在该顺序上严格递增。

择优目标依次为：保留 fee 总和最高、保留笔数最多、被移除交易按输入
位置形成的 hash 序列字典序最小。枚举全部子集求全局最优，不做逐笔
贪心；空集恒合法，故最优解始终存在。

输出字段固定：``id``、``baselineOrder``（全部交易的 fee 降序、
hash 升序基线顺序）、``selectedOrder``（最优保留集合顺序）、
``removed``（按输入位置列出 hash / at / SANDWICH_REMOVED）、
``keptFee``、``removedFee``、``evidence``（baselineOrder 上的全部
夹子证据，按起始位置升序、同位按 victim hash 升序）。
"""

import sys

from . import core
from . import decision
from .cli import parse_args

# 移除原因码：隔离计划中的被移除交易统一记此原因
SANDWICH_REMOVED = "SANDWICH_REMOVED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def error_result(ident):
    """输入错误结果：四个列表为空，keptFee / removedFee 为 0。"""
    return {
        "id": ident,
        "baselineOrder": [],
        "selectedOrder": [],
        "removed": [],
        "keptFee": 0,
        "removedFee": 0,
        "evidence": [],
    }


def _legal(selected):
    """保留集合（已按基线相对顺序排列）是否合法：nonce 严格递增且无夹子。"""
    if not decision.nonce_order_satisfied(selected):
        return False
    return not decision.detect_sandwich_evidence(selected)


def isolate(req):
    """枚举全部子集求全局最优隔离计划，返回结果字典。"""
    txs = req["transactions"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )

    n = len(baseline)
    total_fee = sum(tx["fee"] for tx in txs)

    # 枚举基线顺序的全部子集（mask 第 i 位保留 baseline[i]）。
    # 比较键：fee 总和最大、笔数最多、被移除 hash 序列（按输入位置）
    # 字典序最小；空集恒合法，best 必被赋值。
    best_key = None
    best_kept = ()
    for mask in range(1 << n):
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
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    输入校验与错误码完全沿用统一决策入口；error_code 非空时退出码 2。
    """
    try:
        req = decision.parse_request(raw)
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
    """执行一次隔离计划，返回退出码。缓冲区参数用于测试注入。

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
