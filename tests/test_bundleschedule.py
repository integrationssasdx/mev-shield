"""mev_shield.bundleschedule 捆绑联合排程行为验证（标准库 unittest）。"""

import json
import os
import subprocess
import sys
import unittest
from itertools import permutations, product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mev_shield import bundleschedule as bs
from mev_shield import decision


def tx(h, frm, nonce, fee, bundle, token="TKN", side="buy", sim="success",
       price=100, deadline=None):
    entry = {
        "hash": h, "from": frm, "nonce": nonce, "fee": fee,
        "bundle": bundle, "token": token, "side": side, "sim": sim,
        "price": price,
    }
    if deadline is not None:
        entry["deadline"] = deadline
    return entry


def request(ident, txs, block=10, window=2, capacity=3, market=None,
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


SANDWICH_TXS = [
    tx("front", "A", 0, 30, "X", side="buy", price=100),
    tx("victim", "B", 0, 20, "Y", side="buy", price=110),
    tx("back", "A", 1, 10, "X", side="sell", price=105),
]


class TestBundleSchedule(unittest.TestCase):
    def test_fixed_keys(self):
        res, err = bs.process(
            request("r", [tx("a", "A", 0, 1, "X"), tx("b", "B", 0, 2, "X")]))
        self.assertIsNone(err)
        self.assertEqual(
            list(res.keys()),
            ["id", "baselineOrder", "blocks", "unscheduled", "scheduledFee",
             "unscheduledFee", "totalDelay", "evidence", "feasible"],
        )
        self.assertTrue(res["feasible"])

    def test_bundle_entire_in_one_contiguous_segment_input_order(self):
        # 捆绑 X 两笔 fee 逆序：段内顺序固定为输入相对位置，不按 fee 排
        txs = [
            tx("xlo", "A", 0, 5, "X"),
            tx("y", "B", 0, 9, "Y"),
            tx("xhi", "A", 1, 8, "X"),
        ]
        res, err = bs.process(request("seg", txs, window=1, capacity=3))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        # 基线仍按 fee 降序 hash 升序
        self.assertEqual(res["baselineOrder"], ["y", "xhi", "xlo"])
        # 全局顺序中 X 必须连续且保持输入顺序 [xlo, xhi]
        self.assertEqual(
            res["blocks"][0]["order"], ["xlo", "xhi", "y"])
        self.assertEqual(res["totalDelay"], 0)

    def test_segment_cannot_split_across_blocks(self):
        # 容量 2：X 两笔必须同块，Y 单笔；首块容量不足以再容纳 Y 时
        # X 整组首块、Y 次块
        txs = [
            tx("x1", "A", 0, 5, "X"),
            tx("y1", "B", 0, 9, "Y"),
            tx("x2", "A", 1, 8, "X"),
        ]
        res, err = bs.process(request("split", txs, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["x1", "x2"]},
             {"block": 11, "order": ["y1"]}],
        )
        self.assertEqual(res["totalDelay"], 1)

    def test_bundle_ordering_by_first_member_position(self):
        # 首笔位置 X 在 Y 前；延迟并列、序列比较时初始次序取首笔位置
        txs = [
            tx("x1", "A", 0, 1, "X"),
            tx("y1", "B", 0, 1, "Y"),
            tx("x2", "C", 0, 1, "X"),
            tx("y2", "D", 0, 1, "Y"),
        ]
        res, err = bs.process(request("ord", txs, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        merged = [h for blk in res["blocks"] for h in blk["order"]]
        # 最优方案之一：X 整组首块（输入下标 [0,2]），序列字典序最小
        self.assertEqual(merged, ["x1", "x2", "y1", "y2"])

    def test_whole_bundle_expired(self):
        # 任一成员过期：整组过期，不参与排程与夹子判定
        txs = [
            tx("x1", "A", 0, 5, "X", deadline=5),
            tx("x2", "A", 1, 8, "X"),
            tx("y1", "B", 0, 7, "Y"),
        ]
        res, err = bs.process(request("exp", txs))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "X", "at": 0, "hashes": ["x1", "x2"],
              "reason": "DEADLINE_EXPIRED"}],
        )
        self.assertEqual(res["scheduledFee"], 7)
        self.assertEqual(res["unscheduledFee"], 13)

    def test_capacity_exceeded_classification(self):
        # X 三笔超过单块容量 2，窗口仅一块：CAPACITY_EXCEEDED；Y 排入
        txs = [
            tx("x1", "A", 0, 5, "X"),
            tx("x2", "B", 0, 6, "X"),
            tx("x3", "C", 0, 7, "X"),
            tx("y1", "D", 0, 9, "Y"),
        ]
        res, err = bs.process(request("cap", txs, window=1, capacity=2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "X", "at": 0,
              "hashes": ["x1", "x2", "x3"],
              "reason": "CAPACITY_EXCEEDED"}],
        )
        self.assertEqual(res["blocks"][0]["order"], ["y1"])

    def test_deadline_competition_marks_skipped(self):
        # Y、X 均被期限钉在首块（容量 2）：X(fee 11) 整组排入，Y 排不进
        # 最优方案，但 Y 单笔本身不超容量，故记 BUNDLE_SKIPPED 而非
        # CAPACITY_EXCEEDED（超容量是捆绑体量相对区块容量的固有属性）
        txs = [
            tx("y1", "D", 0, 9, "Y", deadline=10),
            tx("x1", "A", 0, 5, "X", deadline=10),
            tx("x2", "B", 0, 6, "X", deadline=10),
        ]
        res, err = bs.process(
            request("capd", txs, block=10, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], ["x1", "x2"])
        self.assertEqual(res["blocks"][1]["order"], [])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "Y", "at": 0, "hashes": ["y1"],
              "reason": "BUNDLE_SKIPPED"}],
        )

    def test_expired_takes_priority_over_capacity(self):
        # 体量超容量且成员过期：整组过期优先
        txs = [
            tx("x1", "A", 0, 5, "X", deadline=5),
            tx("x2", "B", 0, 6, "X"),
            tx("x3", "C", 0, 7, "X"),
        ]
        res, err = bs.process(request("ec", txs, window=1, capacity=2))
        self.assertIsNone(err)
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "X", "at": 0,
              "hashes": ["x1", "x2", "x3"],
              "reason": "DEADLINE_EXPIRED"}],
        )

    def test_bundle_skipped_in_partial_optimum(self):
        # 容量与期限均允许全部，但全局夹子 / nonce 使最优方案跳过捆绑：
        # 单块容量 3，基线夹子；X=[front,back]、Y=[victim]，全排是
        # [front,back,victim]（按段），非夹子，故应全排。改为两捆绑
        # 排列必然成夹子的场景：X=[front?] ... 这里直接验证跳过原因：
        # 窗口 1、容量 2，三个单成员捆绑，fee 最低者 BUNDLE_SKIPPED
        txs = [
            tx("a", "A", 0, 9, "X"),
            tx("b", "B", 0, 8, "Y"),
            tx("c", "C", 0, 7, "Z"),
        ]
        res, err = bs.process(request("skip", txs, window=1, capacity=2))
        self.assertIsNone(err)
        self.assertFalse(res["feasible"])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "Z", "at": 2, "hashes": ["c"],
              "reason": "BUNDLE_SKIPPED"}],
        )

    def test_member_deadline_intersection(self):
        # X 两笔：一笔 deadline=10（仅首块）、另一笔无期限；X 整组只能
        # 进首块并占满容量 2，Y 只能退次块
        txs = [
            tx("x1", "A", 0, 5, "X", deadline=10),
            tx("y1", "B", 0, 9, "Y"),
            tx("x2", "C", 0, 6, "X"),
        ]
        res, err = bs.process(
            request("dl", txs, block=10, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["x1", "x2"]},
             {"block": 11, "order": ["y1"]}],
        )
        self.assertEqual(res["totalDelay"], 1)

    def test_nonce_constraint_within_global_order(self):
        # X 内两笔同 from，输入顺序已保证 nonce 递增；跨捆绑同 from
        # 逆序时方案非法，须跳过或调整段顺序
        txs = [
            tx("a1", "A", 1, 5, "X"),
            tx("b0", "A", 0, 9, "Y"),
        ]
        # 单块容量 2：段顺序 [X,Y] -> nonce 1 再 0 非法；[Y,X] 合法
        res, err = bs.process(request("nc", txs, window=1, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(res["blocks"][0]["order"], ["b0", "a1"])

    def test_sandwich_baseline_evidence_and_feasible_split(self):
        # X=[front,back]、Y=[victim]；窗口两块容量 2：X 首块、Y 次块，
        # 全局 [front,back,victim] 无夹子，整组排入，evidence 仍报告基线
        res, err = bs.process(
            request("sw", SANDWICH_TXS, window=2, capacity=2))
        self.assertIsNone(err)
        self.assertTrue(res["feasible"])
        self.assertEqual(
            res["blocks"],
            [{"block": 10, "order": ["front", "back"]},
             {"block": 11, "order": ["victim"]}],
        )
        self.assertEqual(res["totalDelay"], 1)
        self.assertEqual(len(res["evidence"]), 1)
        self.assertEqual(res["evidence"][0]["at"], [0, 1, 2])
        self.assertEqual(res["evidence"][0]["victim"], "victim")

    def test_sandwich_forces_partial(self):
        # 窗口 1、容量 3：任何段排列中 front 与 back 连续（同捆绑），
        # victim 放前 [victim,front,back]：victim 与 front 同向买、
        # back 卖，但首尾不同 from，不构成三段；检查无夹子方案全排。
        # 改为构造必然夹子：单捆绑三笔本身即夹子，无法拆段
        txs = [
            tx("front", "A", 0, 30, "X", side="buy", price=100),
            tx("victim", "B", 0, 20, "X", side="buy", price=110),
            tx("back", "A", 1, 10, "X", side="sell", price=105),
        ]
        res, err = bs.process(request("swp", txs, window=2, capacity=3))
        self.assertIsNone(err)
        # 单捆绑整组不可拆，任何排入都保留段内顺序 => 夹子，只能跳过
        self.assertFalse(res["feasible"])
        self.assertEqual(
            res["unscheduled"],
            [{"bundle": "X", "at": 0,
              "hashes": ["front", "victim", "back"],
              "reason": "BUNDLE_SKIPPED"}],
        )
        self.assertEqual(res["scheduledFee"], 0)
        self.assertEqual(res["unscheduledFee"], 60)
        self.assertEqual(res["totalDelay"], 0)

    def test_scheduled_order_valid(self):
        res, err = bs.process(
            request("ok", SANDWICH_TXS, window=2, capacity=2))
        self.assertIsNone(err)
        by_hash = {t["hash"]: t for t in SANDWICH_TXS}
        merged = [h for blk in res["blocks"] for h in blk["order"]]
        ordered = [by_hash[h] for h in merged]
        self.assertTrue(decision.nonce_order_satisfied(ordered))
        self.assertEqual(decision.detect_sandwich_evidence(ordered), [])

    def test_unscheduled_sorted_by_first_position(self):
        txs = [
            tx("x1", "A", 0, 1, "X"),
            tx("y1", "B", 0, 2, "Y", deadline=5),
            tx("x2", "C", 0, 3, "X"),
            tx("y2", "D", 0, 4, "Y"),
            tx("z1", "E", 0, 5, "Z"),
        ]
        res, err = bs.process(
            request("u", txs, block=10, window=1, capacity=2))
        self.assertIsNone(err)
        ats = [entry["at"] for entry in res["unscheduled"]]
        self.assertEqual(ats, sorted(ats))
        reasons = {entry["bundle"]: entry["reason"]
                   for entry in res["unscheduled"]}
        self.assertEqual(reasons["Y"], "DEADLINE_EXPIRED")

    def test_deterministic_bytes(self):
        raw = request("det", SANDWICH_TXS, window=2, capacity=2)
        self.assertEqual(
            bs.serialize(bs.process(raw)[0]),
            bs.serialize(bs.process(raw)[0]),
        )
        out = bs.serialize(bs.process(raw)[0])
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn(" ", out.strip())


def _reference_schedule(req):
    """独立暴力参考实现：枚举每捆绑跳过 / 区块 / 块内段排列。

    返回 (merged_order, scheduled_fee, total_delay, feasible,
    unscheduled_meta)。unscheduled_meta: {bundle_id: reason}。
    """
    txs = req["transactions"]
    block = req["block"]
    window = req["scheduleBlocks"]
    capacity = req["blockCapacity"]
    last = block + window - 1
    input_pos = {t["hash"]: at for at, t in enumerate(txs)}

    grouped = {}
    first = {}
    for at, t in enumerate(txs):
        b = t["bundle"]
        grouped.setdefault(b, []).append(t)
        first.setdefault(b, at)
    bundles = sorted(grouped, key=lambda b: first[b])

    expired = {b for b in bundles
               if any(t["deadline"] is not None and t["deadline"] < block
                      for t in grouped[b])}
    latest = {}
    for b in bundles:
        if b in expired:
            continue
        horizon = last
        for t in grouped[b]:
            if t["deadline"] is not None:
                horizon = min(horizon, t["deadline"])
        latest[b] = horizon - block

    active = [b for b in bundles if b not in expired]

    best = None
    # 每个捆绑：-1 跳过，否则区块偏移
    for assign in product(range(-1, window), repeat=len(active)):
        per_block = [[] for _ in range(window)]
        loads = [0] * window
        ok = True
        for b, off in zip(active, assign):
            if off < 0:
                continue
            if off > latest[b] or loads[off] + len(grouped[b]) > capacity:
                ok = False
                break
            loads[off] += len(grouped[b])
            per_block[off].append(b)
        if not ok:
            continue
        # 枚举每块内捆绑的段排列
        perms = [list(permutations(per_block[off])) for off in range(window)]
        for arrangement in product(*perms):
            ordered = []
            for off in range(window):
                for b in arrangement[off]:
                    ordered.extend(grouped[b])
            if not decision.nonce_order_satisfied(ordered):
                continue
            if decision.detect_sandwich_evidence(ordered):
                continue
            fee = sum(t["fee"] for t in ordered)
            delay = sum(loads[off] * off for off in range(window))
            seq = tuple(input_pos[t["hash"]] for t in ordered)
            key = (-len(ordered), -fee, delay, seq)
            if best is None or key < best[0]:
                best = (key, [t["hash"] for t in ordered],
                        list(arrangement), loads)

    key, order, arrangement, loads = best
    # arrangement 为最优方案各块段排列，据此推导已排捆绑集合
    placed_ids = {b for off in range(window) for b in arrangement[off]}
    meta = {}
    for b in bundles:
        if b in placed_ids:
            continue
        if b in expired:
            meta[b] = "DEADLINE_EXPIRED"
        elif len(grouped[b]) > capacity:
            meta[b] = "CAPACITY_EXCEEDED"
        else:
            meta[b] = "BUNDLE_SKIPPED"
    feasible = not meta
    return order, -key[1], key[2], feasible, meta


class TestGlobalOptimum(unittest.TestCase):
    def _case(self, n, seed, window, capacity, block=10):
        # 固定序列伪随机：多发送者连续 nonce、捆绑标签、双向、价格、期限
        senders = ["A", "B", "C"]
        bundle_ids = ["X", "Y", "Z"]
        cursor = {s: 0 for s in senders}
        txs = []
        for i in range(n):
            s = senders[(i * 7 + seed * 3) % len(senders)]
            nonce = cursor[s]
            cursor[s] += 1
            b = bundle_ids[(i * 5 + seed) % len(bundle_ids)]
            side = "buy" if (i * 13 + seed) % 3 != 0 else "sell"
            sim = "success" if (i * 5 + seed) % 7 != 0 else "revert"
            price = 90 + ((i * 17 + seed * 3) % 41)
            pick = (i * 3 + seed) % 4
            deadline = None
            if pick == 1:
                deadline = block - 1
            elif pick == 2:
                deadline = block + (i + seed) % (window + 1)
            txs.append(tx(f"h{i:02d}{s}{nonce}", s, nonce,
                          1 + (i * 11 + seed * 5) % 60, b,
                          side=side, sim=sim, price=price,
                          deadline=deadline))
        return request(f"g{n}_{seed}_{window}_{capacity}", txs,
                       block=block, window=window, capacity=capacity)

    def test_matches_bruteforce_reference(self):
        for n in range(1, 7):
            for seed in range(4):
                for window, capacity in ((1, 1), (1, 2), (2, 1), (2, 2),
                                         (2, 3), (3, 2)):
                    raw = self._case(n, seed, window, capacity)
                    req = bs.parse_request(raw)
                    ref_order, ref_fee, ref_delay, ref_feasible, ref_meta = \
                        _reference_schedule(req)
                    res, err = bs.process(raw)
                    self.assertIsNone(err)
                    merged = [h for blk in res["blocks"]
                              for h in blk["order"]]
                    self.assertEqual(merged, ref_order)
                    self.assertEqual(res["scheduledFee"], ref_fee)
                    self.assertEqual(res["totalDelay"], ref_delay)
                    self.assertEqual(res["feasible"], ref_feasible)
                    # blocks 与全局顺序一致且区块号连续
                    for off, entry in enumerate(res["blocks"]):
                        self.assertEqual(entry["block"], 10 + off)
                    # unscheduled 原因与参考一致，按首笔位置升序
                    meta = {e["bundle"]: e["reason"]
                            for e in res["unscheduled"]}
                    self.assertEqual(meta, ref_meta)
                    ats = [e["at"] for e in res["unscheduled"]]
                    self.assertEqual(ats, sorted(ats))
                    for e in res["unscheduled"]:
                        self.assertEqual(
                            e["hashes"],
                            [t["hash"] for t in req["transactions"]
                             if t["bundle"] == e["bundle"]][:len(e["hashes"])])
                    # 费用守恒
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

    def test_bad_bundle_values(self):
        for value in (None, "", 5, True, False, ["X"], {"x": 1}):
            bad = tx("a", "A", 0, 1, "X")
            bad["bundle"] = value
            self.assert_error(request("e", [bad]), "BAD_BUNDLE_ID")

    def test_missing_bundle(self):
        bad = tx("a", "A", 0, 1, "X")
        del bad["bundle"]
        self.assert_error(request("e", [bad]), "BAD_BUNDLE_ID")

    def test_any_bad_bundle_fails(self):
        txs = [tx("a", "A", 0, 1, "X"), tx("b", "B", 0, 2, "Y")]
        txs[1]["bundle"] = ""
        self.assert_error(request("e", txs), "BAD_BUNDLE_ID")

    def test_window_errors_take_priority_over_bundle(self):
        bad = tx("a", "A", 0, 1, "X")
        data = json.loads(request("e", [bad]))
        data["block"] = -1
        self.assert_error(json.dumps(data), "BAD_BLOCK")
        data["block"] = 10
        data["scheduleBlocks"] = 0
        self.assert_error(json.dumps(data), "BAD_SCHEDULE_WINDOW")
        data["scheduleBlocks"] = 2
        data["blockCapacity"] = 0
        self.assert_error(json.dumps(data), "BAD_BLOCK_CAPACITY")
        data["blockCapacity"] = 2
        bad2 = tx("b", "B", 0, 1, "Y", deadline=-1)
        data["transactions"].append(bad2)
        self.assert_error(json.dumps(data), "BAD_DEADLINE")

    def test_existing_errors_take_priority(self):
        self.assert_error(request("e", []), "EMPTY_BUNDLE")
        bad = tx("", "A", 0, 1, "X")
        self.assert_error(request("e", [bad]), "UNIDENTIFIED_TRANSACTION")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1, "X"),
                          tx("a", "B", 0, 2, "Y")]),
            "DUPLICATE_TRANSACTION")
        self.assert_error(
            request("e", [tx("a0", "A", 0, 1, "X"),
                          tx("a2", "A", 2, 1, "Y")]),
            "ORDERING_CONFLICT")
        bad = tx("a", "A", 0, 1, "X")
        del bad["price"]
        self.assert_error(request("e", [bad]), "MISSING_MARKET_CONTEXT")
        self.assert_error(request("e", [tx("a", "A", 0, 1, "X")], base=0),
                          "INVALID_PRICE_BASE")
        self.assert_error(request("e", [tx("a", "A", 0, 1, "X")], slip=2),
                          "INVALID_RISK_LIMIT")
        self.assert_error(request("e", [tx("a", "A", 0, 1, "X")], rb=2),
                          "INVALID_ROLLBACK_LIMIT")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1, "X")], policy="x"),
            "BAD_POLICY")
        self.assert_error(
            request("e", [tx("a", "A", 0, 1, "X")], slippage_mode="zzz"),
            "BAD_SLIPPAGE_MODE")
        # 既有错误与 BAD_BUNDLE_ID 并存时既有错误优先
        data = json.loads(request("e", [tx("a", "A", 0, 1, "X")], slip=2))
        bad = tx("b", "B", 0, 1, None)
        data["transactions"].append(bad)
        self.assert_error(json.dumps(data), "INVALID_RISK_LIMIT")

    def test_bad_json_schema(self):
        self.assert_error(b"{not json", "BAD_JSON", ident="")
        self.assert_error(json.dumps([1, 2]), "BAD_SCHEMA", ident="")


class TestCli(unittest.TestCase):
    def test_module_entry_ok(self):
        proc = run_cli(
            [], request("c", [tx("a", "A", 0, 1, "X"),
                              tx("b", "B", 0, 2, "X")]).encode("utf-8"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertTrue(res["feasible"])

    def test_partial_schedule_exit0_no_stderr(self):
        # 单块容量 2：X 两笔占满，Y 被跳过，feasible 为 false 但退出 0
        proc = run_cli(
            [], request("c", SANDWICH_TXS, window=1, capacity=2).encode())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertFalse(res["feasible"])
        self.assertTrue(res["unscheduled"])

    def test_bad_bundle_exit2(self):
        bad = tx("a", "A", 0, 1, "X")
        del bad["bundle"]
        proc = run_cli([], request("c", [bad]).encode("utf-8"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr.decode("utf-8"), "BAD_BUNDLE_ID\n")
        res = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(res["blocks"], [])
        self.assertFalse(res["feasible"])

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
                f.write(request("file", SANDWICH_TXS, window=2, capacity=2))
            proc = run_cli(["--input", inp, "--output", outp])
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stderr, b"")
            with open(outp, encoding="utf-8") as f:
                res = json.load(f)
            self.assertTrue(res["feasible"])
            merged = [h for blk in res["blocks"] for h in blk["order"]]
            self.assertEqual(merged, ["front", "back", "victim"])

    def test_byte_identical(self):
        raw = request("same", SANDWICH_TXS, window=2, capacity=2).encode()
        self.assertEqual(run_cli([], raw).stdout, run_cli([], raw).stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
