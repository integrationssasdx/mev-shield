"""捆绑原子性风险隔离计划：python -m mev_shield.bundleisolate

在风险约束隔离计划（``python -m mev_shield.riskplan``）的输入、校验、
基线顺序与风险合法性，以及不可拆分交易捆绑
（``python -m mev_shield.bundleschedule``）的同值 ``bundle`` 语义之上，
把带非空 ``bundle`` 标识的交易按同值标识组成不可拆分捆绑：同值捆绑
只能整组保留或整组移除，被移除交易笔数不超过批次级预算
``isolationLimit``。既有入口的输入、输出与退出码均保持不变。

输入与校验同 riskplan：统一决策入口的根对象加必需字段
``isolationLimit``（非负 JSON 整数，排除布尔值），且每笔交易必须
携带非空字符串 ``bundle``。校验顺序固定：先完整执行统一决策入口的
全部既有校验，再校验 ``isolationLimit``（缺失、类型错误、布尔值或
小于零返回 ``BAD_ISOLATION_LIMIT``），最后逐笔校验 ``bundle``
（缺失或非非空字符串返回 ``BAD_BUNDLE_ID``）；旧错误一律优先。

合法性（保留集合按基线顺序的相对顺序排列）沿用 riskplan：

- 逐笔通过价格上下文与滑点检查（base 口径用 ``basePrice`` 且 token
  须有市场参考价；market 口径用 ``market.prices`` 中同 token 的正数
  参考价；偏离率不得超过 ``maxSlippage``）；
- 同一 ``from`` 的保留交易 nonce 在该顺序上严格递增；
- 按统一决策的相邻三段规则无任何夹子证据；
- 保留集合中 sim 为 revert 的占比不超过 ``rollbackLimit``，空集合
  占比视为 0。

捆绑原子性：同 ``bundle`` 值的交易要么全部保留、要么全部移除；移除
预算按交易笔数计（整组成员笔数之和），不得超过 ``isolationLimit``。
捆绑仅当全部成员都可保留时才进入候选保留集合。

择优目标依次为：保留 fee 总和最高、保留交易数最多、被移除捆绑按其
首笔交易输入位置形成的序列字典序最小。枚举全部预算内捆绑保留方案
求全局最优，不做逐笔贪心；相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``（全部交易的 fee 降序、
hash 升序基线顺序）、``selectedOrder``（最优保留集合顺序）、
``removed``（按捆绑首笔输入位置列出 ``bundle`` / ``at`` /
``hashes`` / ``BUNDLE_REMOVED``）、``keptFee``、``removedFee``、
``evidence``（baselineOrder 上的全部夹子证据，按起始位置升序、同位
按 victim hash 升序）、``blockers``（沿 riskplan 固定顺序）、
``feasible``、``isolationLimit``。

预算内无解不是输入错误：退出 0，``feasible`` 为 false，
``selectedOrder`` 与 ``removed`` 为空，``keptFee`` 与 ``removedFee``
为 0，``baselineOrder``、``evidence``、``blockers`` 与
``isolationLimit`` 仍取真实值，stderr 不写码。
"""

import json
import sys

from . import bounded
from . import bundleschedule
from . import core
from . import decision
from . import riskplan
from .cli import parse_args

# 输入错误码：isolationLimit 非法 / bundle 缺失或非非空字符串
BAD_ISOLATION_LIMIT = bounded.BAD_ISOLATION_LIMIT
BAD_BUNDLE_ID = "BAD_BUNDLE_ID"

# 移除原因码：被整组移除的捆绑统一记此原因
BUNDLE_REMOVED = "BUNDLE_REMOVED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先执行 riskplan（统一决策 + isolationLimit）的全部校验，再逐笔
    校验 bundle：必须为非空字符串，否则抛 BAD_BUNDLE_ID。规范化交易
    上追加 bundle 字段。
    """
    req = bounded.parse_request(raw)
    # 统一决策与 isolationLimit 校验已通过，raw 必为合法 JSON 对象
    data = json.loads(raw)

    bundles = []
    for tx in data["transactions"]:
        bid = tx.get("bundle")
        if not isinstance(bid, str) or not bid:
            raise decision.DecisionError(BAD_BUNDLE_ID)
        bundles.append(bid)
    for parsed, bid in zip(req["transactions"], bundles):
        parsed["bundle"] = bid
    return req


def error_result(ident):
    """输入错误结果：列表为空，keptFee / removedFee / isolationLimit 为 0。"""
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


def isolate(req):
    """枚举预算内全部捆绑保留方案求全局最优，返回结果字典。"""
    txs = req["transactions"]
    limit = req["isolationLimit"]
    baseline = decision.order_transactions(txs)
    baseline_hashes = [tx["hash"] for tx in baseline]
    input_pos = {tx["hash"]: at for at, tx in enumerate(txs)}

    # 证据固定取自完整基线顺序；起始位置唯一，victim 仅作稳定次序兜底
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )
    blockers = riskplan._blockers(req)
    keepable = riskplan._keepable(req)
    rollback_limit = req["rollbackLimit"]

    # 捆绑构造沿用 bundleschedule：成员保持输入相对位置，捆绑按首笔
    # 输入位置升序
    groups, group_of = bundleschedule._build_bundles(txs)
    g = len(groups)
    sizes = [len(grp["members"]) for grp in groups]
    total_fee = sum(tx["fee"] for tx in txs)

    # 枚举捆绑保留方案（mask 第 gi 位保留 groups[gi]）。只有整组移除
    # 才计预算：被移除成员笔数之和不得超过 isolationLimit。保留集合
    # 取基线中属于保留捆绑的交易（按基线相对顺序），沿用 riskplan
    # 合法性。比较键：fee 总和最大、保留笔数最多、被移除捆绑首笔输入
    # 位置序列字典序最小。预算内可能无任何合法方案，此时 best_key
    # 保持 None。
    best_key = None
    best_kept = ()
    best_mask = 0
    for mask in range(1 << g):
        removed_count = sum(
            sizes[gi] for gi in range(g) if not ((mask >> gi) & 1)
        )
        if removed_count > limit:
            continue
        kept_groups = {
            gi for gi in range(g) if (mask >> gi) & 1
        }
        selected = [
            tx for tx in baseline
            if group_of[input_pos[tx["hash"]]] in kept_groups
        ]
        if not riskplan._legal(selected, keepable, rollback_limit):
            continue
        kept_hashes = [tx["hash"] for tx in selected]
        kept_fee = sum(tx["fee"] for tx in selected)
        removed_groups = [
            groups[gi]["first"]
            for gi in range(g)
            if not ((mask >> gi) & 1)
        ]
        key = (-kept_fee, -len(kept_hashes), removed_groups)
        if best_key is None or key < best_key:
            best_key = key
            best_kept = tuple(kept_hashes)
            best_mask = mask

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
    # 被移除捆绑按首笔输入位置排列（groups 已按 first 升序）
    removed = []
    for gi in range(g):
        if (best_mask >> gi) & 1:
            continue
        grp = groups[gi]
        removed.append(
            {
                "bundle": grp["bundle"],
                "at": grp["first"],
                "hashes": [txs[at]["hash"] for at in grp["members"]],
                "reason": BUNDLE_REMOVED,
            }
        )
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

    输入校验沿用 riskplan 并追加 bundle 校验；error_code 非空时退出
    码 2。预算内无解是正常结论，error_code 为 None。
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
    """执行一次捆绑原子性风险隔离计划，返回退出码。缓冲区参数用于测试注入。

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
