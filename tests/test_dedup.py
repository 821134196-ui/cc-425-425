"""消息去重测试：同 uid 幂等、重复出场不产生新订单/不重复扣费。"""
from __future__ import annotations

import unittest

from tests._helpers import fresh_env, send, paid_order


class MessageDedupeTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.conn, self.adapter, self.ev = fresh_env()

    def test_same_uid_is_idempotent(self):
        kwargs = dict(uid="U-1", lane="T-EN-01", dt="2026-09-23T09:00:00+08:00",
                      direction="ENTRY", plate="沪AD88026")
        send(self.svc, self.ev, **kwargs)
        dup = send(self.svc, self.ev, **kwargs)  # 完全相同的重推
        self.assertTrue(dup["duplicate"])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM lane_messages WHERE msg_uid = 'U-1'").fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0])

    def test_duplicate_exit_does_not_charge_twice(self):
        send(self.svc, self.ev, "U-IN", "T-EN-01", "2026-09-23T09:30:00+08:00",
             "ENTRY", "沪AD88026")
        r1 = send(self.svc, self.ev, "U-OUT", "T-EX-01", "2026-09-23T11:02:00+08:00",
                  "EXIT", "沪AD88026")
        order_id = r1["order_id"]
        self.assertIsNotNone(order_id)

        # 同 uid 重推：完全跳过
        same = send(self.svc, self.ev, "U-OUT", "T-EX-01",
                    "2026-09-23T11:02:00+08:00", "EXIT", "沪AD88026")
        self.assertTrue(same["duplicate"])

        # 新 uid 的二次出场消息：进 DUP_EXIT 复核，不新建会话/订单，不二次扣款
        r2 = send(self.svc, self.ev, "U-OUT-2", "T-EX-01",
                  "2026-09-23T11:14:00+08:00", "EXIT", "沪AD88026", conf=0.81)
        self.assertEqual("DUP_EXIT", r2["anomaly"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE session_id = ?",
            (r1["session_id"],)).fetchone()[0])
        # 支付通道只收到一笔扣款（0 元订单为 FREE，仍只有一条 charge 记录）
        charges = [c for c in self.adapter.charges
                   if c["order_no"] ==
                   self.conn.execute("SELECT order_no FROM orders WHERE id = ?",
                                     (order_id,)).fetchone()[0]]
        self.assertEqual(1, len(charges))
        dup_msg = self.conn.execute(
            "SELECT dup_of_msg_uid FROM lane_messages WHERE msg_uid = 'U-OUT-2'").fetchone()
        self.assertEqual("U-OUT", dup_msg["dup_of_msg_uid"])


if __name__ == "__main__":
    unittest.main()
