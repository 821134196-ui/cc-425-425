"""规则快照测试：订单固化结算时规则；发布新版本不重算历史订单。"""
from __future__ import annotations

import json
import unittest

from parking_review.db import loads, query_one
from parking_review.services import ingest_event, publish_rule_version
from parking_review.world import build_world


class RuleSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.w = build_world()

    def _exit(self, plate, entry, exit_, uid, **kw):
        ingest_event(
            self.w.conn, lane_id=self.w.entry_id, direction="entry",
            server_time=entry, evidence_dir=self.w.evidence_dir,
            plate=plate, message_uid=uid + "-E",
        )
        r = ingest_event(
            self.w.conn, lane_id=self.w.exit_id, direction="exit",
            server_time=exit_, evidence_dir=self.w.evidence_dir,
            plate=plate, message_uid=uid + "-X", **kw,
        )
        return query_one(self.w.conn, "SELECT * FROM payment_order WHERE id=?", (r.order_id,))

    def test_v1_order_kept_after_v2(self):
        order = self._exit(
            "测A-00001", "2026-07-28T10:05:00", "2026-07-28T12:35:00", "S1"
        )
        snap = loads(order["billing_snapshot"])
        self.assertEqual(snap["rule"]["version"], 1)
        self.assertEqual(snap["rule"]["hour_fee_cents"], 600)
        self.assertEqual(order["amount_cents"], 1800)

    def test_publish_new_version_does_not_recompute_old_orders(self):
        old = self._exit(
            "测A-00001", "2026-07-28T10:05:00", "2026-07-28T12:35:00", "S2"
        )
        # 管理员发布一个更贵的 v3
        publish_rule_version(
            self.w.conn, lot_id=self.w.lot_id, staff=self.w.staff.admin,
            effective_from="2026-09-01T00:00:00",
            free_minutes=0, hour_fee_cents=2000, daily_cap_cents=20000,
            note="涨价（虚构）",
        )
        # 重新读取旧订单：快照与金额保持不变
        refreshed = query_one(
            self.w.conn, "SELECT * FROM payment_order WHERE id=?", (old["id"],)
        )
        snap = loads(refreshed["billing_snapshot"])
        self.assertEqual(snap["rule"]["version"], 1)
        self.assertEqual(refreshed["amount_cents"], 1800)
        self.assertEqual(refreshed["payable_cents"], 1800)

    def test_new_entries_use_new_version(self):
        publish_rule_version(
            self.w.conn, lot_id=self.w.lot_id, staff=self.w.staff.admin,
            effective_from="2026-09-01T00:00:00",
            free_minutes=0, hour_fee_cents=1000, daily_cap_cents=10000,
            note="v3 无免费时长",
        )
        order = self._exit(
            "测A-00001", "2026-09-10T10:00:00", "2026-09-10T11:00:00", "S3"
        )
        snap = loads(order["billing_snapshot"])
        self.assertEqual(snap["rule"]["version"], 3)
        self.assertEqual(order["amount_cents"], 1000)  # 无免费，1 小时

    def test_snapshot_contains_day_slices_and_is_json(self):
        order = self._exit(
            "测A-00001", "2026-09-23T21:10:00", "2026-09-25T09:40:00", "S4"
        )
        # 快照必须是合法 JSON 字符串、独立可读，不依赖规则表现存数据
        snap = json.loads(order["billing_snapshot"])
        self.assertTrue(snap["cross_day"])
        self.assertEqual(len(snap["day_slices"]), 3)
        self.assertEqual(snap["payable_cents"], 18400)

    def test_publish_closes_previous_version_window(self):
        publish_rule_version(
            self.w.conn, lot_id=self.w.lot_id, staff=self.w.staff.admin,
            effective_from="2026-09-01T00:00:00",
            free_minutes=10, hour_fee_cents=900, daily_cap_cents=9000,
        )
        v2 = query_one(
            self.w.conn,
            "SELECT * FROM rule_version WHERE lot_id=? AND version=2",
            (self.w.lot_id,),
        )
        self.assertEqual(v2["effective_to"], "2026-09-01T00:00:00")
        v3 = query_one(
            self.w.conn,
            "SELECT * FROM rule_version WHERE lot_id=? AND version=3",
            (self.w.lot_id,),
        )
        self.assertIsNone(v3["effective_to"])


if __name__ == "__main__":
    unittest.main()
