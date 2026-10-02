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
BAD_ARGS = "BAD_ARGS"
INPUT_IO = "INPUT_IO"
OUTPUT_IO = "OUTPUT_IO"

# 业务状态码 / 回滚原因
SANDWICH_DETECTED = "SANDWICH_DETECTED"
REVERT = "REVERT"
DEPENDENT_NONCE = "DEPENDENT_NONCE"

# 固定输出键顺序
RESULT_KEYS = ("id", "status", "code", "order", "hits", "rollback", "kept", "dropped")
HIT_KEYS = ("buy", "victim", "sell", "token", "at")
ROLLBACK_KEYS = ("hash", "at", "reason")


class ShieldError(Exception):
    """携带错误码的校验失败。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _is_nonneg_int(value):
    # JSON 非负整数；bool 是 int 的子类，必须显式排除
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_input(raw):
    """解析并校验输入，返回 (id, transactions)；失败抛 ShieldError。"""
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

    parsed = []
    seen_hash = set()
    # from -> 已出现的 nonce 集合
    seen_nonce = {}

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

    return data["id"], parsed


def peek_id(raw):
    """校验失败前提取 id；任何异常或类型不符都返回空字符串。"""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(data, dict) and isinstance(data.get("id"), str):
        return data["id"]
    return ""


def detect_sandwiches(txs):
    """返回命中三元组 (i, j, k) 列表，按 (i, j, k) 升序。

    对 i<j<k：i、k 同 from，i 为 buy、k 为 sell；j 不同 from 且为 buy；
    token 相同且三者 sim 均为 success。
    """
    hits = []
    n = len(txs)
    for i in range(n - 2):
        ti = txs[i]
        if ti["side"] != "buy" or ti["sim"] != "success":
            continue
        for j in range(i + 1, n - 1):
            tj = txs[j]
            if (
                tj["side"] != "buy"
                or tj["sim"] != "success"
                or tj["from"] == ti["from"]
                or tj["token"] != ti["token"]
            ):
                continue
            for k in range(j + 1, n):
                tk = txs[k]
                if (
                    tk["side"] == "sell"
                    and tk["sim"] == "success"
                    and tk["from"] == ti["from"]
                    and tk["token"] == ti["token"]
                ):
                    hits.append((i, j, k))
    return hits


def build_no_sandwich(txs):
    """无夹子时：排除 revert 与同 from 中 nonce 更大的交易，其余排序打包。

    返回 (order, rollback, kept, dropped)：
    rollback 按原位置记录；order/kept 按 fee 降序、hash 升序；
    dropped 按原位置保存被排除的 hash。
    """
    # 每个 from 的最大 nonce
    max_nonce = {}
    for tx in txs:
        sender = tx["from"]
        if sender not in max_nonce or tx["nonce"] > max_nonce[sender]:
            max_nonce[sender] = tx["nonce"]

    rollback = []
    survivors = []
    for at, tx in enumerate(txs):
        if tx["sim"] == "revert":
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
        ident, txs = parse_input(raw)
    except ShieldError as exc:
        return error_result(peek_id(raw), exc.code), exc.code

    hits_idx = detect_sandwiches(txs)
    if hits_idx:
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

    order, rollback, kept, dropped = build_no_sandwich(txs)
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
