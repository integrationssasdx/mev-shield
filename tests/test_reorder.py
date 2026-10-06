"""mev_shield.reorder 安全重排计划行为验证（标准库 unittest）。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import permutations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import decision
from mev_shield import reorder


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, moves=0, market=None, base=100, slip=0.5, rb=1,
            policy=None, slippage_mode=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "maxMoves": moves,
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
        [sys.executable, "-m", "mev_shield.reorder"] + argv,
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


class TestReorderPlan(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = reorder.process(request("r", [tx("a", "A", 0, 1)], moves=0))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "safeOrder", "moved", "movedCount",
             "displacement", "evidence", "blockers", "result", "feasible",
             "maxMoves"],
        )
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["maxMoves"], 0)

    def test_no_sandwich_keeps_baseline(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, err = reorder.process(request("n", txs, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["safeOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["blockers"], [])

    def test_sandwich_reordered_minimal_moves(self):
        # 交换 victim 与 back 即可消除夹子：2 笔位置变化，位移和 2
        res, err = reorder.process(request("s", SANDWICH_TXS, moves=2))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(res["safeOrder"], ["front", "back", "victim"])
        self.assertEqual(
            res["moved"],
            [
                {"hash": "back", "from": 2, "to": 1, "reason": "REORDERED"},
                {"hash": "victim", "from": 1, "to": 2, "reason": "REORDERED"},
            ],
        )
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["displacement"], 2)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])

    def test_move_limit_exceeded_still_outputs_plan(self):
        # 最优方案需 2 笔变化，上限 1：超限但仍输出最优方案
        res, err = reorder.process(request("s", SANDWICH_TXS, moves=1))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["safeOrder"], ["front", "back", "victim"])
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["displacement"], 2)
        self.assertEqual(res["maxMoves"], 1)

    def test_nonce_violation_baseline_fixed_by_reorder(self):
        # 基线 fee 降序使同 from 的 nonce 逆序；重排恢复 nonce 升序
        txs = [tx("a1", "A", 1, 30), tx("a0", "A", 0, 20)]
        res, err = reorder.process(request("nv", txs, moves=2))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["a1", "a0"])
        self.assertEqual(res["safeOrder"], ["a0", "a1"])
        self.assertEqual(res["blockers"], ["NONCE_ORDER_VIOLATION"])
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["displacement"], 2)

    def test_blockers_both_codes_fixed_order(self):
        # 基线同时有夹子与 nonce 逆序：blockers 顺序固定
        # 基线 fee 降序为 x(买,n1) / victim(买) / y(卖,n0)：相邻三段成夹子，
        # 且 A 的 nonce 在基线上为 1、0 逆序
        txs = [
            tx("y", "A", 0, 10, side="sell", price=105),
            tx("x", "A", 1, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
        ]
        res, err = reorder.process(request("both", txs, moves=3))
        self.assertIsNone(err)
        self.assertEqual(
            res["blockers"], ["SANDWICH_DETECTED", "NONCE_ORDER_VIOLATION"])
        self.assertEqual(res["result"], "OK")
        # 安全顺序满足 nonce 递增且无夹子
        order = [t for h in res["safeOrder"] for t in txs if t["hash"] == h]
        self.assertTrue(decision.nonce_order_satisfied(order))
        self.assertEqual(decision.detect_sandwich_evidence(order), [])

    def test_slippage_and_rollback_do_not_affect_feasibility(self):
        # 滑点超限与回滚超限不进入 blockers，也不影响重排可行性
        txs = [tx("a", "A", 0, 1, sim="revert", price=200)]
        res, err = reorder.process(
            request("risk", txs, moves=0, slip=0.1, rb=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["blockers"], [])

    def test_safe_order_is_permutation(self):
        txs = [
            tx("f1", "A", 0, 50, price=100),
            tx("v1", "B", 0, 40, price=110),
            tx("mid", "A", 1, 30, side="sell", price=105),
            tx("v2", "C", 0, 20, side="sell", price=100),
            tx("b2", "A", 2, 10, price=102),
        ]
        res, err = reorder.process(request("perm", txs, moves=5))
        self.assertIsNone(err)
        self.assertEqual(sorted(res["safeOrder"]),
                         sorted(t["hash"] for t in txs))
        covered = sorted(e["hash"] for e in res["moved"])
        self.assertEqual(
            covered,
            sorted(h for h in res["safeOrder"]
                   if res["safeOrder"].index(h)
                   != res["baselineOrder"].index(h)),
        )

    def test_moved_sorted_by_final_position(self):
        res, err = reorder.process(request("s", SANDWICH_TXS, moves=3))
        self.assertIsNone(err)
        tos = [entry["to"] for entry in res["moved"]]
        self.assertEqual(tos, sorted(tos))

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, moves=2)
        self.assertEqual(
            reorder.serialize(reorder.process(raw)[0]),
            reorder.serialize(reorder.process(raw)[0]),
        )
        out = reorder.serialize(reorder.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_reorder(req):
    """独立参考实现：枚举全部排列，按相同三级目标选全局最优。

    返回 (moved_count, displacement, safe_order)；无合法顺序返回 None。
    """
    txs = req["transactions"]
    baseline = decision.order_transactions(txs)
    baseline_pos = {t["hash"]: at for at, t in enumerate(baseline)}
    n = len(txs)
    best = None
    for perm in permutations(range(n)):
        order = [txs[i] for i in perm]
        if not decision.nonce_order_satisfied(order):
            continue
        if decision.detect_sandwich_evidence(order):
            continue
        moved = sum(
            1 for at, t in enumerate(order)
            if baseline_pos[t["hash"]] != at
        )
        disp = sum(
            abs(baseline_pos[t["hash"]] - at) for at, t in enumerate(order)
        )
        key = (moved, disp, perm)
        if best is None or key < best[0]:
            best = (key, [t["hash"] for t in order])
    if best is None:
        return None
    (moved, disp, _perm), safe = best
    return moved, disp, safe


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, moves):
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
        return request(f"g{n}_{seed}_{moves}", txs, moves=moves)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 8):
            for seed in range(6):
                for moves in (0, 1, 2, n):
                    raw = self._bundle(n, seed, moves)
                    req = reorder.parse_request(raw)
                    ref = _reference_reorder(req)
                    res, err = reorder.process(raw)
                    self.assertIsNone(err)
                    if ref is None:
                        self.assertEqual(res["result"], "SAFE_ORDER_NOT_FOUND")
                        self.assertFalse(res["feasible"])
                        self.assertEqual(res["safeOrder"], [])
                        self.assertEqual(res["moved"], [])
                        self.assertEqual(res["movedCount"], 0)
                        self.assertEqual(res["displacement"], 0)
                    else:
                        moved, disp, safe = ref
                        self.assertEqual(res["safeOrder"], safe)
                        self.assertEqual(res["movedCount"], moved)
                        self.assertEqual(res["displacement"], disp)
                        if moved <= moves:
                            self.assertEqual(res["result"], "OK")
                            self.assertTrue(res["feasible"])
                        else:
                            self.assertEqual(
                                res["result"], "MOVE_LIMIT_EXCEEDED")
                            self.assertFalse(res["feasible"])
                    self.assertEqual(res["maxMoves"], moves)


class TestMaxMovesValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = reorder.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["safeOrder"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["result"], "INPUT_ERROR")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["maxMoves"], 0)

    def test_missing_limit(self):
        raw = json.dumps({
            "id": "e",
            "transactions": [tx("a", "A", 0, 1)],
            "market": {"prices": {"TKN": 100}},
            "basePrice": 100,
            "maxSlippage": 0.5,
            "rollbackLimit": 1,
        })
        self.assert_error(raw, "BAD_MOVE_LIMIT")

    def test_bool_limit(self):
        for value in (True, False):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["maxMoves"] = value
            self.assert_error(json.dumps(raw), "BAD_MOVE_LIMIT")

    def test_negative_limit(self):
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], moves=-1),
            "BAD_MOVE_LIMIT",
        )

    def test_non_integer_limit(self):
        for value in (1.5, "1", None, [1], {"x": 1}):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["maxMoves"] = value
            self.assert_error(json.dumps(raw), "BAD_MOVE_LIMIT")

    def test_zero_limit_valid(self):
        res, err = reorder.process(request("e", [tx("a", "A", 0, 1)], moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["maxMoves"], 0)

    def test_existing_errors_take_priority(self):
        # 既有校验全部优先于 BAD_MOVE_LIMIT
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
        self.assertEqual(res["safeOrder"], ["a"])
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])

    def test_module_entry_limit_exceeded_exit0_no_stderr(self):
        proc = run_cli([], request("c", SANDWICH_TXS, moves=0).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["safeOrder"], ["front", "back", "victim"])
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(len(res["evidence"]), 1)

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "INPUT_ERROR")
        self.assertEqual(res["safeOrder"], [])
        self.assertEqual(res["movedCount"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["maxMoves"], 0)

    def test_module_entry_bad_limit_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        del raw["maxMoves"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_MOVE_LIMIT\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "INPUT_ERROR")
        self.assertEqual(res["maxMoves"], 0)

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "INPUT_ERROR")
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
                f.write(request("file", SANDWICH_TXS, moves=2))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["safeOrder"], ["front", "back", "victim"])
            self.assertEqual(res["result"], "OK")

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, moves=2).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
