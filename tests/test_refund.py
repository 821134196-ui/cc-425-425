"""退款幂等测试：一单一次、重复提交返回原单、金额/通道行为。"""
from __future__ import annotations

import unittest

from tests._helpers import fresh_env, op, send, paid_order
from parking.service import ServiceError


class RefundIdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.conn, self.adapter, self.ev = fresh_env()
        send(self.svc, self.ev, "R-IN", "T-EN-01", "2026-09-12T10:00:00+08:00",
             "ENTRY", "沪AD88026")
        r = send(self.svc, self.ev, "R-OUT", "T-EX-01", "2026-09-12T12:35:00+08:00",
                 "EXIT", "沪AD88026")
        self.order = paid_order(self.conn, r["session_id"])
        self.assertEqual("SUCCESS", self.order["pay_status"])
        self.assertGreater(self.order["amount_fen"], 0)

    def test_refund_once_then_idempotent(self):
        first, idem1 = self.svc.refund_order(op("FINANCE"), self.order["id"], "首次退款")
        self.assertFalse(idem1)
        self.assertEqual(self.order["amount_fen"], first["amount_fen"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])

        # 换操作员、换理由重复提交：返回同一退款单，不产生第二条
        second, idem2 = self.svc.refund_order(op("ADMIN"), self.order["id"], "重复退款申请")
        self.assertTrue(idem2)
        self.assertEqual(first["refund_no"], second["refund_no"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0])
        # 模拟通道也只有一笔退款
        self.assertEqual(1, len(self.adapter.refunds))

    def test_refund_requires_reason(self):
        with self.assertRaises(ServiceError):
            self.svc.refund_order(op("FINANCE"), self.order["id"], "  ")

    def test_unpaid_order_not_refundable(self):
        # 支付失败的订单不能退款
        send(self.svc, self.ev, "F-IN", "T-EN-01", "2026-09-18T19:00:00+08:00",
             "ENTRY", "沪AB55T30")
        r = send(self.svc, self.ev, "F-OUT", "T-EX-01", "2026-09-18T21:00:00+08:00",
                 "EXIT", "沪AB55T30")
        failed = paid_order(self.conn, r["session_id"])
        self.assertEqual("FAILED", failed["pay_status"])
        with self.assertRaises(ServiceError):
            self.svc.refund_order(op("FINANCE"), failed["id"], "未支付也想退")
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM refunds WHERE order_id = ?",
            (failed["id"],)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
