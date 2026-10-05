"""mev_shield.mitigation 最小隔离计划行为验证（标准库 unittest）。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import decision
from mev_shield import mitigation


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, market=None, base=100, slip=0.5, rb=1, policy=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
    }
    if policy is not None:
        data["policy"] = policy
    return json.dumps(data)


def run_cli(argv, stdin_bytes=b""):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "mev_shield.mitigation"] + argv,
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


class TestIsolate(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = mitigation.process(request("b", [tx("a", "A", 0, 1)]))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed",
             "keptFee", "removedFee", "evidence"],
        )
        self.assertEqual(res["removed"], [])

    def test_no_sandwich_keeps_all(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, _ = mitigation.process(request("n", txs))
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["evidence"], [])

    def test_isolate_cheapest_leg_global_optimum(self):
        # 单个夹子：移除 fee 最低的后置腿，保留 fee 总和最大
        res, _ = mitigation.process(request("s1", SANDWICH_TXS))
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])
        self.assertEqual(
            res["removed"],
            [{"hash": "back", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 50)
        self.assertEqual(res["removedFee"], 10)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["evidence"][0]["prices"], [100, 110, 105])

    def test_removed_listed_by_input_position(self):
        # 输入顺序 [back, victim, front]；removed 的 at 为输入位置
        txs = [
            tx("back", "A", 1, 10, side="sell", price=105),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("front", "A", 0, 30, side="buy", price=100),
        ]
        res, _ = mitigation.process(request("pos", txs))
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(
            res["removed"],
            [{"hash": "back", "at": 0, "reason": "SANDWICH_REMOVED"}],
        )

    def test_interlocking_shared_leg(self):
        # 两个夹子共用中间攻击腿 mid：移除一笔即同时消除两个夹子
        txs = [
            tx("f1", "A", 0, 50, price=100),
            tx("v1", "B", 0, 40, price=110),
            tx("mid", "A", 1, 30, side="sell", price=105),
            tx("v2", "C", 0, 20, side="sell", price=100),
            tx("b2", "A", 2, 10, price=102),
        ]
        res, _ = mitigation.process(request("lock", txs))
        self.assertEqual(
            [e["at"] for e in res["evidence"]], [[0, 1, 2], [2, 3, 4]]
        )
        self.assertEqual(res["selectedOrder"], ["f1", "v1", "v2", "b2"])
        self.assertEqual(
            res["removed"],
            [{"hash": "mid", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 120)
        self.assertEqual(res["removedFee"], 30)

    def test_nonce_constraint_forces_extra_removal(self):
        # 高 nonce 高 fee 的 X 排在基线首位；保留 f 必保留 b 才满足
        # 同发送者 nonce 严格递增，而 {f,v,b} 是夹子 -> f、b 均移除
        txs = [
            tx("X", "A", 2, 100),
            tx("f", "A", 0, 30, price=100),
            tx("v", "B", 0, 20, price=110),
            tx("b", "A", 1, 10, side="sell", price=105),
        ]
        res, _ = mitigation.process(request("nc", txs))
        self.assertEqual(res["selectedOrder"], ["X", "v"])
        self.assertEqual(
            [entry["hash"] for entry in res["removed"]], ["f", "b"]
        )
        self.assertEqual([entry["at"] for entry in res["removed"]], [1, 3])
        self.assertEqual(res["keptFee"], 120)
        self.assertEqual(res["removedFee"], 40)

    def test_lexicographic_tiebreak(self):
        # fee、笔数完全相同：三条腿 fee 均为 10，hash 排序恰为前/受/后；
        # 移除前置腿（"aa"）或后置腿（"zz"）等价，取移除 hash 序列更小者
        txs = [
            tx("zz", "A", 1, 10, side="sell", price=105),
            tx("mm", "B", 0, 10, side="buy", price=110),
            tx("aa", "A", 0, 10, side="buy", price=100),
        ]
        res, _ = mitigation.process(request("lex", txs))
        self.assertEqual(res["baselineOrder"], ["aa", "mm", "zz"])
        self.assertEqual(res["selectedOrder"], ["mm", "zz"])
        self.assertEqual(
            res["removed"],
            [{"hash": "aa", "at": 2, "reason": "SANDWICH_REMOVED"}],
        )

    def test_revert_leg_not_evidence_kept_all(self):
        txs = [
            tx("f", "A", 0, 30, sim="revert", price=100),
            tx("v", "B", 0, 20, price=110),
            tx("b", "A", 1, 10, side="sell", price=105),
        ]
        res, _ = mitigation.process(request("rv", txs))
        self.assertEqual(res["selectedOrder"], ["f", "v", "b"])
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["keptFee"], 60)

    def test_evidence_sorted_by_start(self):
        # 两个互不相交的夹子，证据按起始位置升序
        txs = [
            tx("f1", "A", 0, 100, price=100),
            tx("v1", "B", 0, 99, side="buy", price=110),
            tx("b1", "A", 1, 98, side="sell", price=105),
            tx("f2", "A", 2, 97, price=100),
            tx("v2", "C", 0, 96, side="buy", price=110),
            tx("b2", "A", 3, 95, side="sell", price=105),
        ]
        res, _ = mitigation.process(request("ev", txs))
        self.assertEqual(
            [entry["at"] for entry in res["evidence"]], [[0, 1, 2], [3, 4, 5]]
        )

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS)
        self.assertEqual(
            mitigation.serialize(mitigation.process(raw)[0]),
            mitigation.serialize(mitigation.process(raw)[0]),
        )
        out = mitigation.serialize(mitigation.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_isolate(req):
    """独立参考实现：枚举全部子集，按相同三级目标选全局最优。"""
    txs = req["transactions"]
    baseline = decision.order_transactions(txs)
    n = len(baseline)
    best = None
    for r in range(n + 1):
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
    return -best[0][0], best[1]


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
        return request(f"g{n}_{seed}", txs)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 10):
            for seed in range(12):
                raw = self._bundle(n, seed)
                req = decision.parse_request(raw)
                kept_fee, kept = _reference_isolate(req)
                res, err = mitigation.process(raw)
                self.assertIsNone(err)
                self.assertEqual(res["selectedOrder"], kept)
                self.assertEqual(res["keptFee"], kept_fee)
                self.assertEqual(
                    res["removedFee"],
                    sum(t["fee"] for t in req["transactions"]) - kept_fee,
                )


class TestInputErrors(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = mitigation.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)

    def test_error_priority_matches_decision(self):
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

    def test_bad_json_schema_policy(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], policy="x"),
                          "BAD_POLICY")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli([], request("c", [tx("a", "A", 0, 1)]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], ["a"])

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["keptFee"], 0)

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["baselineOrder"], [])

    def test_input_io(self):
        proc = run_cli(["--input", "/nonexistent/x.json"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "INPUT_IO\n")

    def test_input_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", SANDWICH_TXS))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["selectedOrder"], ["front", "victim"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
