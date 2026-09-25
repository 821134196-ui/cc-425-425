"""计费边界测试：免费时长、向上取整、单日封顶、跨日切片、版本选择、优惠券。"""
from __future__ import annotations

import unittest

from parking_review import billing
from parking_review.world import build_world


def rule(conn, lot_id, when):
    return billing.select_rule_version(conn, lot_id, when)


class BillingBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.conn, self.lot = self.w.conn, self.w.lot_id

    def test_free_duration_boundary(self):
        r = rule(self.conn, self.lot, "2026-09-10T10:00:00")  # v2: 免费30分
        # 恰好 30 分钟：免费
        self.assertEqual(
            billing.compute_fee("2026-09-10T10:00:00", "2026-09-10T10:30:00", r)["base_fee_cents"], 0
        )
        # 31 分钟：超免 1 分钟，不足 1 小时按 1 小时 → 800 分
        fee = billing.compute_fee("2026-09-10T10:00:00", "2026-09-10T10:31:00", r)
        self.assertEqual(fee["base_fee_cents"], 800)
        # 恰好进入第 2 小时单位（91 分钟：30 免费 + 61 计费 → 2 单位）
        fee = billing.compute_fee("2026-09-10T10:00:00", "2026-09-10T11:31:00", r)
        self.assertEqual(fee["base_fee_cents"], 1600)
        # 整 2 小时（120 分钟：30 免费 + 90 计费 → 2 单位）
        fee = billing.compute_fee("2026-09-10T10:00:00", "2026-09-10T12:00:00", r)
        self.assertEqual(fee["base_fee_cents"], 1600)

    def test_v1_boundary_uses_15_minutes(self):
        r = rule(self.conn, self.lot, "2026-07-31T23:40:00")  # v1
        self.assertEqual(r.version, 1)
        fee = billing.compute_fee("2026-07-31T23:40:00", "2026-08-01T00:50:00", r)
        # 第1日 20 分：免 15，剩 5 → 1 单位 600；第2日 50 分无免费 → 1 单位 600
        self.assertEqual(fee["base_fee_cents"], 1200)
        self.assertTrue(fee["cross_day"])
        self.assertEqual(len(fee["slices"]), 2)
        # 免费时长只在入场当日抵扣一次
        self.assertEqual(fee["slices"][0]["free_deducted_minutes"], 15)
        self.assertEqual(fee["slices"][1]["free_deducted_minutes"], 0)

    def test_version_selected_by_entry_time(self):
        # 入场在 7 月底、出场在 8 月，仍按 v1
        self.assertEqual(rule(self.conn, self.lot, "2026-07-31T23:59:59").version, 1)
        self.assertEqual(rule(self.conn, self.lot, "2026-08-01T00:00:00").version, 2)

    def test_single_day_cap(self):
        r = rule(self.conn, self.lot, "2026-09-10T08:00:00")  # v2 封顶 8000
        # 停 12 小时：免 30 分后 690 分 → 12 单位 = 9600，封顶 8000
        fee = billing.compute_fee("2026-09-10T08:00:00", "2026-09-10T20:00:00", r)
        self.assertEqual(fee["base_fee_cents"], 8000)
        self.assertTrue(fee["slices"][0]["cap_applied"])

    def test_multi_day_caps_each_day(self):
        r = rule(self.conn, self.lot, "2026-09-23T21:10:00")
        # 跨 3 日：23 日 170 分、24 日 1440 分、25 日 580 分
        fee = billing.compute_fee("2026-09-23T21:10:00", "2026-09-25T09:40:00", r)
        self.assertEqual(len(fee["slices"]), 3)
        self.assertEqual([s["fee_cents"] for s in fee["slices"]],
                         [2400, 8000, 8000])  # 2400+8000+8000=18400
        self.assertEqual(fee["base_fee_cents"], 18400)

    def test_exit_before_entry_rejected(self):
        r = rule(self.conn, self.lot, "2026-09-10T10:00:00")
        with self.assertRaises(ValueError):
            billing.compute_fee("2026-09-10T12:00:00", "2026-09-10T11:00:00", r)

    def test_coupon_invalid_states(self):
        seq = 0

        def make(status="unused", **kw):
            nonlocal seq
            seq += 1
            cid = self.w.coupon(f"CP-T-{status}-{seq}", status=status, **kw)
            return self.conn.execute("SELECT * FROM coupon WHERE id=?", (cid,)).fetchone()

        with self.assertRaises(billing.CouponInvalid) as cm:
            billing.validate_coupon(make(status="used"), "2026-09-10T10:00:00", "测A-00001")
        self.assertEqual(cm.exception.code, "used")
        with self.assertRaises(billing.CouponInvalid):
            billing.validate_coupon(make(status="void"), "2026-09-10T10:00:00", "测A-00001")
        with self.assertRaises(billing.CouponInvalid):
            billing.validate_coupon(
                make(valid_from="2026-09-20T00:00:00", valid_to="2026-09-30T23:59:59"),
                "2026-09-10T10:00:00", "测A-00001",
            )
        with self.assertRaises(billing.CouponInvalid):
            billing.validate_coupon(
                make(valid_from="2026-08-01T00:00:00", valid_to="2026-08-31T23:59:59"),
                "2026-09-10T10:00:00", "测A-00001",
            )
        with self.assertRaises(billing.CouponInvalid):
            billing.validate_coupon(make(plate="测A-99999"), "2026-09-10T10:00:00", "测A-00001")

    def test_coupon_discount_and_cash_cap(self):
        cid = self.w.coupon("CP-CASH", type_="cash", amount=5000)
        row = self.conn.execute("SELECT * FROM coupon WHERE id=?", (cid,)).fetchone()
        # 车费只有 800 分时，现金券抵扣不超过车费
        self.assertEqual(billing.apply_coupon(800, row), 800)

        did = self.w.coupon("CP-DIS", type_="discount", amount=15)
        drow = self.conn.execute("SELECT * FROM coupon WHERE id=?", (did,)).fetchone()
        # 1000 分打 85 折 → 减免 150（四舍五入）
        self.assertEqual(billing.apply_coupon(1000, drow), 150)

    def test_snapshot_is_version_pinned(self):
        r1 = rule(self.conn, self.lot, "2026-07-28T10:00:00")
        fee = billing.compute_fee("2026-07-28T10:05:00", "2026-07-28T12:35:00", r1)
        lot = self.conn.execute("SELECT * FROM parking_lot WHERE id=?", (self.lot,)).fetchone()
        snap = billing.build_snapshot(
            rule=r1, lot_name=lot["name"], plate="测A-00001",
            entry_time="2026-07-28T10:05:00", exit_time="2026-07-28T12:35:00",
            fee_detail=fee, coupon=None, discount_cents=0,
        )
        self.assertEqual(snap["rule"]["version"], 1)
        self.assertEqual(snap["rule"]["free_minutes"], 15)
        self.assertEqual(snap["payable_cents"], 1800)


if __name__ == "__main__":
    unittest.main()
