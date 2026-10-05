"""mev_shield.bounded 预算约束隔离计划行为验证（标准库 unittest）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import bounded
from mev_shield import decision
from mev_shield import mitigation


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, limit, market=None, base=100, slip=0.5, rb=1,
            policy=None, slippage_mode=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "isolationLimit": limit,
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
        [sys.executable, "-m", "mev_shield.bounded"] + argv,
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


class TestFixedShape(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = bounded.process(request("b", [tx("a", "A", 0, 1)], 0))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed",
             "keptFee", "removedFee", "evidence", "feasible",
             "isolationLimit"],
        )
        self.assertTrue(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["removed"], [])

    def test_error_shape_keys(self):
        raw = request("e", [tx("a", "A", 0, 1)], 0)
        data = json.loads(raw)
        del data["isolationLimit"]
        res, err = bounded.process(json.dumps(data))
        self.assertEqual(err, "BAD_ISOLATION_LIMIT")
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed",
             "keptFee", "removedFee", "evidence", "feasible",
             "isolationLimit"],
        )
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)


class TestFeasiblePlans(unittest.TestCase):
    def _assert_partition(self, res, txs):
        # feasible 时 selectedOrder 与 removed 不重不漏覆盖输入，
        # 两 fee 之和等于输入 fee 总和
        self.assertTrue(res["feasible"])
        removed_hashes = [entry["hash"] for entry in res["removed"]]
        self.assertEqual(
            sorted(res["selectedOrder"] + removed_hashes),
            sorted(t["hash"] for t in txs),
        )
        self.assertEqual(
            res["keptFee"] + res["removedFee"],
            sum(t["fee"] for t in txs),
        )
        for entry in res["removed"]:
            self.assertEqual(entry["reason"], "SANDWICH_REMOVED")
        # removed 按输入位置，at 与实际位置一致
        for entry in res["removed"]:
            self.assertEqual(txs[entry["at"]]["hash"], entry["hash"])

    def test_no_sandwich_limit_zero_keeps_all(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, err = bounded.process(request("n", txs, 0))
        self.assertIsNone(err)
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertTrue(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_sandwich_limit_one_matches_unconstrained(self):
        res, err = bounded.process(request("s1", SANDWICH_TXS, 1))
        self.assertIsNone(err)
        self.assertEqual(res["selectedOrder"], ["front", "victim"])
        self.assertEqual(
            res["removed"],
            [{"hash": "back", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 50)
        self.assertEqual(res["removedFee"], 10)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self._assert_partition(res, SANDWICH_TXS)

    def test_generous_limit_matches_mitigation(self):
        # 预算不构成约束时，最优计划与最小隔离入口完全一致
        txs = [
            tx("f1", "A", 0, 50, price=100),
            tx("v1", "B", 0, 40, price=110),
            tx("mid", "A", 1, 30, side="sell", price=105),
            tx("v2", "C", 0, 20, side="sell", price=100),
            tx("b2", "A", 2, 10, price=102),
        ]
        req = decision.parse_request(request("lock", txs, 5))
        res, err = bounded.process(request("lock", txs, 5))
        ref = mitigation.isolate(req)
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ref["selectedOrder"])
        self.assertEqual(res["removed"], ref["removed"])
        self.assertEqual(res["keptFee"], ref["keptFee"])
        self.assertEqual(res["removedFee"], ref["removedFee"])
        self.assertEqual(
            [e["at"] for e in res["evidence"]], [[0, 1, 2], [2, 3, 4]]
        )
        self._assert_partition(res, txs)

    def test_budget_forces_cheaper_count_but_lower_fee(self):
        # 同一 from nonce 在基线上为 2,0,1：fee 最优是保留高 fee 的
        # nonce2（移除 2 笔）；预算只允许 1 笔移除时，被迫保留 0,1，
        # 移除高 fee 交易——可行但 keptFee 更低
        txs = [
            tx("aaa", "A", 0, 1),
            tx("bbb", "A", 1, 1),
            tx("zzz", "A", 2, 300),
        ]
        # 无预算约束的最小隔离入口：保留 zzz，移除两笔低 fee
        req = decision.parse_request(request("bud", txs, 2))
        ref = mitigation.isolate(req)
        self.assertEqual(ref["selectedOrder"], ["zzz"])

        res, err = bounded.process(request("bud", txs, 1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["zzz", "aaa", "bbb"])
        self.assertEqual(res["selectedOrder"], ["aaa", "bbb"])
        self.assertEqual(
            res["removed"],
            [{"hash": "zzz", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 2)
        self.assertEqual(res["removedFee"], 300)
        self.assertEqual(res["evidence"], [])
        self._assert_partition(res, txs)

        # 预算 2 时回到 fee 最优解
        res2, _ = bounded.process(request("bud", txs, 2))
        self.assertEqual(res2["selectedOrder"], ["zzz"])
        self.assertEqual(res2["keptFee"], 300)
        self._assert_partition(res2, txs)

    def test_limit_equal_n_feasible(self):
        # 预算等于笔数时空集也在预算内，必有可行解
        res, err = bounded.process(request("all", SANDWICH_TXS, 3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, 1)
        self.assertEqual(
            bounded.serialize(bounded.process(raw)[0]),
            bounded.serialize(bounded.process(raw)[0]),
        )
        out = bounded.serialize(bounded.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


class TestInfeasible(unittest.TestCase):
    def test_budget_zero_infeasible(self):
        # 至少需要移除一条腿；预算 0 时无可行解，退出 0 不报错
        res, err = bounded.process(request("i0", SANDWICH_TXS, 0))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["isolationLimit"], 0)
        # 基线与证据仍完整
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["evidence"][0]["victim"], "victim")

    def test_nonce_conflict_budget_zero_infeasible(self):
        txs = [
            tx("aaa", "A", 0, 1),
            tx("bbb", "A", 1, 1),
            tx("zzz", "A", 2, 300),
        ]
        res, err = bounded.process(request("nc0", txs, 0))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["baselineOrder"], ["zzz", "aaa", "bbb"])

    def test_infeasible_is_not_error(self):
        raw = request("quiet", SANDWICH_TXS, 0).encode("utf-8")
        proc = run_cli([], raw)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])


def _reference_isolate(req, limit):
    """独立暴力参考：枚举全部子集，施加预算后按相同三级目标选最优。"""
    txs = req["transactions"]
    baseline = decision.order_transactions(txs)
    n = len(baseline)
    best = None
    for r in range(n + 1):
        if n - r > limit:
            continue
        for combo in combinations(range(n), r):
            selected = [baseline[i] for i in combo]
            if decision.detect_sandwich_evidence(selected):
                continue
            if not decision.nonce_order_satisfied(selected):
                continue
            kept = {t["hash"] for t in selected}
            kept_fee = sum(t["fee"] for t in selected)
            removed_seq = [t["hash"] for t in txs if t["hash"] not in kept]
            key = (-kept_fee, -r, removed_seq)
            if best is None or key < best[0]:
                best = (key, [t["hash"] for t in selected])
    evidence = sorted(
        decision.detect_sandwich_evidence(baseline),
        key=lambda entry: (entry["at"][0], entry["victim"]),
    )
    if best is None:
        return None, [t["hash"] for t in baseline], evidence
    return best[1], [t["hash"] for t in baseline], evidence


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed):
        # 固定序列伪随机，覆盖多发送者、连续 nonce、双向与各种价格
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
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price))
        return txs

    def test_matches_bruteforce_reference(self):
        for n in range(1, 9):
            for seed in range(10):
                txs = self._bundle(n, seed)
                for limit in range(n + 1):
                    raw = request(f"g{n}_{seed}_{limit}", txs, limit)
                    req = decision.parse_request(raw)
                    ref_kept, ref_baseline, ref_evidence = _reference_isolate(
                        req, limit)
                    res, err = bounded.process(raw)
                    self.assertIsNone(err)
                    self.assertEqual(res["baselineOrder"], ref_baseline)
                    self.assertEqual(
                        [e["at"] for e in res["evidence"]],
                        [e["at"] for e in ref_evidence],
                    )
                    if ref_kept is None:
                        self.assertFalse(res["feasible"])
                        self.assertEqual(res["selectedOrder"], [])
                        self.assertEqual(res["removed"], [])
                        self.assertEqual(res["keptFee"], 0)
                        self.assertEqual(res["removedFee"], 0)
                    else:
                        self.assertTrue(res["feasible"])
                        self.assertEqual(res["selectedOrder"], ref_kept)
                        kept_fee = sum(
                            t["fee"] for t in txs
                            if t["hash"] in set(ref_kept)
                        )
                        self.assertEqual(res["keptFee"], kept_fee)
                        self.assertEqual(
                            res["removedFee"],
                            sum(t["fee"] for t in txs) - kept_fee,
                        )
                    self.assertEqual(res["isolationLimit"], limit)


class TestIsolationLimitValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = bounded.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def _valid_data(self, ident="e", txs=None):
        return json.loads(request(
            ident, txs if txs is not None else [tx("a", "A", 0, 1)], 0))

    def test_missing_wrong_type_bool_negative(self):
        cases = [
            ("missing", lambda d: d.pop("isolationLimit")),
            ("null", lambda d: d.update(isolationLimit=None)),
            ("bool", lambda d: d.update(isolationLimit=True)),
            ("bool_false", lambda d: d.update(isolationLimit=False)),
            ("negative", lambda d: d.update(isolationLimit=-1)),
            ("float", lambda d: d.update(isolationLimit=1.5)),
            ("string", lambda d: d.update(isolationLimit="1")),
            ("array", lambda d: d.update(isolationLimit=[1])),
            ("object", lambda d: d.update(isolationLimit={})),
        ]
        for _name, mutate in cases:
            with self.subTest(_name):
                data = self._valid_data()
                mutate(data)
                self.assert_error(json.dumps(data), "BAD_ISOLATION_LIMIT")

    def test_zero_and_large_nonneg_int_ok(self):
        for value in (0, 1, 1000000):
            data = self._valid_data("ok")
            data["isolationLimit"] = value
            res, err = bounded.process(json.dumps(data))
            self.assertIsNone(err)
            self.assertTrue(res["feasible"])
            self.assertEqual(res["isolationLimit"], value)

    def test_after_bad_slippage_mode(self):
        # BAD_SLIPPAGE_MODE 优先于 BAD_ISOLATION_LIMIT
        data = self._valid_data()
        data["slippageMode"] = "x"
        data["isolationLimit"] = True
        self.assert_error(json.dumps(data), "BAD_SLIPPAGE_MODE")

    def test_after_all_existing_checks(self):
        # 既有全部错误码均优先于 BAD_ISOLATION_LIMIT
        self.assert_error(request("e", [], -1), "EMPTY_BUNDLE")
        self.assert_error(request("e", [tx("", "A", 0, 1)], -1),
                          "UNIDENTIFIED_TRANSACTION")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2)], -1),
            "DUPLICATE_TRANSACTION",
        )
        self.assert_error(
            request("e", [tx("a0", "A", 0, 1), tx("a2", "A", 2, 1)], -1),
            "ORDERING_CONFLICT",
        )
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        self.assert_error(request("e", [bad], -1), "MISSING_MARKET_CONTEXT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], -1, base=0),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], -1, slip=2),
                          "INVALID_RISK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], -1, rb=2),
                          "INVALID_ROLLBACK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], -1, policy="x"),
                          "BAD_POLICY")

    def test_bad_json_schema_still_first(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")
        data = self._valid_data()
        data["id"] = 5
        self.assert_error(json.dumps(data), "BAD_SCHEMA", ident="")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli([], request("c", [tx("a", "A", 0, 1)], 0).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], ["a"])
        self.assertTrue(res["feasible"])

    def test_bad_isolation_limit_exit2(self):
        data = json.loads(request("c", [tx("a", "A", 0, 1)], 0))
        del data["isolationLimit"]
        proc = run_cli([], json.dumps(data).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ISOLATION_LIMIT\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_existing_error_exit2_priority(self):
        proc = run_cli([], request("c", [], -1).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["baselineOrder"], [])
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_input_io(self):
        proc = run_cli(["--input", "/nonexistent/x.json"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "INPUT_IO\n")

    def test_input_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", SANDWICH_TXS, 1))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["selectedOrder"], ["front", "victim"])
            self.assertTrue(res["feasible"])
            self.assertEqual(res["isolationLimit"], 1)

    def test_output_io_keeps_prior_code(self):
        # 输出不可写且输入本身校验失败：保留更高优先级错误码
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", [], -1))
            outp = os.path.join(d, "no-such-dir", "out.json")
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, 1).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
