"""mev_shield.bundleschedule 捆绑联合排程行为验证（标准库 unittest）。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import bundleschedule as bs
from mev_shield import decision


def tx(h, frm, nonce, fee, token="TKN", side="buy", sim="success", price=100,
       deadline=None, bundle="B"):
    entry = {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "token": token, "side": side, "sim": sim, "price": price,
        "bundle": bundle,
    }
    if deadline is not None:
        entry["deadline"] = deadline
    return entry


def request(ident, txs, block=10, window=2, capacity=2, market=None,
            base=100, slip=0.5, rb=1, policy=None, slippage_mode=None):
    data = {
        "id": ident,
        "transactions": txs,
        "market": market if market is not None else {"prices": {"TKN": 100}},
        "basePrice": base,
        "maxSlippage": slip,
        "rollbackLimit": rb,
        "block": block,
        "scheduleBlocks": window,
        "blockCapacity": capacity,
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
        [sys.executable, "-m", "mev_shield.bundleschedule"] + argv,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )


# 与多区块排程相同的夹子三笔；默认各自独立捆绑，可按块拆开
SANDWICH_TXS = [
    tx("front", "A", 0, 30, side="buy", price=100, bundle="X"),
    tx("victim", "B", 0, 20, side="buy", price=110, bundle="Y"),
    tx("back", "A", 1, 10, side="sell", price=105, bundle="Z"),
]


class TestBundleSchedule(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = bs.process(
            request("r", [tx("a", "A", 0, 1, bundle="g1")]))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "blocks", "unscheduled",
             "scheduledFee", "unscheduledFee", "totalDelay", "evidence",
             "feasible"],
        )
        self.assertTrue(res["feasible"])

    def test_all_scheduled_first_block(self):
        txs = [
            tx("h2", "A", 0, 7, bundle="g2"),
            tx("h1", "B", 0, 9, bundle="g1"),
            tx("h0", "C", 0, 9, bundle="g0"),
        ]
        res, err = bs.process(request("n", txs, window=2, capacity=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        # 三个单成员捆绑同进首块，段按首笔位置：输入顺序即段顺序
        self.assertEqual(res["baselineOrder"], ["h0", "h1", "h2"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["h2", "h1", "h0"]},
             {"block": 11, "order": []}],
        )
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(res["scheduledFee"], 25)
        self.assertEqual(res["unscheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)
        self.assertEqual(res["evidence"], [])

    def test_bundle_indivisible_capacity_exceeded(self):
        # 两笔同捆绑，单块容量 1：任何区块都放不下整组
        txs = [
            tx("a", "A", 0, 9, bundle="G"),
            tx("b", "B", 0, 8, bundle="G"),
        ]
        res, err = bs.process(
            request("c", txs, window=2, capacity=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], [])
        self.assertEqual(res["blocks"][1]["order"], [])
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(res["unscheduledFee"], 17)
        self.assertEqual(res["totalDelay"], 0)
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "G", "at": 0, "hashes": ["a", "b"],
              "reason": "CAPACITY_EXCEEDED"}],
        )

    def test_bundle_contiguous_segment_input_order(self):
        # 同组两笔费用低于中间组，但段内顺序固定为输入相对位置，
        # 区块内段按捆绑首笔位置：[lo0, lo2, hi1]
        txs = [
            tx("lo0", "A", 0, 1, bundle="G"),
            tx("hi1", "B", 0, 9, bundle="H"),
            tx("lo2", "C", 0, 2, bundle="G"),
        ]
        res, err = bs.process(request("s", txs, window=1, capacity=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], ["lo0", "lo2", "hi1"])

    def test_bundle_indivisible_across_blocks(self):
        # 同组两笔容量 2 窗口 2：必须同块；与另一单成员组争抢首块时
        # 整组进首块、单成员进次块更优（笔数相同延迟更小由组选择决定）
        txs = [
            tx("g0", "A", 0, 9, bundle="G"),
            tx("g1", "A", 1, 8, bundle="G"),
            tx("h0", "B", 0, 7, bundle="H"),
        ]
        res, err = bs.process(
            request("i", txs, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], ["g0", "g1"])
        self.assertEqual(res["blocks"][1]["order"], ["h0"])
        self.assertEqual(res["totalDelay"], 1)

    def test_group_expired_when_any_member_expired(self):
        # 组成员一笔过期：整组 DEADLINE_EXPIRED，哪怕容量也放不下
        txs = [
            tx("old", "A", 0, 50, deadline=5, bundle="G"),
            tx("new", "B", 0, 10, bundle="G"),
        ]
        res, err = bs.process(
            request("e", txs, block=10, window=2, capacity=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "G", "at": 0, "hashes": ["old", "new"],
              "reason": "DEADLINE_EXPIRED"}],
        )
        self.assertEqual(res["unscheduledFee"], 60)

    def test_group_deadline_restricts_block(self):
        # 组成员 deadline=10：整组只能进首块
        txs = [
            tx("b0", "B", 0, 8, deadline=10, bundle="BND"),
            tx("b1", "C", 0, 8, deadline=11, bundle="BND"),
            tx("f0", "D", 0, 9, bundle="FREE"),
        ]
        res, err = bs.process(
            request("d", txs, block=10, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], ["b0", "b1"])
        self.assertEqual(res["blocks"][1]["order"], ["f0"])
        self.assertEqual(res["totalDelay"], 1)

    def test_bundle_skipped_in_partial_optimum(self):
        # 单块容量 1：两个单成员组只能进一个，低费组 BUNDLE_SKIPPED
        txs = [
            tx("hi", "A", 0, 9, bundle="H"),
            tx("lo", "B", 0, 8, bundle="L"),
        ]
        res, err = bs.process(
            request("p", txs, window=1, capacity=1))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], ["hi"])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "L", "at": 1, "hashes": ["lo"],
              "reason": "BUNDLE_SKIPPED"}],
        )

    def test_sandwich_avoided_across_blocks_unique_bundles(self):
        # 各自独立捆绑：victim 整组排入次块即可全排程
        res, err = bs.process(
            request("sw2", SANDWICH_TXS, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        flat = [h for blk in res["blocks"] for h in blk["order"]]
        self.assertEqual(flat, ["front", "back", "victim"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["front", "back"]},
             {"block": 11, "order": ["victim"]}],
        )
        self.assertEqual(res["totalDelay"], 1)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["victim"], "victim")

    def test_sandwich_forces_group_skip(self):
        # 三笔同组：整组在任一块都是夹子顺序，只能整组放弃
        txs = [
            tx("front", "A", 0, 30, side="buy", price=100, bundle="G"),
            tx("victim", "B", 0, 20, side="buy", price=110, bundle="G"),
            tx("back", "A", 1, 10, side="sell", price=105, bundle="G"),
        ]
        res, err = bs.process(
            request("swg", txs, window=2, capacity=3))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "G", "at": 0,
              "hashes": ["front", "victim", "back"],
              "reason": "BUNDLE_SKIPPED"}],
        )
        self.assertEqual(len(res["evidence"]), 1)

    def test_unscheduled_sorted_by_first_position(self):
        txs = [
            tx("x", "A", 0, 1, bundle="X"),
            tx("y", "B", 0, 2, deadline=1, bundle="Y"),
            tx("z", "C", 0, 3, bundle="Z"),
        ]
        res, err = bs.process(
            request("u", txs, block=10, window=1, capacity=1))
        self.assertIsNone(err)
        ats = [entry["at"] for entry in res["unscheduled"]]
        self.assertEqual(ats, sorted(ats))
        by_bundle = {e["bundle"]: e["reason"] for e in res["unscheduled"]}
        self.assertEqual(by_bundle["Y"], "DEADLINE_EXPIRED")
        # X 可容纳但最优排不下：BUNDLE_SKIPPED
        self.assertEqual(by_bundle["X"], "BUNDLE_SKIPPED")

    def test_final_order_constraints(self):
        res, err = bs.process(
            request("ok", SANDWICH_TXS, window=2, capacity=2))
        self.assertIsNone(err)
        by_hash = {t["hash"]: t for t in SANDWICH_TXS}
        flat = [h for blk in res["blocks"] for h in blk["order"]]
        ordered = [by_hash[h] for h in flat]
        self.assertTrue(decision.nonce_order_satisfied(ordered))
        self.assertEqual(decision.detect_sandwich_evidence(ordered), [])

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, window=2, capacity=2)
        out = bs.serialize(bs.process(raw)[0])
        self.assertEqual(out, bs.serialize(bs.process(raw)[0]))
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference(req):
    """独立暴力参考：枚举每个捆绑（-1 或区块偏移）求相同四级最优。

    返回 (flat_order, fee, delay, feasible, unscheduled_reasons)。
    """
    txs = req["transactions"]
    block = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]
    last = block + window - 1
    input_pos = {t["hash"]: at for at, t in enumerate(txs)}

    order = []
    index = {}
    groups = []
    for at, t in enumerate(txs):
        bid = t["bundle"]
        if bid not in index:
            index[bid] = len(groups)
            groups.append({"id": bid, "first": at, "members": []})
        groups[index[bid]]["members"].append(at)
    groups.sort(key=lambda gr: gr["first"])

    expired = set()
    latest = []
    for gr in groups:
        horizon = last
        for at in gr["members"]:
            d = txs[at]["deadline"]
            if d is not None and d < horizon:
                horizon = d
        latest.append(horizon - block)
        if horizon < block:
            expired.add(len(latest) - 1)

    best = None
    g = len(groups)
    for choices in product(range(-1, window), repeat=g):
        loads = [0] * window
        ok = True
        for gi, off in enumerate(choices):
            if gi in expired:
                if off != -1:
                    ok = False
                    break
                continue
            if off < 0:
                continue
            if off > latest[gi]:
                ok = False
                break
            size = len(groups[gi]["members"])
            if loads[off] + size > capacity:
                ok = False
                break
            loads[off] += size
        if not ok:
            continue
        placed = [[] for _ in range(window)]
        for gi, off in enumerate(choices):
            if off >= 0:
                placed[off].append(gi)
        ordered = []
        for off in range(window):
            for gi in placed[off]:
                ordered.extend(txs[at] for at in groups[gi]["members"])
        if not decision.nonce_order_satisfied(ordered):
            continue
        if decision.detect_sandwich_evidence(ordered):
            continue
        count = len(ordered)
        fee = sum(t["fee"] for t in ordered)
        delay = sum(off * len(groups[gi]["members"])
                    for gi, off in enumerate(choices) if off >= 0)
        seq = tuple(input_pos[t["hash"]] for t in ordered)
        key = (-count, -fee, delay, seq)
        if best is None or key < best[0]:
            best = (key, choices)

    key, choices = best
    flat = []
    placed = [[] for _ in range(window)]
    for gi, off in enumerate(choices):
        if off >= 0:
            placed[off].append(gi)
    for off in range(window):
        for gi in placed[off]:
            flat.extend(txs[at]["hash"] for at in groups[gi]["members"])
    reasons = {}
    for gi, off in enumerate(choices):
        if off >= 0:
            continue
        gr = groups[gi]
        if gi in expired:
            reason = "DEADLINE_EXPIRED"
        else:
            size = len(gr["members"])
            fits = any(latest[gi] >= off and size <= capacity
                       for off in range(window))
            reason = "CAPACITY_EXCEEDED" if not fits else "BUNDLE_SKIPPED"
        reasons[gr["id"]] = reason
    return flat, -key[1], key[2], all(off >= 0 for off in choices), reasons


class TestGlobalOptimum(unittest.TestCase):
    def _bundle(self, n, seed, window, capacity, n_groups, block=10):
        # 固定序列伪随机：多发送者连续 nonce、双向、各种价格、期限与分组
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
            pick = (i * 3 + seed) % 4
            deadline = None
            if pick == 1:
                deadline = block - 1  # 组成员可能过期
            elif pick == 2:
                deadline = block + (i + seed) % (window + 1)
            bid = f"G{(i * 5 + seed * 3) % n_groups}"
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, side=side,
                          sim=sim, price=price, deadline=deadline,
                          bundle=bid))
        return request(f"g{n}_{seed}_{window}_{capacity}_{n_groups}", txs,
                       block=block, window=window, capacity=capacity)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 8):
            for seed in range(3):
                for window, capacity in ((1, 1), (2, 1), (2, 2), (3, 2)):
                    for n_groups in (1, 2, 3):
                        raw = self._bundle(n, seed, window, capacity, n_groups)
                        req = bs.parse_request(raw)
                        if len(req["transactions"]) > 8:
                            continue
                        ref_order, ref_fee, ref_delay, ref_feasible, ref_reasons = \
                            _reference(req)
                        res, err = bs.process(raw)
                        self.assertIsNone(err)
                        flat = [h for blk in res["blocks"] for h in blk["order"]]
                        self.assertEqual(flat, ref_order)
                        self.assertEqual(res["scheduledFee"], ref_fee)
                        self.assertEqual(res["totalDelay"], ref_delay)
                        self.assertEqual(res["feasible"], ref_feasible)
                        for entry in res["unscheduled"]:
                            self.assertEqual(
                                entry["reason"], ref_reasons[entry["bundle"]])
                            # at 即捆绑首笔的输入位置
                            pos = {t["hash"]: i
                                   for i, t in enumerate(req["transactions"])}
                            self.assertEqual(
                                entry["at"], pos[entry["hashes"][0]])
                        # blocks 区块号连续
                        for off, entry in enumerate(res["blocks"]):
                            self.assertEqual(entry["block"], 10 + off)
                        # 排程与未排程不重不漏覆盖全部交易
                        unsched_hashes = []
                        for entry in res["unscheduled"]:
                            unsched_hashes.extend(entry["hashes"])
                        self.assertEqual(
                            sorted(flat + unsched_hashes),
                            sorted(t["hash"] for t in req["transactions"]),
                        )
                        total_fee = sum(t["fee"] for t in req["transactions"])
                        self.assertEqual(
                            res["scheduledFee"] + res["unscheduledFee"],
                            total_fee)


class TestValidation(unittest.TestCase):
    def assert_error(self, raw, code, ident="e"):
        res, err = bs.process(raw)
        self.assertEqual(err, code)
        self.assertEqual(res["id"], ident)
        self.assertEqual(res["baselineOrder"], [])
        self.assertEqual(res["blocks"], [])
        self.assertEqual(res["unscheduled"], [])
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(res["unscheduledFee"], 0)
        self.assertEqual(res["totalDelay"], 0)
        self.assertEqual(res["evidence"], [])
        self.assertFalse(res["feasible"])

    def _raw(self, **overrides):
        data = json.loads(request("e", [tx("a", "A", 0, 1, bundle="g")]))
        data.update(overrides)
        return json.dumps(data)

    def test_missing_bundle(self):
        bad = tx("a", "A", 0, 1)
        del bad["bundle"]
        self.assert_error(request("e", [bad]), "BAD_BUNDLE_ID")

    def test_bad_bundle_values(self):
        for value in ("", 1, 0, None, True, False, ["g"], {"g": 1}):
            bad = tx("a", "A", 0, 1, bundle=value)
            self.assert_error(request("e", [bad]), "BAD_BUNDLE_ID")

    def test_bad_bundle_among_members(self):
        txs = [
            tx("a", "A", 0, 1, bundle="G"),
            tx("b", "B", 0, 1, bundle=""),
        ]
        self.assert_error(request("e", txs), "BAD_BUNDLE_ID")

    def test_schedule_checks_take_priority(self):
        bad = tx("a", "A", 0, 1, bundle="")
        bad["deadline"] = -1
        data = json.loads(request("e", [bad]))
        data["block"] = -1
        data["scheduleBlocks"] = 0
        data["blockCapacity"] = 0
        # block -> window -> capacity -> deadline 全部优先于 BAD_BUNDLE_ID
        self.assert_error(json.dumps(data), "BAD_BLOCK")
        data["block"] = 10
        self.assert_error(json.dumps(data), "BAD_SCHEDULE_WINDOW")
        data["scheduleBlocks"] = 2
        self.assert_error(json.dumps(data), "BAD_BLOCK_CAPACITY")
        data["blockCapacity"] = 2
        self.assert_error(json.dumps(data), "BAD_DEADLINE")

    def test_existing_errors_take_priority(self):
        self.assert_error(request("e", []), "EMPTY_BUNDLE")
        empty_hash = tx("", "A", 0, 1)
        del empty_hash["bundle"]
        self.assert_error(
            request("e", [empty_hash]),
            "UNIDENTIFIED_TRANSACTION")
        bad = tx("a", "A", 0, 1)
        del bad["price"]
        del bad["bundle"]
        self.assert_error(request("e", [bad]), "MISSING_MARKET_CONTEXT")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1)], slippage_mode="zzz"),
            "BAD_SLIPPAGE_MODE")

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
        self.assertEqual(res["blocks"][0]["order"], ["a"])
        self.assertTrue(res["feasible"])

    def test_partial_schedule_exit0_no_stderr(self):
        proc = run_cli(
            [], request("c", SANDWICH_TXS, window=1, capacity=3).encode())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertEqual(len(res["evidence"]), 1)

    def test_input_error_exit2(self):
        bad = tx("a", "A", 0, 1)
        del bad["bundle"]
        proc = run_cli([], request("c", [bad]).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_BUNDLE_ID\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["blocks"], [])
        self.assertEqual(res["scheduledFee"], 0)
        self.assertFalse(res["feasible"])

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
                f.write(request("file", SANDWICH_TXS, window=2, capacity=2))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            flat = [h for blk in res["blocks"] for h in blk["order"]]
            self.assertEqual(flat, ["front", "back", "victim"])
            self.assertTrue(res["feasible"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, window=2, capacity=2).encode()
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
