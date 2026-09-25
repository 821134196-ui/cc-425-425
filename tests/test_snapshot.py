"""规则版本与计费快照测试：按入场时间选版本，已结算订单不随后续规则重算。"""
from __future__ import annotations

import json
import unittest

from tests import _helpers as H
from tests._helpers import fresh_env, op, send, paid_order


class RuleSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.conn, self.adapter, self.ev = fresh_env()

    def test_rule_chosen_by_entry_time(self):
        # 8/31 入场（v1 生效），9/1 出场（v2 已生效）——必须按 v1 计费
        send(self.svc, self.ev, "S-IN", "T-EN-01", "2026-08-31T21:10:00+08:00",
             "ENTRY", "沪AD88026")
        r = send(self.svc, self.ev, "S-OUT", "T-EX-01", "2026-09-01T09:40:00+08:00",
                 "EXIT", "沪AD88026")
        order = paid_order(self.conn, r["session_id"])
        snap = json.loads(order["rule_snapshot"])
        self.assertEqual(1, snap["rule"]["rule_version_id"])
        self.assertEqual(30, snap["rule"]["free_minutes"])
        self.assertEqual(500, snap["rule"]["unit_price_fen"])

    def test_settled_order_not_recomputed_when_rule_changes(self):
        # 9 月入场出场，按 v2 结算：15 分钟免费 / 300 分每半小时
        send(self.svc, self.ev, "S2-IN", "T-EN-01", "2026-09-12T10:00:00+08:00",
             "ENTRY", "沪AD88026")
        r = send(self.svc, self.ev, "S2-OUT", "T-EX-01", "2026-09-12T13:34:00+08:00",
                 "EXIT", "沪AD88026", coupon="CP-OK-FIX")
        before = paid_order(self.conn, r["session_id"])
        amount_before = before["amount_fen"]
        self.assertGreater(amount_before, 0)
        snap_before = before["rule_snapshot"]

        # 管理员登记激进新版本（免费 0 分钟、1000 分/单位）
        self.svc.create_rule_version(
            op("ADMIN"), H.LOT_ID, free_minutes=0, unit_minutes=30,
            unit_price_fen=1000, daily_cap_fen=9000,
            valid_from="2026-10-01T00:00:00+08:00", note="v3 测试涨价")

        # 老订单金额、快照、明细保持不变
        after = paid_order(self.conn, r["session_id"])
        self.assertEqual(amount_before, after["amount_fen"])
        self.assertEqual(snap_before, after["rule_snapshot"])
        detail = json.loads(after["fee_detail"])
        self.assertEqual(amount_before, detail["amount_fen"])

        # 新规则版本存在但 9 月会话不受影响（版本表只增不改历史）
        versions = [row["version"] for row in self.conn.execute(
            "SELECT version FROM rule_versions WHERE lot_id = ? ORDER BY version",
            (H.LOT_ID,))]
        self.assertEqual([1, 2, 3], versions)

    def test_new_version_applies_to_new_entries_only(self):
        from parking.billing import parse_dt
        # 11 月生效的新规则：10 月入场的会话仍走 v2
        self.svc.create_rule_version(
            op("ADMIN"), H.LOT_ID, free_minutes=0, unit_minutes=60,
            unit_price_fen=800, daily_cap_fen=0,
            valid_from="2026-11-01T00:00:00+08:00", note="v3")
        rule_oct = self.svc.active_rule(H.LOT_ID, parse_dt("2026-10-15T08:00:00+08:00"))
        rule_nov = self.svc.active_rule(H.LOT_ID, parse_dt("2026-11-02T08:00:00+08:00"))
        self.assertEqual(2, rule_oct.version)
        self.assertEqual(3, rule_nov.version)
        self.assertEqual(0, rule_nov.free_minutes)
        self.assertEqual(800, rule_nov.unit_price_fen)


if __name__ == "__main__":
    unittest.main()
