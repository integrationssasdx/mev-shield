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


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", deadline=None):
    d = {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim,
    }
    if deadline is not None:
        d["deadline"] = deadline
    return d


_NO_BLOCK = object()


def payload(ident, txs, policy=None, block=_NO_BLOCK):
    data = {"id": ident, "transactions": txs}
    if policy is not None:
        data["policy"] = policy
    if block is not _NO_BLOCK:
        data["block"] = block
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


class TestDeadline(unittest.TestCase):
    def sandwich(self, deadlines=(None, None, None), **kw):
        legs = [
            tx("f", "MEV", 0, 10, side="buy", deadline=deadlines[0]),
            tx("v", "USER", 0, 5, side="buy", deadline=deadlines[1]),
            tx("b", "MEV", 1, 10, side="sell", deadline=deadlines[2]),
        ]
        return payload("s", legs, **kw)

    def test_block_only_and_empty_are_ok(self):
        for raw in (payload("e", [], block=0), payload("e2", []),
                    payload("e3", [tx("h", "A", 0, 3)], block=7)):
            res, err = core.process(raw)
            self.assertIsNone(err)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["code"], "")
            self.assertEqual(res["hits"], [])

    def test_expiry_boundary(self):
        # deadline == block 可执行；< 过期；> 可执行
        res, _ = core.process(payload("e", [tx("h", "A", 0, 3, deadline=10)], block=10))
        self.assertEqual(res["order"], ["h"])
        self.assertEqual(res["dropped"], [])

        res, _ = core.process(payload("l", [tx("h", "A", 0, 3, deadline=9)], block=10))
        self.assertEqual(res["order"], [])
        self.assertEqual(res["kept"], [])
        self.assertEqual(res["dropped"], ["h"])
        self.assertEqual(
            res["rollback"],
            [{"hash": "h", "at": 0, "reason": "DEADLINE_EXPIRED"}],
        )

        res, _ = core.process(payload("g", [tx("h", "A", 0, 3, deadline=11)], block=10))
        self.assertEqual(res["order"], ["h"])

    def test_expired_beats_revert_and_dependent_nonce(self):
        # 过期交易即使 revert 也只记 DEADLINE_EXPIRED，且不参与 max nonce 统计
        res, _ = core.process(payload("p", [
            tx("s0", "S", 0, 10, deadline=100),
            tx("s2", "S", 2, 10, sim="revert", deadline=5),
        ], block=10))
        self.assertEqual(res["order"], ["s0"])
        self.assertEqual(
            res["rollback"],
            [{"hash": "s2", "at": 1, "reason": "DEADLINE_EXPIRED"}],
        )

    def test_other_txs_keep_reason_precedence(self):
        # 过期者只记过期；未过期者之间仍 REVERT 优先于 DEPENDENT_NONCE
        res, _ = core.process(payload("p", [
            tx("x0", "X", 0, 10, deadline=5),   # 过期
            tx("x1", "X", 1, 10),               # 下方存在更大 nonce 2
            tx("x2", "X", 2, 1, sim="revert"),
        ], block=10))
        self.assertEqual(
            [e["reason"] for e in res["rollback"]],
            ["DEADLINE_EXPIRED", "DEPENDENT_NONCE", "REVERT"],
        )
        self.assertEqual(res["dropped"],
                         [e["hash"] for e in res["rollback"]])

    def test_any_leg_expired_breaks_hit(self):
        # 无新字段时仍是夹子
        res, _ = core.process(self.sandwich())
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(len(res["hits"]), 1)

        for leg, only, at in ((0, "f", 0), (2, "b", 2)):
            dl = [None, None, None]
            dl[leg] = 5
            res, err = core.process(self.sandwich(dl, block=10))
            with self.subTest(leg=leg):
                self.assertIsNone(err)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["hits"], [])
                self.assertEqual(res["dropped"], [only])
                self.assertEqual(res["rollback"], [
                    {"hash": only, "at": at, "reason": "DEADLINE_EXPIRED"}])

        # victim 过期：不成命中；f 按既有 DEPENDENT_NONCE 规则回滚
        res, _ = core.process(self.sandwich((None, 5, None), block=10))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["order"], ["b"])
        self.assertEqual(
            [e["reason"] for e in res["rollback"]],
            ["DEPENDENT_NONCE", "DEADLINE_EXPIRED"],
        )

    def test_expired_sell_revert_only_expired(self):
        raw = payload("s", [
            tx("f", "MEV", 0, 10, side="buy"),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell", sim="revert", deadline=5),
        ], block=10)
        res, _ = core.process(raw)
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["rollback"][-1]["reason"], "DEADLINE_EXPIRED")

    def test_remaining_txs_still_produce_hits(self):
        # (buy,过期victim,sell) 不命中；(buy,正常victim,sell) 仍命中，
        # 命中的 at 为原相对位置
        raw = payload("m", [
            tx("f", "M", 0, 10, side="buy"),
            tx("vexp", "U1", 0, 5, side="buy", deadline=5),
            tx("v2", "U2", 0, 5, side="buy"),
            tx("b", "M", 1, 10, side="sell"),
        ], block=10)
        res, _ = core.process(raw)
        self.assertEqual(res["status"], "rejected")
        self.assertEqual([h["at"] for h in res["hits"]], [[0, 2, 3]])

    def test_all_expired_is_ok(self):
        res, _ = core.process(payload("a", [
            tx("f", "M", 0, 1, side="buy", deadline=1),
            tx("v", "U", 0, 1, side="buy", deadline=1),
            tx("b", "M", 1, 1, side="sell", deadline=1),
        ], block=100))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hits"], [])
        self.assertEqual(len(res["rollback"]), 3)
        self.assertTrue(all(e["reason"] == "DEADLINE_EXPIRED"
                            for e in res["rollback"]))

    def test_quarantine_with_expired(self):
        raw = payload("q", [
            tx("f", "MEV", 0, 10, side="buy"),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
            tx("e", "OLD", 0, 2, deadline=3),
            tx("r", "OTHER", 0, 7, sim="revert"),
        ], policy="quarantine", block=10)
        res, err = core.process(raw)
        self.assertIsNone(err)
        self.assertEqual(res["status"], "mitigated")
        self.assertEqual(res["code"], "SANDWICH_MITIGATED")
        self.assertEqual([h["at"] for h in res["hits"]], [[0, 1, 2]])
        self.assertEqual(res["order"], ["v"])
        self.assertEqual(res["kept"], res["order"])
        # 按输入位置合并，每笔一次
        self.assertEqual(res["rollback"], [
            {"hash": "f", "at": 0, "reason": "SANDWICH_DETECTED"},
            {"hash": "b", "at": 2, "reason": "SANDWICH_DETECTED"},
            {"hash": "e", "at": 3, "reason": "DEADLINE_EXPIRED"},
            {"hash": "r", "at": 4, "reason": "REVERT"},
        ])
        self.assertEqual(res["dropped"], ["f", "b", "e", "r"])

    def test_quarantine_expired_leg_no_fresh_hit_is_ok(self):
        raw = payload("q", [
            tx("f", "MEV", 0, 10, side="buy", deadline=5),
            tx("v", "USER", 0, 5, side="buy"),
            tx("b", "MEV", 1, 10, side="sell"),
        ], policy="quarantine", block=10)
        res, _ = core.process(raw)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["code"], "")
        self.assertEqual(res["hits"], [])
        self.assertEqual(res["dropped"], ["f"])

    def test_bad_deadline_shape(self):
        bad_blocks = (True, 1.5, "3", None, -1, {}, [1])
        for value in bad_blocks:
            with self.subTest(kind="block", value=value):
                raw = json.dumps({"id": "z", "transactions": [], "block": value})
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

        good = {"hash": "h", "from": "A", "nonce": 0, "fee": 1,
                "token": "t", "side": "buy", "sim": "success"}
        for value in (True, 2.0, "x", None, -1):
            with self.subTest(kind="deadline", value=value):
                t = dict(good, deadline=value)
                raw = json.dumps({"id": "z", "block": 5, "transactions": [t]})
                res, err = core.process(raw)
                self.assertEqual(err, "BAD_DEADLINE")
                self.assertEqual(res["code"], "BAD_DEADLINE")

        # 有 deadline 无 block
        t = dict(good, deadline=5)
        res, err = core.process(json.dumps({"id": "z", "transactions": [t]}))
        self.assertEqual(err, "BAD_DEADLINE")

    def test_legacy_errors_take_priority(self):
        cases = [
            ("BAD_SCHEMA", {"id": "z", "block": "nope",
                            "transactions": [{"hash": "h"}]}),
            ("DUP_HASH", {"id": "z", "block": "bad", "transactions":
                          [tx("d", "A", 0, 1), tx("d", "B", 0, 1)]}),
            ("DUP_NONCE", {"id": "z", "transactions":
                           [tx("a", "A", 0, 1, deadline=9),
                            tx("b", "A", 0, 1)]}),
            ("BAD_SIDE", {"id": "z", "block": 5, "transactions":
                          [tx("h", "A", 0, 1, side="x", deadline=True)]}),
            ("BAD_SIM", {"id": "z", "transactions":
                         [tx("h", "A", 0, 1, sim="x", deadline=1)]}),
            ("BAD_POLICY", {"id": "z", "transactions": [],
                            "policy": "x", "block": "bad"}),
        ]
        for code, data in cases:
            with self.subTest(code=code):
                _, err = core.process(json.dumps(data))
                self.assertEqual(err, code)

    def test_cli_bad_deadline_exit_two(self):
        sin = io.BytesIO(json.dumps(
            {"id": "cli", "transactions": [], "block": -1}).encode())
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 2)
        self.assertEqual(serr.getvalue(), "BAD_DEADLINE\n")
        parsed = json.loads(sout.getvalue())
        self.assertEqual(parsed["code"], "BAD_DEADLINE")
        self.assertEqual(parsed["id"], "cli")

    def test_cli_valid_deadline_exit_zero(self):
        sin = io.BytesIO(
            payload("ok", [tx("h", "A", 0, 1, deadline=10)], block=10).encode())
        sout, serr = io.BytesIO(), io.StringIO()
        code = run([], stdin_buffer=sin, stdout_buffer=sout, stderr=serr)
        self.assertEqual(code, 0)
        self.assertEqual(serr.getvalue(), "")
        self.assertEqual(json.loads(sout.getvalue())["order"], ["h"])


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
