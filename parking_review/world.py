"""单元测试共享夹具：构造一个最小但完整的虚构停车场世界。"""
from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from . import config
from .db import connect, init_db, insert
from .payment import gateway


def build_world(tmp: str | Path | None = None) -> SimpleNamespace:
    tmp = Path(tmp or tempfile.mkdtemp(prefix="parking-test-"))
    gateway.reset()
    conn = connect(tmp / "test.db")
    init_db(conn)

    ts = "2026-01-01T00:00:00"
    lot_id = insert(
        conn, "parking_lot", code="LOT-TEST", name="虚构测试停车场",
        city="虚构市", created_at=ts,
    )
    entry_id = insert(
        conn, "lane", lot_id=lot_id, code="E1", name="测试入口",
        direction="entry", device_clock_offset_s=0, status="online",
    )
    exit_id = insert(
        conn, "lane", lot_id=lot_id, code="X1", name="测试出口",
        direction="exit", device_clock_offset_s=0, status="online",
    )
    insert(
        conn, "rule_version", lot_id=lot_id, version=1,
        effective_from="2026-01-01T00:00:00", effective_to="2026-08-01T00:00:00",
        free_minutes=15, hour_fee_cents=600, daily_cap_cents=6000,
        published_by="admin", note="v1",
    )
    insert(
        conn, "rule_version", lot_id=lot_id, version=2,
        effective_from="2026-08-01T00:00:00", effective_to=None,
        free_minutes=30, hour_fee_cents=800, daily_cap_cents=8000,
        published_by="admin", note="v2",
    )

    def vehicle(plate: str, method: str | None, script: str = "ok") -> int:
        return insert(
            conn, "vehicle", plate=plate, plate_color="blue",
            brand_model=f"虚构测试车 {plate}", payment_method=method,
            payment_bound_at=ts if method else None,
            auto_pay_enabled=1, gateway_script=script, created_at=ts,
        )

    vehicles = SimpleNamespace(
        ok=vehicle("测A-00001", "alipay", "ok"),
        fail_once=vehicle("测A-00002", "unionpay", "fail_once"),
        declined=vehicle("测A-00003", "unionpay", "declined"),
        unbound=vehicle("测A-00004", None),
    )

    staff = SimpleNamespace()
    for login, name, role, pin in [
        ("mon", "监控员甲", "monitor", "1"),
        ("rev", "复核员乙", "reviewer", "2"),
        ("cash", "收费员丙", "cashier", "3"),
        ("adm", "管理员丁", "admin", "9"),
    ]:
        sid = insert(conn, "staff", login=login, name=name, role=role, pin=pin)
        setattr(staff, role, conn.execute(
            "SELECT * FROM staff WHERE id=?", (sid,)
        ).fetchone())

    def coupon(code, *, type_="cash", amount=500, plate=None,
               valid_from="2026-09-01T00:00:00", valid_to="2026-09-30T23:59:59",
               status="unused"):
        return insert(
            conn, "coupon", code=code, plate=plate, type=type_, amount=amount,
            valid_from=valid_from, valid_to=valid_to, status=status,
        )

    conn.commit()
    return SimpleNamespace(
        conn=conn, tmp=tmp, lot_id=lot_id, entry_id=entry_id, exit_id=exit_id,
        vehicles=vehicles, staff=staff,
        evidence_dir=tmp / "evidence",
        coupon=coupon,
    )
