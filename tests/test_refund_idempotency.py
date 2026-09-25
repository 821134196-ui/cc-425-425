"""退款幂等测试：一单仅一次退款；重复提交返回同一退款单；未支付不可退。"""
from __future__ import annotations

import unittest

from parking_review.db import query_all, query_one
from parking_review.payment import gateway
from parking_review.services import (
    PermissionDenied,
    ServiceError,
    ingest_event,
    refund_order,
    retry_payment,
)
from parking_review.world import build_world


def park_exit(w, plate, uid, *, entry="2026-09-25T09:00:00",
              exit_="2026-09-25T10:30:00"):
    ingest_event(
        w.conn, lane_id=w.entry_id, direction="entry", server_time=entry,
        evidence_dir=w.evidence_dir, plate=plate, message_uid=uid + "-E",
    )
    r = ingest_event(
        w.conn, lane_id=w.exit_id, direction="exit", server_time=exit_,
        evidence_dir=w.evidence_dir, plate=plate, message_uid=uid + "-X",
    )
    return query_one(w.conn, "SELECT * FROM payment_order WHERE id=?", (r.order_id,))


class RefundIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        gateway.reset()

    def test_refund_succeeds_once_and_idempotent(self):
        order = park_exit(self.w, "测A-00001", "RF1")
        self.assertEqual(order["status"], "paid")
        rid1, dup1 = refund_order(
            self.w.conn, order_id=order["id"], staff=self.w.staff.cashier,
            reason_code="wrong_charge", reason_detail="规则适用错误",
        )
        self.assertFalse(dup1)
        rid2, dup2 = refund_order(
            self.w.conn, order_id=order["id"], staff=self.w.staff.cashier,
            reason_code="wrong_charge", reason_detail="重复提交",
        )
        self.assertTrue(dup2)
        self.assertEqual(rid1, rid2)  # 同一订单始终返回同一退款单

        rows = query_all(
            self.w.conn, "SELECT * FROM refund_record WHERE order_id=?", (order["id"],)
        )
        self.assertEqual(len(rows), 1)  # 数据库唯一约束 + 服务层双保险
        refreshed = query_one(
            self.w.conn, "SELECT * FROM payment_order WHERE id=?", (order["id"],)
        )
        self.assertEqual(refreshed["status"], "refunded")

    def test_unpaid_order_cannot_refund(self):
        # fail_once：首次失败
        ingest_event(
            self.w.conn, lane_id=self.w.entry_id, direction="entry",
            server_time="2026-09-25T09:00:00", evidence_dir=self.w.evidence_dir,
            plate="测A-00002", message_uid="RF2-E",
        )
        r = ingest_event(
            self.w.conn, lane_id=self.w.exit_id, direction="exit",
            server_time="2026-09-25T10:00:00", evidence_dir=self.w.evidence_dir,
            plate="测A-00002", message_uid="RF2-X",
        )
        order = query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r.order_id,))
        self.assertEqual(order["status"], "failed")
        with self.assertRaises(ServiceError):
            refund_order(
                self.w.conn, order_id=order["id"], staff=self.w.staff.cashier,
                reason_code="wrong_charge",
            )
        # 重试支付成功后才可退款
        retry_payment(self.w.conn, order_id=order["id"], staff=self.w.staff.reviewer)
        rid, dup = refund_order(
            self.w.conn, order_id=order["id"], staff=self.w.staff.cashier,
            reason_code="service_complaint",
        )
        self.assertFalse(dup)

    def test_refund_requires_valid_reason(self):
        order = park_exit(self.w, "测A-00001", "RF3")
        with self.assertRaises(ServiceError):
            refund_order(
                self.w.conn, order_id=order["id"], staff=self.w.staff.cashier,
                reason_code="not_a_reason",
            )

    def test_refunded_order_cannot_refund_again_via_gateway(self):
        order = park_exit(self.w, "测A-00001", "RF4")
        refund_order(
            self.w.conn, order_id=order["id"], staff=self.w.staff.cashier,
            reason_code="device_error",
        )
        # 网关层同样保证同一原支付只退一次（幂等返回同一流水号）
        a = gateway.refund(f"ORDER-{order['id']}", order["payable_cents"])
        b = gateway.refund(f"ORDER-{order['id']}", order["payable_cents"])
        self.assertEqual(a.gateway_no, b.gateway_no)

    def test_monitor_cannot_refund(self):
        order = park_exit(self.w, "测A-00001", "RF5")
        with self.assertRaises(PermissionDenied):
            refund_order(
                self.w.conn, order_id=order["id"], staff=self.w.staff.monitor,
                reason_code="wrong_charge",
            )


if __name__ == "__main__":
    unittest.main()
