"""核心逻辑：参数解析、校验、夹子检测、回滚与排序。

仅使用 Python 标准库，不连接任何链节点或钱包等外部服务。
"""

import json
import sys

# 退出码
EXIT_OK = 0
EXIT_ERROR = 2

# 错误码（顺序即需求规定的优先级）
BAD_JSON = "BAD_JSON"
BAD_SCHEMA = "BAD_SCHEMA"
DUP_HASH = "DUP_HASH"
DUP_NONCE = "DUP_NONCE"
BAD_SIDE = "BAD_SIDE"
BAD_SIM = "BAD_SIM"
BAD_ARGS = "BAD_ARGS"
INPUT_IO = "INPUT_IO"
OUTPUT_IO = "OUTPUT_IO"

# 固定输出键顺序
OUTPUT_KEYS = ("id", "status", "code", "order", "hits", "rollback", "kept", "dropped")

REVERT = "REVERT"
DEPENDENT_NONCE = "DEPENDENT_NONCE"


class MevError(Exception):
    """携带错误码的受控失败。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def is_nonneg_int(value):
    """JSON 非负整数：int 且非 bool 且 >= 0。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_str(value):
    return isinstance(value, str)


def parse_args(argv):
    """解析命令行参数，仅接受 --input、--output（均可省略，可与位置混用）。"""
    input_path = None
    output_path = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--input", "--output"):
            if index + 1 >= len(argv):
                raise MevError(BAD_ARGS)
            value = argv[index + 1]
            if arg == "--input":
                input_path = value
            else:
                output_path = value
            index += 2
        elif arg.startswith("--input="):
            input_path = arg[len("--input="):]
            index += 1
        elif arg.startswith("--output="):
            output_path = arg[len("--output="):]
            index += 1
        else:
            raise MevError(BAD_ARGS)
    return input_path, output_path


def read_input(input_path):
    """以字节读取输入；解码失败归入 BAD_JSON（JSON 必须是合法 UTF-8）。"""
    if input_path is None:
        # 缺省从标准输入读取
        data = sys.stdin.buffer.read()
    else:
        try:
            with open(input_path, "rb") as handle:
                data = handle.read()
        except OSError:
            raise MevError(INPUT_IO)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise MevError(BAD_JSON)


def load_and_validate(raw):
    """解析并按需求顺序校验输入，返回 (id, transactions)。

    抛出的 MevError 会附带 ident 属性：id 一旦成功解析，
    后续任何校验失败都在错误记录中保留该 id。
    """
    ident = None

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise MevError(BAD_JSON)

    # 结构：顶层对象，含 id、transactions（列表）
    if not isinstance(payload, dict):
        raise MevError(BAD_SCHEMA)
    if "id" not in payload or "transactions" not in payload:
        raise MevError(BAD_SCHEMA)
    ident = payload["id"]
    transactions = payload["transactions"]

    def validate_body():
        # id 必须为字符串、transactions 必须为列表（BAD_SCHEMA）
        if not is_str(ident) or not isinstance(transactions, list):
            raise MevError(BAD_SCHEMA)
        # 分遍校验，保证错误码优先级：
        # BAD_SCHEMA -> DUP_HASH -> DUP_NONCE -> BAD_SIDE -> BAD_SIM
        cleaned = []
        for tx in transactions:
            # 结构与类型（BAD_SCHEMA）
            if not isinstance(tx, dict):
                raise MevError(BAD_SCHEMA)
            for field in ("hash", "from", "nonce", "fee", "token", "side", "sim"):
                if field not in tx:
                    raise MevError(BAD_SCHEMA)
            if not (is_str(tx["hash"]) and is_str(tx["from"])
                    and is_str(tx["token"])):
                raise MevError(BAD_SCHEMA)
            if not (is_nonneg_int(tx["nonce"]) and is_nonneg_int(tx["fee"])):
                raise MevError(BAD_SCHEMA)
            cleaned.append({
                "hash": tx["hash"],
                "from": tx["from"],
                "nonce": tx["nonce"],
                "fee": tx["fee"],
                "token": tx["token"],
                "side": tx["side"],
                "sim": tx["sim"],
            })

        # hash 全局唯一（DUP_HASH）
        seen_hash = set()
        for tx in cleaned:
            if tx["hash"] in seen_hash:
                raise MevError(DUP_HASH)
            seen_hash.add(tx["hash"])

        # 同一 from 的 nonce 不可重复（DUP_NONCE）
        seen_nonce = set()
        for tx in cleaned:
            key = (tx["from"], tx["nonce"])
            if key in seen_nonce:
                raise MevError(DUP_NONCE)
            seen_nonce.add(key)

        # side 取值（BAD_SIDE）
        for tx in cleaned:
            if tx["side"] not in ("buy", "sell"):
                raise MevError(BAD_SIDE)

        # sim 取值（BAD_SIM）
        for tx in cleaned:
            if tx["sim"] not in ("success", "revert"):
                raise MevError(BAD_SIM)

        return cleaned

    try:
        cleaned = validate_body()
    except MevError as exc:
        # 仅当 id 是合法字符串时才在错误记录中保留它
        if is_str(ident):
            exc.ident = ident
        raise

    return ident, cleaned


def detect_sandwiches(transactions):
    """检测所有 (i, j, k) 夹子命中，按 i 升序排列。

    i、k 同 from：i 为 buy、k 为 sell；j 不同 from 且为 buy；
    token 相同且三者 sim 均为 success。
    """
    hits = []
    count = len(transactions)
    for i in range(count):
        ti = transactions[i]
        if ti["side"] != "buy" or ti["sim"] != "success":
            continue
        for j in range(i + 1, count):
            tj = transactions[j]
            if tj["side"] != "buy" or tj["sim"] != "success":
                continue
            if tj["from"] == ti["from"]:
                continue
            for k in range(j + 1, count):
                tk = transactions[k]
                if tk["side"] != "sell" or tk["sim"] != "success":
                    continue
                if tk["from"] != ti["from"]:
                    continue
                if tk["token"] != ti["token"] or tj["token"] != ti["token"]:
                    continue
                hits.append({
                    "buy": ti["hash"],
                    "victim": tj["hash"],
                    "sell": tk["hash"],
                    "token": ti["token"],
                    "at": [i, j, k],
                })
    # i 升序；同一 i 内按 (j, k) 保持确定顺序
    hits.sort(key=lambda hit: (tuple(hit["at"])))
    return hits


def build_rollback_and_order(transactions):
    """无夹子时：排除 revert 与同 from 中 nonce 更大的交易，其余排序。"""
    # 每个 from 的最小 nonce
    min_nonce = {}
    for tx in transactions:
        sender = tx["from"]
        if sender not in min_nonce or tx["nonce"] < min_nonce[sender]:
            min_nonce[sender] = tx["nonce"]

    rollback = []
    kept_txs = []
    for index, tx in enumerate(transactions):
        if tx["sim"] == "revert":
            rollback.append({"hash": tx["hash"], "at": index, "reason": REVERT})
        elif tx["nonce"] > min_nonce[tx["from"]]:
            rollback.append({
                "hash": tx["hash"],
                "at": index,
                "reason": DEPENDENT_NONCE,
            })
        else:
            kept_txs.append(tx)

    # fee 降序、hash 升序（字符串原样比较）
    kept_txs.sort(key=lambda tx: (-tx["fee"], tx["hash"]))
    order = [tx["hash"] for tx in kept_txs]
    kept = list(order)
    # dropped 按原位置保存 hash：rollback 已按原位置生成
    dropped = [record["hash"] for record in rollback]
    return order, rollback, kept, dropped


def empty_record(ident):
    """构造除 id 外其余字段为空的记录（成功/失败通用基底）。"""
    return {
        "id": ident,
        "status": "",
        "code": "",
        "order": [],
        "hits": [],
        "rollback": [],
        "kept": [],
        "dropped": [],
    }


def build_success(ident, transactions):
    record = empty_record(ident)
    hits = detect_sandwiches(transactions)
    if hits:
        record["status"] = "rejected"
        record["code"] = "SANDWICH_DETECTED"
        record["hits"] = hits
        # order 保持为空
        return record

    order, rollback, kept, dropped = build_rollback_and_order(transactions)
    record["status"] = "accepted"
    record["code"] = "OK"
    record["order"] = order
    record["rollback"] = rollback
    record["kept"] = kept
    record["dropped"] = dropped
    return record


def build_error(ident):
    record = empty_record(ident if ident is not None else "")
    record["status"] = "error"
    record["code"] = ""
    return record


def serialize(record):
    """按固定键顺序序列化，保证逐字节一致。"""
    ordered = {key: record[key] for key in OUTPUT_KEYS}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def write_output(output_path, text):
    data = text.encode("utf-8")
    if output_path is None:
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            raise MevError(OUTPUT_IO)
    else:
        try:
            with open(output_path, "wb") as handle:
                handle.write(data)
        except OSError:
            raise MevError(OUTPUT_IO)


def run(argv):
    """执行一次完整处理，返回退出码。

    普通失败：输出 status=error 的记录，退出 2。
    输出不可写：仅向 stderr 写错误码，退出 2。
    参数错误：无法可靠判定输出目标，仅向 stderr 写 BAD_ARGS。
    """
    try:
        input_path, output_path = parse_args(argv)
    except MevError as exc:
        sys.stderr.write(exc.code + "\n")
        return EXIT_ERROR

    ident = None
    try:
        raw = read_input(input_path)
        ident, transactions = load_and_validate(raw)
        record = build_success(ident, transactions)
        text = serialize(record)
    except MevError as exc:
        # 若 id 已在异常发生前解析出来，错误记录中保留它
        error_id = getattr(exc, "ident", None)
        if error_id is not None:
            ident = error_id
        # 输入/校验类失败：输出 error 记录；若输出本身不可写则仅写 stderr
        try:
            write_output(output_path, serialize(build_error(ident)))
        except MevError:
            sys.stderr.write(OUTPUT_IO + "\n")
            return EXIT_ERROR
        return EXIT_ERROR

    try:
        write_output(output_path, text)
    except MevError:
        sys.stderr.write(OUTPUT_IO + "\n")
        return EXIT_ERROR
    return EXIT_OK


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    sys.exit(run(argv))
