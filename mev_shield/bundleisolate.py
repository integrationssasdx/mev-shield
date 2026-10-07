"""不可拆分捆绑的风险隔离计划：python -m mev_shield.bundleisolate

在风险约束隔离计划（``python -m mev_shield.riskplan``）的输入、校验、
滑点 / 价格上下文 / nonce / 夹子与 ``rollbackLimit`` 口径之上，把带
非空 ``bundle`` 标识的交易按同值标识组成不可拆分捆绑：同值捆绑只能
整组保留或整组移除。既有的统一决策 / bounded / riskplan /
bundleschedule 入口的输入、输出与退出码均保持不变。

输入沿用 riskplan 的统一决策交易包加 ``isolationLimit``（非负 JSON
整数，排除布尔值），另要求每笔交易携带非空字符串 ``bundle``。校验
先执行统一决策校验，再校验 ``isolationLimit``，最后逐笔校验
``bundle``；错误唯一返回 ``BAD_ISOLATION_LIMIT`` 或 ``BAD_BUNDLE_ID``，
旧错误一律优先。

捆绑规则：

- 同 ``bundle`` 值的交易组成一个捆绑，成员保持输入相对位置，捆绑
  初始次序取首笔交易的输入位置；
- 每个捆绑二选一：全部成员按基线相对顺序保留，或整组移除；
- 整组移除的交易笔数之和（不是捆绑数）不超过 ``isolationLimit``。

合法性完全沿用 riskplan：保留集合（按基线相对顺序排列）同一
``from`` 的 nonce 严格递增、无任何相邻三段夹子证据、逐笔通过滑点
与价格上下文检查、revert 占比不超过 ``rollbackLimit``（空集合占比
视为 0）。

择优目标依次为：保留 fee 总和最高、保留笔数最多、被移除捆绑按
首笔输入位置形成的序列字典序最小。枚举满足原子性、风险与预算的
全部方案求全局最优，不做逐笔贪心；相同输入逐字一致。

输出字段固定：``id``、``baselineOrder``（全部交易的 fee 降序、
hash 升序基线顺序）、``selectedOrder``（最优保留集合顺序，按其
相对基线顺序保留）、``removed``（按捆绑首笔位置列出 bundle / at /
hashes / BUNDLE_REMOVED）、``keptFee``、``removedFee``、``evidence``
（完整基线的全部夹子证据，按起始位置升序、同位按 victim hash 升序）、
``blockers``（基线统一决策的原因码，按 riskplan 固定顺序去重）、
``feasible``、``isolationLimit``。

预算内无解不是输入错误：退出 0，``feasible`` 为 false，
``selectedOrder`` 与 ``removed`` 为空，``keptFee`` 与 ``removedFee``
为 0，``baselineOrder``、``evidence``、``blockers`` 与
``isolationLimit`` 真实值仍保留，stderr 不写码。

输入校验失败退出 2、stderr 仅写唯一原因码，stdout 保持同形：
列表为空、数值为 0、``feasible`` 为 false。
"""

import json
import sys

from . import bounded
from . import core
from . import decision
from . import riskplan
from .cli import parse_args

# 输入错误码（沿用 bounded / bundleschedule 常量口径）
BAD_ISOLATION_LIMIT = bounded.BAD_ISOLATION_LIMIT
BAD_BUNDLE_ID = "BAD_BUNDLE_ID"

# 移除原因码：捆绑隔离计划中被整组移除的捆绑统一记此原因
BUNDLE_REMOVED = "BUNDLE_REMOVED"

_EXIT_OK = 0
_EXIT_ERROR = 2


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    先完整执行 riskplan 的全部校验（统一决策校验 + isolationLimit
    非负 JSON 整数且排除布尔值），再逐笔校验 bundle：必须为非空
    字符串，否则抛 BAD_BUNDLE_ID。
    """
    req = riskplan.parse_request(raw)
    # 前述校验已通过，raw 必为合法 JSON 对象且 transactions 均为对象
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
    """输入错误结果：列表为空，数值为 0，feasible 为 false。"""
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


def _build_groups(txs):
    """按输入顺序构造捆绑，成员保持输入相对位置，按首笔位置升序。

    返回 groups：每项为 {"bundle", "first", "members"（输入位置列表）}。
    """
    index = {}
    groups = []
    for at, tx in enumerate(txs):
        bid = tx["bundle"]
        if bid not in index:
            index[bid] = len(groups)
            groups.append({"bundle": bid, "first": at, "members": []})
        groups[index[bid]]["members"].append(at)
    groups.sort(key=lambda grp: grp["first"])
    return groups


def isolate(req):
    """枚举满足原子性、风险与预算的全部捆绑方案求全局最优。"""
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

    groups = _build_groups(txs)
    g = len(groups)
    sizes = [len(grp["members"]) for grp in groups]
    member_set = [set(grp["members"]) for grp in groups]

    total_fee = sum(tx["fee"] for tx in txs)

    # 枚举每个捆绑保留 / 整组移除（共 2^g 种原子方案）。方案下的
    # 保留集合唯一确定：保留捆绑的全部成员，按基线相对顺序排列。
    # 比较键：fee 总和最大、笔数最多、被移除捆绑按首笔输入位置的
    # 序列字典序最小。空保留恒走 riskplan 合法性（空集合视为合法），
    # 故当预算允许移除全部笔数时 best_key 不会为 None。
    best_key = None
    best_kept = ()
    for mask in range(1 << g):
        removed_count = 0
        removed_seq = []
        kept_input = set()
        for gi in range(g):
            if (mask >> gi) & 1:
                # 位为 1：整组移除
                removed_count += sizes[gi]
                if removed_count > limit:
                    break
                removed_seq.append(groups[gi]["first"])
            else:
                kept_input.update(member_set[gi])
        else:
            selected = [
                tx for tx in baseline
                if input_pos[tx["hash"]] in kept_input
            ]
            if not riskplan._legal(selected, keepable, rollback_limit):
                continue
            kept_hashes = [tx["hash"] for tx in selected]
            kept_fee = sum(tx["fee"] for tx in selected)
            key = (-kept_fee, -len(kept_hashes), removed_seq)
            if best_key is None or key < best_key:
                best_key = key
                best_kept = tuple(kept_hashes)

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

    # 被移除捆绑按首笔输入位置升序输出（groups 已按 first 排序）
    removed = []
    for gi in range(g):
        members = groups[gi]["members"]
        if all(txs[at]["hash"] in kept_set for at in members):
            continue
        removed.append(
            {
                "bundle": groups[gi]["bundle"],
                "at": groups[gi]["first"],
                "hashes": [txs[at]["hash"] for at in members],
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
    """执行一次捆绑风险隔离计划，返回退出码。缓冲区参数用于测试注入。

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
