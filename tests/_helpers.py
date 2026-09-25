"""测试共享夹具：每个用例使用独立临时目录与全新 SQLite 库。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from parking import config, evidence
from parking.auth import Operator
from parking.db import connect, init_db
from parking.payment import InMemoryPaymentAdapter
from parking.service import ParkingService

OPS: dict[str, Operator] = {}
LOT_ID: int = 0
VEHICLES: dict[str, int] = {}


def fresh_env():
    tmp = Path(tempfile.mkdtemp(prefix="parking-test-"))
    data_dir = tmp / "data"
    ev_dir = data_dir / "evidence"
    ev_dir.mkdir(parents=True)
    # 直接替换模块全局，service 内通过 config.EVIDENCE_DIR 读取
    config.DATA_DIR = data_dir
    config.DB_PATH = data_dir / "parking.db"
    config.EVIDENCE_DIR = ev_dir

    conn = connect(config.DB_PATH)
    init_db(conn)
    adapter = InMemoryPaymentAdapter()
    svc = ParkingService(conn, adapter)
    lot_id, vehicles, ops = _master(conn)
    conn.commit()

    global LOT_ID
    LOT_ID = lot_id
    VEHICLES.clear()
    VEHICLES.update(vehicles)
    OPS.clear()
    OPS.update(ops)
    return svc, conn, adapter, ev_dir


def _master(conn):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO lots(code, name, address, created_at) "
        "VALUES ('M01','测试广场停车场','虚构地址','2026-01-01T00:00:00+08:00')")
    lot_id = cur.lastrowid
    for code, name, direction in [("T-EN-01", "测试入口", "ENTRY"),
                                  ("T-EX-01", "测试出口", "EXIT")]:
        cur.execute(
            "INSERT INTO lanes(lot_id, code, name, direction, status) "
            "VALUES (?,?,?,?,'IDLE')", (lot_id, code, name, direction))
    cur.execute(
        "INSERT INTO rule_versions(lot_id, version, valid_from, valid_to, free_minutes, "
        "unit_minutes, unit_price_fen, daily_cap_fen, note) "
        "VALUES (?,1,'2026-01-01T00:00:00+08:00','2026-09-01T00:00:00+08:00',"
        "30,60,500,5000,'v1')", (lot_id,))
    cur.execute(
        "INSERT INTO rule_versions(lot_id, version, valid_from, valid_to, free_minutes, "
        "unit_minutes, unit_price_fen, daily_cap_fen, note) "
        "VALUES (?,2,'2026-09-01T00:00:00+08:00',NULL,"
        "15,30,300,6000,'v2')", (lot_id,))

    vehicles = {}
    for plate, token in [("沪AD88026", "OK:ETP-1"), ("沪AB55T30", "FAIL:CRD-2"),
                         ("沪AC00001", None)]:
        cur.execute("INSERT INTO vehicles(plate, plate_color, owner_name) VALUES (?,?,?)",
                    (plate, "BLUE", "虚构车主"))
        vid = cur.lastrowid
        vehicles[plate] = vid
        if token:
            cur.execute(
                "INSERT INTO payment_methods(vehicle_id, method_type, display_name, token, "
                "valid, bound_since) VALUES (?,?,?,?,1,'2026-01-05T09:00:00+08:00')",
                (vid, "ETP", "虚构支付方式", token))
    cur.execute(
        "INSERT INTO coupons(code, vehicle_id, coupon_type, amount, valid_from, valid_to) "
        "VALUES ('CP-OK-FIX', ?, 'FIXED', 500, "
        "'2026-09-01T00:00:00+08:00','2026-09-30T23:59:59+08:00')",
        (vehicles["沪AD88026"],))

    ops = {}
    for login, name, role, code in [
        ("admin", "管理员", "ADMIN", "a"), ("fang", "岗亭员", "OPERATOR", "o"),
        ("shen", "稽核员", "AUDITOR", "s"), ("qian", "财务员", "FINANCE", "q")]:
        cur.execute(
            "INSERT INTO operators(login, display_name, role, pass_code) VALUES (?,?,?,?)",
            (login, name, role, code))
        ops[role] = Operator(cur.lastrowid, login, name, role)
    return lot_id, vehicles, ops


def op(role: str) -> Operator:
    return OPS[role]


def send(svc, ev_dir, uid, lane, dt, direction, plate=None, conf=0.98,
         vehicle_id=None, coupon=None):
    """发送一条车道消息，同时在临时证据目录写入本地抓拍文件。"""
    fn = f"EV-{uid}.svg"
    evidence.write_evidence(fn, plate, lane, dt, conf, directory=ev_dir)
    return svc.ingest_message(
        msg_uid=uid, lane_code=lane, device_time=dt, direction=direction,
        raw_plate=plate, confidence=conf, vehicle_id=vehicle_id,
        coupon_code=coupon, plate_image=fn)


def paid_order(conn, session_id: int):
    return conn.execute(
        "SELECT * FROM orders WHERE session_id = ? ORDER BY id LIMIT 1",
        (session_id,)).fetchone()
