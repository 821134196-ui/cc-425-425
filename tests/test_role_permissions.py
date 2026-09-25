"""岗位权限测试：监控员/复核员/收费员/管理员四岗位的操作边界。"""
from __future__ import annotations

import unittest
from pathlib import Path

from parking_review.db import query_one
from parking_review.services import (
    PermissionDenied,
    ServiceError,
    create_manual_release,
    ingest_event,
    publish_rule_version,
    refund_order,
    review_case,
    retry_payment,
)
from parking_review.world import build_world


def entry(w, plate, uid, **kw):
    return ingest_event(
        w.conn, lane_id=w.entry_id, direction="entry",
        server_time="2026-09-25T08:00:00", evidence_dir=w.evidence_dir,
        plate=plate, message_uid=uid, **kw,
    )


def exit_(w, plate, uid, **kw):
    return ingest_event(
        w.conn, lane_id=w.exit_id, direction="exit",
        server_time="2026-09-25T10:00:00", evidence_dir=w.evidence_dir,
        plate=plate, message_uid=uid, **kw,
    )


class RolePermissionTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world()
        self.m, self.r, self.c, self.a = (
            self.w.staff.monitor, self.w.staff.reviewer,
            self.w.staff.cashier, self.w.staff.admin,
        )

    def _paid_order(self, plate="测A-00001", uid="P-OK"):
        entry(self.w, plate, uid + "-E")
        res = exit_(self.w, plate, uid + "-X")
        return res.order_id

    def test_monitor_is_read_only(self):
        oid = self._paid_order(uid="MON")
        with self.assertRaises(PermissionDenied):
            review_case(self.w.conn, case_id=1, staff=self.m, action="claim")
        with self.assertRaises(PermissionDenied):
            retry_payment(self.w.conn, order_id=oid, staff=self.m)
        with self.assertRaises(PermissionDenied):
            refund_order(self.w.conn, order_id=oid, staff=self.m,
                         reason_code="wrong_charge")
        with self.assertRaises(PermissionDenied):
            publish_rule_version(
                self.w.conn, lot_id=self.w.lot_id, staff=self.m,
                effective_from="2026-10-01T00:00:00",
                free_minutes=10, hour_fee_cents=700, daily_cap_cents=7000,
            )

    def test_reviewer_can_review_and_release_but_not_refund_or_publish(self):
        # 制造一个支付失败异常
        entry(self.w, "测A-00002", "RV-E")
        res = exit_(self.w, "测A-00002", "RV-X")
        case = query_one(
            self.w.conn,
            "SELECT * FROM exception_case WHERE order_id=? AND type='payment_failed'",
            (res.order_id,),
        )
        review_case(self.w.conn, case_id=case["id"], staff=self.r, action="claim")
        retry_payment(self.w.conn, order_id=res.order_id, staff=self.r)  # fail_once 第二次成功
        with self.assertRaises(PermissionDenied):
            refund_order(self.w.conn, order_id=res.order_id, staff=self.r,
                         reason_code="wrong_charge")
        with self.assertRaises(PermissionDenied):
            publish_rule_version(
                self.w.conn, lot_id=self.w.lot_id, staff=self.r,
                effective_from="2026-10-01T00:00:00",
                free_minutes=10, hour_fee_cents=700, daily_cap_cents=7000,
            )

    def test_cashier_can_refund_but_not_release_or_review(self):
        oid = self._paid_order(uid="CASH")
        rid, _ = refund_order(self.w.conn, order_id=oid, staff=self.c,
                              reason_code="service_complaint")
        self.assertTrue(rid)
        with self.assertRaises(PermissionDenied):
            review_case(self.w.conn, case_id=1, staff=self.c, action="claim")

    def test_admin_can_everything(self):
        oid = self._paid_order(uid="ADM")
        rid, _ = refund_order(self.w.conn, order_id=oid, staff=self.a,
                              reason_code="wrong_charge")
        self.assertTrue(rid)
        vid = publish_rule_version(
            self.w.conn, lot_id=self.w.lot_id, staff=self.a,
            effective_from="2026-10-01T00:00:00",
            free_minutes=20, hour_fee_cents=900, daily_cap_cents=9000,
        )
        self.assertTrue(vid)

    def test_release_requires_reason_detail_and_existing_evidence(self):
        res = entry(self.w, None, "REL-E", no_plate_token="NP-T-1", confidence=0.0)
        case_id = res.case_ids[0]
        ev_path = query_one(
            self.w.conn, "SELECT evidence_path FROM lane_event WHERE id=?",
            (res.event_id,),
        )["evidence_path"]
        # 缺原因
        with self.assertRaises(ServiceError):
            create_manual_release(
                self.w.conn, case_id=case_id, staff=self.r,
                reason_code="not_exist", reason_detail="x", evidence_path=ev_path,
            )
        # 缺情况说明
        with self.assertRaises(ServiceError):
            create_manual_release(
                self.w.conn, case_id=case_id, staff=self.r,
                reason_code="token_verified", reason_detail="  ", evidence_path=ev_path,
            )
        # 证据文件不存在
        with self.assertRaises(ServiceError):
            create_manual_release(
                self.w.conn, case_id=case_id, staff=self.r,
                reason_code="token_verified", reason_detail="核验通过",
                evidence_path=str(Path(self.w.tmp) / "missing.png"),
            )
        # 全部齐备才放行成功
        rid = create_manual_release(
            self.w.conn, case_id=case_id, staff=self.r,
            reason_code="token_verified", reason_detail="凭据与访客登记一致",
            evidence_path=ev_path,
        )
        self.assertTrue(rid)
        sess = query_one(
            self.w.conn, "SELECT * FROM parking_session WHERE id=?", (res.session_id,)
        )
        self.assertEqual(sess["status"], "released")
        case = query_one(self.w.conn, "SELECT * FROM exception_case WHERE id=?", (case_id,))
        self.assertEqual(case["status"], "resolved")
        self.assertEqual(case["linked_release_id"], rid)

    def test_cashier_cannot_release_even_with_valid_payload(self):
        res = entry(self.w, None, "REL2-E", no_plate_token="NP-T-2", confidence=0.0)
        ev_path = query_one(
            self.w.conn, "SELECT evidence_path FROM lane_event WHERE id=?",
            (res.event_id,),
        )["evidence_path"]
        with self.assertRaises(PermissionDenied):
            create_manual_release(
                self.w.conn, case_id=res.case_ids[0], staff=self.c,
                reason_code="token_verified", reason_detail="核验通过",
                evidence_path=ev_path,
            )


if __name__ == "__main__":
    unittest.main()
