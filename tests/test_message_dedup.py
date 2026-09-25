"""消息去重测试：message_uid 幂等、重复出场不二次扣费、设备时间偏移。"""
from __future__ import annotations

import unittest

from parking_review.db import query_all, query_one
from parking_review.services import ingest_event
from parking_review.world import build_world


def send(w, lane, direction, t, **kw):
    return ingest_event(
        w.conn, lane_id=getattr(w, lane), direction=direction,
        server_time=t, evidence_dir=w.evidence_dir, **kw,
    )


class MessageDedupTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world()

    def test_same_uid_processed_once(self):
        r1 = send(self.w, "entry_id", "entry", "2026-09-25T08:00:00",
                  plate="测A-00001", message_uid="U-1")
        r2 = send(self.w, "entry_id", "entry", "2026-09-25T08:00:00",
                  plate="测A-00001", message_uid="U-1")
        self.assertTrue(r2.duplicated_message)
        self.assertEqual(r1.event_id, r2.event_id)
        # 同车牌只有一个会话
        sessions = query_all(
            self.w.conn,
            "SELECT id FROM parking_session WHERE plate='测A-00001'",
        )
        self.assertEqual(len(sessions), 1)
        # 消息表也只有一条
        events = query_all(self.w.conn, "SELECT id FROM lane_event WHERE message_uid='U-1'")
        self.assertEqual(len(events), 1)

    def test_duplicate_exit_charged_once(self):
        send(self.w, "entry_id", "entry", "2026-09-25T08:02:00",
             plate="测A-00001", message_uid="D-E")
        r1 = send(self.w, "exit_id", "exit", "2026-09-25T09:55:00",
                  plate="测A-00001", message_uid="D-X1")
        # 不同 uid、8 分钟后再次出场 → 重复出场拦截
        r2 = send(self.w, "exit_id", "exit", "2026-09-25T10:03:00",
                  plate="测A-00001", message_uid="D-X2")
        self.assertIsNotNone(r1.order_id)
        self.assertIsNone(r2.order_id)
        orders = query_all(
            self.w.conn, "SELECT id FROM payment_order WHERE session_id=?",
            (r1.session_id,),
        )
        self.assertEqual(len(orders), 1)  # 同一车辆两条出场消息不能重复扣费
        ev2 = query_one(self.w.conn, "SELECT * FROM lane_event WHERE message_uid='D-X2'")
        self.assertEqual(ev2["processed_as"], "duplicate")
        self.assertIsNotNone(ev2["duplicate_of"])
        dup_case = query_one(
            self.w.conn,
            "SELECT * FROM exception_case WHERE type='duplicate_exit' AND session_id=?",
            (r1.session_id,),
        )
        self.assertIsNotNone(dup_case)

    def test_reentry_after_30min_window_is_new_session(self):
        send(self.w, "entry_id", "entry", "2026-09-25T08:00:00",
             plate="测A-00001", message_uid="R-E1")
        send(self.w, "exit_id", "exit", "2026-09-25T09:00:00",
             plate="测A-00001", message_uid="R-X1")
        # 31 分钟后重新入场并出场，属于第二次正常停车（不是重复出场）
        send(self.w, "entry_id", "entry", "2026-09-25T09:31:00",
             plate="测A-00001", message_uid="R-E2")
        r = send(self.w, "exit_id", "exit", "2026-09-25T10:40:00",
                 plate="测A-00001", message_uid="R-X2")
        self.assertIsNotNone(r.order_id)
        self.assertEqual(
            len(query_all(self.w.conn,
                          "SELECT id FROM payment_order WHERE plate='测A-00001'")),
            2,
        )

    def test_device_clock_offset_orders_device_time(self):
        # 给出口车道加 120 秒偏移：平台 09:58 收到，设备时间应为 10:00
        self.w.conn.execute(
            "UPDATE lane SET device_clock_offset_s=120 WHERE id=?", (self.w.exit_id,)
        )
        self.w.conn.commit()
        send(self.w, "entry_id", "entry", "2026-09-25T08:00:00",
             plate="测A-00001", message_uid="O-E")
        send(self.w, "exit_id", "exit", "2026-09-25T09:58:00",
             plate="测A-00001", message_uid="O-X")
        ev = query_one(self.w.conn, "SELECT * FROM lane_event WHERE message_uid='O-X'")
        self.assertEqual(ev["device_time"], "2026-09-25T10:00:00")
        self.assertEqual(ev["received_at"], "2026-09-25T09:58:00")

    def test_duplicate_uid_even_with_different_payload_ignored(self):
        send(self.w, "entry_id", "entry", "2026-09-25T08:00:00",
             plate="测A-00001", message_uid="P-1")
        r = send(self.w, "entry_id", "entry", "2026-09-25T08:05:00",
                 plate="测A-00002", message_uid="P-1")  # 重放包
        self.assertTrue(r.duplicated_message)


if __name__ == "__main__":
    unittest.main()
