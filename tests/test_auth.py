"""岗位权限测试：四岗位动作矩阵，以及人工放行必须有原因+证据。"""
from __future__ import annotations

import unittest

from parking.auth import PERMISSIONS, PermissionError_
from parking.service import ServiceError
from tests import _helpers as H
from tests._helpers import fresh_env, op, send


class RoleMatrixTest(unittest.TestCase):
    def test_read_actions_all_roles(self):
        for action in ("lane.view", "record.view", "rule.view", "review.view"):
            for role in ("ADMIN", "OPERATOR", "AUDITOR", "FINANCE"):
                self.assertTrue(op(role).can(action), f"{role} 应可 {action}")

    def test_rule_create_admin_only(self):
        self.assertTrue(op("ADMIN").can("rule.create"))
        for role in ("OPERATOR", "AUDITOR", "FINANCE"):
            self.assertFalse(op(role).can("rule.create"))

    def test_release_admin_and_operator_only(self):
        self.assertTrue(op("ADMIN").can("release.create"))
        self.assertTrue(op("OPERATOR").can("release.create"))
        self.assertFalse(op("AUDITOR").can("release.create"))
        self.assertFalse(op("FINANCE").can("release.create"))

    def test_refund_admin_and_finance_only(self):
        self.assertTrue(op("ADMIN").can("refund.create"))
        self.assertTrue(op("FINANCE").can("refund.create"))
        self.assertFalse(op("OPERATOR").can("refund.create"))
        self.assertFalse(op("AUDITOR").can("refund.create"))

    def test_review_resolve_not_auditor_or_finance(self):
        self.assertTrue(op("ADMIN").can("review.resolve"))
        self.assertTrue(op("OPERATOR").can("review.resolve"))
        self.assertFalse(op("AUDITOR").can("review.resolve"))
        self.assertFalse(op("FINANCE").can("review.resolve"))

    def test_bind_payment_admin_operator_only(self):
        for role in ("ADMIN", "OPERATOR"):
            self.assertTrue(op(role).can("payment.bind"))
        for role in ("AUDITOR", "FINANCE"):
            self.assertFalse(op(role).can("payment.bind"))

    def test_require_raises(self):
        with self.assertRaises(PermissionError_):
            op("AUDITOR").require("refund.create")
        with self.assertRaises(PermissionError_):
            op("FINANCE").require("release.create")


class PermissionEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.conn, self.adapter, self.ev = fresh_env()

    def _held_session_with_order(self):
        send(self.svc, self.ev, "P-IN", "T-EN-01", "2026-09-18T19:00:00+08:00",
             "ENTRY", "沪AB55T30")
        r = send(self.svc, self.ev, "P-OUT", "T-EX-01", "2026-09-18T21:00:00+08:00",
                 "EXIT", "沪AB55T30")
        return r

    def test_operator_cannot_create_rule(self):
        with self.assertRaises(PermissionError_):
            self.svc.create_rule_version(
                op("OPERATOR"), H.LOT_ID, free_minutes=0, unit_minutes=30,
                unit_price_fen=100, daily_cap_fen=0,
                valid_from="2026-11-01T00:00:00+08:00")

    def test_finance_cannot_release(self):
        r = self._held_session_with_order()
        with self.assertRaises(PermissionError_):
            self.svc.create_release(
                op("FINANCE"), r["session_id"], "PLATE_CONFIRMED",
                "说明", "EV-P-OUT.svg")

    def test_operator_cannot_refund(self):
        send(self.svc, self.ev, "Z-IN", "T-EN-01", "2026-09-12T10:00:00+08:00",
             "ENTRY", "沪AD88026")
        r = send(self.svc, self.ev, "Z-OUT", "T-EX-01", "2026-09-12T12:35:00+08:00",
                 "EXIT", "沪AD88026")
        with self.assertRaises(PermissionError_):
            self.svc.refund_order(op("OPERATOR"), r["order_id"], "岗亭员想退款")


class ReleaseRequiresReasonAndEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.conn, self.adapter, self.ev = fresh_env()
        # 牌异场景：可信车辆标识匹配但车牌不一致 -> MATCH_REVIEW
        send(self.svc, self.ev, "M-IN", "T-EN-01", "2026-09-20T08:30:00+08:00",
             "ENTRY", "沪AD88026")
        self.r = send(self.svc, self.ev, "M-OUT", "T-EX-01",
                      "2026-09-20T17:52:00+08:00", "EXIT", "沪AD8B026",
                      conf=0.62, vehicle_id=H.VEHICLES["沪AD88026"])

    def test_unknown_reason_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.create_release(
                op("OPERATOR"), self.r["session_id"], "MAYBE", "说明",
                "EV-M-OUT.svg")

    def test_blank_detail_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.create_release(
                op("OPERATOR"), self.r["session_id"], "PLATE_CONFIRMED",
                "   ", "EV-M-OUT.svg")

    def test_missing_evidence_file_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.create_release(
                op("OPERATOR"), self.r["session_id"], "PLATE_CONFIRMED",
                "已核对一致", "EV-not-exist.svg")

    def test_path_traversal_evidence_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.create_release(
                op("OPERATOR"), self.r["session_id"], "PLATE_CONFIRMED",
                "已核对一致", "../../../etc/passwd")

    def test_valid_release_succeeds_and_closes_session(self):
        rid = self.svc.create_release(
            op("OPERATOR"), self.r["session_id"], "PLATE_CONFIRMED",
            "双抓拍核对为同一车辆，OCR 尾号误识", "EV-M-OUT.svg")
        row = self.conn.execute(
            "SELECT * FROM manual_releases WHERE id = ?", (rid,)).fetchone()
        self.assertEqual("PLATE_CONFIRMED", row["reason_code"])
        self.assertEqual("EV-M-OUT.svg", row["evidence_file"])
        status = self.conn.execute(
            "SELECT status FROM sessions WHERE id = ?",
            (self.r["session_id"],)).fetchone()[0]
        self.assertEqual("MANUAL_RELEASED", status)


if __name__ == "__main__":
    unittest.main()
