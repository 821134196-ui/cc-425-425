"""端到端流程：六类异常分流、设备时间排序、补绑支付后重试、无牌放行。"""
from __future__ import annotations

import unittest

from parking import queries
from tests import _helpers as H
from tests._helpers import fresh_env, op, send, paid_order


class AnomalyWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.conn, self.adapter, self.ev = fresh_env()
        # 一张失效券
        self.conn.execute(
            "INSERT INTO coupons(code, vehicle_id, coupon_type, amount, valid_from, valid_to) "
            "VALUES ('CP-OLD', ?, 'FIXED', 500, "
            "'2026-07-01T00:00:00+08:00','2026-08-31T23:59:59+08:00')",
            (H.VEHICLES["沪AD88026"],))
        self.conn.commit()

    def _cases_by_code(self, code):
        return [c for c in queries.cases(self.conn) if c["code"] == code]

    def test_six_anomaly_types_flow(self):
        # 跨日 + 正常扣费
        send(self.svc, self.ev, "X1-IN", "T-EN-01", "2026-08-31T22:00:00+08:00",
             "ENTRY", "沪AD88026")
        r_cross = send(self.svc, self.ev, "X1-OUT", "T-EX-01",
                       "2026-09-01T08:00:00+08:00", "EXIT", "沪AD88026")
        self.assertEqual("CROSS_DAY", r_cross["anomaly"])
        self.assertEqual("SUCCESS", paid_order(self.conn, r_cross["session_id"])["pay_status"])

        # 券失效（同时正常支付成功）
        send(self.svc, self.ev, "X2-IN", "T-EN-01", "2026-09-22T18:40:00+08:00",
             "ENTRY", "沪AD88026")
        r_coup = send(self.svc, self.ev, "X2-OUT", "T-EX-01",
                      "2026-09-22T20:05:00+08:00", "EXIT", "沪AD88026",
                      coupon="CP-OLD")
        self.assertEqual("COUPON_INVALID", r_coup["anomaly"])
        order = paid_order(self.conn, r_coup["session_id"])
        self.assertEqual(order["amount_fen"], order["amount_before_coupon_fen"])

        # 支付失败 -> REVIEW_HOLD -> 补绑有效方式 -> 重试成功
        send(self.svc, self.ev, "X3-IN", "T-EN-01", "2026-09-18T19:20:00+08:00",
             "ENTRY", "沪AB55T30")
        r_pay = send(self.svc, self.ev, "X3-OUT", "T-EX-01",
                     "2026-09-18T21:05:00+08:00", "EXIT", "沪AB55T30")
        self.assertEqual("PAY_FAILED", r_pay["anomaly"])
        self.assertEqual("REVIEW_HOLD", self.conn.execute(
            "SELECT status FROM sessions WHERE id = ?",
            (r_pay["session_id"],)).fetchone()[0])
        # 原失败令牌仍在，直接重试应继续失败
        with self.assertRaises(Exception):
            self.svc.retry_charge(op("OPERATOR"), r_pay["order_id"])
        # 岗亭补绑一张新的有效支付方式（旧失败卡保留，新卡优先）后重试
        self.svc.bind_payment_method(H.VEHICLES["沪AB55T30"], "WALLET",
                                     "商场电子钱包（补绑）", "OK:WLT-NEW")
        self.svc.retry_charge(op("OPERATOR"), r_pay["order_id"])
        self.assertEqual("SUCCESS", paid_order(self.conn, r_pay["session_id"])["pay_status"])

        # 牌异：可信身份 + OCR 不一致 -> MATCH_REVIEW 且不产生订单
        send(self.svc, self.ev, "X4-IN", "T-EN-01", "2026-09-20T08:30:00+08:00",
             "ENTRY", "沪AD88026")
        r_mis = send(self.svc, self.ev, "X4-OUT", "T-EX-01",
                     "2026-09-20T17:52:00+08:00", "EXIT", "沪AD8BO26",
                     conf=0.6, vehicle_id=H.VEHICLES["沪AD88026"])
        self.assertEqual("PLATE_MISMATCH", r_mis["anomaly"])
        # 牌异拦截不产生任何订单
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE session_id = ?",
            (r_mis["session_id"],)).fetchone()[0])
        self.assertEqual("MATCH_REVIEW", self.conn.execute(
            "SELECT status FROM sessions WHERE id = ?",
            (r_mis["session_id"],)).fetchone()[0])

        # 无牌车
        send(self.svc, self.ev, "X5-IN", "T-EN-01", "2026-09-24T10:00:00+08:00",
             "ENTRY", None, conf=0.3)
        r_np = send(self.svc, self.ev, "X5-OUT", "T-EX-01",
                    "2026-09-24T12:20:00+08:00", "EXIT", None, conf=0.3)
        self.assertEqual("NO_PLATE", r_np["anomaly"])
        self.assertEqual(r_np["session_id"],
                         self.conn.execute("SELECT id FROM sessions WHERE plate IS NULL")
                         .fetchone()[0])

        # 重复出场
        send(self.svc, self.ev, "X6-IN", "T-EN-01", "2026-09-23T09:30:00+08:00",
             "ENTRY", "沪AC00001")
        # 沪AC00001 无支付方式 -> 支付失败；为隔离“重出”，给它补绑
        self.svc.bind_payment_method(H.VEHICLES["沪AC00001"], "WALLET",
                                     "电子钱包", "OK:WLT-001")
        r_ok = send(self.svc, self.ev, "X6-OUT", "T-EX-01",
                    "2026-09-23T11:02:00+08:00", "EXIT", "沪AC00001")
        r_dup = send(self.svc, self.ev, "X6-OUT-2", "T-EX-01",
                     "2026-09-23T11:30:00+08:00", "EXIT", "沪AC00001")
        self.assertEqual("DUP_EXIT", r_dup["anomaly"])

        # 六类异常均有案件
        for code in ("CROSS_DAY", "COUPON_INVALID", "PAY_FAILED",
                     "PLATE_MISMATCH", "NO_PLATE", "DUP_EXIT"):
            self.assertGreaterEqual(len(self._cases_by_code(code)), 1, f"缺少 {code} 案件")

        # 无牌车人工放行必须关联证据
        rid = self.svc.create_release(
            op("OPERATOR"), r_np["session_id"], "NO_PLATE_TICKET",
            "纸质临停凭证核对一致", "EV-X5-OUT.svg")
        self.assertGreater(rid, 0)

    def test_lane_messages_ordered_by_device_time(self):
        # 乱序接入，查询必须按设备时间排列
        for uid, dt in [
            ("L-3", "2026-09-25T12:40:00+08:00"),
            ("L-1", "2026-09-25T08:05:00+08:00"),
            ("L-2", "2026-09-25T09:20:00+08:00")]:
            send(self.svc, self.ev, uid, "T-EN-01", dt, "ENTRY", "沪AD88026")
        lane_id = self.conn.execute(
            "SELECT id FROM lanes WHERE code = 'T-EN-01'").fetchone()[0]
        msgs = queries.lane_messages(self.conn, lane_id)
        uids = [m["msg_uid"] for m in msgs]
        self.assertEqual(["L-1", "L-2", "L-3"], uids)

    def test_case_resolve_permissions(self):
        send(self.svc, self.ev, "C-IN", "T-EN-01", "2026-09-24T10:00:00+08:00",
             "ENTRY", None, conf=0.3)
        r = send(self.svc, self.ev, "C-OUT", "T-EX-01",
                 "2026-09-24T12:00:00+08:00", "EXIT", None, conf=0.3)
        case_id = self._cases_by_code("NO_PLATE")[0]["id"]
        # 稽核员不能处理案件
        from parking.auth import PermissionError_
        with self.assertRaises(PermissionError_):
            self.svc.resolve_case(op("AUDITOR"), case_id, "稽核想处理")
        # 岗亭员处理后不可重复处理
        self.svc.resolve_case(op("OPERATOR"), case_id, "凭证一致，已放行")
        with self.assertRaises(Exception):
            self.svc.resolve_case(op("OPERATOR"), case_id, "再次处理")


if __name__ == "__main__":
    unittest.main()
