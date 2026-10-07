"""mev_shield.bundleisolate 捆绑原子性风险隔离计划行为验证（标准库 unittest）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import combinations, product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import bundleisolate as bi
from mev_shield import decision
from mev_shield import riskplan


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100,
       bundle="G"):
    return {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
        "bundle": bundle,
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
        [sys.executable, "-m", "mev_shield.bundleisolate"] + argv,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )


SANDWICH_GROUP = [
    tx("front", "A", 0, 30, side="buy", price=100, bundle="G"),
    tx("victim", "B", 0, 20, side="buy", price=110, bundle="G"),
    tx("back", "A", 1, 10, side="sell", price=105, bundle="G"),
]
SANDWICH_SOLO = [
    dict(t, bundle=bid) for t, bid in zip(
        [
            tx("front", "A", 0, 30, side="buy", price=100),
            tx("victim", "B", 0, 20, side="buy", price=110),
            tx("back", "A", 1, 10, side="sell", price=105),
        ],
        ("X", "Y", "Z"),
    )
]


class TestBundleIsolate(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = bi.process(
            request("b", [tx("a", "A", 0, 1, bundle="g")], limit=0))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "selectedOrder", "removed",
             "keptFee", "removedFee", "evidence", "blockers", "feasible",
             "isolationLimit"],
        )
        self.assertTrue(res["feasible"])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["isolationLimit"], 0)

    def test_no_risk_keeps_all_zero_budget(self):
        txs = [
            tx("h2", "A", 0, 7, bundle="g2"),
            tx("h1", "B", 0, 9, bundle="g1"),
            tx("h0", "C", 0, 9, bundle="g0"),
        ]
        res, err = bi.process(request("n", txs, limit=0))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["blockers"], [])

    def test_atomic_group_whole_removal(self):
        # 三笔同组构成夹子：只能整组移除，预算需覆盖 3 笔
        res, err = bi.process(request("g3", SANDWICH_GROUP, limit=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(
            res["removed"],
            [{"bundle": "G", "at": 0,
              "hashes": ["front", "victim", "back"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 60)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])

    def test_budget_counts_transactions_not_groups(self):
        # 同组 3 笔：预算 2（够 2 个单成员组、不够一个三成员组）无解
        res, err = bi.process(request("g2", SANDWICH_GROUP, limit=2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])
        self.assertEqual(res["isolationLimit"], 2)

    def test_independent_groups_single_removal(self):
        # 夹子三腿各自独立组：预算 1 移除后置腿所在组即可
        res, err = bi.process(request("s1", SANDWICH_SOLO, limit=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["front", "victim"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "Z", "at": 2, "hashes": ["back"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 50)
        self.assertEqual(res["removedFee"], 10)

    def test_unkeepable_member_removes_whole_group(self):
        # 组内一笔超滑点：整组不可保留；预算覆盖整组大小
        txs = [
            tx("ok", "A", 0, 10, price=100, bundle="K"),
            tx("bad", "B", 0, 50, price=200, bundle="G"),
            tx("mate", "C", 0, 7, price=100, bundle="G"),
        ]
        res, err = bi.process(request("sl", txs, limit=2, slip=0.5))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["ok"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "G", "at": 1, "hashes": ["bad", "mate"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["blockers"], ["SLIPPAGE_EXCEEDED"])

    def test_unkeepable_group_over_budget_infeasible(self):
        # 坏成员所在组 2 笔，预算 1：无法整组移除
        txs = [
            tx("bad", "B", 0, 50, price=200, bundle="G"),
            tx("mate", "C", 0, 7, price=100, bundle="G"),
        ]
        res, err = bi.process(request("sl0", txs, limit=1, slip=0.5))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["blockers"], ["SLIPPAGE_EXCEEDED"])

    def test_rollback_ratio_group_atomic(self):
        # revert 笔与成功笔同组：只移除 revert 会拆散同组，必须整组移除
        txs = [
            tx("r1", "A", 0, 5, sim="revert", bundle="G"),
            tx("s1", "B", 0, 50, bundle="G"),
            tx("s2", "C", 0, 40, bundle="H"),
        ]
        res, err = bi.process(request("rl", txs, limit=2, rb=0.3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["s2"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "G", "at": 0, "hashes": ["r1", "s1"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["blockers"], ["ROLLBACK_LIMIT_EXCEEDED"])

    def test_nonce_atomic_within_group(self):
        # f、b 同 from 且同组，保留 X、v 必须整组移除 G（2 笔预算）
        txs = [
            tx("X", "A", 2, 100, bundle="X"),
            tx("f", "A", 0, 30, side="buy", price=100, bundle="G"),
            tx("v", "B", 0, 20, side="buy", price=110, bundle="V"),
            tx("b", "A", 1, 10, side="sell", price=105, bundle="G"),
        ]
        res, err = bi.process(request("nc", txs, limit=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["X", "v"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "G", "at": 1, "hashes": ["f", "b"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 120)
        self.assertEqual(res["removedFee"], 40)

    def test_removed_sorted_by_group_first_position(self):
        # 两个被移除组：按首笔输入位置排列；段内 hashes 取输入相对顺序
        txs = [
            tx("g0", "A", 0, 1, price=200, bundle="G"),
            tx("h0", "B", 0, 9, price=100, bundle="H"),
            tx("g1", "C", 0, 2, price=200, bundle="G"),
            tx("k0", "D", 0, 8, price=100, bundle="K"),
            tx("k1", "E", 0, 3, price=200, bundle="K"),
        ]
        res, err = bi.process(request("ord", txs, limit=4, slip=0.5))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["h0"])
        self.assertEqual(
            [entry["bundle"] for entry in res["removed"]], ["G", "K"])
        self.assertEqual(
            res["removed"][0],
            {"bundle": "G", "at": 0, "hashes": ["g0", "g1"],
             "reason": "BUNDLE_REMOVED"},
        )
        self.assertEqual(
            res["removed"][1],
            {"bundle": "K", "at": 3, "hashes": ["k0", "k1"],
             "reason": "BUNDLE_REMOVED"},
        )

    def test_tiebreak_removed_group_position_sequence(self):
        # 三条腿 fee 相同、各自独立成组：移除任一条腿都能消除夹子且
        # fee / 笔数相同，取被移除组首笔输入位置序列最小者
        txs = [
            tx("ac", "A", 1, 10, side="sell", price=105, bundle="BACK"),
            tx("ab", "B", 0, 10, side="buy", price=110, bundle="VICTIM"),
            tx("aa", "A", 0, 10, side="buy", price=100, bundle="FRONT"),
        ]
        # 输入顺序 back(0)/victim(1)/front(2)，但基线按 hash 为
        # aa,ab,ac = front,victim,back，夹子成立
        res, err = bi.process(request("lex", txs, limit=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        # 移除 front 组（首笔位置 2）、victim 组（1）、back 组（0）均
        # 合法且同 fee/笔数；位置序列最小为 back 组的 [0]
        self.assertEqual(
            res["removed"],
            [{"bundle": "BACK", "at": 0, "hashes": ["ac"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["selectedOrder"], ["aa", "ab"])

    def test_fee_partition_invariant(self):
        # feasible 时 selectedOrder 与 removed 不重不漏，fee 之和为总和
        txs = [
            tx("f1", "A", 0, 50, price=100, bundle="F"),
            tx("v1", "B", 0, 40, price=110, bundle="V"),
            tx("mid", "A", 1, 30, side="sell", price=105, bundle="F"),
            tx("v2", "C", 0, 20, side="sell", price=100, bundle="W"),
            tx("b2", "A", 2, 10, price=102, bundle="F"),
        ]
        res, err = bi.process(request("inv", txs, limit=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        removed_hashes = []
        for entry in res["removed"]:
            removed_hashes.extend(entry["hashes"])
        covered = sorted(res["selectedOrder"] + removed_hashes)
        self.assertEqual(covered, sorted(t["hash"] for t in txs))
        self.assertEqual(res["keptFee"] + res["removedFee"], 150)
        removed_count = sum(len(entry["hashes"]) for entry in res["removed"])
        self.assertLessEqual(removed_count, 3)

    def test_selected_order_follows_baseline_relative_order(self):
        # 同组成员在输入中分散：selectedOrder 仍按基线相对顺序排列
        txs = [
            tx("lo0", "A", 0, 1, price=100, bundle="G"),
            tx("bad", "B", 0, 50, price=200, bundle="H"),
            tx("lo2", "C", 0, 9, price=100, bundle="G"),
        ]
        res, err = bi.process(request("rel", txs, limit=1, slip=0.5))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        # 移除 H（1 笔），保留 G 两笔；基线为 bad,lo2,lo0，保留即
        # [lo2, lo0]，而非输入顺序 [lo0, lo2]
        self.assertEqual(res["selectedOrder"], ["lo2", "lo0"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "H", "at": 1, "hashes": ["bad"],
              "reason": "BUNDLE_REMOVED"}],
        )

    def test_evidence_blockers_match_riskplan_baseline(self):
        raw = request("ev", SANDWICH_SOLO, limit=0)
        res, err = bi.process(raw)
        self.assertIsNone(err)
        # 无 bundle 的同一交易包上 riskplan 的基线证据与 blockers
        plain = json.loads(raw)
        for t in plain["transactions"]:
            t.pop("bundle", None)
        ref, rerr = riskplan.process(json.dumps(plain))
        self.assertIsNone(rerr)
        self.assertEqual(res["evidence"], ref["evidence"])
        self.assertEqual(res["blockers"], ref["blockers"])
        self.assertEqual(res["baselineOrder"], ref["baselineOrder"])

    def test_empty_set_legal_when_budget_covers_all(self):
        # 预算覆盖全部笔数时空保留集合恒合法（空集合 revert 占比为 0）
        res, err = bi.process(request("all", SANDWICH_GROUP, limit=3, rb=0))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        removed_hashes = [
            h for entry in res["removed"] for h in entry["hashes"]]
        self.assertEqual(removed_hashes, ["front", "victim", "back"])

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_SOLO, limit=1)
        out = bi.serialize(bi.process(raw)[0])
        self.assertEqual(out, bi.serialize(bi.process(raw)[0]))
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference(req):
    """独立暴力参考：枚举捆绑保留子集，按相同三级目标求全局最优。

    返回 (kept_hashes, kept_fee, removed_entries)；预算内无解返回 None。
    """
    txs = req["transactions"]
    limit = req["isolationLimit"]
    baseline = decision.order_transactions(txs)
    input_pos = {t["hash"]: at for at, t in enumerate(txs)}
    keepable = riskplan._keepable(req)
    rb_limit = req["rollbackLimit"]

    index = {}
    groups = []
    for at, t in enumerate(txs):
        bid = t["bundle"]
        if bid not in index:
            index[bid] = len(groups)
            groups.append({"id": bid, "first": at, "members": []})
        groups[index[bid]]["members"].append(at)
    groups.sort(key=lambda gr: gr["first"])
    g = len(groups)
    group_of = {}
    for gi, gr in enumerate(groups):
        for at in gr["members"]:
            group_of[at] = gi

    best = None
    for choices in product((False, True), repeat=g):
        removed_count = sum(
            len(groups[gi]["members"]) for gi in range(g) if not choices[gi])
        if removed_count > limit:
            continue
        kept_groups = {gi for gi in range(g) if choices[gi]}
        selected = [
            t for t in baseline
            if group_of[input_pos[t["hash"]]] in kept_groups
        ]
        if any(t["hash"] not in keepable for t in selected):
            continue
        if not decision.nonce_order_satisfied(selected):
            continue
        if decision.detect_sandwich_evidence(selected):
            continue
        if selected:
            reverts = sum(1 for t in selected if t["sim"] == "revert")
            if reverts / len(selected) > rb_limit:
                continue
        kept_hashes = [t["hash"] for t in selected]
        kept_fee = sum(t["fee"] for t in selected)
        removed_seq = [
            groups[gi]["first"] for gi in range(g) if not choices[gi]
        ]
        key = (-kept_fee, -len(kept_hashes), removed_seq)
        if best is None or key < best[0]:
            removed_entries = [
                {
                    "bundle": groups[gi]["id"],
                    "at": groups[gi]["first"],
                    "hashes": [txs[at]["hash"] for at in groups[gi]["members"]],
                    "reason": "BUNDLE_REMOVED",
                }
                for gi in range(g)
                if not choices[gi]
            ]
            best = (key, kept_hashes, kept_fee, removed_entries)
    if best is None:
        return None
    return best[1], best[2], best[3]


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, limit, n_groups, rb=1, slip=0.5):
        # 固定序列伪随机：多发送者连续 nonce、双向、各种价格与分组
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
            bid = f"G{(i * 5 + seed * 3) % n_groups}"
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price, bundle=bid))
        return request(f"g{n}_{seed}_{limit}_{n_groups}", txs,
                       limit=limit, rb=rb, slip=slip)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 9):
            for seed in range(6):
                for limit in (0, 1, 2, n):
                    for n_groups in (1, 2, 3):
                        for rb, slip in ((1, 0.5), (0.3, 0.5), (1, 0.05)):
                            raw = self._bundle(
                                n, seed, limit, n_groups,
                                rb=rb, slip=slip)
                            req = bi.parse_request(raw)
                            ref = _reference(req)
                            res, err = bi.process(raw)
                            self.assertIsNone(err)
                            total_fee = sum(
                                t["fee"] for t in req["transactions"])
                            if ref is None:
                                self.assertFalse(res["feasible"])
                                self.assertEqual(res["selectedOrder"], [])
                                self.assertEqual(res["removed"], [])
                                self.assertEqual(res["keptFee"], 0)
                                self.assertEqual(res["removedFee"], 0)
                            else:
                                kept_hashes, kept_fee, removed_entries = ref
                                self.assertTrue(res["feasible"])
                                self.assertEqual(
                                    res["selectedOrder"], kept_hashes)
                                self.assertEqual(res["keptFee"], kept_fee)
                                self.assertEqual(
                                    res["removedFee"], total_fee - kept_fee)
                                self.assertEqual(
                                    res["removed"], removed_entries)
                                removed_count = sum(
                                    len(e["hashes"]) for e in res["removed"])
                                self.assertLessEqual(removed_count, limit)


class TestValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = bi.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["evidence"], [])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_missing_bundle(self):
        bad = tx("a", "A", 0, 1)
        del bad["bundle"]
        self.assert_error(request("e", [bad], limit=1), "BAD_BUNDLE_ID")

    def test_bad_bundle_values(self):
        for value in ("", 1, 0, None, True, False, ["g"], {"g": 1}):
            bad = tx("a", "A", 0, 1, bundle=value)
            self.assert_error(request("e", [bad], limit=1), "BAD_BUNDLE_ID")

    def test_bad_bundle_among_members(self):
        txs = [
            tx("a", "A", 0, 1, bundle="G"),
            tx("b", "B", 0, 1, bundle=""),
        ]
        self.assert_error(request("e", txs, limit=2), "BAD_BUNDLE_ID")

    def test_missing_limit(self):
        raw = json.loads(request("e", [tx("a", "A", 0, 1, bundle="g")]))
        del raw["isolationLimit"]
        self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_bool_limit(self):
        for value in (True, False):
            raw = json.loads(request("e", [tx("a", "A", 0, 1, bundle="g")]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_negative_or_non_integer_limit(self):
        self.assert_error(
            request("e", [tx("a", "A", 0, 1, bundle="g")], limit=-1),
            "BAD_ISOLATION_LIMIT",
        )
        for value in (1.5, "1", None, [1], {"x": 1}):
            raw = json.loads(request("e", [tx("a", "A", 0, 1, bundle="g")]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_limit_check_before_bundle(self):
        # isolationLimit 非法与 bundle 非法并存：BAD_ISOLATION_LIMIT 优先
        bad = tx("a", "A", 0, 1)
        del bad["bundle"]
        raw = json.loads(request("e", [bad]))
        del raw["isolationLimit"]
        self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_existing_errors_take_priority(self):
        # 统一决策全部错误优先于 BAD_ISOLATION_LIMIT 与 BAD_BUNDLE_ID
        self.assert_error(request("e", []), "EMPTY_BUNDLE")
        self.assert_error(request("e", [tx("", "A", 0, 1)]),
                          "UNIDENTIFIED_TRANSACTION")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1, bundle="G"),
                          tx("a", "B", 0, 2, bundle="G")]),
            "DUPLICATE_TRANSACTION",
        )
        self.assert_error(
            request("e", [tx("a0", "A", 0, 1, bundle="G"),
                          tx("a2", "A", 2, 1, bundle="G")]),
            "ORDERING_CONFLICT",
        )
        bad = tx("a", "A", 0, 1, bundle="")
        del bad["price"]
        self.assert_error(request("e", [bad]), "MISSING_MARKET_CONTEXT")
        self.assert_error(request("e", [tx("a", "A", 0, 1, bundle="")],
                                 base=0),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1, bundle="")],
                                 slip=2),
                          "INVALID_RISK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1, bundle="")],
                                 rb=2),
                          "INVALID_ROLLBACK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1, bundle="")],
                                 policy="x"),
                          "BAD_POLICY")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1, bundle="")],
                    slippage_mode="zzz"),
            "BAD_SLIPPAGE_MODE",
        )

    def test_bad_json_schema(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli(
            [], request("c", [tx("a", "A", 0, 1, bundle="g")]).encode())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], ["a"])
        self.assertTrue(res["feasible"])

    def test_module_entry_infeasible_exit0_no_stderr(self):
        proc = run_cli([], request("c", SANDWICH_GROUP, limit=0).encode())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])

    def test_module_entry_bad_bundle_exit2(self):
        bad = tx("a", "A", 0, 1)
        del bad["bundle"]
        proc = run_cli([], request("c", [bad]).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_BUNDLE_ID\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_module_entry_bad_limit_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1, bundle="g")]))
        del raw["isolationLimit"]
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stderr.decode("utf-8"), "BAD_ISOLATION_LIMIT\n")
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
                f.write(request("file", SANDWICH_SOLO, limit=1))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["selectedOrder"], ["front", "victim"])
            self.assertTrue(res["feasible"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_SOLO, limit=1).encode()
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
