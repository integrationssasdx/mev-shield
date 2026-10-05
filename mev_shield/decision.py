"""统一决策入口：python -m mev_shield.decision [--input IN] [--output OUT]

把单个交易包的打包排序、夹子检测与回滚保护收敛为一份可直接判断的
JSON 结论。既有 ``python -m mev_shield`` 入口与公开行为保持不变。

输入（JSON 对象）：

- ``id``：交易包标识，字符串；
- ``transactions``：候选交易（数组顺序即候选顺序），每笔含
  ``hash`` / ``from`` / ``nonce`` / ``fee`` / ``token`` / ``side`` /
  ``sim`` / ``price``（正数执行价）；
- ``market``：市场上下文，对象，含 ``prices``（token -> 正数参考价）；
- ``basePrice``：基准价格，正数；
- ``maxSlippage``：滑点上限，[0, 1] 含端点；
- ``rollbackLimit``：回滚范围，[0, 1] 含端点；
- ``policy``：可选策略，reject（缺省）/ quarantine；
- ``slippageMode``：可选滑点口径，base（缺省）/ market。base 用
  basePrice 计算每笔交易价格的绝对偏离率；market 用 market.prices 中
  同 token 的正数参考价计算，缺参考价的交易只记 PRICE_CONTEXT_MISSING、
  不参与滑点取值。

输出字段固定：``id``、``conclusion``、``finalOrder``、``sandwich``、
``involved``、``reasons``、``rollbackAllowed``、``basis``。数组按最终
顺序排列，原因码与布尔值稳定；相同输入逐字一致。
"""

import json
import math
import sys

from . import core
from .cli import parse_args

ALLOW = "ALLOW"
BLOCK = "BLOCK"

# 输入错误码（退出码 2；结论必为 BLOCK，不夹带允许结论）
EMPTY_BUNDLE = "EMPTY_BUNDLE"
UNIDENTIFIED_TRANSACTION = "UNIDENTIFIED_TRANSACTION"
DUPLICATE_TRANSACTION = "DUPLICATE_TRANSACTION"
ORDERING_CONFLICT = "ORDERING_CONFLICT"
MISSING_MARKET_CONTEXT = "MISSING_MARKET_CONTEXT"
INVALID_PRICE_BASE = "INVALID_PRICE_BASE"
INVALID_RISK_LIMIT = "INVALID_RISK_LIMIT"
INVALID_ROLLBACK_LIMIT = "INVALID_ROLLBACK_LIMIT"
BAD_SLIPPAGE_MODE = "BAD_SLIPPAGE_MODE"

# 滑点口径：base 用 basePrice；market 用 market.prices 中同 token 参考价
SLIPPAGE_MODE_BASE = "base"
SLIPPAGE_MODE_MARKET = "market"

# 决策原因码（结论 BLOCK 时的阻塞原因）
SANDWICH_DETECTED = "SANDWICH_DETECTED"
SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
PRICE_CONTEXT_MISSING = "PRICE_CONTEXT_MISSING"
NONCE_ORDER_VIOLATION = "NONCE_ORDER_VIOLATION"
ROLLBACK_LIMIT_EXCEEDED = "ROLLBACK_LIMIT_EXCEEDED"
DETECTION_UNAVAILABLE = "DETECTION_UNAVAILABLE"
ORDERING_UNAVAILABLE = "ORDERING_UNAVAILABLE"
ROLLBACK_EVALUATION_FAILED = "ROLLBACK_EVALUATION_FAILED"

# 原因码固定优先级：同一证据不得产生冲突结论，reasons 按此顺序输出
_REASON_PRIORITY = (
    SANDWICH_DETECTED,
    SLIPPAGE_EXCEEDED,
    PRICE_CONTEXT_MISSING,
    NONCE_ORDER_VIOLATION,
    ROLLBACK_LIMIT_EXCEEDED,
    DETECTION_UNAVAILABLE,
    ORDERING_UNAVAILABLE,
    ROLLBACK_EVALUATION_FAILED,
)

_EXIT_OK = 0
_EXIT_ERROR = 2


class DecisionError(Exception):
    """携带错误码的输入校验失败。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _is_nonneg_int(value):
    # JSON 非负整数；bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value):
    # 有限 JSON 数；排除布尔值与 NaN / Infinity
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_positive(value):
    return _is_number(value) and value > 0


def _is_unit_interval(value):
    return _is_number(value) and 0 <= value <= 1


def parse_request(raw):
    """解析并校验输入，返回规范化请求；失败抛 DecisionError。

    校验优先级固定：JSON / schema -> EMPTY_BUNDLE ->
    UNIDENTIFIED_TRANSACTION -> DUPLICATE_TRANSACTION -> 交易字段
    schema -> ORDERING_CONFLICT -> MISSING_MARKET_CONTEXT ->
    INVALID_PRICE_BASE -> INVALID_RISK_LIMIT -> INVALID_ROLLBACK_LIMIT
    -> BAD_POLICY -> BAD_SLIPPAGE_MODE。
    """
    try:
        data = json.loads(raw)
    except ValueError:
        raise DecisionError(core.BAD_JSON)
    if not isinstance(data, dict):
        raise DecisionError(core.BAD_SCHEMA)
    if not isinstance(data.get("id"), str):
        raise DecisionError(core.BAD_SCHEMA)
    txs = data.get("transactions")
    if not isinstance(txs, list):
        raise DecisionError(core.BAD_SCHEMA)
    if not txs:
        raise DecisionError(EMPTY_BUNDLE)

    # 可识别哈希：非空字符串；在重复判定与字段 schema 之前
    for tx in txs:
        if not isinstance(tx, dict):
            raise DecisionError(core.BAD_SCHEMA)
        h = tx.get("hash")
        if not isinstance(h, str) or not h:
            raise DecisionError(UNIDENTIFIED_TRANSACTION)

    seen = set()
    for tx in txs:
        if tx["hash"] in seen:
            raise DecisionError(DUPLICATE_TRANSACTION)
        seen.add(tx["hash"])

    # 交易其余字段沿用既有语义：from/token 字符串，nonce/fee 非负整数
    # （排除布尔值），side / sim 枚举固定
    for tx in txs:
        if not isinstance(tx.get("from"), str) or not isinstance(tx.get("token"), str):
            raise DecisionError(core.BAD_SCHEMA)
        if not _is_nonneg_int(tx.get("nonce")) or not _is_nonneg_int(tx.get("fee")):
            raise DecisionError(core.BAD_SCHEMA)
        if tx.get("side") not in ("buy", "sell"):
            raise DecisionError(core.BAD_SCHEMA)
        if tx.get("sim") not in ("success", "revert"):
            raise DecisionError(core.BAD_SCHEMA)

    # 相邻 nonce 依赖：同一 from 的 nonce 不重复且连续，否则任何排序都
    # 无法满足依赖
    nonces_by_sender = {}
    for tx in txs:
        nonces_by_sender.setdefault(tx["from"], []).append(tx["nonce"])
    for sender_nonces in nonces_by_sender.values():
        if len(set(sender_nonces)) != len(sender_nonces):
            raise DecisionError(ORDERING_CONFLICT)
        if max(sender_nonces) - min(sender_nonces) + 1 != len(sender_nonces):
            raise DecisionError(ORDERING_CONFLICT)

    # 市场上下文：market.prices 为 token -> 正数参考价；每笔交易必须
    # 携带正数 price 字段
    market = data.get("market")
    if not isinstance(market, dict):
        raise DecisionError(MISSING_MARKET_CONTEXT)
    prices = market.get("prices")
    if not isinstance(prices, dict):
        raise DecisionError(MISSING_MARKET_CONTEXT)
    for value in prices.values():
        if not _is_positive(value):
            raise DecisionError(MISSING_MARKET_CONTEXT)
    for tx in txs:
        if not _is_positive(tx.get("price")):
            raise DecisionError(MISSING_MARKET_CONTEXT)

    if not _is_positive(data.get("basePrice")):
        raise DecisionError(INVALID_PRICE_BASE)
    if not _is_unit_interval(data.get("maxSlippage")):
        raise DecisionError(INVALID_RISK_LIMIT)
    if not _is_unit_interval(data.get("rollbackLimit")):
        raise DecisionError(INVALID_ROLLBACK_LIMIT)

    policy = data.get("policy", core.POLICY_REJECT)
    if policy not in (core.POLICY_REJECT, core.POLICY_QUARANTINE):
        raise DecisionError(core.BAD_POLICY)

    # 滑点口径：缺省 base；只接受 base / market。排在原有输入与 policy
    # 校验之后，旧错误一律优先；类型（非字符串）或取值非法均失败
    slippage_mode = data.get("slippageMode", SLIPPAGE_MODE_BASE)
    if slippage_mode not in (SLIPPAGE_MODE_BASE, SLIPPAGE_MODE_MARKET):
        raise DecisionError(BAD_SLIPPAGE_MODE)

    parsed = [
        {
            "hash": tx["hash"],
            "from": tx["from"],
            "nonce": tx["nonce"],
            "fee": tx["fee"],
            "token": tx["token"],
            "side": tx["side"],
            "sim": tx["sim"],
            "price": tx["price"],
        }
        for tx in txs
    ]
    return {
        "id": data["id"],
        "transactions": parsed,
        "market": {"prices": dict(prices)},
        "basePrice": data["basePrice"],
        "maxSlippage": data["maxSlippage"],
        "rollbackLimit": data["rollbackLimit"],
        "policy": policy,
        "slippageMode": slippage_mode,
    }


def order_transactions(txs):
    """沿用既有 fee 排序语义：fee 降序、hash 升序。

    最终顺序恰好覆盖输入交易，不增加、丢失或重复。
    """
    return sorted(txs, key=lambda tx: (-tx["fee"], tx["hash"]))


def nonce_order_satisfied(ordered):
    """最终顺序中同一 from 的 nonce 必须严格升序（相邻 nonce 依赖）。"""
    last = {}
    for tx in ordered:
        sender = tx["from"]
        if sender in last and tx["nonce"] <= last[sender]:
            return False
        last[sender] = tx["nonce"]
    return True


def detect_sandwich_evidence(ordered):
    """在最终顺序上识别相邻三段夹子，返回可复核证据列表（按位置升序）。

    对相邻三元组 (p, p+1, p+2)：三笔 sim 均 success、token 相同；
    p 与 p+2 同 from（攻击者前置/后置腿），p+1 为不同 from（受害
    交易）；p 与 p+1 同向、p+2 反向；且价格沿受害方向移动并在后置
    腿回落：

    - 正向（买-买-卖）：price[p+1] > price[p] 且 price[p+2] < price[p+1]；
    - 反向（卖-卖-买）：price[p+1] < price[p] 且 price[p+2] > price[p+1]。

    每条证据记录三者位置、hash、token、价格与 move（受害者相对前置
    腿的不利价格变化率，恒为正）。
    """
    evidence = []
    for p in range(len(ordered) - 2):
        t0, t1, t2 = ordered[p], ordered[p + 1], ordered[p + 2]
        if t0["sim"] != "success" or t1["sim"] != "success" or t2["sim"] != "success":
            continue
        # 首尾同一发送者（攻击者），受害交易发送者不同
        if t0["from"] != t2["from"] or t0["from"] == t1["from"]:
            continue
        if not (t0["token"] == t1["token"] == t2["token"]):
            continue
        # 前置腿与受害者同向，后置腿反向
        if t0["side"] != t1["side"] or t0["side"] == t2["side"]:
            continue
        p0, p1, p2 = t0["price"], t1["price"], t2["price"]
        if t0["side"] == "buy":
            # 正向：受害者买在被推高的价格，后置卖出时价格自高点回落
            if not (p1 > p0 and p2 < p1):
                continue
            move = (p1 - p0) / p0
        else:
            # 反向：受害者卖在被压低的价格，后置买回时价格自低点回升
            if not (p1 < p0 and p2 > p1):
                continue
            move = (p0 - p1) / p0
        evidence.append(
            {
                "at": [p, p + 1, p + 2],
                "front": t0["hash"],
                "victim": t1["hash"],
                "back": t2["hash"],
                "token": t0["token"],
                "prices": [p0, p1, p2],
                "move": move,
            }
        )
    return evidence


def evaluate_rollback(txs, limit):
    """预计回滚：sim 为 revert 的交易占比；超过 limit 视为超出范围。"""
    total = len(txs)
    reverts = sum(1 for tx in txs if tx["sim"] == "revert")
    ratio = reverts / total
    return {
        "expectedRollback": reverts,
        "rollbackRatio": ratio,
        "within": ratio <= limit,
    }


def _empty_sandwich():
    return {
        "detected": False,
        "front": None,
        "victim": None,
        "back": None,
        "evidence": [],
    }


def _sandwich_result(evidence):
    if evidence:
        first = evidence[0]
        front, victim, back = first["front"], first["victim"], first["back"]
    else:
        front = victim = back = None
    return {
        "detected": bool(evidence),
        "front": front,
        "victim": victim,
        "back": back,
        "evidence": evidence,
    }


def _involved(final_order, evidence):
    """涉及交易：命中三腿的 hash 去重后按最终顺序排列。"""
    in_evidence = set()
    for entry in evidence:
        in_evidence.update((entry["front"], entry["victim"], entry["back"]))
    return [h for h in final_order if h in in_evidence]


def _basis(req, tx_count, expected, ratio, max_dev):
    """决策依据：固定键，记录策略、限额与实测值。"""
    return {
        "policy": req["policy"],
        "basePrice": req["basePrice"],
        "maxSlippage": req["maxSlippage"],
        "rollbackLimit": req["rollbackLimit"],
        "txCount": tx_count,
        "expectedRollback": expected,
        "rollbackRatio": ratio,
        "maxSlippageObserved": max_dev,
    }


def decide(req, orderer=None, detector=None, rollback=None):
    """对规范化请求作出统一结论，返回结果字典。

    orderer / detector / rollback 可注入以替换默认组件；任一组件异常
    均失败关闭：结论 BLOCK 且 rollbackAllowed 为 false。
    """
    orderer = order_transactions if orderer is None else orderer
    detector = detect_sandwich_evidence if detector is None else detector
    rollback = evaluate_rollback if rollback is None else rollback
    txs = req["transactions"]

    # 排序器不可用：无法生成最终顺序，直接失败关闭
    try:
        ordered = orderer(txs)
    except Exception:
        return {
            "id": req["id"],
            "conclusion": BLOCK,
            "finalOrder": [],
            "sandwich": _empty_sandwich(),
            "involved": [],
            "reasons": [ORDERING_UNAVAILABLE],
            "rollbackAllowed": False,
            "basis": _basis(req, len(txs), 0, 0, 0.0),
        }
    final_order = [tx["hash"] for tx in ordered]

    found = set()

    # 最终顺序必须满足同发送者相邻 nonce 依赖
    if not nonce_order_satisfied(ordered):
        found.add(NONCE_ORDER_VIOLATION)

    # 夹子检测：检测器不可用时失败关闭
    try:
        evidence = detector(ordered)
    except Exception:
        evidence = None
    if evidence is None:
        found.add(DETECTION_UNAVAILABLE)
        evidence = []
    if evidence:
        found.add(SANDWICH_DETECTED)
    sandwich = _sandwich_result(evidence)
    involved = _involved(final_order, evidence)

    # 风险检查：价格上下文完整性与滑点上限。
    # base 口径：每笔相对 basePrice 取绝对偏离率，全部参与取值。
    # market 口径：每笔相对 market.prices 中同 token 的正数参考价取
    # abs(price-reference)/reference；缺参考价只记上下文缺失，该笔不
    # 参与滑点取值，无任何可计算结果时 maxSlippageObserved 为 0.0。
    prices = req["market"]["prices"]
    base = req["basePrice"]
    limit = req["maxSlippage"]
    market_mode = req["slippageMode"] == SLIPPAGE_MODE_MARKET
    max_dev = 0.0
    context_missing = False
    slippage_exceeded = False
    for tx in ordered:
        if market_mode:
            reference = prices.get(tx["token"])
            if reference is None:
                # market 口径缺参考价：只记上下文缺失，不参与滑点取值
                context_missing = True
                continue
        else:
            reference = base
            if tx["token"] not in prices:
                context_missing = True
        dev = abs(tx["price"] - reference) / reference
        if dev > max_dev:
            max_dev = dev
        if dev > limit:
            slippage_exceeded = True
    if context_missing:
        found.add(PRICE_CONTEXT_MISSING)
    if slippage_exceeded:
        found.add(SLIPPAGE_EXCEEDED)

    # 回滚评估：异常失败关闭；评估成功但预计回滚超过范围同样阻塞
    try:
        rb = rollback(txs, req["rollbackLimit"])
        expected = rb["expectedRollback"]
        ratio = rb["rollbackRatio"]
        within = rb["within"]
    except Exception:
        expected, ratio, within = 0, 0, False
        found.add(ROLLBACK_EVALUATION_FAILED)
    if not within and ROLLBACK_EVALUATION_FAILED not in found:
        found.add(ROLLBACK_LIMIT_EXCEEDED)

    reasons = [code for code in _REASON_PRIORITY if code in found]
    conclusion = ALLOW if not reasons else BLOCK
    return {
        "id": req["id"],
        "conclusion": conclusion,
        "finalOrder": final_order,
        "sandwich": sandwich,
        "involved": involved,
        "reasons": reasons,
        "rollbackAllowed": conclusion == ALLOW,
        "basis": _basis(req, len(txs), expected, ratio, max_dev),
    }


def _peek(raw):
    """校验失败前提取 (id, 交易数)；任何异常或类型不符返回 ("", 0)。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return "", 0
    if not isinstance(data, dict):
        return "", 0
    ident = data.get("id")
    txs = data.get("transactions")
    count = len(txs) if isinstance(txs, list) else 0
    return (ident if isinstance(ident, str) else ""), count


def error_result(ident, code, tx_count=0):
    """输入错误结果：结论 BLOCK，reasons 只含该错误码，不夹带允许结论。"""
    return {
        "id": ident,
        "conclusion": BLOCK,
        "finalOrder": [],
        "sandwich": _empty_sandwich(),
        "involved": [],
        "reasons": [code],
        "rollbackAllowed": False,
        "basis": {
            "policy": None,
            "basePrice": None,
            "maxSlippage": None,
            "rollbackLimit": None,
            "txCount": tx_count,
            "expectedRollback": 0,
            "rollbackRatio": 0,
            "maxSlippageObserved": 0,
        },
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。

    error_code 非空表示输入校验失败（退出码 2）；否则为正常决策
    （ALLOW 或 BLOCK，退出码 0）。
    """
    try:
        req = parse_request(raw)
    except DecisionError as exc:
        ident, count = _peek(raw)
        return error_result(ident, exc.code, count), exc.code
    return decide(req), None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return core.serialize(result)


def _stderr(code, stderr):
    stderr.write(code + "\n")
    stderr.flush()


def run(argv=None, stdin_buffer=None, stdout_buffer=None, stderr=None):
    """执行一次决策，返回退出码。缓冲区参数用于测试注入。

    参数约定与既有入口一致：仅接受 --input IN / --output OUT，缺省
    使用标准输入 / 标准输出。输入校验失败退出 2 并向 stderr 写码；
    业务 BLOCK 是正常决策结论，退出 0。
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
        payload = serialize(error_result("", core.BAD_ARGS)).encode("utf-8")
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
        result, prior_code = error_result("", core.INPUT_IO), core.INPUT_IO
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
