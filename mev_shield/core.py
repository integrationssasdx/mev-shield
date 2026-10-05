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

# 可选打包模式；缺省 fee：fee 降序、hash 升序
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

    可选批次级 packing 只接受 fee / nonce，缺省等价于 fee。该字段最后校验：
    JSON、schema、重复 hash/nonce、side、sim、policy、期限错误均优先于
    BAD_PACKING。
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

    # 打包模式：最后校验，任何其他输入错误均优先；只接受 fee / nonce
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

    对 i<j<k：i、k 同 from，j 不同 from，三者 token 相同且 sim 均为
    success。i 恒为攻击者前置腿，k 为攻击者后置腿：
    - 正向夹子：i 为 buy、j 为 buy、k 为 sell（攻击者先买后卖）；
    - 反向夹子：i 为 sell、j 为 sell、k 为 buy（攻击者先卖后买）。
    """
    hits = []
    n = len(txs)
    for i in range(n - 2):
        ti = txs[i]
        if ti["sim"] != "success":
            continue
        # victim 与前置腿同向，后置腿为另一方向
        front_side = ti["side"]
        back_side = "sell" if front_side == "buy" else "buy"
        for j in range(i + 1, n - 1):
            tj = txs[j]
            if (
                tj["side"] != front_side
                or tj["sim"] != "success"
                or tj["from"] == ti["from"]
                or tj["token"] != ti["token"]
            ):
                continue
            for k in range(j + 1, n):
                tk = txs[k]
                if (
                    tk["side"] == back_side
                    and tk["sim"] == "success"
                    and tk["from"] == ti["from"]
                    and tk["token"] == ti["token"]
                ):
                    hits.append((i, j, k))
    return hits


def _nonce_suffix_dependent(txs, excluded):
    """nonce 模式：在每个 from 剩余成功交易上求最长连续 nonce 后缀。

    excluded 为已按更高优先级排除的位置（攻击腿、过期、revert）。
    以各 from 的最大 nonce 为终点，向下保留 nonce 连续存在的后缀；
    返回后缀外应记 DEPENDENT_NONCE 的位置集合。
    """
    nonces_by_sender = {}
    for at, tx in enumerate(txs):
        if at not in excluded:
            nonces_by_sender.setdefault(tx["from"], set()).add(tx["nonce"])

    keep_nonces = {}
    for sender, nonces in nonces_by_sender.items():
        top = max(nonces)
        kept = {top}
        n = top
        while n - 1 in nonces:
            n -= 1
            kept.add(n)
        keep_nonces[sender] = kept

    dependent = set()
    for at, tx in enumerate(txs):
        if at in excluded:
            continue
        if tx["nonce"] not in keep_nonces[tx["from"]]:
            dependent.add(at)
    return dependent


def _pack_order(survivors, packing):
    """幸存者排序：fee 模式按 fee 降序、hash 升序；nonce 模式按发送者道。

    nonce 模式：道内 nonce 升序；发送者道按道内最高 fee 降序，
    最高 fee 相同则按道内最小 hash 升序。
    """
    if packing == PACKING_NONCE:
        lanes = {}
        for tx in survivors:
            lanes.setdefault(tx["from"], []).append(tx)
        order = []
        for lane in sorted(
            lanes.values(),
            key=lambda lane: (
                -max(tx["fee"] for tx in lane),
                min(tx["hash"] for tx in lane),
            ),
        ):
            lane.sort(key=lambda tx: tx["nonce"])
            order.extend(tx["hash"] for tx in lane)
        return order
    survivors.sort(key=lambda tx: (-tx["fee"], tx["hash"]))
    return [tx["hash"] for tx in survivors]


def _build(txs, expired, packing, attack=frozenset()):
    """分类并打包，返回 (order, rollback, kept, dropped)。

    attack 为 quarantine 攻击腿位置集合（优先级最高，记 SANDWICH_DETECTED）。
    rollback 按输入位置记录，每笔最多一次，原因优先级：
    SANDWICH_DETECTED -> DEADLINE_EXPIRED -> REVERT -> DEPENDENT_NONCE。
    dropped 按输入位置；order/kept 同序，按 packing 排序。
    """
    expired = set(expired)
    attack = set(attack)

    if packing == PACKING_NONCE:
        # 先排除攻击腿与过期，再排除 revert，剩余成功交易上求连续 nonce 后缀
        pre_excluded = set(attack) | expired
        for at, tx in enumerate(txs):
            if at not in pre_excluded and tx["sim"] == "revert":
                pre_excluded.add(at)
        dependent = _nonce_suffix_dependent(txs, pre_excluded)
        max_nonce = None
    else:
        # fee 模式：每个 from（攻击腿除外）的最大 nonce，含过期与 revert 交易
        dependent = None
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
            reason = SANDWICH_DETECTED
        elif at in expired:
            reason = DEADLINE_EXPIRED
        elif tx["sim"] == "revert":
            reason = REVERT
        elif packing == PACKING_NONCE:
            reason = DEPENDENT_NONCE if at in dependent else None
        else:
            reason = (
                DEPENDENT_NONCE
                if tx["nonce"] < max_nonce[tx["from"]]
                else None
            )
        if reason is None:
            survivors.append(tx)
        else:
            rollback.append({"hash": tx["hash"], "at": at, "reason": reason})

    order = _pack_order(survivors, packing)
    dropped = [entry["hash"] for entry in rollback]
    return order, rollback, order[:], dropped


def build_no_sandwich(txs, expired=(), packing=PACKING_FEE):
    """无夹子时：排除过期、revert 与不连续 nonce 交易，其余排序打包。

    expired 为过期交易的输入位置集合。返回 (order, rollback, kept, dropped)：
    rollback/dropped 按输入位置；order/kept 按 packing 排序。
    原因优先级：DEADLINE_EXPIRED -> REVERT -> DEPENDENT_NONCE。
    """
    return _build(txs, expired, packing)


def build_quarantine(txs, hits_idx, expired=(), packing=PACKING_FEE):
    """quarantine 策略：隔离全部命中的 buy/sell 攻击腿，保留 victim 及其余交易。

    expired 为过期交易的输入位置集合。返回 (order, rollback, kept, dropped)，
    约定同 build_no_sandwich。未进 order 的交易只记一次 rollback，原因优先级：
    SANDWICH_DETECTED（攻击腿） -> DEADLINE_EXPIRED -> REVERT -> DEPENDENT_NONCE
    （后两者在隔离后的剩余交易集合上判定；攻击腿由未过期命中构成，
    与过期集合互不相交）。
    攻击腿全部移除后，剩余交易按输入相对顺序不再含可识别夹子。
    """
    attack = set()
    for (i, _j, k) in hits_idx:
        attack.add(i)
        attack.add(k)
    return _build(txs, expired, packing, attack)


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
    hits = []
    for (i, j, k) in hits_idx:
        # buy/sell 固定为攻击者的买入腿与卖出腿；正向（i 买 k 卖）时
        # at=[i,j,k]，反向（i 卖 k 买）时买腿在 k，at=[k,j,i]，
        # 故反向夹子里 buy 的位置晚于 sell。
        if txs[i]["side"] == "buy":
            buy_at, sell_at = i, k
        else:
            buy_at, sell_at = k, i
        hits.append(
            {
                "buy": txs[buy_at]["hash"],
                "victim": txs[j]["hash"],
                "sell": txs[sell_at]["hash"],
                "token": txs[i]["token"],
                "at": [buy_at, j, sell_at],
            }
        )

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
