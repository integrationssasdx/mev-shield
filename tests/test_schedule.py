"""mev_shield.schedule 多区块排程行为验证（标准库 unittest）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import decision
from mev_shield import schedule


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100,
       deadline=None):
    data = {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }
    if deadline is not None:
        data["deadline"] = deadline
    return data


def request(ident, txs, block=10, blocks=3, cap=2, market=None, base=100,
            slip=0.5, rb=1, policy=None, slippage_mode=None, extra=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "block": block,
        "scheduleBlocks": blocks,
        "blockCapacity": cap,
    }
    if policy is not None:
        data["policy"] = policy
    if slippage_mode is not None:
        data["slippageMode"] = slippage_mode
    if extra:
        data.update(extra)
    return json.dumps(data)


def run_cli(argv, stdin_bytes=b""):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "mev_shield.schedule"] + argv,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )


SANDWICH_TXS = [
    tx("front", "A", 0, 30, side="buy", price=100),
    tx("victim", "B", 0, 20, side="buy", price=110),
    tx("back", "A", 1, 10, side="sell", price=105),
]


class TestSchedulePlan(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = schedule.process(
            request("b", [tx("a", "A", 0, 1)], block=5, blocks=2, cap=1))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "block", "scheduleBlocks", "blockCapacity",
             "baselineOrder", "blocks", "scheduledOrder", "unscheduled",
             "scheduledFee", "unscheduledFee", "totalDelay", "evidence",
             "feasible"],
        )
        self.assertEqual(res["block"], 5)
        self.assertEqual(res["scheduleBlocks"], 2)
        self.assertEqual(res["blockCapacity"], 1)
        self.assertEqual(
            [list(b.keys()) for b in res["blocks"]],
            [["blockHeight", "order", "fee"],
             ["blockHeight", "order", "fee"]],
        )
        self.assertEqual([b["blockHeight"] for b in res["blocks"]], [5, 6])

    def test_all_fit_first_block_fee_hash_order(self):
        # 容量充足：全部进块 0，fee 降序、hash 升序，totalDelay 0
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9),
               tx("h0", "C", 0, 9), tx("h3", "D", 0, 9)]
        res, err = schedule.process(request("n", txs, cap=4))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h3", "h2"])
        self.assertEqual(res["scheduledOrder"], ["h0", "h1", "h3", "h2"])
        self.assertEqual(res["blocks"][0]["order"], ["h0", "h1", "h3", "h2"])
        self.assertEqual(res["blocks"][0]["fee"], 34)
        self.assertEqual(res["blocks"][1]["order"], [])
        self.assertEqual(res["blocks"][1]["fee"], 0)
        self.assertEqual(res["blocks"][2]["order"], [])
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(res["scheduledFee"], 34)
        self.assertEqual(res["unscheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)

    def test_capacity_splits_across_blocks(self):
        # cap=2，4 笔：基线前两块各放 2 笔，delay 为 0+0+1+1
        txs = [tx("h0", "A", 0, 40), tx("h1", "B", 0, 30),
               tx("h2", "C", 0, 20), tx("h3", "D", 0, 10)]
        res, err = schedule.process(request("cap", txs, cap=2, blocks=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual([b["order"] for b in res["blocks"]],
                         [["h0", "h1"], ["h2", "h3"]])
        self.assertEqual([b["fee"] for b in res["blocks"]], [70, 30])
        self.assertEqual(res["scheduledOrder"], ["h0", "h1", "h2", "h3"])
        self.assertEqual(res["totalDelay"], 2)
        self.assertEqual(res["scheduledFee"], 100)

    def test_deadline_forces_early_block(self):
        # d 的 deadline 即起始块，必须进块 0；块序为 d,a,c
        txs = [tx("a", "A", 0, 10),
               tx("d", "D", 0, 9, deadline=10),
               tx("c", "C", 0, 8)]
        res, err = schedule.process(request("dl", txs, cap=1, blocks=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual([b["order"] for b in res["blocks"]],
                         [["d"], ["a"], ["c"]])
        self.assertEqual(res["scheduledOrder"], ["d", "a", "c"])
        self.assertEqual(res["totalDelay"], 3)

    def test_deadline_at_block_boundary_allowed(self):
        # deadline == block + 窗口末块：恰好可进最后一块
        txs = [tx("a", "A", 0, 10), tx("b", "B", 0, 9, deadline=12)]
        res, err = schedule.process(request("db", txs, cap=1, blocks=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual([b["order"] for b in res["blocks"]],
                         [["a"], ["b"], []])

    def test_expired_excluded_everywhere(self):
        # deadline < block：不排程、不进基线与证据，记 DEADLINE_EXPIRED
        sw = [
            tx("gone", "X", 0, 25, side="buy", price=109, deadline=9),
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
        ]
        res, err = schedule.process(request("ex", sw, cap=4, blocks=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"],
                         ["front", "victim", "back"])
        self.assertEqual(res["scheduledOrder"],
                         ["front", "back", "victim"])
        # 未过期基线仍是夹子；过期腿不参与证据
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["unscheduled"], [
            {"hash": "gone", "at": 0, "reason": "DEADLINE_EXPIRED"}])
        self.assertEqual(res["unscheduledFee"], 25)
        self.assertEqual(res["scheduledFee"], 60)

    def test_expired_alone_feasible(self):
        txs = [tx("x", "X", 0, 5, deadline=3)]
        res, err = schedule.process(request("ex0", txs, block=9, cap=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])  # 无未过期交易需要排程
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["scheduledOrder"], [])
        self.assertEqual(res["totalDelay"], 0)

    def test_capacity_shortfall_partial_schedule(self):
        # 窗口容量只能放 2 笔：取 fee 最高两笔，feasible false
        res, err = schedule.process(
            request("short", SANDWICH_TXS, cap=1, blocks=2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["front", "victim"])
        self.assertEqual(res["scheduledFee"], 50)
        self.assertEqual(res["unscheduledFee"], 10)
        self.assertEqual(
            res["unscheduled"],
            [{"hash": "back", "at": 2, "reason": "SCHEDULE_SKIPPED"}],
        )

    def test_sandwich_split_across_blocks_feasible(self):
        # 同窗口两块拆开三条腿即无相邻夹子，全部排程，delay 优先最小
        res, err = schedule.process(
            request("split", SANDWICH_TXS, cap=2, blocks=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual([b["order"] for b in res["blocks"]],
                         [["front", "back"], ["victim"]])
        self.assertEqual(res["scheduledOrder"], ["front", "back", "victim"])
        self.assertEqual(res["totalDelay"], 1)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])

    def test_sandwich_single_window_must_skip(self):
        # 只有一块且容量 3：三腿相邻成夹子，最多排 2 笔
        res, err = schedule.process(
            request("one", SANDWICH_TXS, cap=3, blocks=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(len(res["scheduledOrder"]), 2)
        self.assertEqual(res["scheduledFee"], 50)
        self.assertEqual(
            [e["reason"] for e in res["unscheduled"]], ["SCHEDULE_SKIPPED"])

    def test_nonce_enforced_across_blocks(self):
        # 基线为 a1(nonce1,fee高)、a0(nonce0)；同 from 必须 nonce 升序，
        # 唯一全排程方式是 a0 进块0、a1 进块1
        txs = [tx("a1", "A", 1, 100), tx("a0", "A", 0, 1)]
        res, err = schedule.process(request("nc", txs, cap=2, blocks=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual([b["order"] for b in res["blocks"]],
                         [["a0"], ["a1"]])
        self.assertEqual(res["scheduledOrder"], ["a0", "a1"])
        self.assertEqual(res["totalDelay"], 1)

    def test_lexicographic_tiebreak(self):
        # 同 fee、cap=1：a@0/z@1 与 z@0/a@1 的 count/fee/delay 相同，
        # 取输入下标序列更小者
        txs = [tx("a", "A", 0, 10), tx("z", "B", 0, 10)]
        res, err = schedule.process(request("lex", txs, cap=1, blocks=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual([b["order"] for b in res["blocks"]],
                         [["a"], ["z"]])
        self.assertEqual(res["totalDelay"], 1)

    def test_fee_partition_invariant(self):
        txs = [
            tx("f1", "A", 0, 50, price=100),
            tx("v1", "B", 0, 40, price=110),
            tx("mid", "A", 1, 30, side="sell", price=105),
            tx("v2", "C", 0, 20, side="sell", price=100),
            tx("b2", "A", 2, 10, price=102),
        ]
        res, _ = schedule.process(request("inv", txs, cap=2, blocks=2))
        covered = sorted(res["scheduledOrder"]
                         + [e["hash"] for e in res["unscheduled"]])
        self.assertEqual(covered, sorted(t["hash"] for t in txs))
        self.assertEqual(res["scheduledFee"] + res["unscheduledFee"], 150)

    def test_unscheduled_sorted_by_input_position(self):
        txs = [tx("a", "A", 0, 10),
               tx("x", "X", 0, 9, deadline=9),
               tx("b", "B", 0, 8),
               tx("y", "Y", 0, 7, deadline=9)]
        res, _ = schedule.process(request("ord", txs, cap=1, blocks=1))
        self.assertEqual([e["at"] for e in res["unscheduled"]], [1, 2, 3])
        self.assertEqual([e["hash"] for e in res["unscheduled"]],
                         ["x", "b", "y"])
        self.assertEqual([e["reason"] for e in res["unscheduled"]],
                         ["DEADLINE_EXPIRED", "SCHEDULE_SKIPPED",
                          "DEADLINE_EXPIRED"])

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, cap=2, blocks=2)
        out1 = schedule.serialize(schedule.process(raw)[0])
        out2 = schedule.serialize(schedule.process(raw)[0])
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith("\n"))
        self.assertNotIn(" ", out1.strip())


def _reference_schedule(req):
    """独立穷举参考：每笔未过期交易分配 -1（跳过）或区块偏移。

    块内顺序固定为基线子序列；拼接后检查 nonce 严格递增与无夹子。
    择优 (-笔数, -fee, totalDelay, 输入下标序列)。
    """
    txs = req["transactions"]
    start = req["block"]
    window = req["scheduleBlocks"]
    cap = req["blockCapacity"]
    expired = {
        i for i, t in enumerate(txs)
        if t["deadline"] is not None and t["deadline"] < start
    }
    active_idx = [i for i in range(len(txs)) if i not in expired]
    baseline = decision.order_transactions([txs[i] for i in active_idx])
    hash_to_input = {t["hash"]: i for i, t in enumerate(txs)}
    base_input = [hash_to_input[t["hash"]] for t in baseline]
    n = len(baseline)

    best = None
    for assignment in product(range(-1, window), repeat=n):
        per_slot = [[] for _ in range(window)]
        ok = True
        for j, s in enumerate(assignment):
            if s == -1:
                continue
            if len(per_slot[s]) >= cap:
                ok = False
                break
            dl = baseline[j]["deadline"]
            if dl is not None and start + s > dl:
                ok = False
                break
            per_slot[s].append(j)
        if not ok:
            continue
        ordered_idx = [j for s in range(window) for j in per_slot[s]]
        ordered = [baseline[j] for j in ordered_idx]
        if not decision.nonce_order_satisfied(ordered):
            continue
        if decision.detect_sandwich_evidence(ordered):
            continue
        count = len(ordered_idx)
        fee_sum = sum(baseline[j]["fee"] for j in ordered_idx)
        delay = sum(assignment[j] for j in ordered_idx)
        seq = tuple(base_input[j] for j in ordered_idx)
        key = (-count, -fee_sum, delay, seq)
        if best is None or key < best[0]:
            best = (key, assignment)

    _, assignment = best
    per_slot = [[] for _ in range(window)]
    for j, s in enumerate(assignment):
        if s >= 0:
            per_slot[s].append(j)
    return {
        "baselineOrder": [t["hash"] for t in baseline],
        "assignment": tuple(assignment),
        "blocks": [[baseline[j]["hash"] for j in per_slot[s]]
                   for s in range(window)],
        "scheduledOrder": [baseline[j]["hash"]
                           for s in range(window) for j in per_slot[s]],
        "count": sum(1 for s in assignment if s >= 0),
        "scheduledFee": sum(baseline[j]["fee"] for j, s in
                            enumerate(assignment) if s >= 0),
        "totalDelay": sum(s for s in assignment if s >= 0),
        "active": len(active_idx),
    }


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, cap, window, block=10):
        # 固定序列伪随机：多发送者且每道 nonce 连续，deadline 含过期
        senders = ["A", "B", "C"]
        chosen = [senders[(i * 7 + seed * 3) % len(senders)] for i in range(n)]
        cursor = {s: 0 for s in senders}
        txs = []
        for i, s in enumerate(chosen):
            nonce = cursor[s]
            cursor[s] += 1
            side = "buy" if (i * 13 + seed) % 3 != 0 else "sell"
            sim = "success" if (i * 5 + seed) % 7 != 0 else "revert"
            price = 90 + ((i * 17 + seed * 3) % 41)
            deadline = None
            pick = (i * 3 + seed) % 5
            if pick == 0:
                deadline = block - 1  # 过期
            elif pick == 1:
                deadline = block     # 只能进首块
            elif pick == 2:
                deadline = block + window - 1  # 任意窗口块
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price, deadline=deadline))
        return request(f"g{n}_{seed}_{cap}_{window}", txs,
                       block=block, blocks=window, cap=cap)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 8):
            for seed in range(6):
                for cap, window in ((1, 1), (1, 2), (2, 1), (2, 2),
                                    (2, 3), (3, 2), (5, 1)):
                    raw = self._bundle(n, seed, cap, window)
                    req = schedule.parse_request(raw)
                    ref = _reference_schedule(req)
                    res, err = schedule.process(raw)
                    self.assertIsNone(err)
                    self.assertEqual(res["baselineOrder"],
                                     ref["baselineOrder"])
                    self.assertEqual(res["scheduledOrder"],
                                     ref["scheduledOrder"])
                    self.assertEqual([b["order"] for b in res["blocks"]],
                                     ref["blocks"])
                    self.assertEqual(res["scheduledFee"], ref["scheduledFee"])
                    self.assertEqual(res["totalDelay"], ref["totalDelay"])
                    self.assertEqual(res["feasible"],
                                     ref["count"] == ref["active"])


class TestScheduleValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = schedule.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["block"], 0)
        self.assertEqual(res["scheduleBlocks"], 0)
        self.assertEqual(res["blockCapacity"], 0)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["blocks"], [])
        self.assertEqual(res["scheduledOrder"], [])
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(res["unscheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)
        self.assertFalse(res["feasible"])

    def test_bad_block(self):
        for value in (-1, True, False, 1.5, "10", None, [10], {}):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            if value is None:
                del raw["block"]
            else:
                raw["block"] = value
            self.assert_error(json.dumps(raw), "BAD_BLOCK")

    def test_bad_schedule_window(self):
        for value in (0, -1, True, False, 1.5, "2", None, [2]):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            if value is None:
                del raw["scheduleBlocks"]
            else:
                raw["scheduleBlocks"] = value
            self.assert_error(json.dumps(raw), "BAD_SCHEDULE_WINDOW")

    def test_bad_block_capacity(self):
        for value in (0, -1, True, False, 2.0, "1", None):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            if value is None:
                del raw["blockCapacity"]
            else:
                raw["blockCapacity"] = value
            self.assert_error(json.dumps(raw), "BAD_BLOCK_CAPACITY")

    def test_bad_deadline(self):
        for value in (-1, True, False, 3.0, "5", [5]):
            bad = tx("a", "A", 0, 1, deadline=value)
            self.assert_error(request("e", [bad]), "BAD_DEADLINE")

    def test_missing_deadline_valid(self):
        res, err = schedule.process(request("e", [tx("a", "A", 0, 1)]))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])

    def test_validation_order_among_new_fields(self):
        base = json.loads(request("e", [tx("a", "A", 0, 1)]))
        raw = json.loads(json.dumps(base))
        raw["block"] = -1
        raw["scheduleBlocks"] = 0
        self.assert_error(json.dumps(raw), "BAD_BLOCK")

        raw = json.loads(json.dumps(base))
        raw["scheduleBlocks"] = 0
        raw["blockCapacity"] = 0
        self.assert_error(json.dumps(raw), "BAD_SCHEDULE_WINDOW")

        raw = json.loads(json.dumps(base))
        raw["blockCapacity"] = 0
        raw["transactions"][0]["deadline"] = -1
        self.assert_error(json.dumps(raw), "BAD_BLOCK_CAPACITY")

        raw = json.loads(json.dumps(base))
        raw["transactions"][0]["deadline"] = -1
        self.assert_error(json.dumps(raw), "BAD_DEADLINE")

    def test_existing_errors_take_priority(self):
        # 统一决策入口的既有校验全部优先于四个新错误码
        self.assert_error(request("e", [], block=-1), "EMPTY_BUNDLE")
        self.assert_error(request("e", [tx("", "A", 0, 1)], block=-1),
                          "UNIDENTIFIED_TRANSACTION")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2)], block=-1),
            "DUPLICATE_TRANSACTION",
        )
        self.assert_error(
            request("e", [tx("a0", "A", 0, 1), tx("a2", "A", 2, 1)],
                    block=-1),
            "ORDERING_CONFLICT",
        )
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        self.assert_error(request("e", [bad], block=-1),
                          "MISSING_MARKET_CONTEXT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=0),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], slip=2),
                          "INVALID_RISK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], rb=2),
                          "INVALID_ROLLBACK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], policy="x"),
                          "BAD_POLICY")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], slippage_mode="zzz"),
            "BAD_SLIPPAGE_MODE",
        )

    def test_bad_json_schema(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli([], request("c", [tx("a", "A", 0, 1)]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["scheduledOrder"], ["a"])
        self.assertTrue(res["feasible"])

    def test_module_entry_infeasible_exit0_no_stderr(self):
        proc = run_cli(
            [], request("c", SANDWICH_TXS, cap=1, blocks=2).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(len(res["scheduledOrder"]), 2)

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(res["scheduledOrder"], [])
        self.assertEqual(res["blocks"], [])
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)
        self.assertEqual(res["block"], 0)

    def test_module_entry_bad_block_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        raw["block"] = True
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_BLOCK\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(res["blockCapacity"], 0)

    def test_module_entry_bad_deadline_exit2(self):
        proc = run_cli(
            [], request("c", [tx("a", "A", 0, 1, deadline=-2)]).encode())
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_DEADLINE\n")

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["scheduledOrder"], [])
        self.assertFalse(res["feasible"])

    def test_input_io(self):
        proc = run_cli(["--input", "/nonexistent/x.json"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "INPUT_IO\n")

    def test_input_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", SANDWICH_TXS, cap=2, blocks=2))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertTrue(res["feasible"])
            self.assertEqual(res["scheduledOrder"],
                             ["front", "back", "victim"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, cap=2, blocks=2).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)

    def test_existing_entries_unchanged(self):
        # 既有决策入口行为不受影响
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        raw = json.dumps({
            "id": "old",
            "transactions": [
                {"hash": "a", "from": "A", "nonce": 0, "fee": 1,
                 "token": "T", "side": "buy", "sim": "success",
                 "price": 100},
            ],
            "market": {"prices": {"T": 100}},
            "basePrice": 100,
            "maxSlippage": 0.5,
            "rollbackLimit": 1,
        }).encode("utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "mev_shield.decision"],
            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertEqual(res["finalOrder"], ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
