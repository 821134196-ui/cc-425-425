"""SQLite 连接层与 schema。

金额一律以整数「分」存储；时间为 ISO8601 字符串（设备时间原样保留）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 停车场
CREATE TABLE IF NOT EXISTS parking_lot (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 车道（含设备时钟偏移，模拟车道设备时间）
CREATE TABLE IF NOT EXISTS lane (
    id                     INTEGER PRIMARY KEY,
    lot_id                 INTEGER NOT NULL REFERENCES parking_lot(id),
    code                   TEXT NOT NULL,
    name                   TEXT NOT NULL,
    direction              TEXT NOT NULL CHECK (direction IN ('entry','exit')),
    device_clock_offset_s  INTEGER NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'online'
        CHECK (status IN ('online','offline','maintenance')),
    last_device_time       TEXT,
    UNIQUE (lot_id, code)
);

-- 车辆（payment_method 为 NULL 表示未绑定有效支付方式）
CREATE TABLE IF NOT EXISTS vehicle (
    id                INTEGER PRIMARY KEY,
    plate             TEXT UNIQUE NOT NULL,
    plate_color       TEXT NOT NULL DEFAULT 'blue',
    brand_model       TEXT,
    payment_method    TEXT CHECK (payment_method IN ('alipay','wechat','unionpay') OR payment_method IS NULL),
    payment_bound_at  TEXT,
    auto_pay_enabled  INTEGER NOT NULL DEFAULT 1,
    gateway_script    TEXT NOT NULL DEFAULT 'ok'
        CHECK (gateway_script IN ('ok','fail_once','declined')),
    created_at        TEXT NOT NULL
);

-- 岗位员工：monitor 监控员 / reviewer 复核员 / cashier 收费员 / admin 管理员
CREATE TABLE IF NOT EXISTS staff (
    id      INTEGER PRIMARY KEY,
    login   TEXT UNIQUE NOT NULL,
    name    TEXT NOT NULL,
    role    TEXT NOT NULL CHECK (role IN ('monitor','reviewer','cashier','admin')),
    pin     TEXT NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1
);

-- 计费规则版本（按入场时间选版本；结算订单保存快照，不随后续修改重算）
CREATE TABLE IF NOT EXISTS rule_version (
    id               INTEGER PRIMARY KEY,
    lot_id           INTEGER NOT NULL REFERENCES parking_lot(id),
    version          INTEGER NOT NULL,
    effective_from   TEXT NOT NULL,
    effective_to     TEXT,
    free_minutes     INTEGER NOT NULL,
    hour_fee_cents   INTEGER NOT NULL,
    daily_cap_cents  INTEGER NOT NULL,
    published_by     TEXT,
    note             TEXT,
    UNIQUE (lot_id, version)
);

-- 优惠券
CREATE TABLE IF NOT EXISTS coupon (
    id             INTEGER PRIMARY KEY,
    code           TEXT UNIQUE NOT NULL,
    plate          TEXT,
    type           TEXT NOT NULL CHECK (type IN ('cash','discount')),
    amount         INTEGER NOT NULL,          -- cash=抵扣分；discount=百分点（20 表示减免 20%）
    valid_from     TEXT NOT NULL,
    valid_to       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'unused'
        CHECK (status IN ('unused','used','void')),
    used_order_id  INTEGER,
    used_at        TEXT
);

-- 停车会话（一条入场到结算的主线）
CREATE TABLE IF NOT EXISTS parking_session (
    id             INTEGER PRIMARY KEY,
    lot_id         INTEGER NOT NULL REFERENCES parking_lot(id),
    vehicle_id     INTEGER REFERENCES vehicle(id),
    plate          TEXT,                       -- 入场车牌；无牌车为 NULL
    no_plate_token TEXT,                       -- 无牌车扫码凭据
    entry_lane_id  INTEGER NOT NULL REFERENCES lane(id),
    exit_lane_id   INTEGER REFERENCES lane(id),
    entry_time     TEXT NOT NULL,              -- 以车道设备时间为准
    exit_time      TEXT,
    status         TEXT NOT NULL
        CHECK (status IN ('in_park','settled','released','flagged')),
    order_id       INTEGER,
    created_at     TEXT NOT NULL
);

-- 车道识别消息（入场/出场），message_uid 全局唯一用于幂等去重
CREATE TABLE IF NOT EXISTS lane_event (
    id            INTEGER PRIMARY KEY,
    message_uid   TEXT UNIQUE NOT NULL,
    lane_id       INTEGER NOT NULL REFERENCES lane(id),
    direction     TEXT NOT NULL CHECK (direction IN ('entry','exit')),
    device_time   TEXT NOT NULL,               -- 车道设备时间（排序依据）
    received_at   TEXT NOT NULL,               -- 平台接收时间
    plate         TEXT,
    confidence    REAL,
    no_plate_token TEXT,
    evidence_path TEXT,
    session_id    INTEGER REFERENCES parking_session(id),
    duplicate_of  INTEGER REFERENCES lane_event(id),
    processed_as  TEXT NOT NULL DEFAULT 'received'
        CHECK (processed_as IN (
            'received','entry_opened','exit_settled','duplicate','ignored','exception'
        )),
    note          TEXT
);

-- 支付订单（billing_snapshot 为不可变计费快照 JSON）
CREATE TABLE IF NOT EXISTS payment_order (
    id               INTEGER PRIMARY KEY,
    order_no         TEXT UNIQUE NOT NULL,
    session_id       INTEGER NOT NULL REFERENCES parking_session(id),
    lot_id           INTEGER NOT NULL REFERENCES parking_lot(id),
    plate            TEXT,
    rule_version_id  INTEGER NOT NULL REFERENCES rule_version(id),
    billing_snapshot TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    amount_cents     INTEGER NOT NULL,
    discount_cents   INTEGER NOT NULL DEFAULT 0,
    coupon_id        INTEGER REFERENCES coupon(id),
    coupon_code      TEXT,
    payable_cents    INTEGER NOT NULL,
    status           TEXT NOT NULL
        CHECK (status IN ('created','paid','failed','refunded','partially_refunded','closed')),
    pay_attempts     INTEGER NOT NULL DEFAULT 0,
    fail_reason      TEXT,
    payment_method   TEXT,
    paid_at          TEXT,
    created_at       TEXT NOT NULL
);

-- 异常复核单
CREATE TABLE IF NOT EXISTS exception_case (
    id                 INTEGER PRIMARY KEY,
    code               TEXT UNIQUE NOT NULL,
    type               TEXT NOT NULL CHECK (type IN (
        'plate_mismatch','duplicate_exit','payment_failed',
        'no_plate','cross_day','coupon_invalid'
    )),
    session_id         INTEGER REFERENCES parking_session(id),
    entry_event_id     INTEGER REFERENCES lane_event(id),
    exit_event_id      INTEGER REFERENCES lane_event(id),
    order_id           INTEGER REFERENCES payment_order(id),
    plate_in           TEXT,
    plate_out          TEXT,
    short_tag          TEXT NOT NULL,
    summary            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','in_review','resolved','dismissed')),
    opened_at          TEXT NOT NULL,
    resolved_at        TEXT,
    resolved_by        INTEGER REFERENCES staff(id),
    resolution         TEXT,
    linked_release_id  INTEGER,
    linked_refund_id   INTEGER
);

-- 人工放行台账（必须选原因 + 关联证据文件）
CREATE TABLE IF NOT EXISTS manual_release (
    id            INTEGER PRIMARY KEY,
    release_no    TEXT UNIQUE NOT NULL,
    session_id    INTEGER REFERENCES parking_session(id),
    lane_id       INTEGER NOT NULL REFERENCES lane(id),
    reason_code   TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    evidence_path TEXT NOT NULL,
    staff_id      INTEGER NOT NULL REFERENCES staff(id),
    created_at    TEXT NOT NULL
);

-- 退款台账（一单仅允许一次，order_id 唯一约束兜底）
CREATE TABLE IF NOT EXISTS refund_record (
    id               INTEGER PRIMARY KEY,
    refund_no        TEXT UNIQUE NOT NULL,
    order_id         INTEGER NOT NULL UNIQUE REFERENCES payment_order(id),
    amount_cents     INTEGER NOT NULL,
    reason_code      TEXT NOT NULL,
    reason_detail    TEXT,
    staff_id         INTEGER NOT NULL REFERENCES staff(id),
    status           TEXT NOT NULL CHECK (status IN ('success','failed')),
    gateway_refund_no TEXT,
    fail_reason      TEXT,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_time ON lane_event(device_time);
CREATE INDEX IF NOT EXISTS idx_session_status ON parking_session(status);
CREATE INDEX IF NOT EXISTS idx_case_status ON exception_case(status);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def insert(conn: sqlite3.Connection, table: str, **values: Any) -> int:
    cols = ", ".join(values.keys())
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values())
    )
    return int(cur.lastrowid)


def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, tuple(params)))


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str) -> Any:
    return json.loads(text)
