"""mev_shield.decision 行为验证（标准库 unittest，运行后删除亦可）。"""

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


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
    }


def request(ident, txs, market=None, base=100, slip=0.5, rb=1, policy=None,
            slippage_mode=...):
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
    if slippage_mode is not ...:
        data["slippageMode"] = slippage_mode
    return json.dumps(data)


def run_cli(argv, stdin_bytes=b""):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "mev_shield.decision"] + argv,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )


class TestDecide(unittest.TestCase):
    def test_allow_order_covers_inputs(self):
        # 正常放行：fee 降序、hash 升序；最终顺序恰好覆盖输入交易
        txs = [
            tx("h2", "A", 0, 7),
            tx("h1", "B", 0, 9),
            tx("h0", "C", 0, 9),
        ]
        res, err = decision.process(request("b1", txs))
        self.assertIsNone(err)
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertTrue(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], [])
        self.assertEqual(res["finalOrder"], ["h0", "h1", "h2"])
        self.assertEqual(sorted(res["finalOrder"]), sorted(t["hash"] for t in txs))
        self.assertFalse(res["sandwich"]["detected"])
        self.assertEqual(res["involved"], [])
        basis = res["basis"]
        self.assertEqual(basis["txCount"], 3)
        self.assertEqual(basis["expectedRollback"], 0)
        self.assertEqual(basis["rollbackRatio"], 0.0)
        self.assertEqual(basis["maxSlippageObserved"], 0.0)
        self.assertEqual(basis["policy"], "reject")

    def test_fixed_keys(self):
        res, _ = decision.process(request("b", [tx("a", "A", 0, 1)]))
        self.assertEqual(
            list(res.keys()),
            ["id", "conclusion", "finalOrder", "sandwich", "involved",
             "reasons", "rollbackAllowed", "basis"],
        )
        self.assertEqual(
            list(res["sandwich"].keys()),
            ["detected", "front", "victim", "back", "evidence"],
        )
        self.assertEqual(
            list(res["basis"].keys()),
            ["policy", "basePrice", "maxSlippage", "rollbackLimit",
             "txCount", "expectedRollback", "rollbackRatio",
             "maxSlippageObserved"],
        )

    def test_sandwich_confirmed(self):
        # 确认夹子：攻击者 A 买(100) -> 受害者 B 买(110) -> A 卖(105)
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
        ]
        res, err = decision.process(request("sw", txs))
        self.assertIsNone(err)
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["SANDWICH_DETECTED"])
        self.assertEqual(res["finalOrder"], ["front", "victim", "back"])
        sw = res["sandwich"]
        self.assertTrue(sw["detected"])
        self.assertEqual(sw["front"], "front")
        self.assertEqual(sw["victim"], "victim")
        self.assertEqual(sw["back"], "back")
        self.assertEqual(res["involved"], ["front", "victim", "back"])
        ev = sw["evidence"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["at"], [0, 1, 2])
        self.assertEqual(ev[0]["prices"], [100, 110, 105])
        self.assertAlmostEqual(ev[0]["move"], 0.1)
        self.assertEqual(ev[0]["token"], "TKN")

    def test_reverse_sandwich(self):
        # 反向夹子：攻击者先卖 -> 受害者卖(更低) -> 攻击者买回
        txs = [
            tx("f", "A", 0, 30, side="sell", price=100),
            tx("v", "B", 0, 20, side="sell", price=90),
            tx("b", "A", 1, 10, side="buy", price=95),
        ]
        res, _ = decision.process(request("sw2", txs))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["SANDWICH_DETECTED"])
        ev = res["sandwich"]["evidence"][0]
        self.assertEqual((ev["front"], ev["victim"], ev["back"]), ("f", "v", "b"))
        self.assertAlmostEqual(ev["move"], 0.1)

    def test_no_sandwich_without_price_move(self):
        # 结构相似但价格未沿受害方向移动：不构成夹子
        txs = [
            tx("f", "A", 0, 30, side="buy", price=100),
            tx("v", "B", 0, 20, side="buy", price=100),
            tx("b", "A", 1, 10, side="sell", price=100),
        ]
        res, _ = decision.process(request("sw3", txs))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertFalse(res["sandwich"]["detected"])

    def test_rollback_within_limit(self):
        # 允许范围内的回滚：1/2 revert，rollbackLimit 0.5 -> ALLOW
        txs = [
            tx("ok", "A", 0, 10),
            tx("bad", "B", 0, 5, sim="revert"),
        ]
        res, _ = decision.process(request("rb", txs, rb=0.5))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertTrue(res["rollbackAllowed"])
        self.assertEqual(res["basis"]["expectedRollback"], 1)
        self.assertEqual(res["basis"]["rollbackRatio"], 0.5)
        # revert 交易仍在最终顺序中（不增加、丢失或重复）
        self.assertEqual(res["finalOrder"], ["ok", "bad"])

    def test_rollback_exceeds_limit(self):
        txs = [
            tx("a", "A", 0, 10, sim="revert"),
            tx("b", "B", 0, 5, sim="revert"),
            tx("c", "C", 0, 1),
        ]
        res, _ = decision.process(request("rb2", txs, rb=0.5))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["ROLLBACK_LIMIT_EXCEEDED"])

    def test_slippage_exceeded(self):
        txs = [tx("a", "A", 0, 1, price=120)]
        res, _ = decision.process(request("sl", txs, base=100, slip=0.1))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["SLIPPAGE_EXCEEDED"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.2)

    def test_slippage_at_limit_allowed(self):
        txs = [tx("a", "A", 0, 1, price=110)]
        res, _ = decision.process(request("sl2", txs, base=100, slip=0.1))
        self.assertEqual(res["conclusion"], "ALLOW")

    def test_price_context_missing(self):
        # 交易 token 无市场参考价：价格上下文缺失 -> BLOCK
        txs = [tx("a", "A", 0, 1, token="XXX")]
        res, _ = decision.process(request("pc", txs))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["PRICE_CONTEXT_MISSING"])

    def test_nonce_dependency_order(self):
        # nonce 依赖顺序：fee 顺序恰为 nonce 升序 -> ALLOW
        txs = [
            tx("a0", "A", 0, 100),
            tx("a1", "A", 1, 50),
            tx("b0", "B", 0, 10),
        ]
        res, _ = decision.process(request("n1", txs))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertEqual(res["finalOrder"], ["a0", "a1", "b0"])

    def test_nonce_order_violation(self):
        # fee 顺序使同 sender 高 nonce 排在低 nonce 前 -> BLOCK
        txs = [
            tx("a0", "A", 0, 1),
            tx("a1", "A", 1, 100),
        ]
        res, _ = decision.process(request("n2", txs))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["NONCE_ORDER_VIOLATION"])
        self.assertEqual(res["finalOrder"], ["a1", "a0"])

    def test_reasons_priority_stable(self):
        # 夹子 + 滑点超限并存：原因码按固定优先级排列
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
        ]
        res, _ = decision.process(request("both", txs, slip=0.05))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["SANDWICH_DETECTED", "SLIPPAGE_EXCEEDED"])

    def test_no_conflicting_conclusion(self):
        # 同一证据不得产生冲突结论：detected 为真时结论必为 BLOCK
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
        ]
        res, _ = decision.process(request("cc", txs))
        self.assertEqual(res["sandwich"]["detected"], res["conclusion"] == "BLOCK")
        self.assertEqual(bool(res["reasons"]), res["conclusion"] == "BLOCK")

    def test_fail_closed_detection_unavailable(self):
        req = decision.parse_request(request("f1", [tx("a", "A", 0, 1)]))

        def boom(_ordered):
            raise RuntimeError("detector down")

        res = decision.decide(req, detector=boom)
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["DETECTION_UNAVAILABLE"])

    def test_fail_closed_ordering_unavailable(self):
        req = decision.parse_request(request("f2", [tx("a", "A", 0, 1)]))

        def boom(_txs):
            raise RuntimeError("orderer down")

        res = decision.decide(req, orderer=boom)
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["ORDERING_UNAVAILABLE"])
        self.assertEqual(res["finalOrder"], [])

    def test_fail_closed_rollback_evaluation(self):
        req = decision.parse_request(request("f3", [tx("a", "A", 0, 1)]))

        def boom(_txs, _limit):
            raise RuntimeError("rollback down")

        res = decision.decide(req, rollback=boom)
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], ["ROLLBACK_EVALUATION_FAILED"])

    def test_deterministic_bytes(self):
        raw = request("det", [
            tx("h2", "A", 0, 7, price=101),
            tx("h1", "B", 0, 9, price=99),
            tx("h0", "C", 0, 9, sim="revert"),
        ], rb=0.5)
        out1 = decision.serialize(decision.process(raw)[0])
        out2 = decision.serialize(decision.process(raw)[0])
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith("\n"))
        self.assertNotIn(" ", out1.strip())


class TestSlippageModeMarket(unittest.TestCase):
    MARKET = {"prices": {"TKN": 200, "ALT": 50}}

    def test_default_and_explicit_base_unchanged(self):
        # 缺失与显式 base：继续用 basePrice 计算偏离率
        res, err = decision.process(
            request("b0", [tx("a", "A", 0, 1, price=120)], base=100, slip=0.1))
        self.assertIsNone(err)
        self.assertEqual(res["reasons"], ["SLIPPAGE_EXCEEDED"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.2)
        res, _ = decision.process(
            request("b1", [tx("a", "A", 0, 1, price=120)], base=100, slip=0.1,
                    slippage_mode="base"))
        self.assertEqual(res["reasons"], ["SLIPPAGE_EXCEEDED"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.2)

    def test_market_uses_token_reference(self):
        # 执行价 220 相对参考价 200：偏离率 0.10
        txs = [tx("a", "A", 0, 1, price=220)]
        res, err = decision.process(
            request("m0", txs, market=self.MARKET, base=100, slip=0.5,
                    slippage_mode="market"))
        self.assertIsNone(err)
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.1)
        # basePrice 仍校验并从 basis 返回
        self.assertEqual(res["basis"]["basePrice"], 100)

    def test_market_above_limit_blocks_equal_limit_allows(self):
        res, _ = decision.process(
            request("m1", [tx("a", "A", 0, 1, price=221)],
                    market=self.MARKET, slip=0.1, slippage_mode="market"))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["SLIPPAGE_EXCEEDED"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.105)
        res, _ = decision.process(
            request("m2", [tx("a", "A", 0, 1, price=220)],
                    market=self.MARKET, slip=0.1, slippage_mode="market"))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertEqual(res["reasons"], [])

    def test_market_missing_reference_only_context_missing(self):
        # 无参考价的交易不参与取值；另一笔 210/200 = 0.05 仍计入 observed
        txs = [
            tx("x", "X", 0, 1, token="ZZZ", price=100000),
            tx("a", "A", 0, 1, price=210),
        ]
        res, _ = decision.process(
            request("m3", txs, market=self.MARKET, slip=0.1,
                    slippage_mode="market"))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["PRICE_CONTEXT_MISSING"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.05)

    def test_market_all_missing_observed_zero(self):
        res, _ = decision.process(
            request("m4", [tx("x", "X", 0, 1, token="ZZZ")],
                    market=self.MARKET, slip=0.1, slippage_mode="market"))
        self.assertEqual(res["reasons"], ["PRICE_CONTEXT_MISSING"])
        self.assertEqual(res["basis"]["maxSlippageObserved"], 0.0)

    def test_market_missing_and_exceeded_priority(self):
        txs = [
            tx("x", "X", 0, 1, token="ZZZ"),
            tx("a", "A", 0, 1, price=230),
        ]
        res, _ = decision.process(
            request("m5", txs, market=self.MARKET, slip=0.1,
                    slippage_mode="market"))
        self.assertEqual(res["reasons"],
                         ["SLIPPAGE_EXCEEDED", "PRICE_CONTEXT_MISSING"])

    def test_market_per_token_max_observed(self):
        txs = [
            tx("a", "A", 0, 1, token="TKN", price=208),
            tx("b", "B", 0, 1, token="ALT", price=56),
        ]
        res, _ = decision.process(
            request("m6", txs, market=self.MARKET, slip=0.1,
                    slippage_mode="market"))
        self.assertEqual(res["reasons"], ["SLIPPAGE_EXCEEDED"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.12)

    def test_market_base_mode_still_flags_missing_context(self):
        # base 口径下缺市场参考价仍记 PRICE_CONTEXT_MISSING，且仍按
        # basePrice 计算偏离率（口径不回退）
        res, _ = decision.process(
            request("m7", [tx("x", "X", 0, 1, token="ZZZ", price=120)],
                    market=self.MARKET, base=100, slip=0.1))
        self.assertEqual(res["reasons"],
                         ["SLIPPAGE_EXCEEDED", "PRICE_CONTEXT_MISSING"])
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.2)

    def test_market_mode_with_sandwich_keeps_priority(self):
        txs = [
            tx("front", "A", 0, 30, side="buy", price=200),
            tx("victim", "B", 0, 20, side="buy", price=230),
            tx("back", "A", 1, 10, side="sell", price=205),
        ]
        res, _ = decision.process(
            request("m8", txs, market=self.MARKET, slip=0.05,
                    slippage_mode="market"))
        self.assertEqual(res["reasons"],
                         ["SANDWICH_DETECTED", "SLIPPAGE_EXCEEDED"])


class TestInputErrors(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = decision.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["reasons"], [code])
        self.assertEqual(res["finalOrder"], [])
        self.assertEqual(res["involved"], [])
        self.assertFalse(res["sandwich"]["detected"])

    def test_empty_bundle(self):
        self.assert_error(request("e", []), "EMPTY_BUNDLE")

    def test_unidentified_transaction(self):
        self.assert_error(request("e", [tx("", "A", 0, 1)]),
                          "UNIDENTIFIED_TRANSACTION")
        bad = tx("x", "A", 0, 1)
        del bad["hash"]
        self.assert_error(request("e", [bad]), "UNIDENTIFIED_TRANSACTION")

    def test_duplicate_transaction(self):
        self.assert_error(request("e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2)]),
                          "DUPLICATE_TRANSACTION")

    def test_ordering_conflict_nonce_gap(self):
        self.assert_error(request("e", [tx("a0", "A", 0, 1), tx("a2", "A", 2, 1)]),
                          "ORDERING_CONFLICT")

    def test_ordering_conflict_dup_nonce(self):
        self.assert_error(request("e", [tx("a", "A", 0, 1), tx("b", "A", 0, 2)]),
                          "ORDERING_CONFLICT")

    def test_missing_market_context(self):
        raw = request("e", [tx("a", "A", 0, 1)])
        data = json.loads(raw)
        del data["market"]
        self.assert_error(json.dumps(data), "MISSING_MARKET_CONTEXT")
        data = json.loads(raw)
        data["market"] = {}
        self.assert_error(json.dumps(data), "MISSING_MARKET_CONTEXT")

    def test_missing_price_field(self):
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        self.assert_error(request("e", [bad]), "MISSING_MARKET_CONTEXT")

    def test_invalid_price_base(self):
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=0),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=-5),
                          "INVALID_PRICE_BASE")

    def test_invalid_risk_limit(self):
        self.assert_error(request("e", [tx("a", "A", 0, 1)], slip=-0.1),
                          "INVALID_RISK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], slip=1.1),
                          "INVALID_RISK_LIMIT")

    def test_invalid_rollback_limit(self):
        self.assert_error(request("e", [tx("a", "A", 0, 1)], rb=2),
                          "INVALID_ROLLBACK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], rb=-1),
                          "INVALID_ROLLBACK_LIMIT")

    def test_boundary_limits_accepted(self):
        for slip, rb in ((0, 0), (1, 1), (0, 1), (1, 0)):
            res, err = decision.process(
                request("ok", [tx("a", "A", 0, 1)], slip=slip, rb=rb))
            self.assertIsNone(err)
            self.assertEqual(res["conclusion"], "ALLOW")

    def test_error_priority(self):
        # 空交易包优先于其他一切校验
        raw = request("e", [], base=0, slip=2)
        self.assert_error(raw, "EMPTY_BUNDLE")
        # 重复交易优先于 nonce 冲突与市场上下文
        raw = request("e", [tx("a", "A", 0, 1), tx("a", "A", 0, 2)], base=0)
        self.assert_error(raw, "DUPLICATE_TRANSACTION")
        # 市场上下文优先于基准价格
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        self.assert_error(request("e", [bad], base=0), "MISSING_MARKET_CONTEXT")
        # 基准价格优先于滑点上限，滑点上限优先于回滚范围
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=0, slip=2, rb=2),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], slip=2, rb=2),
                          "INVALID_RISK_LIMIT")

    def test_bad_json_and_schema(self):
        res, err = decision.process(b"{not json")
        self.assertEqual(err, core.BAD_JSON)
        self.assertEqual(res["reasons"], [core.BAD_JSON])
        res, err = decision.process(json.dumps([1, 2]))
        self.assertEqual(err, core.BAD_SCHEMA)
        res, err = decision.process(json.dumps({"transactions": []}))
        self.assertEqual(err, core.BAD_SCHEMA)

    def test_bad_policy(self):
        res, err = decision.process(
            request("e", [tx("a", "A", 0, 1)], policy="yolo"))
        self.assertEqual(err, core.BAD_POLICY)

    def test_bad_slippage_mode(self):
        for bad in ("MARKET", "", 1, True, None, ["base"], {}):
            raw = request("e", [tx("a", "A", 0, 1)], slippage_mode=bad)
            self.assert_error(raw, "BAD_SLIPPAGE_MODE")

    def test_bad_slippage_mode_error_shape(self):
        raw = request("e", [tx("a", "A", 0, 1)], slippage_mode="nope")
        res, err = decision.process(raw)
        self.assertEqual(err, "BAD_SLIPPAGE_MODE")
        self.assertEqual(
            list(res.keys()),
            ["id", "conclusion", "finalOrder", "sandwich", "involved",
             "reasons", "rollbackAllowed", "basis"],
        )
        basis = res["basis"]
        self.assertIsNone(basis["policy"])
        self.assertIsNone(basis["basePrice"])
        self.assertIsNone(basis["maxSlippage"])
        self.assertIsNone(basis["rollbackLimit"])
        self.assertEqual(basis["txCount"], 1)
        self.assertEqual(basis["expectedRollback"], 0)
        self.assertEqual(basis["rollbackRatio"], 0)
        self.assertEqual(basis["maxSlippageObserved"], 0)

    def test_bad_slippage_mode_after_old_errors(self):
        # 该检查排在原有输入与 policy 校验之后，旧错误优先
        self.assert_error(
            request("e", [], slippage_mode="nope"), "EMPTY_BUNDLE")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], base=0, slippage_mode=3),
            "INVALID_PRICE_BASE")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], slip=2, slippage_mode=3),
            "INVALID_RISK_LIMIT")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], policy="yolo",
                    slippage_mode="nope"),
            core.BAD_POLICY)

    def test_bool_and_nan_rejected(self):
        # 布尔值不是合法数值；NaN 不是正数
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=True),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], base=float("nan")),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1)], slip=float("nan")),
                          "INVALID_RISK_LIMIT")


class TestCli(unittest.TestCase):
    def test_module_entry_allow(self):
        proc = run_cli([], request("cli", [tx("a", "A", 0, 1)]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertEqual(res["finalOrder"], ["a"])

    def test_module_entry_block_exit_zero(self):
        # 业务 BLOCK 是正常决策结论，退出码 0
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
        ]
        proc = run_cli([], request("cli2", txs).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["SANDWICH_DETECTED"])

    def test_module_entry_input_error(self):
        proc = run_cli([], request("cli3", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["reasons"], ["EMPTY_BUNDLE"])
        self.assertEqual(res["conclusion"], "BLOCK")

    def test_module_entry_bad_args(self):
        proc = run_cli(["--nope"], b"")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_ARGS\n")

    def test_module_entry_market_mode(self):
        market = {"prices": {"TKN": 200}}
        raw = request("cm", [tx("a", "A", 0, 1, price=220)],
                      market=market, slip=0.1,
                      slippage_mode="market").encode("utf-8")
        proc = run_cli([], raw)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["conclusion"], "ALLOW")
        self.assertAlmostEqual(res["basis"]["maxSlippageObserved"], 0.1)

    def test_module_entry_bad_slippage_mode(self):
        raw = request("cm2", [tx("a", "A", 0, 1)],
                      slippage_mode="nope").encode("utf-8")
        proc = run_cli([], raw)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"),
                         "BAD_SLIPPAGE_MODE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["conclusion"], "BLOCK")
        self.assertEqual(res["reasons"], ["BAD_SLIPPAGE_MODE"])
        self.assertFalse(res["rollbackAllowed"])
        self.assertEqual(res["finalOrder"], [])
        self.assertEqual(res["involved"], [])

    def test_module_entry_input_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(request("file", [tx("a", "A", 0, 1)]))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["id"], "file")
            self.assertEqual(res["conclusion"], "ALLOW")

    def test_module_entry_byte_identical(self):
        raw = request("same", [tx("a", "A", 0, 1), tx("b", "B", 0, 2)]).encode()
        out1 = run_cli([], raw).stdout
        out2 = run_cli([], raw).stdout
        self.assertEqual(out1, out2)

    def test_baseline_entry_unchanged(self):
        # 既有 python -m mev_shield 入口行为不变
        payload = json.dumps({
            "id": "old",
            "transactions": [
                {"hash": "a", "from": "A", "nonce": 0, "fee": 1,
                 "token": "T", "side": "buy", "sim": "success"},
            ],
        }).encode("utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-m", "mev_shield"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["order"], ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
