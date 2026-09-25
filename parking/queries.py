"""只读台账查询：车道监控、停车记录、异常队列、规则版本、放行、退款。"""
from __future__ import annotations

import json
import sqlite3

from .service import ANOMALY_LABELS, RELEASE_REASONS


def _label(code: str | None) -> dict | None:
    if not code:
        return None
    short, full = ANOMALY_LABELS.get(code, (code, code))
    return {"code": code, "short": short, "full": full}


def lots(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM lots ORDER BY id")]


def lanes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT l.*, o.name AS lot_name FROM lanes l JOIN lots o ON o.id = l.lot_id "
        "ORDER BY o.id, l.direction DESC, l.id"
    )
    return [dict(r) for r in rows]


def lane_messages(conn: sqlite3.Connection, lane_id: int) -> list[dict]:
    """单车道消息严格按车道设备时间排序（同时间按接入顺序）。"""
    rows = conn.execute(
        "SELECT m.*, la.name AS lane_name FROM lane_messages m "
        "JOIN lanes la ON la.id = m.lane_id WHERE m.lane_id = ? "
        "ORDER BY m.device_time, m.id",
        (lane_id,),
    )
    return [dict(r) for r in rows]


def sessions(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    """停车记录：按入场时间 -> 结算时间排列的时间线。"""
    sql = (
        "SELECT s.*, o.order_no, o.amount_fen, o.pay_status, "
        "  el.name AS entry_lane_name, xl.name AS exit_lane_name, "
        "  o.fee_detail, o.rule_snapshot "
        "FROM sessions s "
        "LEFT JOIN orders o ON o.id = (SELECT id FROM orders WHERE session_id = s.id "
        "                             ORDER BY id LIMIT 1) "
        "LEFT JOIN lanes el ON el.id = s.entry_lane_id "
        "LEFT JOIN lanes xl ON xl.id = s.exit_lane_id "
    )
    params: tuple = ()
    if status:
        sql += " WHERE s.status = ?"
        params = (status,)
    sql += " ORDER BY s.entry_time, COALESCE(s.exit_time, s.entry_time), s.id"
    result = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["anomaly"] = _label(d.get("anomaly_code"))
        if d.get("fee_detail"):
            d["fee_detail"] = json.loads(d["fee_detail"])
        if d.get("rule_snapshot"):
            d["rule_snapshot"] = json.loads(d["rule_snapshot"])
        result.append(d)
    return result


def session_detail(conn: sqlite3.Connection, session_id: int) -> dict:
    row = conn.execute(
        "SELECT s.*, el.name AS entry_lane_name, xl.name AS exit_lane_name, "
        "  em.plate_image AS entry_evidence, xm.plate_image AS exit_evidence, "
        "  em.device_time AS entry_device_time, xm.device_time AS exit_device_time "
        "FROM sessions s "
        "LEFT JOIN lanes el ON el.id = s.entry_lane_id "
        "LEFT JOIN lanes xl ON xl.id = s.exit_lane_id "
        "LEFT JOIN lane_messages em ON em.msg_uid = s.entry_msg_uid "
        "LEFT JOIN lane_messages xm ON xm.msg_uid = s.exit_msg_uid "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return {}
    d = dict(row)
    d["anomaly"] = _label(d.get("anomaly_code"))
    d["orders"] = [dict(r) for r in conn.execute(
        "SELECT * FROM orders WHERE session_id = ? ORDER BY id", (session_id,))]
    for o in d["orders"]:
        o["fee_detail_json"] = json.loads(o["fee_detail"])
        o["rule_snapshot_json"] = json.loads(o["rule_snapshot"])
    d["releases"] = [dict(r) for r in conn.execute(
        "SELECT mr.*, op.display_name AS operator_name FROM manual_releases mr "
        "JOIN operators op ON op.id = mr.operator_id WHERE session_id = ? ORDER BY mr.id",
        (session_id,))]
    return d


def cases(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    sql = (
        "SELECT c.*, s.plate, s.vehicle_id, s.entry_time, s.exit_time, o.order_no, "
        "  o.amount_fen, op.display_name AS resolver_name FROM review_cases c "
        "LEFT JOIN sessions s ON s.id = c.session_id "
        "LEFT JOIN orders o ON o.id = c.order_id "
        "LEFT JOIN operators op ON op.id = c.resolver_id "
    )
    params: tuple = ()
    if status:
        sql += " WHERE c.status = ?"
        params = (status,)
    sql += " ORDER BY CASE c.status WHEN 'OPEN' THEN 0 WHEN 'CLAIMED' THEN 1 ELSE 2 END, c.id"
    result = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["label"] = _label(d["code"])
        result.append(d)
    return result


def rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT rv.*, o.name AS lot_name FROM rule_versions rv "
        "JOIN lots o ON o.id = rv.lot_id ORDER BY rv.lot_id, rv.version")]


def releases(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT mr.*, la.name AS lane_name, op.display_name AS operator_name, "
        "  s.plate FROM manual_releases mr "
        "JOIN lanes la ON la.id = mr.lane_id "
        "JOIN operators op ON op.id = mr.operator_id "
        "JOIN sessions s ON s.id = mr.session_id ORDER BY mr.id")
    result = []
    for r in rows:
        d = dict(r)
        d["reason_full"] = RELEASE_REASONS.get(d["reason_code"], d["reason_code"])
        result.append(d)
    return result


def refunds(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT rf.*, o.order_no, o.amount_fen AS order_amount_fen, o.plate, "
        "  op.display_name AS operator_name FROM refunds rf "
        "JOIN orders o ON o.id = rf.order_id "
        "JOIN operators op ON op.id = rf.operator_id ORDER BY rf.id")]


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql, *p):
        return conn.execute(sql, p).fetchone()[0]
    return {
        "lanes_total": one("SELECT COUNT(*) FROM lanes"),
        "lanes_alert": one("SELECT COUNT(*) FROM lanes WHERE status = 'ALERT'"),
        "inside": one("SELECT COUNT(*) FROM sessions WHERE exit_time IS NULL"),
        "open_cases": one("SELECT COUNT(*) FROM review_cases WHERE status IN ('OPEN','CLAIMED')"),
        "paid_orders": one("SELECT COUNT(*) FROM orders WHERE pay_status IN ('SUCCESS','SKIPPED_FREE')"),
        "paid_amount_fen": one(
            "SELECT COALESCE(SUM(amount_fen),0) FROM orders WHERE pay_status = 'SUCCESS'"),
        "refund_amount_fen": one("SELECT COALESCE(SUM(amount_fen),0) FROM refunds"),
    }
