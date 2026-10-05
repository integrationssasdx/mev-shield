"""mev_shield 行为验证（标准库 unittest，运行后删除亦可）。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import core
from mev_shield.cli import run


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success"):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim,
    }


def payload(ident, txs, policy=None):
    data = {"id": ident, "transactions": txs}
    if policy is not None:
        data["policy"] = policy
    return json.dumps(data)


class TestCore(unittest.TestCase):
    def test_no_sandwich_sort_and_rollback(self):
        # a: nonce 0(success,fee10) nonce 1(revert) nonce 2(success,fee5)
        # b: nonce 0(success,fee100)
        # a 的 max nonce=2：a0 为 DEPENDENT_NONCE(at0)，a1 为 REVERT(at1)，
        # a2 保留；b0 保留。rollback/dropped 按原位置。
        # order: fee 降序 b0(100), a2(5)
        txs = [
            tx("a0", "A", 0, 10),
            tx("a1", "A", 1, 99, side="sell", sim="revert"),
            tx("a2", "A", 2, 5),
            tx("b0", "B", 0, 100, side="sell"),
        ]
        res, err = core.process(payload("job1", txs))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["code"], "")
        self.assertEqual(res["order"], ["b0", "a2"])
        self.assertEqual(res["kept"], ["b0", "a2"])
        self.assertEqual(res["dropped"], ["a0", "a1"])
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "a0", "at": 0, "reason": "DEPENDENT_NONCE"},
                {"hash": "a1", "at": 1, "reason": "REVERT"},
            ],
        )
        # 键顺序
        self.assertEqual(list(res.keys()),
                         ["id", "status", "code", "order", "hits",
                          "rollback", "kept", "dropped"])

    def test_dependent_nonce_basic(self):
        # 同 from nonce 0 成功但存在更大 nonce -> DEPENDENT_NONCE
        txs = [tx("x0", "X", 0, 1), tx("x1", "X", 1, 100)]
        res, err = core.process(payload("p", txs))
        self.assertEqual(res["order"], ["x1"])
        self.assertEqual(res["dropped"], ["x0"])
        self.assertEqual(res["rollback"], [
            {"hash": "x0", "at": 0, "reason": "DEPENDENT_NONCE"},
        ])

    def test_fee_tie_hash_order(self):
        txs = [tx("h2", "A", 0, 7), tx("h1", "B", 0, 7), tx("h0", "C", 0, 7)]
        res, _ = core.process(payload("t", txs))
        self.assertEqual(res["order"], ["h0", "h1", "h2"])

    def test_sandwich_detected(self):
        txs = [
            tx("front", "MEV", 0, 10, side="buy"),
            tx("vic", "USER", 0, 5, side="buy"),
            tx("back", "MEV", 1, 10, side="sell"),
        ]
        res, err = core.process(payload("s", txs))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["code"], "SANDWICH_DETECTED")
        self.assertEqual(res["order"], [])
        self.assertEqual(res["kept"], [])
        self.assertEqual(res["dropped"], [])
        self.assertEqual(res["rollback"], [])
        self.assertEqual(len(res["hits"]), 1)
        hit = res["hits"][0]
        self.assertEqual(list(hit.keys()),
                         ["buy", "victim", "sell", "token", "at"])
        self.assertEqual(hit["buy"], "front")
        self.assertEqual(hit["victim"], "vic")
        self.assertEqual(hit["sell"], "back")
        self.assertEqual(hit["token"], "TKN")
        self.assertEqual(hit["at"], [0, 1, 2])

    def test_sandwich_overlap_sorted_by_i(self):
        # 两个前置 buy(i=0,1) 夹同一对 victim(2)/sell(3)
        txs = [
            tx("f2", "M", 0, 1, side="buy"),   # i=0 -> hits (0,2,3)
            tx("f1", "M", 1, 1, side="buy"),   # i=1 -> hits (1,2,3)
            tx("v", "U", 0, 1, side="buy"),    # j=2
            tx("s", "M", 2, 1, side="sell"),   # k=3
        ]
        res, _ = core.process(payload("o", txs))
        self.assertEqual(res["status"], "rejected")
        self.assertEqual([h["at"] for h in res["hits"]],
                         [[0, 2, 3], [1, 2, 3]])

    def test_not_sandwich_cases(self):
        # 不同 token
        txs = [
            tx("f", "M", 0, 1, token="A", side="buy"),
            tx("v", "U", 0, 1, token="B", side="buy"),
            tx("s", "M", 1, 1, token="A", side="sell"),
        ]
        res, _ = core.process(payload("n", txs))
        self.assertEqual(res["status"], "ok")

        # 前置 revert -> 不算夹子，但正常路径下前置被 rollback
        txs2 = [
            tx("f", "M", 0, 1, side="buy", sim="revert"),
            tx("v", "U", 0, 1, side="buy"),
            tx("s", "M", 1, 1, side="sell"),
        ]
        res2, _ = core.process(payload("n", txs2))
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["dropped"], ["f"])

        # j 与 i 同 from -> 不命中
        txs3 = [
            tx("f", "M", 0, 1, side="buy"),
            tx("v", "M", 1, 1, side="buy"),
            tx("s", "M", 2, 1, side="sell"),
        ]
        res3, _ = core.process(payload("n", txs3))
        self.assertEqual(res3["status"], "ok")

        # i 是 sell -> 不命中
        txs4 = [
            tx("f", "M", 0, 1, side="sell"),
            tx("v", "U", 0, 1, side="buy"),
            tx("s", "M", 1, 1, side="sell"),
        ]
        res4, _ = core.process(payload("n", txs4))
        self.assertEqual(res4["status"], "ok")

    def test_empty_transactions(self):
        res, err = core.process(payload("e", []))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["order"], [])
        self.assertEqual(res["id"], "e")

    def test_validation_errors(self):
        cases = [
            ("BAD_JSON", "{not json"),
            ("BAD_SCHEMA", json.dumps({"transactions": []})),
            ("BAD_SCHEMA", json.dumps({"id": 1, "transactions": []})),
            ("BAD_SCHEMA", json.dumps({"id": "z"})),
            ("BAD_SCHEMA", json.dumps({"id": "z", "transactions": {}})),
            ("BAD_SCHEMA", json.dumps([1, 2])),
            ("BAD_SCHEMA", payload("z", [{"hash": "h"}])),
            ("BAD_SCHEMA", payload("z", [tx("h", "A", -1, 1)])),
            ("BAD_SCHEMA", payload("z", [tx("h", "A", 0, True)])),
            ("BAD_SCHEMA", payload("z", [tx("h", 9, 0, 1)])),
            ("BAD_SIDE", payload("z", [tx("h", "A", 0, 1, side="bid")])),
            ("BAD_SIM", payload("z", [tx("h", "A", 0, 1, sim="fail")])),
        ]
        for code, raw in cases:
            with self.subTest(code=code, raw=raw[:30]):
                res, err = core.process(raw)
                self.assertEqual(err, code)
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], code)

    def test_dup_hash(self):
        txs = [tx("dup", "A", 0, 1), tx("dup", "B", 0, 1)]
        res, err = core.process(payload("d", txs))
        self.assertEqual(err, "DUP_HASH")
        self.assertEqual(res["id"], "d")

    def test_dup_nonce(self):
        txs = [tx("a", "A", 0, 1), tx("b", "A", 0, 2)]
        res, err = core.process(payload("d", txs))
        self.assertEqual(err, "DUP_NONCE")
        # 不同 from 的同 nonce 合法
        res2, err2 = core.process(payload("d", [
            tx("a", "A", 0, 1), tx("b", "B", 0, 1),
        ]))
        self.assertIsNone(err2)

    def test_error_priority_hash_before_side(self):
        # 第二条既与第一条 hash 重复、side 又非法 -> DUP_HASH 优先
        txs = [tx("dup", "A", 0, 1), tx("dup", "B", 0, 1, side="wat")]
        _, err = core.process(payload("p", txs))
        self.assertEqual(err, "DUP_HASH")

    def test_error_priority_nonce_before_side_sim(self):
        # 同 from 同 nonce 且 side/sim 非法 -> DUP_NONCE 优先
        txs = [tx("a", "A", 0, 1), tx("b", "A", 0, 1, side="x", sim="nope")]
        _, err = core.process(payload("p", txs))
        self.assertEqual(err, "DUP_NONCE")

    def test_error_priority_side_before_sim(self):
        txs = [tx("a", "A", 0, 1, side="x", sim="nope")]
        _, err = core.process(payload("p", txs))
        self.assertEqual(err, "BAD_SIDE")

    def test_error_id_unavailable(self):
        res, err = core.process("{bad json")
        self.assertEqual(err, "BAD_JSON")
        self.assertEqual(res["id"], "")
        res2, _ = core.process(json.dumps({"id": 5, "transactions": []}))
        self.assertEqual(res2["id"], "")

    def test_error_shape_empty_fields(self):
        res, _ = core.process("nope")
        for k in ("order", "hits", "rollback", "kept", "dropped"):
            self.assertEqual(res[k], [])

    def test_deterministic_bytes(self):
        txs = [
            tx("a", "A", 0, 5),
            tx("b", "B", 0, 9, sim="revert"),
            tx("c", "A", 1, 5),
            tx("d", "C", 0, 5),
        ]
        raw = payload("x", txs)
        b1 = core.serialize(core.process(raw)[0])
        b2 = core.serialize(core.process(raw)[0])
        self.assertEqual(b1, b2)
        self.assertTrue(b1.endswith("\n"))

    def test_unicode_preserved(self):
        txs = [tx("哈希α", "用户β", 0, 1, token="代币γ")]
        res, _ = core.process(payload("任务", txs))
        out = core.serialize(res)
        self.assertIn("哈希α", out)
        self.assertIn("任务", out)

    def test_max_nonce_includes_reverted_tx(self):
        # 同 from 中更大 nonce 的存在与否按全部输入交易判定；
        # 但被判定交易自身 revert 时原因记 REVERT（revert 优先）。
        # s0(nonce0 success) + s2(nonce2 revert)：s0 下方存在更大 nonce
        # -> DEPENDENT_NONCE；s2 -> REVERT
        txs = [
            tx("s0", "S", 0, 10),
            tx("s2", "S", 2, 10, sim="revert"),
        ]
        res, _ = core.process(payload("m", txs))
        self.assertEqual(res["order"], [])
        self.assertEqual(
            [e["reason"] for e in res["rollback"]],
            ["DEPENDENT_NONCE", "REVERT"],
        )

    def test_multi_token_hits_order(self):
        # token A: (i=0,j=2,k=4)；token B: (i=1,j=3,k=5)
        txs = [
            tx("a0", "M", 0, 1, token="A", side="buy"),
            tx("b0", "M", 1, 1, token="B", side="buy"),
            tx("av", "U", 0, 1, token="A", side="buy"),
            tx("bv", "V", 0, 1, token="B", side="buy"),
            tx("a1", "M", 2, 1, token="A", side="sell"),
            tx("b1", "M", 3, 1, token="B", side="sell"),
        ]
        res, _ = core.process(payload("m", txs))
        self.assertEqual(res["status"], "rejected")
        self.assertEqual([h["at"] for h in res["hits"]],
                         [[0, 2, 4], [1, 3, 5]])
        self.assertEqual(res["hits"][0]["token"], "A")
        self.assertEqual(res["hits"][1]["token"], "B")

    def test_extra_input_keys_ignored(self):
        raw = json.dumps({
            "id": "x", "extra": 1,
            "transactions": [
                {"hash": "h", "from": "A", "nonce": 0, "fee": 1,
                 "token": "t", "side": "buy", "sim": "success", "memo": 9},
            ],
        })
        res, err = core.process(raw)
        self.assertIsNone(err)
        self.assertEqual(res["order"], ["h"])


class TestPolicy(unittest.TestCase):
    def sandwich_txs(self):
        return [
            tx("f", "MEV", 0, 10, side="buy"),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
        ]

    def test_default_policy_is_reject(self):
        # 缺省 policy 与显式 reject 行为一致
        for raw in (payload("s", self.sandwich_txs()),
                    payload("s", self.sandwich_txs(), policy="reject")):
            res, err = core.process(raw)
            self.assertIsNone(err)
            self.assertEqual(res["status"], "rejected")
            self.assertEqual(res["code"], "SANDWICH_DETECTED")
            self.assertEqual(res["order"], [])
            self.assertEqual(res["kept"], [])
            self.assertEqual(res["dropped"], [])
            self.assertEqual(res["rollback"], [])
            self.assertEqual(len(res["hits"]), 1)

    def test_bad_policy_values(self):
        raws = [
            payload("z", [], policy="hold"),
            payload("z", [], policy="REJECT"),
            payload("z", [], policy=""),
            json.dumps({"id": "z", "transactions": [], "policy": 1}),
            json.dumps({"id": "z", "transactions": [], "policy": None}),
            json.dumps({"id": "z", "transactions": [], "policy": True}),
            json.dumps({"id": "z", "transactions": [], "policy": ["reject"]}),
        ]
        for raw in raws:
            with self.subTest(raw=raw):
                res, err = core.process(raw)
                self.assertEqual(err, "BAD_POLICY")
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], "BAD_POLICY")
                self.assertEqual(res["id"], "z")
                for k in ("order", "hits", "rollback", "kept", "dropped"):
                    self.assertEqual(res[k], [])
                self.assertEqual(list(res.keys()),
                                 ["id", "status", "code", "order", "hits",
                                  "rollback", "kept", "dropped"])

    def test_bad_policy_id_extraction(self):
        # id 缺失/类型不符时按错误提取规则为空
        res, err = core.process(json.dumps({"transactions": [], "policy": "x"}))
        self.assertEqual(err, "BAD_SCHEMA")
        res2, err2 = core.process(
            json.dumps({"id": 5, "transactions": [], "policy": "x"}))
        self.assertEqual(err2, "BAD_SCHEMA")
        self.assertEqual(res2["id"], "")

    def test_quarantine_mitigated(self):
        txs = self.sandwich_txs() + [
            tx("r", "OTHER", 0, 7, sim="revert"),
            tx("d0", "DEP", 0, 3),
            tx("d1", "DEP", 1, 4),
        ]
        res, err = core.process(payload("q", txs, policy="quarantine"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "mitigated")
        self.assertEqual(res["code"], "SANDWICH_MITIGATED")
        # hits 全部保留，键顺序固定
        self.assertEqual(len(res["hits"]), 1)
        hit = res["hits"][0]
        self.assertEqual(list(hit.keys()), ["buy", "victim", "sell", "token", "at"])
        self.assertEqual((hit["buy"], hit["victim"], hit["sell"]), ("f", "v", "b"))
        self.assertEqual(hit["at"], [0, 1, 2])
        # victim 不删除；其余按 fee 降序、hash 升序
        self.assertEqual(res["order"], ["v", "d1"])
        self.assertEqual(res["kept"], res["order"])
        # rollback 按输入位置升序，原因优先级正确
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "f", "at": 0, "reason": "SANDWICH_DETECTED"},
                {"hash": "b", "at": 2, "reason": "SANDWICH_DETECTED"},
                {"hash": "r", "at": 3, "reason": "REVERT"},
                {"hash": "d0", "at": 4, "reason": "DEPENDENT_NONCE"},
            ],
        )
        self.assertEqual(res["dropped"], ["f", "b", "r", "d0"])

    def test_quarantine_overlapping_hits(self):
        # 两条前置 buy 夹同一 victim/sell：攻击腿全部隔离，victim 保留
        txs = [
            tx("f2", "M", 0, 1, side="buy"),
            tx("f1", "M", 1, 1, side="buy"),
            tx("v", "U", 0, 1, side="buy"),
            tx("s", "M", 2, 1, side="sell"),
        ]
        res, err = core.process(payload("q", txs, policy="quarantine"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "mitigated")
        self.assertEqual([h["at"] for h in res["hits"]], [[0, 2, 3], [1, 2, 3]])
        self.assertEqual(res["order"], ["v"])
        self.assertEqual(res["dropped"], ["f2", "f1", "s"])
        self.assertEqual([e["reason"] for e in res["rollback"]],
                         ["SANDWICH_DETECTED"] * 3)

    def test_quarantine_result_has_no_sandwich(self):
        # 隔离后剩余交易按相对顺序不再含可识别夹子
        txs = [
            tx("f", "M", 0, 9, side="buy"),
            tx("v", "U", 0, 5, side="buy"),
            tx("b", "M", 1, 9, side="sell"),
            tx("x", "U", 1, 6, side="buy"),
            tx("y", "V", 0, 7, side="sell"),
        ]
        res, _ = core.process(payload("q", txs, policy="quarantine"))
        self.assertEqual(res["status"], "mitigated")
        kept_idx = [i for i, t in enumerate(txs) if t["hash"] in res["order"]]
        remaining = [txs[i] for i in kept_idx]
        self.assertEqual(core.detect_sandwiches(remaining), [])

    def test_quarantine_no_hit_is_ok(self):
        txs = [tx("a", "A", 0, 5), tx("b", "B", 0, 9, sim="revert")]
        res, err = core.process(payload("q", txs, policy="quarantine"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["code"], "")
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["order"], ["a"])
        self.assertEqual(res["dropped"], ["b"])

    def test_quarantine_exit_zero_and_bad_policy_exit_two(self):
        sin = io.BytesIO(payload("q", self.sandwich_txs(),
                                 policy="quarantine").encode())
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 0)
        self.assertEqual(serr.getvalue(), "")
        self.assertEqual(json.loads(sout.getvalue())["status"], "mitigated")

        sin = io.BytesIO(payload("q", [], policy="hold").encode())
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 2)
        self.assertEqual(serr.getvalue(), "BAD_POLICY\n")
        parsed = json.loads(sout.getvalue())
        self.assertEqual(parsed["code"], "BAD_POLICY")
        self.assertEqual(parsed["id"], "q")


class TestDeadline(unittest.TestCase):
    def payload_block(self, ident, txs, block=None, policy=None):
        data = {"id": ident, "transactions": txs}
        if policy is not None:
            data["policy"] = policy
        if block is not None:
            data["block"] = block
        return json.dumps(data)

    def with_deadline(self, t, deadline):
        t = dict(t)
        t["deadline"] = deadline
        return t

    def test_bad_deadline_block_type(self):
        # block 类型错误：字符串、布尔、负数、浮点
        for bad in ("5", True, False, -1, 1.5, None, []):
            with self.subTest(block=bad):
                raw = json.dumps({"id": "z", "transactions": [], "block": bad})
                res, err = core.process(raw)
                self.assertEqual(err, "BAD_DEADLINE")
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], "BAD_DEADLINE")
                self.assertEqual(res["id"], "z")
                for k in ("order", "hits", "rollback", "kept", "dropped"):
                    self.assertEqual(res[k], [])
                self.assertEqual(list(res.keys()),
                                 ["id", "status", "code", "order", "hits",
                                  "rollback", "kept", "dropped"])

    def test_bad_deadline_deadline_type(self):
        for bad in ("5", True, -1, 1.5, None, {}):
            with self.subTest(deadline=bad):
                txs = [self.with_deadline(tx("h", "A", 0, 1), bad)]
                res, err = core.process(self.payload_block("z", txs, block=3))
                self.assertEqual(err, "BAD_DEADLINE")
                self.assertEqual(res["code"], "BAD_DEADLINE")

    def test_deadline_without_block(self):
        txs = [self.with_deadline(tx("h", "A", 0, 1), 5)]
        res, err = core.process(payload("z", txs))
        self.assertEqual(err, "BAD_DEADLINE")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["code"], "BAD_DEADLINE")

    def test_original_errors_take_priority(self):
        # 原字段错误优先于 BAD_DEADLINE
        bad_block = {"id": "z", "transactions": [tx("h", "A", 0, 1, side="x")],
                     "block": "no"}
        _, err = core.process(json.dumps(bad_block))
        self.assertEqual(err, "BAD_SIDE")

        dup = {"id": "z",
               "transactions": [tx("d", "A", 0, 1),
                                self.with_deadline(tx("d", "B", 0, 1), 2)]}
        _, err = core.process(json.dumps(dup))
        self.assertEqual(err, "DUP_HASH")

        bad_policy = {"id": "z", "transactions": [], "policy": "hold",
                      "block": "no"}
        _, err = core.process(json.dumps(bad_policy))
        self.assertEqual(err, "BAD_POLICY")

    def test_block_only_and_empty_ok(self):
        for raw in (json.dumps({"id": "z", "transactions": [], "block": 7}),
                    self.payload_block("z", [tx("h", "A", 0, 1)], block=0)):
            res, err = core.process(raw)
            self.assertIsNone(err)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["code"], "")

    def test_expired_rollback_and_dropped(self):
        # block=5：d0(deadline 4) 过期，d1(deadline 5 等于 block) 未过期
        txs = [
            self.with_deadline(tx("d0", "A", 0, 10), 4),
            self.with_deadline(tx("d1", "B", 0, 3), 5),
            tx("k", "C", 0, 7),
        ]
        res, err = core.process(self.payload_block("z", txs, block=5))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["order"], ["k", "d1"])
        self.assertEqual(res["kept"], ["k", "d1"])
        self.assertEqual(res["dropped"], ["d0"])
        self.assertEqual(res["rollback"],
                         [{"hash": "d0", "at": 0, "reason": "DEADLINE_EXPIRED"}])

    def test_expired_reason_priority(self):
        # 过期交易即使 revert 或同 from 有更大 nonce，也只记 DEADLINE_EXPIRED
        txs = [
            self.with_deadline(tx("e0", "A", 0, 1, sim="revert"), 1),
            self.with_deadline(tx("e1", "A", 1, 1), 2),
            tx("a2", "A", 2, 9),
        ]
        res, _ = core.process(self.payload_block("z", txs, block=5))
        self.assertEqual([e["reason"] for e in res["rollback"]],
                         ["DEADLINE_EXPIRED", "DEADLINE_EXPIRED"])
        self.assertEqual(res["order"], ["a2"])

    def test_expired_breaks_sandwich(self):
        # 任一腿过期 -> 不命中，走 ok；过期交易记 DEADLINE_EXPIRED。
        # 其余交易沿用既有规则：MEV 的 f(nonce0) 在 b(nonce1) 存在时为
        # DEPENDENT_NONCE（max_nonce 计算不因过期改变）。
        sandwich = [
            tx("f", "MEV", 0, 10, side="buy"),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
        ]
        expected = {
            0: [("f", 0, "DEADLINE_EXPIRED")],
            1: [("f", 0, "DEPENDENT_NONCE"), ("v", 1, "DEADLINE_EXPIRED")],
            2: [("f", 0, "DEPENDENT_NONCE"), ("b", 2, "DEADLINE_EXPIRED")],
        }
        for pos in range(3):
            with self.subTest(expired_leg=pos):
                txs = [dict(t) for t in sandwich]
                txs[pos]["deadline"] = 1
                res, _ = core.process(self.payload_block("z", txs, block=5))
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["hits"], [])
                self.assertEqual(
                    res["rollback"],
                    [{"hash": h, "at": at, "reason": r}
                     for (h, at, r) in expected[pos]],
                )
                self.assertEqual(res["dropped"],
                                 [h for (h, _at, _r) in expected[pos]])

    def test_hits_use_original_positions(self):
        # 过期交易夹在中间：命中三元组的 at 仍为输入位置
        txs = [
            tx("f", "MEV", 0, 10, side="buy"),
            self.with_deadline(tx("x", "X", 0, 1), 0),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
        ]
        res, _ = core.process(self.payload_block("z", txs, block=5))
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["code"], "SANDWICH_DETECTED")
        self.assertEqual([h["at"] for h in res["hits"]], [[0, 2, 3]])
        # 拒绝结果中过期交易也不写 rollback
        self.assertEqual(res["rollback"], [])
        self.assertEqual(res["dropped"], [])

    def test_quarantine_with_expired(self):
        txs = [
            tx("f", "MEV", 0, 10, side="buy"),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
            self.with_deadline(tx("e", "E", 0, 1), 2),
            tx("k", "K", 0, 6),
        ]
        res, err = core.process(
            self.payload_block("z", txs, block=5, policy="quarantine"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "mitigated")
        self.assertEqual(res["code"], "SANDWICH_MITIGATED")
        self.assertEqual([h["at"] for h in res["hits"]], [[0, 1, 2]])
        self.assertEqual(res["order"], ["k", "v"])
        self.assertEqual(res["kept"], res["order"])
        # rollback 按输入位置合并，每笔一次
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "f", "at": 0, "reason": "SANDWICH_DETECTED"},
                {"hash": "b", "at": 2, "reason": "SANDWICH_DETECTED"},
                {"hash": "e", "at": 3, "reason": "DEADLINE_EXPIRED"},
            ],
        )
        self.assertEqual(res["dropped"], ["f", "b", "e"])

    def test_no_deadline_fields_byte_identical(self):
        # 无新字段的输入输出字节与旧行为一致（确定性）
        txs = [tx("a", "A", 0, 5), tx("b", "B", 0, 9, sim="revert")]
        raw = payload("x", txs)
        out1 = core.serialize(core.process(raw)[0])
        out2 = core.serialize(core.process(raw)[0])
        self.assertEqual(out1, out2)

    def test_cli_bad_deadline_exit_two(self):
        raw = json.dumps({"id": "z", "transactions": [], "block": -1}).encode()
        sin = io.BytesIO(raw)
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 2)
        self.assertEqual(serr.getvalue(), "BAD_DEADLINE\n")
        parsed = json.loads(sout.getvalue())
        self.assertEqual(parsed["code"], "BAD_DEADLINE")
        self.assertEqual(parsed["status"], "error")


class TestPacking(unittest.TestCase):
    def payload_packing(self, ident, txs, packing=...):
        data = {"id": ident, "transactions": txs}
        if packing is not ...:
            data["packing"] = packing
        return json.dumps(data)

    def payload_full(self, ident, txs, packing, policy=None, block=None):
        data = {"id": ident, "transactions": txs, "packing": packing}
        if policy is not None:
            data["policy"] = policy
        if block is not None:
            data["block"] = block
        return json.dumps(data)

    def with_deadline(self, t, deadline):
        t = dict(t)
        t["deadline"] = deadline
        return t

    def test_bad_packing_values(self):
        for bad in ("NONCE", "Fee", "", "nonce ", 1, None, True, ["nonce"], {}):
            with self.subTest(bad=bad):
                res, err = core.process(
                    self.payload_packing("z", [tx("h", "A", 0, 1)], bad))
                self.assertEqual(err, "BAD_PACKING")
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], "BAD_PACKING")
                self.assertEqual(res["id"], "z")
                for k in ("order", "hits", "rollback", "kept", "dropped"):
                    self.assertEqual(res[k], [])
                self.assertEqual(list(res.keys()),
                                 ["id", "status", "code", "order", "hits",
                                  "rollback", "kept", "dropped"])

    def test_bad_packing_existing_errors_take_priority(self):
        # JSON / schema / 重复 hash / 重复 nonce / side / sim / policy /
        # 期限错误均优先于 BAD_PACKING
        cases = [
            ("BAD_JSON", "{bad"),
            ("BAD_SCHEMA", json.dumps({"id": 5, "transactions": [],
                                       "packing": "wat"})),
            ("DUP_HASH", self.payload_packing("z", [
                tx("d", "A", 0, 1), tx("d", "B", 0, 1)], "wat")),
            ("DUP_NONCE", self.payload_packing("z", [
                tx("a", "A", 0, 1), tx("b", "A", 0, 1)], "wat")),
            ("BAD_SIDE", self.payload_packing("z", [
                tx("h", "A", 0, 1, side="x")], "wat")),
            ("BAD_SIM", self.payload_packing("z", [
                tx("h", "A", 0, 1, sim="nope")], "wat")),
            ("BAD_POLICY", json.dumps(
                {"id": "z", "transactions": [], "policy": "hold",
                 "packing": "wat"})),
            ("BAD_DEADLINE", json.dumps(
                {"id": "z", "transactions": [], "block": "no",
                 "packing": "wat"})),
        ]
        for code, raw in cases:
            with self.subTest(code=code):
                res, err = core.process(raw)
                self.assertEqual(err, code)
                self.assertEqual(res["code"], code)

    def test_default_and_explicit_fee_identical(self):
        txs = [
            tx("a0", "A", 0, 10),
            tx("a1", "A", 1, 99, side="sell", sim="revert"),
            tx("a2", "A", 2, 5),
            tx("b0", "B", 0, 100, side="sell"),
        ]
        out_default = core.serialize(core.process(self.payload_packing("j", txs))[0])
        out_fee = core.serialize(
            core.process(self.payload_packing("j", txs, "fee"))[0])
        out_nonce = core.serialize(
            core.process(self.payload_packing("j", txs, "nonce"))[0])
        self.assertEqual(out_default, out_fee)
        # fee 模式：fee 降序 b0(100), a2(5)；a1 revert，a0 依赖
        parsed = json.loads(out_fee)
        self.assertEqual(parsed["order"], ["b0", "a2"])

    def test_nonce_longest_suffix(self):
        # A: nonce 0,2,3 全成功 -> 以 3 为终点连续后缀 {2,3}，0 记 DEPENDENT_NONCE
        txs = [tx("a0", "A", 0, 1), tx("a2", "A", 2, 9), tx("a3", "A", 3, 2)]
        res, err = core.process(self.payload_packing("n", txs, "nonce"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["code"], "")
        self.assertEqual(res["order"], ["a2", "a3"])
        self.assertEqual(res["kept"], ["a2", "a3"])
        self.assertEqual(res["dropped"], ["a0"])
        self.assertEqual(res["rollback"],
                         [{"hash": "a0", "at": 0, "reason": "DEPENDENT_NONCE"}])

    def test_nonce_multi_gap(self):
        # 成功 nonce {0,1,4,5,6} -> 保留 {4,5,6}，道内 nonce 升序
        txs = [
            tx("s0", "S", 0, 5), tx("s1", "S", 1, 5),
            tx("s4", "S", 4, 5), tx("s5", "S", 5, 5), tx("s6", "S", 6, 5),
        ]
        res, _ = core.process(self.payload_packing("n", txs, "nonce"))
        self.assertEqual(res["order"], ["s4", "s5", "s6"])
        self.assertEqual(res["dropped"], ["s0", "s1"])
        self.assertEqual(
            [e["reason"] for e in res["rollback"]],
            ["DEPENDENT_NONCE", "DEPENDENT_NONCE"])

    def test_nonce_revert_excluded_before_suffix(self):
        # {0 成功, 1 revert, 2 成功}：成功集合 {0,2}，2 向下断裂 -> 只留 2；
        # 0 记 DEPENDENT_NONCE，1 记 REVERT，rollback 按输入位置
        txs = [
            tx("s0", "S", 0, 10),
            tx("s1", "S", 1, 10, sim="revert"),
            tx("s2", "S", 2, 10),
        ]
        res, _ = core.process(self.payload_packing("n", txs, "nonce"))
        self.assertEqual(res["order"], ["s2"])
        self.assertEqual(res["dropped"], ["s0", "s1"])
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "s0", "at": 0, "reason": "DEPENDENT_NONCE"},
                {"hash": "s1", "at": 1, "reason": "REVERT"},
            ],
        )

    def test_nonce_lane_order(self):
        # A 道：n5(fee10) n4(fee100)，道内最高 fee 100、最小 hash a4
        # C 道：n1(fee1)  n0(fee100)，道内最高 fee 100、最小 hash c0
        # B 道：n0(fee50)
        # 道间：A、C 最高 fee 同为 100，按最小 hash a4 < c0；其后 B
        # 输入顺序故意打乱；a4 用异种 token，避免与 c1/c0 构成反向夹子
        txs = [
            tx("a5", "A", 5, 10),
            tx("b0", "B", 0, 50, side="sell"),
            tx("c1", "C", 1, 1, side="sell"),
            tx("a4", "A", 4, 100, side="sell", token="ALT"),
            tx("c0", "C", 0, 100),
        ]
        res, _ = core.process(self.payload_packing("n", txs, "nonce"))
        self.assertEqual(res["order"],
                         ["a4", "a5", "c0", "c1", "b0"])
        self.assertEqual(res["kept"], res["order"])
        self.assertEqual(res["dropped"], [])

    def test_nonce_lane_tie_uses_min_hash_of_lane(self):
        # 两道最高 fee 同为 9：P 道含 hash 更小的 pa（虽其 fee 仅 1），
        # 故 P 道排在 Q 道之前
        txs = [
            tx("pz", "P", 1, 9),
            tx("pa", "P", 0, 1),
            tx("q0", "Q", 0, 9),
        ]
        res, _ = core.process(self.payload_packing("n", txs, "nonce"))
        self.assertEqual(res["order"], ["pa", "pz", "q0"])

    def test_nonce_empty_batch(self):
        res, err = core.process(self.payload_packing("e", [], "nonce"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["order"], [])

    def test_nonce_expired_excluded_first(self):
        # block=5：n1 过期。剩余成功 {0,2}，后缀只含 2；
        # n0 记 DEPENDENT_NONCE，n1 记 DEADLINE_EXPIRED，按输入位置
        txs = [
            tx("a0", "A", 0, 10),
            self.with_deadline(tx("a1", "A", 1, 10), 4),
            tx("a2", "A", 2, 10),
        ]
        res, _ = core.process(self.payload_full("n", txs, "nonce", block=5))
        self.assertEqual(res["order"], ["a2"])
        self.assertEqual(res["dropped"], ["a0", "a1"])
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "a0", "at": 0, "reason": "DEPENDENT_NONCE"},
                {"hash": "a1", "at": 1, "reason": "DEADLINE_EXPIRED"},
            ],
        )

        # n0 过期、n1/n2 成功 -> {1,2} 连续，全部保留
        txs2 = [
            self.with_deadline(tx("b0", "B", 0, 10), 0),
            tx("b1", "B", 1, 10),
            tx("b2", "B", 2, 10),
        ]
        res2, _ = core.process(self.payload_full("n", txs2, "nonce", block=5))
        self.assertEqual(res2["order"], ["b1", "b2"])
        self.assertEqual(res2["dropped"], ["b0"])
        self.assertEqual(res2["rollback"],
                         [{"hash": "b0", "at": 0, "reason": "DEADLINE_EXPIRED"}])

    def test_nonce_reject_still_whole_batch(self):
        txs = [
            tx("f", "MEV", 0, 10, side="buy"),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
        ]
        res, err = core.process(self.payload_packing("s", txs, "nonce"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["code"], "SANDWICH_DETECTED")
        self.assertEqual(len(res["hits"]), 1)
        self.assertEqual(res["order"], [])
        self.assertEqual(res["kept"], [])
        self.assertEqual(res["dropped"], [])
        self.assertEqual(res["rollback"], [])

    def test_nonce_quarantine_mitigated(self):
        txs = [
            tx("f", "MEV", 0, 10, side="buy"),    # 0 攻击腿
            tx("v", "USER", 0, 5, side="buy"),    # 1 victim 保留
            tx("b", "MEV", 1, 10, side="sell"),   # 2 攻击腿
            tx("r", "OTHER", 0, 7, sim="revert"),  # 3 REVERT
            tx("d0", "DEP", 0, 3),                # 4 连续后缀
            tx("d1", "DEP", 1, 4),                # 5
            tx("x0", "X", 0, 2),                  # 6 后缀外
            tx("x2", "X", 2, 6),                  # 7 保留，且使 X 道最高 fee
        ]
        res, err = core.process(
            self.payload_full("q", txs, "nonce", policy="quarantine"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "mitigated")
        self.assertEqual(res["code"], "SANDWICH_MITIGATED")
        self.assertEqual([h["at"] for h in res["hits"]], [[0, 1, 2]])
        # 道：X 最高 fee 6 -> v(5) -> D(4)；D 道 nonce 升序
        self.assertEqual(res["order"], ["x2", "v", "d0", "d1"])
        self.assertEqual(res["kept"], res["order"])
        self.assertEqual(res["dropped"], ["f", "b", "r", "x0"])
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "f", "at": 0, "reason": "SANDWICH_DETECTED"},
                {"hash": "b", "at": 2, "reason": "SANDWICH_DETECTED"},
                {"hash": "r", "at": 3, "reason": "REVERT"},
                {"hash": "x0", "at": 6, "reason": "DEPENDENT_NONCE"},
            ],
        )

    def test_nonce_deterministic_bytes(self):
        txs = [
            tx("a5", "A", 5, 10),
            tx("a4", "A", 4, 100),
            tx("c1", "C", 1, 1, sim="revert"),
            tx("b0", "B", 0, 50),
        ]
        raw = self.payload_packing("x", txs, "nonce")
        out1 = core.serialize(core.process(raw)[0])
        out2 = core.serialize(core.process(raw)[0])
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith("\n"))
        self.assertNotIn(" ", out1.rstrip("\n"))

    def test_cli_bad_packing_exit_two(self):
        sin = io.BytesIO(json.dumps(
            {"id": "z", "transactions": [], "packing": "nonce!"}).encode())
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 2)
        self.assertEqual(serr.getvalue(), "BAD_PACKING\n")
        parsed = json.loads(sout.getvalue())
        self.assertEqual(parsed["status"], "error")
        self.assertEqual(parsed["code"], "BAD_PACKING")
        for k in ("order", "hits", "rollback", "kept", "dropped"):
            self.assertEqual(parsed[k], [])

    def test_cli_nonce_ok_exit_zero(self):
        raw = self.payload_packing(
            "c", [tx("s0", "S", 0, 1), tx("s1", "S", 1, 9)], "nonce").encode()
        sin = io.BytesIO(raw)
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 0)
        self.assertEqual(serr.getvalue(), "")
        self.assertEqual(json.loads(sout.getvalue())["order"], ["s0", "s1"])


class TestReverseSandwich(unittest.TestCase):
    def reverse_txs(self):
        return [
            tx("front", "MEV", 0, 10, side="sell"),
            tx("vic", "USER", 0, 5, side="sell"),
            tx("back", "MEV", 1, 10, side="buy"),
        ]

    def test_reverse_sandwich_detected(self):
        # 攻击者先卖、victim 再卖、攻击者后买回：buy 位置晚于 sell
        res, err = core.process(payload("r", self.reverse_txs()))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["code"], "SANDWICH_DETECTED")
        self.assertEqual(res["order"], [])
        self.assertEqual(res["kept"], [])
        self.assertEqual(res["dropped"], [])
        self.assertEqual(res["rollback"], [])
        self.assertEqual(len(res["hits"]), 1)
        hit = res["hits"][0]
        self.assertEqual(list(hit.keys()),
                         ["buy", "victim", "sell", "token", "at"])
        self.assertEqual(hit["buy"], "back")
        self.assertEqual(hit["victim"], "vic")
        self.assertEqual(hit["sell"], "front")
        self.assertEqual(hit["token"], "TKN")
        self.assertEqual(hit["at"], [2, 1, 0])

    def test_reverse_not_sandwich_cases(self):
        # victim 与攻击者同 from -> 不命中
        txs = [
            tx("f", "M", 0, 1, side="sell"),
            tx("v", "M", 1, 1, side="sell"),
            tx("b", "M", 2, 1, side="buy"),
        ]
        res, _ = core.process(payload("n", txs))
        self.assertEqual(res["status"], "ok")

        # token 不同 -> 不命中
        txs2 = [
            tx("f", "M", 0, 1, token="A", side="sell"),
            tx("v", "U", 0, 1, token="B", side="sell"),
            tx("b", "M", 1, 1, token="A", side="buy"),
        ]
        res2, _ = core.process(payload("n", txs2))
        self.assertEqual(res2["status"], "ok")

        # 后置买入 revert -> 不命中
        txs3 = [
            tx("f", "M", 0, 1, side="sell"),
            tx("v", "U", 0, 1, side="sell"),
            tx("b", "M", 1, 1, side="buy", sim="revert"),
        ]
        res3, _ = core.process(payload("n", txs3))
        self.assertEqual(res3["status"], "ok")

        # victim 为 buy（正反方向侧型不一致）-> 不命中
        txs4 = [
            tx("f", "M", 0, 1, side="sell"),
            tx("v", "U", 0, 1, side="buy"),
            tx("b", "M", 1, 1, side="buy"),
        ]
        res4, _ = core.process(payload("n", txs4))
        self.assertEqual(res4["status"], "ok")

    def test_mixed_hits_sorted_by_front_leg(self):
        # 前置腿 i=0 的反向夹子与 i=1 的正向夹子：按前置腿位置升序
        txs = [
            tx("rs", "M", 0, 1, side="sell"),   # 反向前置 -> hit at [4, 2, 0]
            tx("fb", "M", 1, 1, side="buy"),    # 正向前置 -> hit at [1, 3, 5]
            tx("rv", "U", 0, 1, side="sell"),   # 反向 victim
            tx("fv", "V", 0, 1, side="buy"),    # 正向 victim
            tx("rb", "M", 2, 1, side="buy"),    # 反向后置买入
            tx("fs", "M", 3, 1, side="sell"),   # 正向后置卖出
        ]
        res, _ = core.process(payload("m", txs))
        self.assertEqual(res["status"], "rejected")
        self.assertEqual([h["at"] for h in res["hits"]],
                         [[4, 2, 0], [1, 3, 5]])
        self.assertEqual((res["hits"][0]["buy"], res["hits"][0]["sell"]),
                         ("rb", "rs"))
        self.assertEqual((res["hits"][1]["buy"], res["hits"][1]["sell"]),
                         ("fb", "fs"))

    def test_reverse_overlap_hits(self):
        # 两条前置 sell 夹同一 victim/buy：逐条保留，按前置腿升序
        txs = [
            tx("s2", "M", 0, 1, side="sell"),   # i=0 -> hit at [3, 2, 0]
            tx("s1", "M", 1, 1, side="sell"),   # i=1 -> hit at [3, 2, 1]
            tx("v", "U", 0, 1, side="sell"),    # victim
            tx("b", "M", 2, 1, side="buy"),     # 后置买入
        ]
        res, _ = core.process(payload("o", txs))
        self.assertEqual(res["status"], "rejected")
        self.assertEqual([h["at"] for h in res["hits"]],
                         [[3, 2, 0], [3, 2, 1]])

    def test_reverse_quarantine_mitigated(self):
        txs = self.reverse_txs() + [
            tx("r", "OTHER", 0, 7, sim="revert"),
            tx("k", "KEEP", 0, 6),
        ]
        res, err = core.process(payload("q", txs, policy="quarantine"))
        self.assertIsNone(err)
        self.assertEqual(res["status"], "mitigated")
        self.assertEqual(res["code"], "SANDWICH_MITIGATED")
        self.assertEqual([h["at"] for h in res["hits"]], [[2, 1, 0]])
        # victim 与其他交易保留，攻击两腿各记一次 SANDWICH_DETECTED
        self.assertEqual(res["order"], ["k", "vic"])
        self.assertEqual(res["kept"], res["order"])
        self.assertEqual(
            res["rollback"],
            [
                {"hash": "front", "at": 0, "reason": "SANDWICH_DETECTED"},
                {"hash": "back", "at": 2, "reason": "SANDWICH_DETECTED"},
                {"hash": "r", "at": 3, "reason": "REVERT"},
            ],
        )
        self.assertEqual(res["dropped"], ["front", "back", "r"])

    def test_reverse_expired_breaks_sandwich(self):
        # 反向夹子任一腿过期 -> 不命中，过期交易记 DEADLINE_EXPIRED
        for pos in range(3):
            with self.subTest(expired_leg=pos):
                txs = [dict(t) for t in self.reverse_txs()]
                txs[pos]["deadline"] = 1
                raw = json.dumps({"id": "z", "transactions": txs, "block": 5})
                res, _ = core.process(raw)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["hits"], [])
                self.assertEqual(
                    [e["reason"] for e in res["rollback"]].count(
                        "DEADLINE_EXPIRED"),
                    1,
                )


class TestCli(unittest.TestCase):
    def invoke(self, argv, in_bytes=None):
        sin = io.BytesIO(in_bytes or b"")
        sout = io.BytesIO()
        serr = io.StringIO()
        code = run(argv, stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        return code, sout.getvalue(), serr.getvalue()

    def test_stdin_stdout_ok(self):
        data = payload("cli", [tx("h", "A", 0, 3)]).encode()
        code, out, err = self.invoke([], data)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertEqual(parsed["id"], "cli")
        self.assertEqual(parsed["order"], ["h"])

    def test_sandwich_exit_zero(self):
        data = payload("s", [
            tx("f", "M", 0, 1, side="buy"),
            tx("v", "U", 0, 1, side="buy"),
            tx("b", "M", 1, 1, side="sell"),
        ]).encode()
        code, out, err = self.invoke([], data)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "rejected")

    def test_validation_exit_two(self):
        code, out, err = self.invoke([], b"{bad")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["code"], "BAD_JSON")
        self.assertEqual(err.strip(), "BAD_JSON")

    def test_bad_args(self):
        for argv in (["--nope"], ["--input"], ["pos"], ["--input=a", "--input=b"],
                     ["-i", "x"], ["--input=x", "extra"]):
            code, out, err = self.invoke(argv, b"{}")
            self.assertEqual(code, 2, argv)
            self.assertEqual(err.strip(), "BAD_ARGS", argv)
            parsed = json.loads(out)
            self.assertEqual(parsed["status"], "error", argv)
            self.assertEqual(parsed["code"], "BAD_ARGS", argv)
            self.assertEqual(parsed["id"], "", argv)

    def test_file_roundtrip_and_io_errors(self):
        with tempfile.TemporaryDirectory() as d:
            inp = os.path.join(d, "in.json")
            outp = os.path.join(d, "out.json")
            with open(inp, "w", encoding="utf-8") as f:
                f.write(payload("f", [tx("h", "A", 0, 1)]))
            code, out, err = self.invoke(
                ["--input", inp, "--output", outp])
            self.assertEqual(code, 0)
            self.assertEqual(out, b"")
            with open(outp, "rb") as f:
                self.assertEqual(json.loads(f.read())["order"], ["h"])

            # 输入文件不存在 -> INPUT_IO（错误 JSON 写入输出文件）
            code, out, err = self.invoke(
                ["--input", os.path.join(d, "missing"), "--output", outp])
            self.assertEqual(code, 2)
            self.assertEqual(out, b"")
            with open(outp, "rb") as f:
                self.assertEqual(json.loads(f.read())["code"], "INPUT_IO")
            self.assertEqual(err.strip(), "INPUT_IO")

            # 输出路径不可写 -> OUTPUT_IO，仅 stderr
            bad_out = os.path.join(d, "no_such_dir", "x.json")
            code, out, err = self.invoke(
                ["--input", inp, "--output", bad_out])
            self.assertEqual(code, 2)
            self.assertEqual(out, b"")
            self.assertEqual(err.strip(), "OUTPUT_IO")

            # = 形式参数
            outp2 = os.path.join(d, "o2.json")
            code, _, _ = self.invoke(
                [f"--input={inp}", f"--output={outp2}"])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(outp2))

    def test_subprocess_module_entry(self):
        # 端到端：python -m mev_shield
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-m", "mev_shield"],
            input=payload("sub", []).encode(),
            capture_output=True, cwd=root,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["id"], "sub")


if __name__ == "__main__":
    unittest.main(verbosity=2)
