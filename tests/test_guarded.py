"""mev_shield.guarded 双预算守卫计划行为验证（标准库 unittest）。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import combinations, permutations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import decision
from mev_shield import guarded
from mev_shield import riskplan


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, limit=0, moves=0, market=None, base=100, slip=0.5,
            rb=1, policy=None, slippage_mode=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "isolationLimit": limit,
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
        [sys.executable, "-m", "mev_shield.guarded"] + argv,
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


class TestGuardedPlan(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = guarded.process(
            request("g", [tx("a", "A", 0, 1)], limit=0, moves=0))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed", "moved",
             "keptFee", "removedFee", "movedCount", "displacement",
             "requiredMoves", "evidence", "blockers", "result", "feasible",
             "isolationLimit", "maxMoves"],
        )
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 0)

    def test_no_sandwich_keeps_all_zero_budgets(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, err = guarded.process(request("n", txs, limit=0, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["blockers"], [])

    def test_removal_within_isolation_budget(self):
        # 预算 1 足够移除 fee 最低的后置腿，无需任何位置变化
        res, err = guarded.process(
            request("s1", SANDWICH_TXS, limit=1, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])
        self.assertEqual(
            res["removed"],
            [{"hash": "back", "at": 2, "reason": "RISK_REMOVED"}],
        )
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 50)
        self.assertEqual(res["removedFee"], 10)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])

    def test_reorder_within_move_budget(self):
        # 移除预算 0：交换 victim 与 back 消除夹子，2 笔位置变化
        res, err = guarded.process(
            request("s2", SANDWICH_TXS, limit=0, moves=2))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], ["front", "back", "victim"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(
            res["moved"],
            [
                {"hash": "back", "from": 2, "to": 1, "reason": "REORDERED"},
                {"hash": "victim", "from": 1, "to": 2, "reason": "REORDERED"},
            ],
        )
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["displacement"], 2)
        self.assertEqual(res["requiredMoves"], 2)
        self.assertEqual(res["keptFee"], 60)
        self.assertEqual(res["removedFee"], 0)

    def test_fee_beats_moves_required_moves_independent(self):
        # 保留全部重排（fee 60、2 笔变化）优于移除后置腿（fee 50、0 笔
        # 变化）：requiredMoves 只计 isolationLimit 与合法性，仍为 0
        res, err = guarded.process(
            request("fm", SANDWICH_TXS, limit=1, moves=2))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], ["front", "back", "victim"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 60)
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["requiredMoves"], 0)

    def test_move_limit_exceeded(self):
        # 不可移除且最少需 2 笔变化，上限 1：超限，统计清零但留 requiredMoves
        res, err = guarded.process(
            request("ml", SANDWICH_TXS, limit=0, moves=1))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["front", "victim", "back"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["requiredMoves"], 2)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 1)

    def test_isolation_limit_exceeded(self):
        # 超滑点交易必须移除但预算为 0：无隔离集合，requiredMoves 为 0
        txs = [tx("bad", "B", 0, 50, price=200)]
        res, err = guarded.process(
            request("il", txs, limit=0, moves=3, slip=0.5))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "ISOLATION_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["blockers"], ["SLIPPAGE_EXCEEDED"])

    def test_empty_subset_legal(self):
        # 全部移除后空集合占比视为 0：预算覆盖全部笔数时恒可行
        txs = [tx("r1", "A", 0, 5, sim="revert")]
        res, err = guarded.process(
            request("rz", txs, limit=1, moves=0, rb=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(
            res["removed"],
            [{"hash": "r1", "at": 0, "reason": "RISK_REMOVED"}],
        )
        self.assertEqual(res["requiredMoves"], 0)

    def test_rollback_limit_on_kept_set(self):
        # 保留集合 revert 占比不得超过 rollbackLimit
        txs = [
            tx("r1", "A", 0, 5, sim="revert"),
            tx("s1", "B", 0, 50),
            tx("s2", "C", 0, 40),
        ]
        res, err = guarded.process(
            request("rl", txs, limit=1, moves=0, rb=0.3))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], ["s1", "s2"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["r1"])
        self.assertEqual(res["blockers"], ["ROLLBACK_LIMIT_EXCEEDED"])

    def test_market_mode_missing_reference_not_keepable(self):
        # market 口径：缺同 token 参考价的交易不可保留
        txs = [
            tx("ok", "A", 0, 10, token="TKN", price=100),
            tx("nor", "B", 0, 40, token="OTHER", price=100),
        ]
        market = {"prices": {"TKN": 100}}
        res, err = guarded.process(
            request("mm", txs, limit=1, moves=1, market=market,
                    slippage_mode="market"))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["nor", "ok"])
        self.assertEqual(res["selectedOrder"], ["ok"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["nor"])
        # 移除 nor 后 ok 相对基线前移一位：1 笔位置变化
        self.assertEqual(
            res["moved"],
            [{"hash": "ok", "from": 1, "to": 0, "reason": "REORDERED"}],
        )
        self.assertEqual(res["movedCount"], 1)
        self.assertEqual(res["requiredMoves"], 1)
        self.assertEqual(res["blockers"], ["PRICE_CONTEXT_MISSING"])

    def test_nonce_violation_fixed_by_reorder(self):
        # 基线 fee 降序使同 from 的 nonce 逆序；重排恢复 nonce 升序
        txs = [tx("a1", "A", 1, 30), tx("a0", "A", 0, 20)]
        res, err = guarded.process(request("nv", txs, limit=0, moves=2))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["a1", "a0"])
        self.assertEqual(res["selectedOrder"], ["a0", "a1"])
        self.assertEqual(res["blockers"], ["NONCE_ORDER_VIOLATION"])
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["displacement"], 2)
        self.assertEqual(res["requiredMoves"], 2)

    def test_combined_removal_and_reorder(self):
        # 移除预算 1、变化预算 2：保留全部并重排（fee 100）优于移除
        # 后置腿（fee 90）；requiredMoves 仍可取到 0
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
            tx("x", "C", 0, 40, side="buy", price=100),
        ]
        res, err = guarded.process(request("cb", txs, limit=1, moves=2))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["x", "front", "victim", "back"])
        # 同为 2 笔变化、位移和 2 的合法顺序中，最终输入下标序列
        # (0,3,1,2) 字典序最小
        self.assertEqual(res["selectedOrder"], ["front", "x", "victim", "back"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(
            res["moved"],
            [
                {"hash": "front", "from": 1, "to": 0, "reason": "REORDERED"},
                {"hash": "x", "from": 0, "to": 1, "reason": "REORDERED"},
            ],
        )
        self.assertEqual(res["movedCount"], 2)
        self.assertEqual(res["displacement"], 2)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["keptFee"], 100)
        self.assertEqual(res["removedFee"], 0)

    def test_removal_shifts_positions(self):
        # 三条腿 fee 均为 10：变化预算 0 时只能移除基线末尾的 zz；
        # 移除 aa 或 mm 会使后续交易相对基线前移，超出变化预算
        txs = [
            tx("zz", "A", 1, 10, side="sell", price=105),
            tx("mm", "B", 0, 10, side="buy", price=110),
            tx("aa", "A", 0, 10, side="buy", price=100),
        ]
        res, err = guarded.process(request("lex", txs, limit=1, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["baselineOrder"], ["aa", "mm", "zz"])
        self.assertEqual(res["selectedOrder"], ["aa", "mm"])
        self.assertEqual(
            res["removed"],
            [{"hash": "zz", "at": 0, "reason": "RISK_REMOVED"}],
        )
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["requiredMoves"], 0)

    def test_fee_partition_invariant(self):
        # feasible 时 selectedOrder 与 removed 不重不漏，fee 之和为总和
        txs = [
            tx("f1", "A", 0, 50, price=100),
            tx("v1", "B", 0, 40, price=110),
            tx("mid", "A", 1, 30, side="sell", price=105),
            tx("v2", "C", 0, 20, side="sell", price=100),
            tx("b2", "A", 2, 10, price=102),
        ]
        res, err = guarded.process(request("inv", txs, limit=2, moves=5))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        covered = sorted(res["selectedOrder"]
                         + [e["hash"] for e in res["removed"]])
        self.assertEqual(covered, sorted(t["hash"] for t in txs))
        self.assertEqual(res["keptFee"] + res["removedFee"], 150)
        # 最终顺序合法：nonce 严格递增且无夹子证据
        order = [t for h in res["selectedOrder"] for t in txs if t["hash"] == h]
        self.assertTrue(decision.nonce_order_satisfied(order))
        self.assertEqual(decision.detect_sandwich_evidence(order), [])

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, limit=1, moves=2)
        self.assertEqual(
            guarded.serialize(guarded.process(raw)[0]),
            guarded.serialize(guarded.process(raw)[0]),
        )
        out = guarded.serialize(guarded.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_guarded(req):
    """独立参考实现：枚举预算内全部子集与全部排列，按相同六级目标选全局最优。

    返回 (result, required_moves, plan)；plan 为 (kept_fee, kept_hashes,
    moved_count, displacement)，双预算内无解时 plan 为 None。
    """
    txs = req["transactions"]
    isolation_limit = req["isolationLimit"]
    max_moves = req["maxMoves"]
    baseline = decision.order_transactions(txs)
    baseline_pos = {t["hash"]: at for at, t in enumerate(baseline)}
    input_pos = {t["hash"]: at for at, t in enumerate(txs)}
    keepable = riskplan._keepable(req)
    rb_limit = req["rollbackLimit"]
    n = len(txs)
    required = None
    best = None
    for r in range(max(0, n - isolation_limit), n + 1):
        for combo in combinations(range(n), r):
            selected = [baseline[i] for i in combo]
            if any(t["hash"] not in keepable for t in selected):
                continue
            if selected:
                reverts = sum(1 for t in selected if t["sim"] == "revert")
                if reverts / len(selected) > rb_limit:
                    continue
            kept = {t["hash"] for t in selected}
            removed_seq = tuple(t["hash"] for t in txs if t["hash"] not in kept)
            for perm in permutations(selected):
                if not decision.nonce_order_satisfied(list(perm)):
                    continue
                if decision.detect_sandwich_evidence(list(perm)):
                    continue
                moved = sum(
                    1 for at, t in enumerate(perm)
                    if baseline_pos[t["hash"]] != at
                )
                disp = sum(
                    abs(baseline_pos[t["hash"]] - at)
                    for at, t in enumerate(perm)
                )
                if required is None or moved < required:
                    required = moved
                if moved > max_moves:
                    continue
                kept_fee = sum(t["fee"] for t in perm)
                seq = tuple(input_pos[t["hash"]] for t in perm)
                key = (-kept_fee, -r, moved, disp, removed_seq, seq)
                if best is None or key < best[0]:
                    best = (key, [t["hash"] for t in perm])
    if required is None:
        return "ISOLATION_LIMIT_EXCEEDED", 0, None
    if required > max_moves:
        return "MOVE_LIMIT_EXCEEDED", required, None
    (key, kept_hashes) = best
    return "OK", required, (-key[0], kept_hashes, key[2], key[3])


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, limit, moves, rb=1, slip=0.5):
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
            price = 90 + ((i * 17 + seed * 3) % 21)
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price))
        return request(f"g{n}_{seed}_{limit}_{moves}", txs, limit=limit,
                       moves=moves, rb=rb, slip=slip)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 7):
            for seed in range(5):
                for limit in (0, 1, n):
                    for moves in (0, 1, n):
                        for rb in (0.3, 1):
                            raw = self._bundle(n, seed, limit, moves, rb=rb)
                            req = guarded.parse_request(raw)
                            result, required, plan = _reference_guarded(req)
                            res, err = guarded.process(raw)
                            self.assertIsNone(err)
                            self.assertEqual(res["result"], result)
                            self.assertEqual(res["requiredMoves"], required)
                            self.assertEqual(
                                res["feasible"], result == "OK")
                            if plan is None:
                                self.assertEqual(res["selectedOrder"], [])
                                self.assertEqual(res["removed"], [])
                                self.assertEqual(res["moved"], [])
                                self.assertEqual(res["keptFee"], 0)
                                self.assertEqual(res["removedFee"], 0)
                                self.assertEqual(res["movedCount"], 0)
                                self.assertEqual(res["displacement"], 0)
                            else:
                                kept_fee, kept, moved, disp = plan
                                self.assertEqual(res["selectedOrder"], kept)
                                self.assertEqual(res["keptFee"], kept_fee)
                                self.assertEqual(res["movedCount"], moved)
                                self.assertEqual(res["displacement"], disp)
                                self.assertEqual(
                                    res["removedFee"],
                                    sum(t["fee"] for t in req["transactions"])
                                    - kept_fee,
                                )
                                self.assertLessEqual(
                                    len(res["removed"]), limit)
                                self.assertLessEqual(
                                    res["movedCount"], moves)
                            self.assertEqual(res["isolationLimit"], limit)
                            self.assertEqual(res["maxMoves"], moves)


class TestLimitValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = guarded.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["result"], "INPUT_ERROR")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 0)

    def test_missing_limits(self):
        base = {
            "id": "e",
            "transactions": [tx("a", "A", 0, 1)],
            "market": {"prices": {"TKN": 100}},
            "basePrice": 100,
            "maxSlippage": 0.5,
            "rollbackLimit": 1,
        }
        self.assert_error(json.dumps(base), "BAD_ISOLATION_LIMIT")
        with_isolation = dict(base, isolationLimit=0)
        self.assert_error(json.dumps(with_isolation), "BAD_MOVE_LIMIT")

    def test_bool_limits(self):
        for value in (True, False):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["maxMoves"] = value
            self.assert_error(json.dumps(raw), "BAD_MOVE_LIMIT")

    def test_negative_limits(self):
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], limit=-1),
            "BAD_ISOLATION_LIMIT",
        )
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], moves=-1),
            "BAD_MOVE_LIMIT",
        )

    def test_non_integer_limits(self):
        for value in (1.5, "1", None, [1], {"x": 1}):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["maxMoves"] = value
            self.assert_error(json.dumps(raw), "BAD_MOVE_LIMIT")

    def test_isolation_limit_checked_before_move_limit(self):
        # 两者均非法时唯一返回 BAD_ISOLATION_LIMIT
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], limit=-1, moves=-1),
            "BAD_ISOLATION_LIMIT",
        )

    def test_zero_limits_valid(self):
        res, err = guarded.process(
            request("e", [tx("a", "A", 0, 1)], limit=0, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 0)

    def test_existing_errors_take_priority(self):
        # 既有校验全部优先于 BAD_ISOLATION_LIMIT / BAD_MOVE_LIMIT
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
        proc = run_cli(
            [], request("c", [tx("a", "A", 0, 1)]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], ["a"])
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])

    def test_module_entry_exceeded_exit0_no_stderr(self):
        proc = run_cli([], request("c", SANDWICH_TXS, limit=0,
                                   moves=0).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["requiredMoves"], 2)
        self.assertEqual(len(res["evidence"]), 1)

    def test_module_entry_isolation_exceeded_exit0_no_stderr(self):
        bad = [tx("bad", "B", 0, 50, price=200)]
        proc = run_cli([], request("c", bad, limit=0, moves=0).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "ISOLATION_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["requiredMoves"], 0)

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "INPUT_ERROR")
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["moved"], [])
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 0)

    def test_module_entry_bad_limits_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        del raw["isolationLimit"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ISOLATION_LIMIT\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "INPUT_ERROR")
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        del raw["maxMoves"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_MOVE_LIMIT\n")

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
                f.write(request("file", SANDWICH_TXS, limit=1, moves=2))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["selectedOrder"], ["front", "back", "victim"])
            self.assertEqual(res["result"], "OK")

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, limit=1, moves=2).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
