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
BAD_PACKING = "BAD_PACKING"
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

# 可选打包模式；缺省等价于 fee（fee 降序、hash 升序）
PACKING_FEE = "fee"
PACKING_NONCE = "nonce"

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
    """解析并校验输入，返回 (id, transactions, policy, block, packing)；失败抛 ShieldError。

    可选区块期限字段：批次级 block（当前区块高度）与交易级 deadline
    （最后可执行区块），均须为非负整数（排除布尔值）。原输入全部有效后
    才校验这两个字段；类型错误或有 deadline 无 block 时抛 BAD_DEADLINE。

    可选批次字段 packing 只接受 fee / nonce（缺省 fee）；它最后校验，
    任何 JSON、schema、重复 hash/nonce、side、sim、policy、期限错误都优先
    于 BAD_PACKING。
    """
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

    parsed = []
    seen_hash = set()
    # from -> 已出现的 nonce 集合
    seen_nonce = {}
    # 每笔交易的 deadline 原始值（缺省为 None），待原字段校验全部通过后统一检查
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

        parsed.append(
            {
                "hash": h,
                "from": sender,
                "nonce": nonce,
                "fee": fee,
                "token": token,
                "side": side,
                "sim": sim,
                "deadline": None,
            }
        )
        raw_deadlines.append(tx["deadline"] if "deadline" in tx else None)

    # 区块期限字段：仅在原输入全部有效后校验，错误一律 BAD_DEADLINE
    block = data.get("block")
    if "block" in data:
        if not _is_nonneg_int(block):
            raise ShieldError(BAD_DEADLINE)
    else:
        block = None
    for at, tx in enumerate(txs):
        if "deadline" not in tx:
            continue
        if block is None or not _is_nonneg_int(raw_deadlines[at]):
            raise ShieldError(BAD_DEADLINE)
        parsed[at]["deadline"] = raw_deadlines[at]

    # packing 最后校验：原有全部错误（含 policy 与期限）均优先于 BAD_PACKING
    packing = data.get("packing", PACKING_FEE)
    if packing not in (PACKING_FEE, PACKING_NONCE):
        raise ShieldError(BAD_PACKING)

    return data["id"], parsed, policy, block, packing


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


def _nonce_suffix_keep(txs, excluded):
    """在未排除（excluded 为输入位置集合）的成功交易上，按 from 计算以
    最大 nonce 为终点的最长连续 nonce 后缀，返回保留位置集合。

    每个 from 的 nonce 已由输入校验保证唯一；被排除位置（过期或隔离的
    攻击腿）与 revert 交易均不参与后缀，也不影响连续性。
    """
    by_sender = {}
    for at, tx in enumerate(txs):
        if at in excluded or tx["sim"] != "success":
            continue
        by_sender.setdefault(tx["from"], []).append((tx["nonce"], at))
    keep = set()
    for entries in by_sender.values():
        entries.sort()
        chain = [entries[-1]]
        for nonce, at in reversed(entries[:-1]):
            if nonce == chain[0][0] - 1:
                chain.insert(0, (nonce, at))
            else:
                break
        keep.update(at for _nonce, at in chain)
    return keep


def _sort_survivors(txs, packing):
    """fee 模式：fee 降序、hash 升序。

    nonce 模式：按 from 分道，道内 nonce 升序；道间按道内最高 fee 降序，
    最高 fee 相同则按道内最小 hash 升序。
    """
    if packing == PACKING_FEE:
        return sorted(txs, key=lambda tx: (-tx["fee"], tx["hash"]))
    lanes = {}
    for tx in txs:
        lanes.setdefault(tx["from"], []).append(tx)
    ordered_senders = sorted(
        lanes,
        key=lambda sender: (
            -max(tx["fee"] for tx in lanes[sender]),
            min(tx["hash"] for tx in lanes[sender]),
        ),
    )
    ordered = []
    for sender in ordered_senders:
        ordered.extend(sorted(lanes[sender], key=lambda tx: tx["nonce"]))
    return ordered


def build_no_sandwich(txs, expired=(), packing=PACKING_FEE):
    """无夹子时：排除过期、revert 与不连续 nonce，其余排序打包。

    expired 为过期交易的输入位置集合。返回 (order, rollback, kept, dropped)：
    rollback 按原位置记录；dropped 按原位置保存被排除的 hash。

    fee 模式（缺省）：同 from 中 nonce 小于该 from 最大 nonce 的交易记
    DEPENDENT_NONCE，存活者按 fee 降序、hash 升序。原因优先级：
    DEADLINE_EXPIRED -> REVERT -> DEPENDENT_NONCE。
    nonce 模式：对每个 from 未过期的成功交易，以最大 nonce 为终点向下
    保留最长连续 nonce 后缀，后缀外的成功交易记 DEPENDENT_NONCE；存活者
    按发送者道排列（见 _sort_survivors）。
    """
    expired = set(expired)
    if packing == PACKING_NONCE:
        keep_idx = _nonce_suffix_keep(txs, expired)
    else:
        # 每个 from 的最大 nonce
        max_nonce = {}
        for tx in txs:
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
        elif packing == PACKING_NONCE:
            if at in keep_idx:
                survivors.append(tx)
            else:
                rollback.append({"hash": tx["hash"], "at": at, "reason": DEPENDENT_NONCE})
        elif tx["nonce"] < max_nonce[tx["from"]]:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEPENDENT_NONCE})
        else:
            survivors.append(tx)

    survivors = _sort_survivors(survivors, packing)
    order = [tx["hash"] for tx in survivors]
    dropped = [entry["hash"] for entry in rollback]
    return order, rollback, order[:], dropped


def build_quarantine(txs, hits_idx, expired=(), packing=PACKING_FEE):
    """quarantine 策略：隔离全部命中的 buy/sell 攻击腿，保留 victim 及其余交易。

    expired 为过期交易的输入位置集合。返回 (order, rollback, kept, dropped)，
    约定同 build_no_sandwich。未进 order 的交易只记一次 rollback，原因优先级：
    SANDWICH_DETECTED（攻击腿） -> DEADLINE_EXPIRED -> REVERT -> DEPENDENT_NONCE
    （后两者在隔离后的剩余交易集合上判定；攻击腿由未过期命中构成，
    与过期集合互不相交）。packing 选择 fee / nonce 两种判定与排序规则，
    见 build_no_sandwich 与 _sort_survivors。
    攻击腿全部移除后，剩余交易按输入相对顺序不再含可识别夹子。
    """
    expired = set(expired)
    attack = set()
    for (i, _j, k) in hits_idx:
        attack.add(i)
        attack.add(k)

    if packing == PACKING_NONCE:
        keep_idx = _nonce_suffix_keep(txs, expired | attack)
    else:
        # 剩余交易每个 from 的最大 nonce
        max_nonce = {}
        for at, tx in enumerate(txs):
            if at in attack:
                continue
            sender = tx["from"]
            if sender not in max_nonce or tx["nonce"] > max_nonce[sender]:
                max_nonce[sender] = tx["nonce"]

    rollback = []
    survivors = []
    for at, tx in enumerate(txs):
        if at in attack:
            rollback.append({"hash": tx["hash"], "at": at, "reason": SANDWICH_DETECTED})
        elif at in expired:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEADLINE_EXPIRED})
        elif tx["sim"] == "revert":
            rollback.append({"hash": tx["hash"], "at": at, "reason": REVERT})
        elif packing == PACKING_NONCE:
            if at in keep_idx:
                survivors.append(tx)
            else:
                rollback.append({"hash": tx["hash"], "at": at, "reason": DEPENDENT_NONCE})
        elif tx["nonce"] < max_nonce[tx["from"]]:
            rollback.append({"hash": tx["hash"], "at": at, "reason": DEPENDENT_NONCE})
        else:
            survivors.append(tx)

    survivors = _sort_survivors(survivors, packing)
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
        ident, txs, policy, block, packing = parse_input(raw)
    except ShieldError as exc:
        return error_result(peek_id(raw), exc.code), exc.code

    # 过期交易：deadline 小于当前区块高度；不参与夹子识别、order 或 kept
    expired = set()
    if block is not None:
        for at, tx in enumerate(txs):
            deadline = tx["deadline"]
            if deadline is not None and deadline < block:
                expired.add(at)

    # 夹子检测只使用未过期交易的原相对位置；hits 的 at 记录输入位置
    active_idx = [at for at in range(len(txs)) if at not in expired]
    active_txs = [txs[at] for at in active_idx]
    hits_idx = [
        (active_idx[i], active_idx[j], active_idx[k])
        for (i, j, k) in detect_sandwiches(active_txs)
    ]
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
            order, rollback, kept, dropped = build_quarantine(
                txs, hits_idx, expired, packing)
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

    order, rollback, kept, dropped = build_no_sandwich(txs, expired, packing)
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
