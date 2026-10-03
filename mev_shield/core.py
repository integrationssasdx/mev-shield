"""核心逻辑：输入校验、夹子检测、打包排序与回滚记录。

只依赖标准库；字符串一律原样比较（区分大小写、不去空白）。
"""

import json

# 错误码（同时作为 stderr 中输出的 code）
BAD_JSON = "BAD_JSON"
BAD_SCHEMA = "BAD_SCHEMA"
DUP_HASH = "DUP_HASH"
DUP_NONCE = "DUP_NONCE"
BAD_SIDE = "BAD_SIDE"
BAD_SIM = "BAD_SIM"
BAD_POLICY = "BAD_POLICY"
BAD_DEADLINE = "BAD_DEADLINE"
BAD_ARGS = "BAD_ARGS"
INPUT_IO = "INPUT_IO"
OUTPUT_IO = "OUTPUT_IO"

# 业务状态码 / 回滚原因
SANDWICH_DETECTED = "SANDWICH_DETECTED"
SANDWICH_MITIGATED = "SANDWICH_MITIGATED"
REVERT = "REVERT"
DEPENDENT_NONCE = "DEPENDENT_NONCE"
DEADLINE_EXPIRED = "DEADLINE_EXPIRED"

# 可选保护策略；缺省等价于 reject
POLICY_REJECT = "reject"
POLICY_QUARANTINE = "quarantine"

# 固定输出键顺序
RESULT_KEYS = ("id", "status", "code", "order", "hits", "rollback", "kept", "dropped")
HIT_KEYS = ("buy", "victim", "sell", "token", "at")
ROLLBACK_KEYS = ("hash", "at", "reason")


class ShieldError(Exception):
    """携带错误码的校验失败。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# 区分“字段缺省”与“显式 null”（后者属于类型错误）
_MISSING = object()


def _is_nonneg_int(value):
    # JSON 非负整数；bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_input(raw):
    """解析并校验输入，返回 (id, transactions, policy, block)；失败抛 ShieldError。"""
    try:
        data = json.loads(raw)
    except ValueError:
        raise ShieldError(BAD_JSON)

    if not isinstance(data, dict):
        raise ShieldError(BAD_SCHEMA)
    if "id" not in data or not isinstance(data["id"], str):
        raise ShieldError(BAD_SCHEMA)
    txs = data.get("transactions")
    if not isinstance(txs, list):
        raise ShieldError(BAD_SCHEMA)

    # 可选策略：缺省等价 reject；只接受 reject / quarantine
    policy = data.get("policy", POLICY_REJECT)
    if policy not in (POLICY_REJECT, POLICY_QUARANTINE):
        raise ShieldError(BAD_POLICY)

    # 可选当前区块高度（类型在原字段全部校验通过后才检查，见下文）
    raw_block = data.get("block", _MISSING)

    parsed = []
    seen_hash = set()
    # from -> 已出现交易的 nonce 集合（过期与否不影响原有校验）
    seen_nonce = {}
    # 先收集 deadline 原值，block/deadline 的类型错误统一在原字段校验后判定
    raw_deadlines = []

    for tx in txs:
        if not isinstance(tx, dict):
            raise ShieldError(BAD_SCHEMA)
        for key in ("hash", "from", "nonce", "fee", "token", "side", "sim"):
            if key not in tx:
                raise ShieldError(BAD_SCHEMA)
        h = tx["hash"]
        sender = tx["from"]
        nonce = tx["nonce"]
        fee = tx["fee"]
        token = tx["token"]
        side = tx["side"]
        sim = tx["sim"]

        if not all(isinstance(v, str) for v in (h, sender, token)):
            raise ShieldError(BAD_SCHEMA)
        if not _is_nonneg_int(nonce) or not _is_nonneg_int(fee):
            raise ShieldError(BAD_SCHEMA)

        # 同一交易多问题并存时，按规范列举顺序判定：
        # DUP_HASH -> DUP_NONCE -> BAD_SIDE -> BAD_SIM
        if h in seen_hash:
            raise ShieldError(DUP_HASH)
        seen_hash.add(h)

        sender_nonces = seen_nonce.setdefault(sender, set())
        if nonce in sender_nonces:
            raise ShieldError(DUP_NONCE)
        sender_nonces.add(nonce)

        if side not in ("buy", "sell"):
            raise ShieldError(BAD_SIDE)
        if sim not in ("success", "revert"):
            raise ShieldError(BAD_SIM)

        raw_deadlines.append(tx.get("deadline", _MISSING))
        parsed.append(
            {
                "hash": h,
                "from": sender,
                "nonce": nonce,
                "fee": fee,
                "token": token,
                "side": side,
                "sim": sim,
            }
        )

    # 原字段全部有效后再处理期限：block/deadline 必须为非负 JSON 整数
    # （排除布尔值，显式 null 也算类型错误）；出现 deadline 却无 block 同样报错。
    # deadline 与 block 的大小关系（是否过期）不属于校验错误。
    if raw_block is not _MISSING and not _is_nonneg_int(raw_block):
        raise ShieldError(BAD_DEADLINE)
    block = raw_block if raw_block is not _MISSING else None

    for raw_deadline in raw_deadlines:
        if raw_deadline is not _MISSING and not _is_nonneg_int(raw_deadline):
            raise ShieldError(BAD_DEADLINE)
    if any(d is not _MISSING for d in raw_deadlines) and raw_block is _MISSING:
        raise ShieldError(BAD_DEADLINE)

    for tx, raw_deadline in zip(parsed, raw_deadlines):
        tx["deadline"] = raw_deadline if raw_deadline is not _MISSING else None

    return data["id"], parsed, policy, block


def peek_id(raw):
    """校验失败前提取 id；任何异常或类型不符都返回空字符串。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(data, dict) and isinstance(data.get("id"), str):
        return data["id"]
    return ""


def _is_expired(tx, block):
    """deadline < block 即过期；无 deadline/block 时永不过期。"""
    return block is not None and tx["deadline"] is not None and tx["deadline"] < block


def detect_sandwiches(txs, expired=()):
    """返回命中三元组 (i, j, k) 列表，按 (i, j, k) 升序。

    对 i<j<k：i、k 同 from，i 为 buy、k 为 sell；j 不同 from 且为 buy；
    token 相同且三者 sim 均为 success。expired 中任一位置参与即不成命中。
    """
    hits = []
    n = len(txs)
    for i in range(n - 2):
        ti = txs[i]
        if i in expired or ti["side"] != "buy" or ti["sim"] != "success":
            continue
        for j in range(i + 1, n - 1):
            tj = txs[j]
            if (
                j in expired
                or tj["side"] != "buy"
                or tj["sim"] != "success"
                or tj["from"] == ti["from"]
                or tj["token"] != ti["token"]
            ):
                continue
            for k in range(j + 1, n):
                tk = txs[k]
                if (
                    k not in expired
                    and tk["side"] == "sell"
                    and tk["sim"] == "success"
                    and tk["from"] == ti["from"]
                    and tk["token"] == ti["token"]
                ):
                    hits.append((i, j, k))
    return hits


def build_no_sandwich(txs, expired=()):
    """无夹子时：过期交易直接回滚，其余排除 revert 与同 from 中 nonce 更大者。

    返回 (order, rollback, kept, dropped)：
    rollback 按原位置记录；order/kept 按 fee 降序、hash 升序；
    dropped 按原位置保存被排除的 hash。
    原因优先级：DEADLINE_EXPIRED -> REVERT -> DEPENDENT_NONCE；
    过期交易即使 revert 或同 from 有更大 nonce 也只记 DEADLINE_EXPIRED，
    且不参与各 from 最大 nonce 统计。
    """
    # 每个 from 的最大 nonce（只看未过期交易）
    max_nonce = {}
    for at, tx in enumerate(txs):
        if at in expired:
            continue
        sender = tx["from"]
        if sender not in max_nonce or tx["nonce"] > max_nonce[sender]:
            max_nonce[sender] = tx["nonce"]

    rollback = []
    survivors = []
    for at, tx in enumerate(txs):
        if at in expired:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEADLINE_EXPIRED})
        elif tx["sim"] == "revert":
            rollback.append({"hash": tx["hash"], "at": at, "reason": REVERT})
        elif tx["nonce"] < max_nonce[tx["from"]]:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEPENDENT_NONCE})
        else:
            survivors.append(tx)

    survivors.sort(key=lambda tx: (-tx["fee"], tx["hash"]))
    order = [tx["hash"] for tx in survivors]
    dropped = [entry["hash"] for entry in rollback]
    return order, rollback, order[:], dropped


def build_quarantine(txs, hits_idx, expired=()):
    """quarantine 策略：隔离全部命中的 buy/sell 攻击腿，保留 victim 及其余交易。

    返回 (order, rollback, kept, dropped)，约定同 build_no_sandwich。
    未进 order 的交易只记一次 rollback，原因优先级：
    DEADLINE_EXPIRED -> SANDWICH_DETECTED（攻击腿） -> REVERT -> DEPENDENT_NONCE
    （后两者在隔离攻击腿后的未过期剩余交易集合上判定）。
    攻击腿全部移除后，剩余交易按输入相对顺序不再含可识别夹子。
    """
    attack = set()
    for (i, _j, k) in hits_idx:
        attack.add(i)
        attack.add(k)

    # 剩余未过期交易每个 from 的最大 nonce
    max_nonce = {}
    for at, tx in enumerate(txs):
        if at in expired or at in attack:
            continue
        sender = tx["from"]
        if sender not in max_nonce or tx["nonce"] > max_nonce[sender]:
            max_nonce[sender] = tx["nonce"]

    rollback = []
    survivors = []
    for at, tx in enumerate(txs):
        if at in expired:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEADLINE_EXPIRED})
        elif at in attack:
            rollback.append({"hash": tx["hash"], "at": at, "reason": SANDWICH_DETECTED})
        elif tx["sim"] == "revert":
            rollback.append({"hash": tx["hash"], "at": at, "reason": REVERT})
        elif tx["nonce"] < max_nonce[tx["from"]]:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEPENDENT_NONCE})
        else:
            survivors.append(tx)

    survivors.sort(key=lambda tx: (-tx["fee"], tx["hash"]))
    order = [tx["hash"] for tx in survivors]
    dropped = [entry["hash"] for entry in rollback]
    return order, rollback, order[:], dropped


def error_result(ident, code):
    return {
        "id": ident,
        "status": "error",
        "code": code,
        "order": [],
        "hits": [],
        "rollback": [],
        "kept": [],
        "dropped": [],
    }


def process(raw):
    """处理输入文本，返回 (result_dict, error_code_or_None)。"""
    try:
        ident, txs, policy, block = parse_input(raw)
    except ShieldError as exc:
        return error_result(peek_id(raw), exc.code), exc.code

    # 过期交易（deadline < block）不参与夹子识别、order 与 kept
    expired = {at for at, tx in enumerate(txs) if _is_expired(tx, block)}

    hits_idx = detect_sandwiches(txs, expired)
    hits = [
        {
            "buy": txs[i]["hash"],
            "victim": txs[j]["hash"],
            "sell": txs[k]["hash"],
            "token": txs[i]["token"],
            "at": [i, j, k],
        }
        for (i, j, k) in hits_idx
    ]

    if policy == POLICY_QUARANTINE:
        if hits_idx:
            order, rollback, kept, dropped = build_quarantine(txs, hits_idx, expired)
            result = {
                "id": ident,
                "status": "mitigated",
                "code": SANDWICH_MITIGATED,
                "order": order,
                "hits": hits,
                "rollback": rollback,
                "kept": kept,
                "dropped": dropped,
            }
            return result, None
        # 无命中：与缺省路径一致走 ok 结果
    elif hits_idx:
        result = {
            "id": ident,
            "status": "rejected",
            "code": SANDWICH_DETECTED,
            "order": [],
            "hits": hits,
            "rollback": [],
            "kept": [],
            "dropped": [],
        }
        return result, None

    order, rollback, kept, dropped = build_no_sandwich(txs, expired)
    result = {
        "id": ident,
        "status": "ok",
        "code": "",
        "order": order,
        "hits": [],
        "rollback": rollback,
        "kept": kept,
        "dropped": dropped,
    }
    return result, None


def serialize(result):
    """确定性序列化：插入顺序即固定键顺序，无多余空白，末尾换行。"""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
