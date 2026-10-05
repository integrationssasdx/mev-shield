"""mev_shield.mitigation 行为验证（标准库 unittest，运行后删除亦可）。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import core
from mev_shield import decision
from mev_shield import mitigation


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, market=None, base=100, slip=1, rb=1, policy=None):
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


def plan(raw):
    return mitigation.process(raw)[0]


class TestPlan(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = mitigation.process(request("b", [tx("a", "A", 0, 1)]))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed",
             "keptFee", "removedFee", "evidence"],
        )
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["evidence"], [])

    def test_no_sandwich_keeps_all(self):
        txs = [
            tx("h2", "A", 0, 7),
            tx("h1", "B", 0, 9),
            tx("h0", "C", 0, 9),
        ]
        res = plan(request("b1", txs))
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["evidence"], [])

    def test_simple_sandwich_removes_min_fee(self):
        # a(A,买,100) -> v(B,买,110) -> d(A,卖,105)：基线即输入顺序
        txs = [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v", "B", 0, 10, side="buy", price=110),
            tx("d", "A", 1, 1, side="sell", price=105),
        ]
        res = plan(request("sw", txs))
        # 移除任意单笔即合法；d fee 最低
        self.assertEqual(res["selectedOrder"], ["a", "v"])
        self.assertEqual(res["removed"], [
            {"hash": "d", "at": 2, "reason": "SANDWICH_REMOVED"}])
        self.assertEqual(res["keptFee"], 110)
        self.assertEqual(res["removedFee"], 1)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["evidence"][0]["front"], "a")
        self.assertEqual(res["evidence"][0]["victim"], "v")
        self.assertEqual(res["evidence"][0]["back"], "d")

    def test_remove_attacker_when_cheaper(self):
        # victim fee 高于攻击腿：移除最便宜的攻击腿
        txs = [
            tx("a", "A", 0, 1, side="buy", price=100),
            tx("v", "B", 0, 100, side="buy", price=110),
            tx("d", "A", 1, 50, side="sell", price=105),
        ]
        res = plan(request("sw2", txs))
        self.assertEqual(res["selectedOrder"], ["v", "d"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["a"])
        self.assertEqual(res["removed"][0]["at"], 0)
        self.assertEqual(res["keptFee"], 150)
        self.assertEqual(res["removedFee"], 1)

    def test_overlapping_sandwiches_shared_leg(self):
        # 攻击者 A 三腿 a(买)->v1(买, B)->d(卖)->v2(卖, C)->e(买)，
        # 两个相邻夹子共享 d：移除 d 一笔同时化解，fee 总和最高。
        txs = [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v1", "B", 0, 90, side="buy", price=110),
            tx("d", "A", 1, 80, side="sell", price=105),
            tx("v2", "C", 0, 70, side="sell", price=100),
            tx("e", "A", 2, 60, side="buy", price=103),
        ]
        res = plan(request("ov", txs))
        self.assertEqual(len(res["evidence"]), 2)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["evidence"][1]["at"], [2, 3, 4])
        self.assertEqual(res["selectedOrder"], ["a", "v1", "v2", "e"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["d"])
        self.assertEqual(res["keptFee"], 320)
        self.assertEqual(res["removedFee"], 80)
        # 保留后重新相邻：a,v1,v2（买买买）与 v1,v2,e（首尾不同 from）
        # 均不构成夹子；A 的 nonce 0 -> 2 严格递增（允许跳号）

    def test_readjacency_trap_global_optimum(self):
        # p0(买,A,100) p1(买,B,110) p2(卖,A,109) p3(卖,A,105)
        # 唯一基线夹子 (p0,p1,p2)；最便宜的单腿移除 p2 会使 p0,p1,p3
        # 重新相邻并仍为夹子，故 p2 单独不合法；全局最优为移除 p1。
        txs = [
            tx("p0", "A", 0, 100, side="buy", price=100),
            tx("p1", "B", 0, 10, side="buy", price=110),
            tx("p2", "A", 1, 8, side="sell", price=109),
            tx("p3", "A", 2, 7, side="sell", price=105),
        ]
        res = plan(request("rj", txs))
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["selectedOrder"], ["p0", "p2", "p3"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["p1"])
        self.assertEqual(res["keptFee"], 115)
        self.assertEqual(res["removedFee"], 10)

    def test_iterated_greedy_still_worse(self):
        # 迭代贪心：先移除 p2(8)，再在新夹子 (p0,p1,p3) 中移除最便宜的
        # p3(7)，共移除 15；全局移除 p1(10) 更优（上一例的加强断言）。
        txs = [
            tx("p0", "A", 0, 100, side="buy", price=100),
            tx("p1", "B", 0, 10, side="buy", price=110),
            tx("p2", "A", 1, 8, side="sell", price=109),
            tx("p3", "A", 2, 7, side="sell", price=105),
        ]
        res = plan(request("rj2", txs))
        self.assertLess(res["removedFee"], 8 + 7)

    def test_nonce_constraint_on_kept(self):
        # 基线顺序使 A 的高 nonce 在前；不移除 a1 则保留序列 nonce 不递增。
        # 夹子与 nonce 同时存在：a1(victim 角色) 不能与 a0 同时保留。
        txs_input = [
            tx("a0", "A", 0, 1, side="buy", price=100),
            tx("zzz", "B", 0, 50, side="buy", price=110),
            tx("a1", "A", 1, 100, side="sell", price=105),
        ]
        # 基线 fee 顺序：a1(100), zzz(50), a0(1)
        # (a1 卖, zzz 买) 方向不一致，不构成夹子；但保留全部时
        # A 的 nonce 1 在 0 前 -> 不合法，必须移除 a0 或 a1。
        res = plan(request("nc", txs_input))
        self.assertEqual(res["baselineOrder"], ["a1", "zzz", "a0"])
        self.assertEqual(res["selectedOrder"], ["a1", "zzz"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["a0"])
        self.assertEqual(res["removed"][0]["at"], 0)
        self.assertEqual(res["keptFee"], 150)

    def test_removed_tie_lexicographic_by_input_position(self):
        # 三笔 fee 相同，基线顺序由 hash 决定为 ha,hv,hz（恰为夹子）；
        # 移除任意单笔均合法且 fee 相同 -> 取按输入位置 hash 序列最小者。
        txs = [
            tx("ha", "A", 0, 5, side="buy", price=100),
            tx("hv", "B", 0, 5, side="buy", price=110),
            tx("hz", "A", 1, 5, side="sell", price=105),
        ]
        res = plan(request("tie", txs))
        self.assertEqual(res["baselineOrder"], ["ha", "hv", "hz"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["ha"])

    def test_removed_ordered_by_input_position(self):
        # 输入顺序与基线顺序不同；removed 必须按输入 at 排列
        txs_input = [
            tx("v", "B", 0, 10, side="buy", price=110),     # at 0
            tx("d", "A", 1, 1, side="sell", price=105),     # at 1
            tx("a", "A", 0, 100, side="buy", price=100),    # at 2
        ]
        res = plan(request("io", txs_input))
        self.assertEqual(res["baselineOrder"], ["a", "v", "d"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["d"])
        self.assertEqual(res["removed"][0]["at"], 1)

    def test_evidence_sorted_by_start_then_victim(self):
        txs = [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v1", "B", 0, 90, side="buy", price=110),
            tx("d", "A", 1, 1, side="sell", price=105),
        ]
        res = plan(request("ev", txs))
        starts = [e["at"][0] for e in res["evidence"]]
        self.assertEqual(starts, sorted(starts))

    def test_non_adjacent_structural_triple_is_legal(self):
        # 存在非相邻的结构性三元组，但相邻判定无夹子：保留全部
        txs = [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("x", "C", 0, 95, side="sell", price=100),
            tx("v", "B", 0, 90, side="buy", price=110),
            tx("d", "A", 1, 80, side="sell", price=105),
        ]
        # 相邻三元组：(a,x,v) 买卖买不成立；(x,v,d) 首尾不同 from
        res = plan(request("na", txs))
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["selectedOrder"], ["a", "x", "v", "d"])
        self.assertEqual(res["removed"], [])

    def test_fee_totals(self):
        txs = [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v", "B", 0, 30, side="buy", price=110),
            tx("d", "A", 1, 20, side="sell", price=105),
        ]
        res = plan(request("sum", txs))
        self.assertEqual(res["keptFee"] + res["removedFee"], 150)

    def test_deterministic_bytes(self):
        raw = request("det", [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v", "B", 0, 30, side="buy", price=110),
            tx("d", "A", 1, 20, side="sell", price=105),
        ])
        out1 = mitigation.serialize(mitigation.process(raw)[0])
        out2 = mitigation.serialize(mitigation.process(raw)[0])
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith("\n"))
        self.assertNotIn(" ", out1.strip())


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

    def test_empty_bundle(self):
        self.assert_error(request("e", []), "EMPTY_BUNDLE")

    def test_unidentified(self):
        self.assert_error(request("e", [tx("", "A", 0, 1)]),
                          "UNIDENTIFIED_TRANSACTION")

    def test_duplicate(self):
        self.assert_error(request("e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2)]),
                          "DUPLICATE_TRANSACTION")

    def test_ordering_conflict(self):
        self.assert_error(request("e", [tx("a0", "A", 0, 1), tx("a2", "A", 2, 1)]),
                          "ORDERING_CONFLICT")

    def test_missing_market(self):
        raw = request("e", [tx("a", "A", 0, 1)])
        data = json.loads(raw)
        del data["market"]
        self.assert_error(json.dumps(data), "MISSING_MARKET_CONTEXT")

    def test_bad_json_schema_policy(self):
        res, err = mitigation.process(b"{bad")
        self.assertEqual(err, core.BAD_JSON)
        self.assertEqual(res["id"], "")
        res, err = mitigation.process(json.dumps([1]))
        self.assertEqual(err, core.BAD_SCHEMA)
        self.assertEqual(res["id"], "")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], policy="x"),
                          core.BAD_POLICY)

    def test_error_priority_matches_decision(self):
        # 与统一入口相同的固定优先级
        self.assert_error(request("e", [], base=0, slip=2), "EMPTY_BUNDLE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=0, slip=2, rb=2),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], slip=2, rb=2),
                          "INVALID_RISK_LIMIT")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli([], request("cli", [tx("a", "A", 0, 1)]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["id"], "cli")
        self.assertEqual(res["selectedOrder"], ["a"])

    def test_sandwich_exit_zero(self):
        txs = [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v", "B", 0, 10, side="buy", price=110),
            tx("d", "A", 1, 1, side="sell", price=105),
        ]
        proc = run_cli([], request("cli2", txs).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["removed"][0]["reason"], "SANDWICH_REMOVED")

    def test_input_error_exit_2(self):
        proc = run_cli([], request("cli3", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["keptFee"], 0)

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")

    def test_only_input_output_flags(self):
        proc = run_cli(["--verbose"], b"")
        self.assertEqual(proc.returncode, 2)

    def test_input_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", [tx("a", "A", 0, 1)]))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, b"")
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["id"], "file")

    def test_byte_identical(self):
        raw = request("same", [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v", "B", 0, 10, side="buy", price=110),
            tx("d", "A", 1, 1, side="sell", price=105),
        ]).encode()
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


class TestOldEntriesUnchanged(unittest.TestCase):
    def test_decision_still_works(self):
        raw = request("d", [
            tx("a", "A", 0, 100, side="buy", price=100),
            tx("v", "B", 0, 10, side="buy", price=110),
            tx("d", "A", 1, 1, side="sell", price=105),
        ])
        res, err = decision.process(raw)
        self.assertIsNone(err)
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["SANDWICH_DETECTED"])

    def test_baseline_entry_still_works(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        payload = json.dumps({
            "id": "old",
            "transactions": [
                {"hash": "a", "from": "A", "nonce": 0, "fee": 1,
                 "token": "T", "side": "buy", "sim": "success"},
            ],
        }).encode("utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "mev_shield"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["order"], ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
