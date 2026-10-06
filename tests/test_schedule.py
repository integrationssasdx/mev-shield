"""mev_shield.schedule 多区块排程计划行为验证（标准库 unittest）。"""

import io
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
    entry = {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }
    if deadline is not None:
        entry["deadline"] = deadline
    return entry


def request(ident, txs, block=10, window=2, capacity=2, market=None,
            base=100, slip=0.5, rb=1, policy=None, slippage_mode=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "block": block,
        "scheduleBlocks": window,
        "blockCapacity": capacity,
    }
    if policy is not None:
        data["policy"] = policy
    if slippage_mode is not None:
        data["slippageMode"] = slippage_mode
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
        res, err = schedule.process(request("r", [tx("a", "A", 0, 1)]))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "block", "scheduleBlocks", "blockCapacity",
             "baselineOrder", "blocks", "scheduledOrder", "unscheduled",
             "scheduledFee", "unscheduledFee", "totalDelay", "evidence",
             "feasible"],
        )
        self.assertEqual(res["block"], 10)
        self.assertEqual(res["scheduleBlocks"], 2)
        self.assertEqual(res["blockCapacity"], 2)
        self.assertTrue(res["feasible"])

    def test_all_scheduled_first_block(self):
        # 容量充足且无夹子：全部进首块，顺序同基线，无延迟
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, err = schedule.process(request("n", txs, window=2, capacity=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["scheduledOrder"], ["h0", "h1", "h2"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["h0", "h1", "h2"]},
             {"block": 11, "order": []}],
        )
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(res["scheduledFee"], 25)
        self.assertEqual(res["unscheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)
        self.assertEqual(res["evidence"], [])

    def test_capacity_spreads_and_delay(self):
        # 单块容量 1：两笔分进两块；延迟相同按输入下标字典序取 [h1, h2]
        txs = [tx("h1", "A", 0, 9), tx("h2", "B", 0, 8)]
        res, err = schedule.process(
            request("c", txs, window=2, capacity=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["h1", "h2"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["h1"]}, {"block": 11, "order": ["h2"]}],
        )
        self.assertEqual(res["totalDelay"], 1)
        self.assertEqual(res["scheduledFee"], 17)

    def test_capacity_insufficient_skips_lowest_fee(self):
        # 窗口仅一块且容量 1：排程数相同取 fee 最高，低费记 SCHEDULE_SKIPPED
        txs = [tx("hi", "A", 0, 9), tx("lo", "B", 0, 8)]
        res, err = schedule.process(
            request("s", txs, window=1, capacity=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["hi"])
        self.assertEqual(res["scheduledFee"], 9)
        self.assertEqual(res["unscheduledFee"], 8)
        self.assertEqual(
            res["unscheduled"],
            [{"hash": "lo", "at": 1, "reason": "SCHEDULE_SKIPPED"}],
        )

    def test_deadline_expired_excluded(self):
        # deadline 小于 block：过期，不参与排程与夹子判定
        txs = [
            tx("old", "A", 0, 50, deadline=5),
            tx("new", "B", 0, 10),
        ]
        res, err = schedule.process(request("e", txs, block=10))
        self.assertIsNone(err)
        self.assertEqual(res["scheduledOrder"], ["new"])
        self.assertEqual(res["baselineOrder"], ["old", "new"])
        self.assertEqual(
            res["unscheduled"],
            [{"hash": "old", "at": 0, "reason": "DEADLINE_EXPIRED"}],
        )
        self.assertEqual(res["unscheduledFee"], 50)
        self.assertEqual(res["evidence"], [])
        # 未过期交易仅 new 且已排程：feasible 为 true
        self.assertTrue(res["feasible"])

    def test_deadline_restricts_block(self):
        # deadline 等于 block：只能进首块；容量被占则记 SCHEDULE_SKIPPED
        txs = [
            tx("free", "A", 0, 9),
            tx("bound", "B", 0, 8, deadline=10),
        ]
        res, err = schedule.process(
            request("d", txs, block=10, window=2, capacity=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        # bound 只能进块 10，free 进块 11；排程数与 fee 优先于延迟
        self.assertEqual(res["scheduledOrder"], ["bound", "free"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["bound"]},
             {"block": 11, "order": ["free"]}],
        )
        self.assertEqual(res["totalDelay"], 1)

    def test_deadline_beyond_window_uses_window(self):
        # deadline 超出窗口末端：窗口为限，可进任意窗口区块
        txs = [tx("a", "A", 0, 5, deadline=99), tx("b", "B", 0, 6)]
        res, err = schedule.process(
            request("w", txs, block=10, window=2, capacity=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        # 两种排法延迟相同，按输入下标字典序取 [a, b]
        self.assertEqual(res["scheduledOrder"], ["a", "b"])
        self.assertEqual(res["totalDelay"], 1)

    def test_nonce_constraint_across_blocks(self):
        # 同 from 两 nonce，fee 使块内逆序：单块容量 2 无法同块共存
        txs = [tx("a1", "A", 1, 30), tx("a0", "A", 0, 20)]
        res, err = schedule.process(
            request("nv", txs, window=1, capacity=2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        # 只能排一笔：笔数相同取 fee 最高的 a1
        self.assertEqual(res["scheduledOrder"], ["a1"])
        self.assertEqual(
            res["unscheduled"],
            [{"hash": "a0", "at": 1, "reason": "SCHEDULE_SKIPPED"}],
        )
        # 窗口两块容量 1：a0 进首块、a1 进次块即可全部排程
        res2, err2 = schedule.process(
            request("nv2", txs, window=2, capacity=1))
        self.assertIsNone(err2)
        self.assertTrue(res2["feasible"])
        self.assertEqual(res2["scheduledOrder"], ["a0", "a1"])
        self.assertEqual(res2["totalDelay"], 1)

    def test_sandwich_avoided_by_partial_schedule(self):
        # 单块容量 3：基线即夹子；全排不合法，最优为 fee 最高的无夹子对
        res, err = schedule.process(
            request("sw", SANDWICH_TXS, window=1, capacity=3))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["front", "victim"])
        self.assertEqual(res["scheduledFee"], 50)
        self.assertEqual(
            res["unscheduled"],
            [{"hash": "back", "at": 2, "reason": "SCHEDULE_SKIPPED"}],
        )
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["evidence"][0]["victim"], "victim")

    def test_sandwich_avoided_across_blocks(self):
        # 窗口两块容量 2：victim 排入次块即可全排程；首尾块拼接后
        # [front, back, victim] 相邻三段不成夹子且 nonce 递增
        res, err = schedule.process(
            request("sw2", SANDWICH_TXS, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["front", "back", "victim"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["front", "back"]},
             {"block": 11, "order": ["victim"]}],
        )
        self.assertEqual(res["totalDelay"], 1)

    def test_scheduled_order_has_no_sandwich_and_nonce_ok(self):
        res, err = schedule.process(
            request("ok", SANDWICH_TXS, window=2, capacity=2))
        self.assertIsNone(err)
        by_hash = {t["hash"]: t for t in SANDWICH_TXS}
        ordered = [by_hash[h] for h in res["scheduledOrder"]]
        self.assertTrue(decision.nonce_order_satisfied(ordered))
        self.assertEqual(decision.detect_sandwich_evidence(ordered), [])

    def test_unscheduled_sorted_by_input_position(self):
        txs = [
            tx("x", "A", 0, 1),
            tx("y", "B", 0, 2, deadline=1),
            tx("z", "C", 0, 3),
        ]
        res, err = schedule.process(
            request("u", txs, block=10, window=1, capacity=1))
        self.assertIsNone(err)
        ats = [entry["at"] for entry in res["unscheduled"]]
        self.assertEqual(ats, sorted(ats))
        reasons = {entry["hash"]: entry["reason"]
                   for entry in res["unscheduled"]}
        self.assertEqual(reasons["y"], "DEADLINE_EXPIRED")
        self.assertEqual(reasons["x"], "SCHEDULE_SKIPPED")

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, window=2, capacity=2)
        self.assertEqual(
            schedule.serialize(schedule.process(raw)[0]),
            schedule.serialize(schedule.process(raw)[0]),
        )
        out = schedule.serialize(schedule.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_schedule(req):
    """独立参考实现：枚举全部排程选择，按相同四级目标选全局最优。

    返回 (scheduled_order, scheduled_fee, total_delay, feasible)。
    """
    txs = req["transactions"]
    block = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]
    last = block + window - 1
    input_pos = {t["hash"]: at for at, t in enumerate(txs)}

    candidates = []
    for t in txs:
        d = t["deadline"]
        if d is not None and d < block:
            continue
        horizon = last if d is None else min(d, last)
        candidates.append((t, horizon - block))

    best = None
    for choices in product(range(-1, window), repeat=len(candidates)):
        loads = [0] * window
        ok = True
        for (_t, latest_off), off in zip(candidates, choices):
            if off < 0:
                continue
            if off > latest_off or loads[off] >= capacity:
                ok = False
                break
            loads[off] += 1
        if not ok:
            continue
        per_block = [[] for _ in range(window)]
        for (t, _latest_off), off in zip(candidates, choices):
            if off >= 0:
                per_block[off].append(t)
        ordered = []
        for off in range(window):
            per_block[off].sort(key=lambda t: (-t["fee"], t["hash"]))
            ordered.extend(per_block[off])
        if not decision.nonce_order_satisfied(ordered):
            continue
        if decision.detect_sandwich_evidence(ordered):
            continue
        fee = sum(t["fee"] for t in ordered)
        delay = sum(off for off in choices if off >= 0)
        seq = tuple(input_pos[t["hash"]] for t in ordered)
        key = (-len(ordered), -fee, delay, seq)
        if best is None or key < best[0]:
            best = (key, [t["hash"] for t in ordered])
    key, order = best
    return order, -key[1], key[2], len(order) == len(candidates)


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, window, capacity, block=10):
        # 固定序列伪随机，覆盖多发送者、连续 nonce、双向、各种价格与期限
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
            pick = (i * 3 + seed) % 4
            deadline = None
            if pick == 1:
                deadline = block - 1  # 过期
            elif pick == 2:
                deadline = block + (i + seed) % (window + 1)  # 窗口内外
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price, deadline=deadline))
        return request(f"g{n}_{seed}_{window}_{capacity}", txs,
                       block=block, window=window, capacity=capacity)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 7):
            for seed in range(4):
                for window, capacity in ((1, 1), (2, 1), (2, 2), (3, 2)):
                    raw = self._bundle(n, seed, window, capacity)
                    req = schedule.parse_request(raw)
                    ref = _reference_schedule(req)
                    res, err = schedule.process(raw)
                    self.assertIsNone(err)
                    order, fee, delay, feasible = ref
                    self.assertEqual(res["scheduledOrder"], order)
                    self.assertEqual(res["scheduledFee"], fee)
                    self.assertEqual(res["totalDelay"], delay)
                    self.assertEqual(res["feasible"], feasible)
                    # blocks 与 scheduledOrder 相互一致
                    merged = []
                    for off, entry in enumerate(res["blocks"]):
                        self.assertEqual(entry["block"], 10 + off)
                        merged.extend(entry["order"])
                    self.assertEqual(merged, res["scheduledOrder"])
                    # 排程与未排程不重不漏覆盖全部交易
                    self.assertEqual(
                        sorted(res["scheduledOrder"]
                               + [e["hash"] for e in res["unscheduled"]]),
                        sorted(t["hash"] for t in req["transactions"]),
                    )
                    total_fee = sum(t["fee"] for t in req["transactions"])
                    self.assertEqual(
                        res["scheduledFee"] + res["unscheduledFee"], total_fee)


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
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(res["unscheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertFalse(res["feasible"])

    def _raw(self, **overrides):
        data = json.loads(request("e", [tx("a", "A", 0, 1)]))
        data.update(overrides)
        return json.dumps(data)

    def test_missing_block(self):
        data = json.loads(request("e", [tx("a", "A", 0, 1)]))
        del data["block"]
        self.assert_error(json.dumps(data), "BAD_BLOCK")

    def test_bad_block_values(self):
        for value in (-1, 1.5, "10", None, True, False, [10], {"x": 1}):
            self.assert_error(self._raw(block=value), "BAD_BLOCK")

    def test_missing_window(self):
        data = json.loads(request("e", [tx("a", "A", 0, 1)]))
        del data["scheduleBlocks"]
        self.assert_error(json.dumps(data), "BAD_SCHEDULE_WINDOW")

    def test_bad_window_values(self):
        for value in (0, -1, 1.5, "2", None, True, False, [2], {"x": 1}):
            self.assert_error(
                self._raw(scheduleBlocks=value), "BAD_SCHEDULE_WINDOW")

    def test_missing_capacity(self):
        data = json.loads(request("e", [tx("a", "A", 0, 1)]))
        del data["blockCapacity"]
        self.assert_error(json.dumps(data), "BAD_BLOCK_CAPACITY")

    def test_bad_capacity_values(self):
        for value in (0, -1, 1.5, "2", None, True, False, [2], {"x": 1}):
            self.assert_error(
                self._raw(blockCapacity=value), "BAD_BLOCK_CAPACITY")

    def test_bad_deadline_values(self):
        for value in (-1, 1.5, "10", None, True, False, [10], {"x": 1}):
            bad = tx("a", "A", 0, 1)
            bad["deadline"] = value
            self.assert_error(
                request("e", [bad]), "BAD_DEADLINE")

    def test_zero_block_and_deadline_valid(self):
        res, err = schedule.process(
            request("z", [tx("a", "A", 0, 1, deadline=0)], block=0))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["a"])
        self.assertEqual(res["totalDelay"], 0)

    def test_new_code_priority_order(self):
        # 四项新校验按 BAD_BLOCK -> BAD_SCHEDULE_WINDOW ->
        # BAD_BLOCK_CAPACITY -> BAD_DEADLINE 依次判定
        bad = tx("a", "A", 0, 1)
        bad["deadline"] = -1
        data = json.loads(request("e", [bad]))
        data["block"] = -1
        data["scheduleBlocks"] = 0
        data["blockCapacity"] = 0
        self.assert_error(json.dumps(data), "BAD_BLOCK")
        data["block"] = 10
        self.assert_error(json.dumps(data), "BAD_SCHEDULE_WINDOW")
        data["scheduleBlocks"] = 2
        self.assert_error(json.dumps(data), "BAD_BLOCK_CAPACITY")
        data["blockCapacity"] = 2
        self.assert_error(json.dumps(data), "BAD_DEADLINE")

    def test_existing_errors_take_priority(self):
        # 既有校验全部优先于排程字段校验
        self.assert_error(request("e", []), "EMPTY_BUNDLE")
        self.assert_error(request("e", [tx("", "A", 0, 1)]),
                          "UNIDENTIFIED_TRANSACTION")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2)]),
            "DUPLICATE_TRANSACTION",
        )
        self.assert_error(
            request("e", [tx("a0", "A", 0, 1), tx("a2", "A", 2, 1)]),
            "ORDERING_CONFLICT",
        )
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        self.assert_error(request("e", [bad]), "MISSING_MARKET_CONTEXT")
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
        # 排程字段与既有错误并存时既有错误优先
        data = json.loads(request("e", [tx("a", "A", 0, 1)], slip=2))
        data["block"] = -1
        self.assert_error(json.dumps(data), "INVALID_RISK_LIMIT")

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

    def test_module_entry_partial_schedule_exit0_no_stderr(self):
        proc = run_cli(
            [], request("c", SANDWICH_TXS, window=1, capacity=3).encode())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(res["scheduledOrder"], ["front", "victim"])
        self.assertEqual(len(res["evidence"]), 1)

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["scheduledOrder"], [])
        self.assertFalse(res["feasible"])
        self.assertEqual(res["block"], 0)

    def test_module_entry_bad_block_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        del raw["block"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_BLOCK\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["scheduledOrder"], [])
        self.assertFalse(res["feasible"])

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["baselineOrder"], [])
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
                f.write(request("file", SANDWICH_TXS, window=2, capacity=2))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["scheduledOrder"], ["front", "back", "victim"])
            self.assertTrue(res["feasible"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, window=2, capacity=2).encode()
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
