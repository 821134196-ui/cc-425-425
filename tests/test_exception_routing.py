"""异常复核路由测试：六类异常分别入队；自动扣费前置条件与拦截行为。"""
from __future__ import annotations

import unittest

from parking_review.db import query_all, query_one
from parking_review.services import ingest_event
from parking_review.world import build_world


def send(w, lane_attr, direction, t, plate=None, uid=None, **kw):
    return ingest_event(
        w.conn, lane_id=getattr(w, lane_attr), direction=direction,
        server_time=t, evidence_dir=w.evidence_dir,
        plate=plate, message_uid=uid, **kw,
    )


def case_types(w, session_id=None):
    if session_id is None:
        return {r["type"] for r in query_all(w.conn, "SELECT type FROM exception_case")}
    return {
        r["type"]
        for r in query_all(
            w.conn, "SELECT type FROM exception_case WHERE session_id=?", (session_id,)
        )
    }


class ExceptionRoutingTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world()

    def test_no_plate_on_entry_and_exit(self):
        r = send(self.w, "entry_id", "entry", "2026-09-25T10:10:00",
                 uid="NP-E", no_plate_token="NP-1", confidence=0.0)
        self.assertIn("no_plate", case_types(self.w, r.session_id))
        # 无牌会话出场：仍挂无牌异常，不生成订单
        x = send(self.w, "exit_id", "exit", "2026-09-25T11:00:00",
                 uid="NP-X", no_plate_token="NP-1", confidence=0.0)
        self.assertIsNone(x.order_id)
        self.assertEqual(
            len(query_all(self.w.conn,
                          "SELECT id FROM payment_order WHERE session_id=?",
                          (r.session_id,))),
            0,
        )
        # 完全无凭据出场也入队
        send(self.w, "exit_id", "exit", "2026-09-25T11:30:00",
             uid="NP-X2", confidence=0.0)
        self.assertGreaterEqual(
            query_one(self.w.conn,
                      "SELECT COUNT(*) c FROM exception_case WHERE type='no_plate'")["c"],
            2,
        )

    def test_payment_failed_without_bound_method(self):
        r1 = send(self.w, "entry_id", "entry", "2026-09-25T09:00:00",
                  plate="测A-00004", uid="UB-E")
        r2 = send(self.w, "exit_id", "exit", "2026-09-25T10:00:00",
                  plate="测A-00004", uid="UB-X")
        order = query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r2.order_id,))
        self.assertEqual(order["status"], "failed")
        self.assertIn("payment_failed", case_types(self.w, r1.session_id))

    def test_fail_once_payment_then_retryable(self):
        send(self.w, "entry_id", "entry", "2026-09-25T08:40:00",
             plate="测A-00002", uid="FO-E")
        r = send(self.w, "exit_id", "exit", "2026-09-25T09:40:00",
                 plate="测A-00002", uid="FO-X")
        order = query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r.order_id,))
        self.assertEqual(order["status"], "failed")
        self.assertEqual(order["pay_attempts"], 1)

    def test_declined_payment(self):
        send(self.w, "entry_id", "entry", "2026-09-25T09:10:00",
             plate="测A-00003", uid="DC-E")
        r = send(self.w, "exit_id", "exit", "2026-09-25T10:25:00",
                 plate="测A-00003", uid="DC-X")
        order = query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r.order_id,))
        self.assertEqual(order["status"], "failed")
        self.assertIn("注销", order["fail_reason"])

    def test_plate_mismatch_blocks_charge(self):
        send(self.w, "entry_id", "entry", "2026-09-25T07:15:00",
             plate="测A-00001", uid="PM-E")
        r = send(self.w, "exit_id", "exit", "2026-09-25T09:47:00",
                 plate="测A-00009", confidence=0.71, uid="PM-X")
        # 两牌编辑距离太大 → 挂无候选的车牌异常，无订单
        self.assertIsNone(r.order_id)
        # 单字符误识的情况：与在场车匹配
        send(self.w, "entry_id", "entry", "2026-09-25T07:20:00",
             plate="测B-T2046", uid="PM2-E")
        r2 = send(self.w, "exit_id", "exit", "2026-09-25T09:50:00",
                  plate="测B-T20A6", confidence=0.71, uid="PM2-X")
        self.assertIsNone(r2.order_id)
        c = query_one(
            self.w.conn,
            "SELECT * FROM exception_case WHERE type='plate_mismatch' AND plate_in=?",
            ("测B-T2046",),
        )
        self.assertIsNotNone(c)
        self.assertEqual(c["plate_out"], "测B-T20A6")

    def test_cross_day_routed_but_charged(self):
        r1 = send(self.w, "entry_id", "entry", "2026-09-23T21:10:00",
                  plate="测A-00001", uid="CD-E")
        r2 = send(self.w, "exit_id", "exit", "2026-09-25T09:40:00",
                  plate="测A-00001", uid="CD-X")
        self.assertIn("cross_day", case_types(self.w, r1.session_id))
        order = query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r2.order_id,))
        self.assertEqual(order["status"], "paid")  # 事后复核不阻断扣费

    def test_coupon_expired_routed_but_charged_full_price(self):
        self.w.coupon(
            "CP-OLD", type_="cash", amount=1000, plate="测A-00001",
            valid_from="2026-07-01T00:00:00", valid_to="2026-08-31T23:59:59",
        )
        send(self.w, "entry_id", "entry", "2026-09-25T08:20:00",
             plate="测A-00001", uid="CI-E")
        r = send(self.w, "exit_id", "exit", "2026-09-25T10:02:00",
                 plate="测A-00001", coupon_code="CP-OLD", uid="CI-X")
        self.assertIn("coupon_invalid", case_types(self.w))
        order = query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r.order_id,))
        self.assertEqual(order["discount_cents"], 0)
        self.assertIsNone(order["coupon_id"])
        self.assertEqual(order["status"], "paid")

    def test_duplicate_exit_routed(self):
        send(self.w, "entry_id", "entry", "2026-09-25T08:02:00",
             plate="测A-00001", uid="DE-E")
        send(self.w, "exit_id", "exit", "2026-09-25T09:55:00",
             plate="测A-00001", uid="DE-X1")
        r = send(self.w, "exit_id", "exit", "2026-09-25T10:03:00",
                 plate="测A-00001", uid="DE-X2")
        self.assertIsNone(r.order_id)
        self.assertIn("duplicate_exit", case_types(self.w))


if __name__ == "__main__":
    unittest.main()
