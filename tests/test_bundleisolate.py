"""mev_shield.bundleisolate 捆绑风险隔离计划行为验证（标准库 unittest）。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import bundleisolate as bi
from mev_shield import decision
from mev_shield import riskplan


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success",
       price=100, bundle="B"):
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


SANDWICH_TXS = [
    tx("front", "A", 0, 30, side="buy", price=100, bundle="X"),
    tx("victim", "B", 0, 20, side="buy", price=110, bundle="Y"),
    tx("back", "A", 1, 10, side="sell", price=105, bundle="Z"),
]


class TestBundleIsolate(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = bi.process(request("b", [tx("a", "A", 0, 1)], limit=0))
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

    def test_keeps_all_zero_budget(self):
        txs = [
            tx("h2", "A", 0, 7, bundle="g1"),
            tx("h1", "B", 0, 9, bundle="g2"),
            tx("h0", "C", 0, 9, bundle="g1"),
        ]
        res, err = bi.process(request("n", txs, limit=0))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["selectedOrder"], ["h0", "h1", "h2"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 25)
        self.assertEqual(res["removedFee"], 0)

    def test_atomic_removal_multi_member_bundle(self):
        # 夹子三腿分属三个捆绑；预算 1 只够移除单笔捆绑 Z(back)，
        # 与 riskplan 单笔情形一致
        res, err = bi.process(request("s1", SANDWICH_TXS, limit=1))
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
        self.assertEqual(res["blockers"], ["SANDWICH_DETECTED"])

    def test_atomicity_forces_whole_bundle(self):
        # nonce 耦合的夹子场景（沿用 riskplan.nonce 用例）：f 与 b 同属
        # 捆绑 X。基线 Xfee(100)、f(30)、v(20)、b(10)，f-v-b 成夹子且
        # A 的 nonce 在基线上为 2,0,1。移除单笔捆绑（Xfee 或 v）均不能
        # 同时消除夹子与 nonce 冲突，只有整组移除 X（f、b 两笔）合法。
        txs = [
            tx("Xfee", "A", 2, 100, bundle="W"),
            tx("f", "A", 0, 30, side="buy", price=100, bundle="X"),
            tx("v", "B", 0, 20, side="buy", price=110, bundle="Y"),
            tx("b", "A", 1, 10, side="sell", price=105, bundle="X"),
        ]
        res, err = bi.process(request("a0", txs, limit=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertEqual(res["removedFee"], 0)
        self.assertGreaterEqual(len(res["evidence"]), 1)
        self.assertEqual(
            res["blockers"],
            ["SANDWICH_DETECTED", "NONCE_ORDER_VIOLATION"],
        )
        self.assertEqual(res["isolationLimit"], 1)

        res, err = bi.process(request("a2", txs, limit=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["Xfee", "v"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "X", "at": 1, "hashes": ["f", "b"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["keptFee"], 120)
        self.assertEqual(res["removedFee"], 40)

    def test_budget_counts_transactions_not_bundles(self):
        # 两个双成员捆绑各含一笔坏滑点：坏笔不可保留 -> 两个捆绑都必须
        # 整组移除，共耗 4 笔预算（而非 2 个捆绑）。预算 3 不可行，
        # 预算 4 可行
        txs = [
            tx("g1a", "A", 0, 30, price=100, bundle="G1"),
            tx("g1bad", "B", 0, 20, price=200, bundle="G1"),
            tx("g2bad", "C", 0, 20, price=200, bundle="G2"),
            tx("g2a", "D", 0, 30, price=100, bundle="G2"),
        ]
        res, err = bi.process(request("bc3", txs, limit=3, slip=0.5))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        res, err = bi.process(request("bc4", txs, limit=4, slip=0.5))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(
            [(e["bundle"], e["at"], e["hashes"]) for e in res["removed"]],
            [("G1", 0, ["g1a", "g1bad"]), ("G2", 2, ["g2bad", "g2a"])],
        )

    def test_slippage_removes_whole_bundle(self):
        # base 口径：bad 偏离超 maxSlippage，它与 ok 同组，必须整组
        # 移除；另一组 clean 保留
        txs = [
            tx("ok", "A", 0, 10, price=100, bundle="G"),
            tx("bad", "B", 0, 50, price=200, bundle="G"),
            tx("clean", "C", 0, 5, price=100, bundle="H"),
        ]
        res, err = bi.process(request("sl", txs, limit=2, slip=0.5))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["clean"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "G", "at": 0, "hashes": ["ok", "bad"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["blockers"], ["SLIPPAGE_EXCEEDED"])
        # 预算只够 1 笔：整组 2 笔移不动 -> 无解
        res, err = bi.process(request("sl0", txs, limit=1, slip=0.5))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["blockers"], ["SLIPPAGE_EXCEEDED"])

    def test_rollback_ratio_on_kept_bundles(self):
        # 捆绑 R 含 revert 成员 r1 与成功成员 s0（不同 sender 避免 nonce
        # 相关）：整组保留时 revert 占比 1/3 > 0.2，只能整组移除 R
        # （2 笔）；预算 1 不足，预算 2 可行
        txs = [
            tx("r1", "A", 0, 5, sim="revert", bundle="R"),
            tx("s0", "D", 0, 6, sim="success", bundle="R"),
            tx("s1", "B", 0, 50, bundle="S"),
            tx("s2", "C", 0, 40, bundle="T"),
        ]
        res, err = bi.process(request("rl0", txs, limit=1, rb=0.2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["blockers"], ["ROLLBACK_LIMIT_EXCEEDED"])

        res, err = bi.process(request("rl", txs, limit=2, rb=0.2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["s1", "s2"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "R", "at": 0, "hashes": ["r1", "s0"],
              "reason": "BUNDLE_REMOVED"}],
        )
        self.assertEqual(res["blockers"], ["ROLLBACK_LIMIT_EXCEEDED"])

    def test_removed_sorted_by_first_input_position(self):
        # 被移除多个捆绑：按首笔输入位置升序，hashes 保持输入相对顺序
        txs = [
            tx("g1a", "A", 0, 1, price=200, bundle="G1"),   # 超滑点
            tx("g2a", "B", 0, 2, price=100, bundle="G2"),
            tx("g1b", "C", 0, 3, price=200, bundle="G1"),   # 超滑点
            tx("g3a", "D", 0, 4, price=200, bundle="G3"),   # 超滑点
        ]
        res, err = bi.process(request("ord", txs, limit=3, slip=0.1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["g2a"])
        self.assertEqual(
            [(e["bundle"], e["at"], e["hashes"]) for e in res["removed"]],
            [("G1", 0, ["g1a", "g1b"]), ("G3", 3, ["g3a"])],
        )
        self.assertTrue(all(e["reason"] == "BUNDLE_REMOVED")
                        for e in res["removed"])

    def test_fee_partition_invariant(self):
        txs = [
            tx("f1", "A", 0, 50, price=100, bundle="P"),
            tx("v1", "B", 0, 40, price=110, bundle="Q"),
            tx("mid", "A", 1, 30, side="sell", price=105, bundle="P"),
            tx("v2", "C", 0, 20, side="sell", price=100, bundle="R"),
            tx("b2", "A", 2, 10, price=102, bundle="P"),
        ]
        res, err = bi.process(request("inv", txs, limit=4))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        removed_hashes = [h for e in res["removed"] for h in e["hashes"]]
        covered = sorted(res["selectedOrder"] + removed_hashes)
        self.assertEqual(covered, sorted(t["hash"] for t in txs))
        self.assertEqual(res["keptFee"] + res["removedFee"], 150)
        # 被移除笔数不超过预算
        self.assertEqual(len(removed_hashes), 2)

    def test_selected_order_follows_baseline_relative_order(self):
        # 同组两笔在输入中 fee 顺序颠倒，selectedOrder 仍按基线相对顺序
        txs = [
            tx("lo", "A", 0, 10, bundle="G"),
            tx("hi", "B", 0, 90, bundle="G"),
            tx("x", "C", 0, 50, price=200, bundle="H"),
        ]
        res, err = bi.process(request("rel", txs, limit=1, slip=0.5))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["hi", "lo"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "H", "at": 2, "hashes": ["x"],
              "reason": "BUNDLE_REMOVED"}],
        )

    def test_lexicographic_tiebreak_by_first_position(self):
        # 三个单笔捆绑 fee 均为 10，基线 aa,mm,zz 成夹子；移除前置
        # 捆绑 zz（首笔输入位置 0）或后置捆绑 aa（首笔输入位置 2）
        # 等价。按首笔输入位置序列取更小者：移除 zz（[0] < [2]）。
        # 注意与 riskplan 的 hash 序列口径（移除 aa）不同
        txs = [
            tx("zz", "A", 1, 10, side="sell", price=105, bundle="Gz"),
            tx("mm", "B", 0, 10, side="buy", price=110, bundle="Gm"),
            tx("aa", "A", 0, 10, side="buy", price=100, bundle="Ga"),
        ]
        res, err = bi.process(request("lex", txs, limit=1))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["selectedOrder"], ["aa", "mm"])
        self.assertEqual(
            res["removed"],
            [{"bundle": "Gz", "at": 0, "hashes": ["zz"],
              "reason": "BUNDLE_REMOVED"}],
        )

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, limit=1)
        self.assertEqual(
            bi.serialize(bi.process(raw)[0]),
            bi.serialize(bi.process(raw)[0]),
        )
        out = bi.serialize(bi.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_bundleisolate(req):
    """独立参考实现：枚举原子捆绑方案，按三级目标选全局最优。

    返回 (kept_fee, kept_hashes)；预算内无解返回 None。
    """
    txs = req["transactions"]
    limit = req["isolationLimit"]
    baseline = decision.order_transactions(txs)
    keepable = riskplan._keepable(req)
    rb_limit = req["rollbackLimit"]

    groups = bi._build_groups(txs)
    members_of_hash = {}
    for grp in groups:
        for at in grp["members"]:
            members_of_hash[txs[at]["hash"]] = grp["first"]

    best = None
    g = len(groups)
    for removed_idx in range(1 << g):
        removed_groups = set()
        removed_count = 0
        removed_firsts = []
        for gi in range(g):
            if (removed_idx >> gi) & 1:
                removed_groups.add(gi)
                removed_count += len(groups[gi]["members"])
                removed_firsts.append(groups[gi]["first"])
        if removed_count > limit:
            continue
        selected = [
            t for t in baseline
            if members_of_hash[t["hash"]] not in removed_groups
        ]
        if any(t["hash"] not in keepable for t in selected):
            continue
        if decision.detect_sandwich_evidence(selected):
            continue
        if not decision.nonce_order_satisfied(selected):
            continue
        if selected:
            reverts = sum(1 for t in selected if t["sim"] == "revert")
            if reverts / len(selected) > rb_limit:
                continue
        kept_fee = sum(t["fee"] for t in selected)
        removed_firsts.sort()
        key = (-kept_fee, -len(selected), removed_firsts)
        if best is None or key < best[0]:
            best = (key, [t["hash"] for t in selected])
    if best is None:
        return None
    return -best[0][0], best[1]


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, limit, rb=1, slip=0.5, n_bids=3):
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
            bid = f"g{(i * 3 + seed) % n_bids}"
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price, bundle=bid))
        return request(f"g{n}_{seed}_{limit}", txs, limit=limit,
                       rb=rb, slip=slip)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 9):
            for seed in range(6):
                for limit in (0, 1, 2, n):
                    for rb in (0.3, 1):
                        raw = self._bundle(n, seed, limit, rb=rb)
                        req = bi.parse_request(raw)
                        ref = _reference_bundleisolate(req)
                        res, err = bi.process(raw)
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
        raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
        del raw["transactions"][0]["bundle"]
        self.assert_error(json.dumps(raw), "BAD_BUNDLE_ID")

    def test_empty_bundle(self):
        for value in ("", 1, True, None, ["x"], {}):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["transactions"][0]["bundle"] = value
            self.assert_error(json.dumps(raw), "BAD_BUNDLE_ID")

    def test_missing_limit(self):
        raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
        del raw["isolationLimit"]
        self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_bad_limit(self):
        for value in (True, False, -1, 1.5, "1", None, [1]):
            raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
            raw["isolationLimit"] = value
            self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_validation_order_limit_before_bundle(self):
        # isolationLimit 非法优先于 bundle 非法
        raw = json.loads(request("e", [tx("a", "A", 0, 1)]))
        raw["isolationLimit"] = -1
        del raw["transactions"][0]["bundle"]
        self.assert_error(json.dumps(raw), "BAD_ISOLATION_LIMIT")

    def test_existing_errors_take_priority(self):
        # 统一决策既有错误优先于两个新错误
        bad_empty = [dict(t, bundle="G") for t in []]
        self.assert_error(request("e", bad_empty), "EMPTY_BUNDLE")
        no_hash = [tx("", "A", 0, 1)]
        self.assert_error(request("e", no_hash), "UNIDENTIFIED_TRANSACTION")
        raw = json.loads(request(
            "e", [tx("a", "A", 0, 1), tx("a", "B", 0, 2, bundle="G")]))
        raw["transactions"][1]["bundle"] = ""
        # 重复 hash 先于 bundle 校验
        self.assert_error(json.dumps(raw), "DUPLICATE_TRANSACTION")
        raw = json.loads(request("e", [tx("a", "A", 0, 1)], slip=2))
        raw["transactions"][0]["bundle"] = ""
        self.assert_error(json.dumps(raw), "INVALID_RISK_LIMIT")
        raw = json.loads(
            request("e", [tx("a", "A", 0, 1)], slippage_mode="zzz"))
        raw["transactions"][0]["bundle"] = ""
        self.assert_error(json.dumps(raw), "BAD_SLIPPAGE_MODE")

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
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100, bundle="X"),
            tx("victim", "B", 0, 20, side="buy", price=110, bundle="X"),
            tx("back", "A", 1, 10, side="sell", price=105, bundle="X"),
        ]
        proc = run_cli([], request("c", txs, limit=0).encode("utf-8"))
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
        self.assertEqual(res["isolationLimit"], 0)
        self.assertEqual(
            res["baselineOrder"], ["front", "victim", "back"])

    def test_module_entry_input_error_exit2(self):
        proc = run_cli([], request("c", []).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "EMPTY_BUNDLE\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["selectedOrder"], [])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["blockers"], [])
        self.assertEqual(res["keptFee"], 0)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["isolationLimit"], 0)

    def test_module_entry_bad_bundle_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
        raw["transactions"][0]["bundle"] = ""
        proc = run_cli([], json.dumps(raw).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_BUNDLE_ID\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["removed"], [])
        self.assertFalse(res["feasible"])

    def test_module_entry_bad_limit_exit2(self):
        raw = json.loads(request("c", [tx("a", "A", 0, 1)]))
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
        import tempfile
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
