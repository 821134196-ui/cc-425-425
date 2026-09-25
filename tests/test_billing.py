"""计费边界测试：免费时长、向上取整、自然日封顶、跨日分段、优惠券。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from parking.billing import Coupon, Rule, compute_fee, parse_dt

# v1：免费 30 分钟，60 分钟/单位，500 分/单位，自然日封顶 5000 分
RULE = Rule(id=1, version=1, free_minutes=30, unit_minutes=60,
            unit_price_fen=500, daily_cap_fen=5000)
TZ = "+08:00"


def fee_at(entry: str, minutes: int, rule=RULE, coupon=None, honored=True):
    e = parse_dt(entry)
    return compute_fee(e, e + timedelta(minutes=minutes), rule, coupon, honored)


class FreeMinutesBoundaryTest(unittest.TestCase):
    def test_within_free_is_zero(self):
        self.assertEqual(0, fee_at("2026-09-10T10:00:00+08:00", 0)["amount_fen"])
        self.assertEqual(0, fee_at("2026-09-10T10:00:00+08:00", 29)["amount_fen"])

    def test_exact_free_boundary_is_zero(self):
        # 恰好 30 分钟：免费
        self.assertEqual(0, fee_at("2026-09-10T10:00:00+08:00", 30)["amount_fen"])

    def test_one_minute_over_free_rounds_up_to_one_unit(self):
        # 31 分钟：计 1 分钟也向上取整为一个计费单位
        f = fee_at("2026-09-10T10:00:00+08:00", 31)
        self.assertEqual(1, f["day_segments"][0]["units"])
        self.assertEqual(500, f["amount_fen"])

    def test_unit_rounding_boundaries(self):
        # 免费后计 30 / 60 分钟 -> 1 单位；61 分钟 -> 2 单位
        self.assertEqual(1, fee_at("2026-09-10T10:00:00+08:00", 60)
                         ["day_segments"][0]["units"])
        self.assertEqual(1, fee_at("2026-09-10T10:00:00+08:00", 90)
                         ["day_segments"][0]["units"])
        self.assertEqual(2, fee_at("2026-09-10T10:00:00+08:00", 91)
                         ["day_segments"][0]["units"])


class DailyCapTest(unittest.TestCase):
    def test_single_day_cap_applied(self):
        # 24 小时：免费 30 后 1410 分钟 = 24 单位 = 12000 分，触顶 5000
        f = fee_at("2026-09-10T00:00:00+08:00", 24 * 60)
        seg = f["day_segments"][0]
        self.assertTrue(seg["capped"])
        self.assertEqual(5000, seg["fee_fen"])
        self.assertEqual(5000, f["amount_fen"])

    def test_free_minutes_consumed_before_midnight(self):
        # 23:00 入场，次日 01:00 出场：免费 30 分钟全部落在第一天
        f = fee_at("2026-09-10T23:00:00+08:00", 120)
        self.assertTrue(f["cross_day"])
        self.assertEqual([30, 0], [d["free_minutes_used"] for d in f["day_segments"]])
        self.assertEqual([500, 500], [d["fee_fen"] for d in f["day_segments"]])
        self.assertEqual(1000, f["amount_fen"])

    def test_cross_day_two_independent_caps(self):
        # 两个完整自然日，各自封顶 5000
        f = fee_at("2026-09-10T00:00:00+08:00", 48 * 60)
        self.assertEqual(2, len(f["day_segments"]))
        self.assertTrue(all(d["capped"] for d in f["day_segments"]))
        self.assertEqual(10000, f["amount_fen"])

    def test_zero_cap_means_unlimited(self):
        rule = Rule(2, 2, 15, 30, 300, 0)
        f = compute_fee(parse_dt("2026-09-10T00:00:00+08:00"),
                        parse_dt("2026-09-10T23:59:00+08:00"), rule)
        self.assertFalse(any(d["capped"] for d in f["day_segments"]))


class CouponBoundaryTest(unittest.TestCase):
    def test_free_minutes_coupon_covers_duration(self):
        c = Coupon(1, "CP-F120", "FREE_MINUTES", 120,
                   parse_dt("2026-09-01T00:00:00+08:00"),
                   parse_dt("2026-09-30T23:59:59+08:00"))
        # 免费 30 + 券 120 = 150 分钟
        self.assertEqual(0, fee_at("2026-09-10T10:00:00+08:00", 150, coupon=c)["amount_fen"])
        f = fee_at("2026-09-10T10:00:00+08:00", 151, coupon=c)
        self.assertEqual(1, f["day_segments"][0]["units"])
        self.assertEqual(500, f["amount_fen"])

    def test_fixed_coupon_floors_at_zero(self):
        c = Coupon(2, "CP-FIX", "FIXED", 500,
                   parse_dt("2026-09-01T00:00:00+08:00"),
                   parse_dt("2026-09-30T23:59:59+08:00"))
        # 150 分钟 -> 2 单位 1000 分，减 500
        self.assertEqual(500, fee_at("2026-09-10T10:00:00+08:00", 150, coupon=c)["amount_fen"])
        big = Coupon(3, "CP-BIG", "FIXED", 9999,
                     parse_dt("2026-09-01T00:00:00+08:00"),
                     parse_dt("2026-09-30T23:59:59+08:00"))
        self.assertEqual(0, fee_at("2026-09-10T10:00:00+08:00", 150, coupon=big)["amount_fen"])

    def test_coupon_validity_endpoint_inclusive(self):
        end = parse_dt("2026-09-30T23:59:59+08:00")
        c = Coupon(4, "CP-X", "FIXED", 100,
                   parse_dt("2026-09-01T00:00:00+08:00"), end)
        self.assertTrue(c.usable_at(end))
        self.assertFalse(c.usable_at(end + timedelta(seconds=1)))

    def test_expired_coupon_recorded_but_not_applied(self):
        c = Coupon(5, "CP-OLD", "FIXED", 500,
                   parse_dt("2026-07-01T00:00:00+08:00"),
                   parse_dt("2026-08-31T23:59:59+08:00"))
        f = fee_at("2026-09-22T18:00:00+08:00", 150, coupon=c, honored=False)
        self.assertFalse(f["coupon"]["applied"])
        self.assertEqual("INVALID_OR_EXPIRED", f["coupon"]["reason"])
        self.assertEqual(1000, f["amount_fen"])  # 与无券一致

    def test_v2_rule_smaller_unit_boundaries(self):
        # v2：免费 15，30 分钟/单位，300 分
        rule = Rule(2, 2, 15, 30, 300, 6000)
        f = compute_fee(parse_dt("2026-09-10T10:00:00+08:00"),
                        parse_dt("2026-09-10T10:46:00+08:00"), rule)
        # 计 31 分钟 -> 2 单位 -> 600
        self.assertEqual(2, f["day_segments"][0]["units"])
        self.assertEqual(600, f["amount_fen"])


if __name__ == "__main__":
    unittest.main()
