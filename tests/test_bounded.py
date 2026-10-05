"""mev_shield.bounded 预算约束隔离计划行为验证（标准库 unittest）。"""

import io
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


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, limit=0, market=None, base=100, slip=0.5, rb=1,
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


class TestBoundedIsolate(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = bounded.process(request("b", [tx("a", "A", 0, 1)], limit=0))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed",
             "keptFee", "removedFee", "evidence", "feasible",
             "isolationLimit"],
        )
        self.assertTrue(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_no_sandwich_keeps_all_zero_budget(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, err = bounded.process(request("n", txs, limit=0))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["evidence"], [])

    def test_budget_allows_single_removal(self):
        # 单个夹子：预算 1 足够移除 fee 最低的后置腿
        res, err = bounded.process(request("s1", SANDWICH_TXS, limit=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])
        self.assertEqual(
            res["removed"],
            [{"hash": "back", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 50)
        self.assertEqual(res["removedFee"], 10)
        self.assertEqual(res["keptFee"] + res["removedFee"], 60)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["isolationLimit"], 1)

    def test_budget_insufficient_infeasible(self):
        # 预算 0 无法移除任何腿：feasible 为 false，但基线与证据完整
        res, err = bounded.process(request("s0", SANDWICH_TXS, limit=0))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["isolationLimit"], 0)

    def test_budget_covers_all_always_feasible(self):
        # 预算不小于笔数时空集恒合法，必可行
        res, err = bounded.process(request("all", SANDWICH_TXS, limit=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])

    def test_nonce_constraint_within_budget(self):
        # 保留 f 必保留 b 才满足 nonce 递增，而 {f,v,b} 是夹子：
        # 预算 2 时移除 f、b，保留 X、v
        txs = [
            tx("X", "A", 2, 100),
            tx("f", "A", 0, 30, price=100),
            tx("v", "B", 0, 20, price=110),
            tx("b", "A", 1, 10, side="sell", price=105),
        ]
        res, err = bounded.process(request("nc", txs, limit=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["X", "v"])
        self.assertEqual(
            [entry["hash"] for entry in res["removed"]], ["f", "b"]
        )
        self.assertEqual([entry["at"] for entry in res["removed"]], [1, 3])
        self.assertEqual(res["keptFee"], 120)
        self.assertEqual(res["removedFee"], 40)

    def test_nonce_constraint_over_budget(self):
        # 同上但预算 1：任何合法保留集至少移除 2 笔，预算内无解
        txs = [
            tx("X", "A", 2, 100),
            tx("f", "A", 0, 30, price=100),
            tx("v", "B", 0, 20, price=110),
            tx("b", "A", 1, 10, side="sell", price=105),
        ]
        res, err = bounded.process(request("nc1", txs, limit=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(len(res["evidence"]), 1)

    def test_lexicographic_tiebreak_within_budget(self):
        # 三条腿 fee 均为 10：移除 "aa" 或 "zz" 等价，取移除序列更小者
        txs = [
            tx("zz", "A", 1, 10, side="sell", price=105),
            tx("mm", "B", 0, 10, side="buy", price=110),
            tx("aa", "A", 0, 10, side="buy", price=100),
        ]
        res, err = bounded.process(request("lex", txs, limit=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["mm", "zz"])
        self.assertEqual(
            res["removed"],
            [{"hash": "aa", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )

    def test_fee_partition_invariant(self):
        # feasible 时 selectedOrder 与 removed 不重不漏，fee 之和为总和
        txs = [
            tx("f1", "A", 0, 50, price=100),
            tx("v1", "B", 0, 40, price=110),
            tx("mid", "A", 1, 30, side="sell", price=105),
            tx("v2", "C", 0, 20, side="sell", price=100),
            tx("b2", "A", 2, 10, price=102),
        ]
        res, err = bounded.process(request("inv", txs, limit=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        covered = sorted(res["selectedOrder"]
                         + [e["hash"] for e in res["removed"]])
        self.assertEqual(covered, sorted(t["hash"] for t in txs))
        self.assertEqual(res["keptFee"] + res["removedFee"], 150)

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, limit=1)
        self.assertEqual(
            bounded.serialize(bounded.process(raw)[0]),
            bounded.serialize(bounded.process(raw)[0]),
        )
        out = bounded.serialize(bounded.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_bounded(req):
    """独立参考实现：枚举预算内全部子集，按相同三级目标选全局最优。

    返回 (kept_fee, kept_hashes)；预算内无解返回 None。
    """
    txs = req["transactions"]
    limit = req["isolationLimit"]
    baseline = decision.order_transactions(txs)
    n = len(baseline)
    best = None
    for r in range(max(0, n - limit), n + 1):
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
    if best is None:
        return None
    return -best[0][0], best[1]


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, limit):
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
        return request(f"g{n}_{seed}_{limit}", txs, limit=limit)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 10):
            for seed in range(8):
                for limit in (0, 1, 2, n):
                    raw = self._bundle(n, seed, limit)
                    req = bounded.parse_request(raw)
                    ref = _reference_bounded(req)
                    res, err = bounded.process(raw)
                    self.assertIsNone(err)
                    if ref is None:
                        self.assertFalse(res["feasible"])
                        self.assertEqual(res["selectedOrder"], [])
                        self.assertEqual(res["removed"], [])
                        self.assertEqual(res["keptFee"], 0)
                        self.assertEqual(res["removedFee"], 0)
                    else:
                        kept_fee, kept = ref
                        self.assertTrue(res["feasible"])
                        self.assertEqual(res["selectedOrder"], kept)
                        self.assertEqual(res["keptFee"], kept_fee)
                        self.assertEqual(
                            res["removedFee"],
                            sum(t["fee"] for t in req["transactions"])
                            - kept_fee,
                        )
                        self.assertLessEqual(len(res["removed"]), limit)


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

    def test_missing_limit(self):
        raw = json.dumps({
            "id": "e",
            "transactions": [tx("a", "A", 0, 1)],
            "market": {"prices": {"TKN": 100}},
            "basePrice": 100,
            "maxSlippage": 0.5,
            "rollbackLimit": 1,
        })
        self.assert_error(raw, "BAD_ISOLATION_LIMIT")

    def test_bool_limit(self):
        for value in (True, False):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_negative_limit(self):
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], limit=-1),
            "BAD_ISOLATION_LIMIT",
        )

    def test_non_integer_limit(self):
        for value in (1.5, "1", None, [1], {"x": 1}):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_zero_limit_valid(self):
        res, err = bounded.process(request("e", [tx("a", "A", 0, 1)], limit=0))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_existing_errors_take_priority(self):
        # 既有校验全部优先于 BAD_ISOLATION_LIMIT
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

    def test_bad_json_schema(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli([], request("c", [tx("a", "A", 0, 1)]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], ["a"])
        self.assertTrue(res["feasible"])

    def test_module_entry_infeasible_exit0_no_stderr(self):
        proc = run_cli([], request("c", SANDWICH_TXS, limit=0).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(len(res["evidence"]), 1)

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_module_entry_bad_limit_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        del raw["isolationLimit"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ISOLATION_LIMIT\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

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
                f.write(request("file", SANDWICH_TXS, limit=1))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["selectedOrder"], ["front", "victim"])
            self.assertTrue(res["feasible"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, limit=1).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
