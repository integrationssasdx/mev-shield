"""mev_shield.guarded 统一隔离重排行为验证（标准库 unittest）。"""

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


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, iso=0, moves=0, market=None, base=100, slip=0.5,
            rb=1, policy=None, slippage_mode=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "isolationLimit": iso,
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

FIXED_KEYS = [
    "id", "baselineOrder", "selectedOrder", "removed", "moved",
    "keptFee", "removedFee", "movedCount", "displacement",
    "requiredMoves", "evidence", "blockers", "result", "feasible",
    "isolationLimit", "maxMoves",
]


class TestGuardedPlan(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = guarded.process(
            request("g", [tx("a", "A", 0, 1)], iso=0, moves=0))
        self.assertIsNone(err)
        self.assertEqual(list(res.keys()), FIXED_KEYS)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 0)

    def test_no_risk_keeps_baseline_zero_budgets(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 9), tx("h0", "C", 0, 9)]
        res, err = guarded.process(request("n", txs, iso=0, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
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

    def test_isolation_zero_moves(self):
        # 预算允许移除后置腿，保留集合基线即安全：0 移动
        res, err = guarded.process(request("i", SANDWICH_TXS, iso=1, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])
        self.assertEqual(
            res["removed"],
            [{"hash": "back", "at": 2, "reason": "RISK_REMOVED"}],
        )
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["keptFee"], 50)
        self.assertEqual(res["removedFee"], 10)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])

    def test_reorder_only(self):
        # 不移除：交换 victim/back 消除夹子，2 笔位置变化
        res, err = guarded.process(request("r", SANDWICH_TXS, iso=0, moves=2))
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

    def test_move_limit_exceeded(self):
        res, err = guarded.process(request("m", SANDWICH_TXS, iso=0, moves=1))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["requiredMoves"], 2)
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        # 基线证据与 blockers 仍完整
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(res["maxMoves"], 1)

    def test_isolation_limit_exceeded(self):
        # 预算 0 且无夹子可在基线修复，但可重排（需 2 移动）：
        # 隔离可行（存在合法集合），故为 MOVE_LIMIT_EXCEEDED 而非隔离超限
        res, err = guarded.process(request("m0", SANDWICH_TXS, iso=0, moves=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertEqual(res["requiredMoves"], 2)

    def test_isolation_exceeded_when_no_legal_subset(self):
        # 超滑点交易不可保留，预算 0 无法移除：无合法集合
        txs = [tx("bad", "B", 0, 50, price=200)]
        res, err = guarded.process(
            request("x", txs, iso=0, moves=0, slip=0.5))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "ISOLATION_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["moved"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["displacement"], 0)
        self.assertEqual(res["blockers"], ["SLIPPAGE_EXCEEDED"])

    def test_remove_all_empty_kept_is_safe(self):
        txs = [tx("r1", "A", 0, 5, sim="revert")]
        res, err = guarded.process(
            request("z", txs, iso=1, moves=0, rb=0))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(
            res["removed"],
            [{"hash": "r1", "at": 0, "reason": "RISK_REMOVED"}],
        )
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 5)

    def test_rollback_ratio_blocks_full_keep(self):
        # 预算 0：必须保留全部，revert 占比 1/2 > 0.3 且夹子也存在；
        # 无可保留方案 -> ISOLATION_LIMIT_EXCEEDED
        txs = [
            tx("r1", "A", 0, 5, sim="revert"),
            tx("s1", "B", 0, 50),
        ]
        res, err = guarded.process(
            request("rb0", txs, iso=0, moves=0, rb=0.3))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "ISOLATION_LIMIT_EXCEEDED")
        self.assertEqual(res["requiredMoves"], 0)
        self.assertIn("ROLLBACK_LIMIT_EXCEEDED", res["blockers"])
        # 预算 1：移除 revert，保留 s1，0 移动
        res, err = guarded.process(
            request("rb1", txs, iso=1, moves=0, rb=0.3))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], ["s1"])
        self.assertEqual(
            [e["hash"] for e in res["removed"]], ["r1"])

    def test_market_mode_missing_reference_must_remove(self):
        # 无参考价的 nor 排基线末位：移除它是后缀删除，保留笔不位移
        txs = [
            tx("ok", "A", 0, 40, token="TKN", price=100),
            tx("nor", "B", 0, 10, token="OTHER", price=100),
        ]
        market = {"prices": {"TKN": 100}}
        res, err = guarded.process(
            request("mm", txs, iso=1, moves=0, market=market,
                    slippage_mode="market"))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], ["ok"])
        self.assertEqual([e["hash"] for e in res["removed"]], ["nor"])
        self.assertEqual(res["movedCount"], 0)
        self.assertEqual(res["requiredMoves"], 0)
        self.assertEqual(res["blockers"], ["PRICE_CONTEXT_MISSING"])

    def test_removing_early_tx_shifts_survivors(self):
        # 无参考价的 nor 排基线首位：移除后保留笔从位置 1 压到 0，
        # 相对完整基线算 1 次位置变化，故 moves=0 超限、moves=1 可行
        txs = [
            tx("ok", "A", 0, 10, token="TKN", price=100),
            tx("nor", "B", 0, 40, token="OTHER", price=100),
        ]
        market = {"prices": {"TKN": 100}}
        res, err = guarded.process(
            request("ms0", txs, iso=1, moves=0, market=market,
                    slippage_mode="market"))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertEqual(res["requiredMoves"], 1)
        res, err = guarded.process(
            request("ms1", txs, iso=1, moves=1, market=market,
                    slippage_mode="market"))
        self.assertIsNone(err)
        self.assertEqual(res["result"], "OK")
        self.assertEqual(res["selectedOrder"], ["ok"])
        self.assertEqual(
            res["moved"],
            [{"hash": "ok", "from": 1, "to": 0, "reason": "REORDERED"}],
        )
        self.assertEqual(res["movedCount"], 1)
        self.assertEqual(res["displacement"], 1)

    def test_blockers_fixed_order_dedup(self):
        txs = [
            tx("c0", "C", 0, 5),
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
            tx("c1", "C", 1, 35),
        ]
        res, err = guarded.process(request("bo", txs, iso=0, moves=0))
        # c0/c1 在基线倒序但可重排；夹子需重排，moves=0 超限
        self.assertIsNone(err)
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertEqual(
            res["blockers"], ["SANDWICH_DETECTED", "NONCE_ORDER_VIOLATION"]
        )

    def test_required_moves_independent_of_max_moves(self):
        # 同一输入只改 maxMoves：requiredMoves 恒为最少值
        a = guarded.process(request("s", SANDWICH_TXS, iso=0, moves=0))[0]
        b = guarded.process(request("s", SANDWICH_TXS, iso=0, moves=1))[0]
        c = guarded.process(request("s", SANDWICH_TXS, iso=0, moves=9))[0]
        self.assertEqual(a["requiredMoves"], 2)
        self.assertEqual(b["requiredMoves"], 2)
        self.assertEqual(c["requiredMoves"], 2)

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, iso=1, moves=2)
        self.assertEqual(
            guarded.serialize(guarded.process(raw)[0]),
            guarded.serialize(guarded.process(raw)[0]),
        )
        out = guarded.serialize(guarded.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_plan(req):
    """独立暴力参考：枚举预算内保留子集 × 全部合法排列。

    返回 (result, payload)；payload 为 OK 方案的 dict 或 requiredMoves。
    """
    txs = req["transactions"]
    iso = req["isolationLimit"]
    mv = req["maxMoves"]
    baseline = decision.order_transactions(txs)
    n = len(baseline)
    baseline_pos = {t["hash"]: i for i, t in enumerate(baseline)}
    input_pos = {t["hash"]: i for i, t in enumerate(txs)}
    keepable = decision_mod_keepable(req)
    rb = req["rollbackLimit"]

    all_plans = []   # (key, kept_hashes, order_hashes, moves, disp, fee)
    min_moves = None
    for r in range(max(0, n - iso), n + 1):
        for combo in combinations(range(n), r):
            selected = [baseline[i] for i in combo]
            if any(t["hash"] not in keepable for t in selected):
                continue
            if selected:
                reverts = sum(1 for t in selected if t["sim"] == "revert")
                if reverts / len(selected) > rb:
                    continue
            kept_set = {t["hash"] for t in selected}
            removed_seq = [t["hash"] for t in txs if t["hash"] not in kept_set]
            fee = sum(t["fee"] for t in selected)
            # 全部排列中筛 nonce 合法且无夹子
            for perm in permutations(selected):
                perm = list(perm)
                if not decision.nonce_order_satisfied(perm):
                    continue
                if decision.detect_sandwich_evidence(perm):
                    continue
                moves = sum(
                    1 for p, t in enumerate(perm)
                    if baseline_pos[t["hash"]] != p
                )
                disp = sum(
                    abs(baseline_pos[t["hash"]] - p)
                    for p, t in enumerate(perm)
                )
                seq = tuple(input_pos[t["hash"]] for t in perm)
                if min_moves is None or moves < min_moves:
                    min_moves = moves
                key = (-fee, -len(perm), moves, disp, removed_seq, seq)
                all_plans.append(
                    (key, sorted(kept_set), [t["hash"] for t in perm],
                     moves, disp, fee)
                )

    if min_moves is None:
        return guarded.ISOLATION_LIMIT_EXCEEDED, None
    feasible = [p for p in all_plans if p[3] <= mv]
    if not feasible:
        return guarded.MOVE_LIMIT_EXCEEDED, min_moves
    best = min(feasible, key=lambda p: p[0])
    return guarded.OK, best


def decision_mod_keepable(req):
    # 复刻 riskplan._keepable 的独立实现
    prices = req["market"]["prices"]
    base = req["basePrice"]
    limit = req["maxSlippage"]
    market_mode = req["slippageMode"] == decision.SLIPPAGE_MODE_MARKET
    ok = set()
    for t in req["transactions"]:
        if market_mode:
            ref = prices.get(t["token"])
            if ref is None:
                continue
        else:
            ref = base
            if t["token"] not in prices:
                continue
        if abs(t["price"] - ref) / ref > limit:
            continue
        ok.add(t["hash"])
    return ok


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, iso, mv, rb=1, slip=0.5):
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
        return request(f"g{n}_{seed}_{iso}_{mv}", txs, iso=iso, moves=mv,
                       rb=rb, slip=slip)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 8):
            for seed in range(6):
                for iso in (0, 1, n):
                    for mv in (0, 1, n):
                        raw = self._bundle(n, seed, iso, mv)
                        req = guarded.parse_request(raw)
                        ref_result, ref_payload = _reference_plan(req)
                        res, err = guarded.process(raw)
                        self.assertIsNone(err)
                        self.assertEqual(res["result"], ref_result)
                        if ref_result == guarded.ISOLATION_LIMIT_EXCEEDED:
                            self.assertFalse(res["feasible"])
                            self.assertEqual(res["requiredMoves"], 0)
                            self.assertEqual(res["selectedOrder"], [])
                            continue
                        if ref_result == guarded.MOVE_LIMIT_EXCEEDED:
                            self.assertFalse(res["feasible"])
                            self.assertEqual(res["requiredMoves"], ref_payload)
                            self.assertEqual(res["selectedOrder"], [])
                            self.assertEqual(res["movedCount"], 0)
                            continue
                        # OK：逐字段比对参考最优
                        key, kept, order, moves, disp, fee = ref_payload
                        self.assertTrue(res["feasible"])
                        self.assertEqual(res["selectedOrder"], order)
                        self.assertEqual(sorted(
                            res["selectedOrder"]
                            + [e["hash"] for e in res["removed"]]
                        ), sorted(t["hash"] for t in req["transactions"]))
                        self.assertEqual(res["movedCount"], moves)
                        self.assertEqual(res["displacement"], disp)
                        self.assertEqual(res["keptFee"], fee)
                        self.assertEqual(
                            res["removedFee"],
                            sum(t["fee"] for t in req["transactions"]) - fee,
                        )
                        # moved 与 selectedOrder 一致且按最终位置升序
                        self.assertEqual(
                            [m["hash"] for m in res["moved"]],
                            [h for p, h in enumerate(order)
                             if res["baselineOrder"].index(h) != p],
                        )
                        self.assertEqual(
                            [m["to"] for m in res["moved"]],
                            sorted(m["to"] for m in res["moved"]),
                        )


class TestValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = guarded.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        for key in ("baselineOrder", "selectedOrder", "removed", "moved",
                    "evidence", "blockers"):
            self.assertEqual(res[key], [])
        for key in ("keptFee", "removedFee", "movedCount", "displacement",
                    "requiredMoves", "isolationLimit", "maxMoves"):
            self.assertEqual(res[key], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["result"], "INPUT_ERROR")

    def test_missing_both(self):
        raw = json.dumps({
            "id": "e",
            "transactions": [tx("a", "A", 0, 1)],
            "market": {"prices": {"TKN": 100}},
            "basePrice": 100,
            "maxSlippage": 0.5,
            "rollbackLimit": 1,
        })
        self.assert_error(raw, "BAD_ISOLATION_LIMIT")

    def test_missing_moves(self):
        raw = json.loads(request("e", [tx("a", "A", 0, 1)], iso=0, moves=0))
        del raw["maxMoves"]
        self.assert_error(json.dumps(raw), "BAD_MOVE_LIMIT")

    def test_bad_isolation_first(self):
        raw = json.loads(request("e", [tx("a", "A", 0, 1)], iso=0, moves=0))
        raw["isolationLimit"] = -1
        raw["maxMoves"] = True
        self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_bool_values(self):
        for key, code in (("isolationLimit", "BAD_ISOLATION_LIMIT"),
                          ("maxMoves", "BAD_MOVE_LIMIT")):
            for value in (True, False):
                raw = json.loads(
                    request("e", [tx("a", "A", 0, 1)], iso=0, moves=0))
                raw[key] = value
                self.assert_error(json.dumps(raw), code)

    def test_negative_and_non_integer(self):
        for key, code in (("isolationLimit", "BAD_ISOLATION_LIMIT"),
                          ("maxMoves", "BAD_MOVE_LIMIT")):
            for value in (-1, 1.5, "1", None, [1], {"x": 1}):
                raw = json.loads(
                    request("e", [tx("a", "A", 0, 1)], iso=0, moves=0))
                raw[key] = value
                self.assert_error(json.dumps(raw), code)

    def test_old_errors_take_priority(self):
        self.assert_error(request("e", [], iso=0, moves=0), "EMPTY_BUNDLE")
        self.assert_error(
            request("e", [tx("", "A", 0, 1)], iso=0, moves=0),
            "UNIDENTIFIED_TRANSACTION")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2)],
                    iso=0, moves=0),
            "DUPLICATE_TRANSACTION")
        self.assert_error(
            request("e", [tx("a0", "A", 0, 1), tx("a2", "A", 2, 1)],
                    iso=0, moves=0),
            "ORDERING_CONFLICT")
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        self.assert_error(
            request("e", [bad], iso=0, moves=0), "MISSING_MARKET_CONTEXT")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], iso=0, moves=0, base=0),
            "INVALID_PRICE_BASE")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], iso=0, moves=0, slip=2),
            "INVALID_RISK_LIMIT")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], iso=0, moves=0, rb=2),
            "INVALID_ROLLBACK_LIMIT")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], iso=0, moves=0, policy="x"),
            "BAD_POLICY")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], iso=0, moves=0,
                    slippage_mode="zzz"),
            "BAD_SLIPPAGE_MODE")

    def test_bad_json_schema(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")


class TestCli(unittest.TestCase):
    def test_ok_exit0_no_stderr(self):
        proc = run_cli([], request("c", [tx("a", "A", 0, 1)],
                                   iso=0, moves=0).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "OK")
        self.assertTrue(res["feasible"])

    def test_infeasible_exit0_no_stderr(self):
        proc = run_cli([], request("c", SANDWICH_TXS,
                                   iso=0, moves=1).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "MOVE_LIMIT_EXCEEDED")
        self.assertFalse(res["feasible"])
        self.assertEqual(res["requiredMoves"], 2)

    def test_input_error_exit2(self):
        proc = run_cli([], request("c", [], iso=0, moves=0).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")

    def test_bad_isolation_limit_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)],
                                 iso=0, moves=0))
        del raw["isolationLimit"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.decode("utf-8"), "BAD_ISOLATION_LIMIT\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["result"], "INPUT_ERROR")
        self.assertEqual(res["isolationLimit"], 0)

    def test_bad_move_limit_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)],
                                 iso=0, moves=0))
        del raw["maxMoves"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_MOVE_LIMIT\n")

    def test_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")

    def test_input_io(self):
        proc = run_cli(["--input", "/nonexistent/x.json"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "INPUT_IO\n")

    def test_input_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", SANDWICH_TXS, iso=1, moves=0))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["selectedOrder"], ["front", "victim"])
            self.assertTrue(res["feasible"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, iso=1, moves=2).encode("utf-8")
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
