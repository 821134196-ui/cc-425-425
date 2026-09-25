"""SQLite 数据库初始化与连接管理。

库表说明：
- lots / lanes            停车场与车道（车道带横向监控使用）
- vehicles / payment_methods 车辆与绑定的有效支付方式
- rule_versions           免费时长 / 单价 / 封顶金额 / 计费单位 的版本化规则
- coupons                 虚构优惠券（按有效期在结算时校验）
- sessions                停车会话（入场 -> 结算一条线）
- lane_messages           车道设备原始消息（msg_uid 去重，按设备时间排序）
- orders                  支付订单，保存计费快照（rule_snapshot / fee_detail JSON）
- refunds                 退款台账（一单最多一次，唯一约束保证幂等）
- manual_releases         人工放行（必须填原因并关联本地证据文件）
- review_cases            异常复核队列（短标签 code + 完整说明 detail）
- operators               平台操作员与岗位
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    address         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lanes (
    id              INTEGER PRIMARY KEY,
    lot_id          INTEGER NOT NULL REFERENCES lots(id),
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK(direction IN ('ENTRY','EXIT')),
    device_time     TEXT,                       -- 最近一条设备消息时间（车道带状态）
    last_event      TEXT,                       -- 最近事件摘要
    status          TEXT NOT NULL DEFAULT 'IDLE'
                    CHECK(status IN ('IDLE','PASS','ALERT','OFFLINE')),
    seq             INTEGER NOT NULL DEFAULT 0  -- 设备消息序号
);

CREATE TABLE IF NOT EXISTS vehicles (
    id              INTEGER PRIMARY KEY,
    plate           TEXT NOT NULL UNIQUE,
    plate_color     TEXT NOT NULL DEFAULT 'BLUE',
    owner_name      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'NORMAL'
                    CHECK(status IN ('NORMAL','BLOCKED')),
    note            TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS payment_methods (
    id              INTEGER PRIMARY KEY,
    vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id),
    method_type     TEXT NOT NULL CHECK(method_type IN ('ETP','CREDIT','WALLET')),
    display_name    TEXT NOT NULL,             -- 脱敏展示名（虚构）
    token           TEXT NOT NULL UNIQUE,      -- 模拟支付令牌
    valid           INTEGER NOT NULL DEFAULT 1,
    bound_since     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id              INTEGER PRIMARY KEY,
    lot_id          INTEGER NOT NULL REFERENCES lots(id),
    version         INTEGER NOT NULL,
    valid_from      TEXT NOT NULL,             -- 本地时间含时区偏移
    valid_to        TEXT,
    free_minutes    INTEGER NOT NULL,          -- 免费时长
    unit_minutes    INTEGER NOT NULL,          -- 计费单位（向上取整）
    unit_price_fen  INTEGER NOT NULL,          -- 每单位金额（分）
    daily_cap_fen   INTEGER NOT NULL,          -- 自然日封顶（分），0 表示不封顶
    note            TEXT NOT NULL DEFAULT '',
    UNIQUE(lot_id, version)
);

CREATE TABLE IF NOT EXISTS coupons (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id),
    coupon_type     TEXT NOT NULL CHECK(coupon_type IN ('FREE_MINUTES','FIXED')),
    amount          INTEGER NOT NULL,          -- 免费分钟 或 立减分
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    used_order_id   INTEGER,
    status          TEXT NOT NULL DEFAULT 'ISSUED'
                    CHECK(status IN ('ISSUED','USED','EXPIRED'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    lot_id          INTEGER NOT NULL REFERENCES lots(id),
    plate           TEXT,                      -- 无牌车为 NULL
    vehicle_id      INTEGER REFERENCES vehicles(id),
    entry_lane_id   INTEGER NOT NULL REFERENCES lanes(id),
    entry_time      TEXT NOT NULL,
    entry_msg_uid   TEXT NOT NULL UNIQUE,
    exit_lane_id    INTEGER REFERENCES lanes(id),
    exit_time       TEXT,
    exit_msg_uid    TEXT UNIQUE,
    status          TEXT NOT NULL
                    CHECK(status IN ('INSIDE','MATCH_REVIEW','CHARGED','PAID',
                                     'PAY_FAILED','MANUAL_RELEASED','REVIEW_HOLD','CLOSED')),
    anomaly_code    TEXT,                      -- 短标签
    anomaly_detail  TEXT,                      -- 完整文字
    rule_version_id INTEGER,
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_entry ON sessions(entry_time);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_plate ON sessions(plate);

CREATE TABLE IF NOT EXISTS lane_messages (
    id              INTEGER PRIMARY KEY,
    msg_uid         TEXT NOT NULL UNIQUE,      -- 去重键
    lane_id         INTEGER NOT NULL REFERENCES lanes(id),
    device_time     TEXT NOT NULL,             -- 车道设备时间（以此排序）
    direction       TEXT NOT NULL CHECK(direction IN ('ENTRY','EXIT')),
    raw_plate       TEXT,                      -- OCR 原始识别
    confidence      REAL NOT NULL DEFAULT 0,
    vehicle_id      INTEGER REFERENCES vehicles(id), -- 入场绑定结果
    session_id      INTEGER REFERENCES sessions(id),
    plate_image     TEXT,                      -- 本地证据文件
    coupon_code     TEXT,                      -- 出场时出示的优惠券
    plate_image_exit TEXT,
    dup_of_msg_uid  TEXT,                      -- 重复消息指向首条
    processed       INTEGER NOT NULL DEFAULT 0,
    note            TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_msg_lane_time ON lane_messages(lane_id, device_time);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    order_no        TEXT NOT NULL UNIQUE,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    lot_id          INTEGER NOT NULL REFERENCES lots(id),
    vehicle_id      INTEGER REFERENCES vehicles(id),
    plate           TEXT,
    entry_time      TEXT NOT NULL,
    exit_time       TEXT NOT NULL,
    duration_min    INTEGER NOT NULL,
    rule_version_id INTEGER NOT NULL,
    rule_snapshot   TEXT NOT NULL,             -- 规则计费快照 JSON（不随后续改规则重算）
    fee_detail      TEXT NOT NULL,             -- 计算过程 JSON
    amount_fen      INTEGER NOT NULL,
    amount_before_coupon_fen INTEGER NOT NULL,
    coupon_id       INTEGER REFERENCES coupons(id),
    pay_method_id   INTEGER,
    pay_status      TEXT NOT NULL DEFAULT 'CREATED'
                    CHECK(pay_status IN ('CREATED','SUCCESS','FAILED','SKIPPED_FREE')),
    paid_at         TEXT,
    pay_message     TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id);

CREATE TABLE IF NOT EXISTS refunds (
    id              INTEGER PRIMARY KEY,
    refund_no       TEXT NOT NULL UNIQUE,
    order_id        INTEGER NOT NULL UNIQUE,   -- 一单一次退款
    amount_fen      INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    operator_id     INTEGER NOT NULL REFERENCES operators(id),
    status          TEXT NOT NULL CHECK(status IN ('SUCCESS','FAILED')),
    pay_message     TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_releases (
    id              INTEGER PRIMARY KEY,
    release_no      TEXT NOT NULL UNIQUE,
    session_id      INTEGER NOT NULL REFERENCES sessions(id),
    lane_id         INTEGER NOT NULL REFERENCES lanes(id),
    reason_code     TEXT NOT NULL,             -- 短标签
    reason_detail   TEXT NOT NULL,             -- 完整说明
    evidence_file   TEXT NOT NULL,             -- 必须关联本地识别证据
    operator_id     INTEGER NOT NULL REFERENCES operators(id),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_cases (
    id              INTEGER PRIMARY KEY,
    case_no         TEXT NOT NULL UNIQUE,
    session_id      INTEGER REFERENCES sessions(id),
    order_id        INTEGER REFERENCES orders(id),
    msg_id          INTEGER REFERENCES lane_messages(id),
    code            TEXT NOT NULL,             -- 短标签 PLATE_MISMATCH ...
    detail          TEXT NOT NULL,             -- 完整异常文字
    status          TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK(status IN ('OPEN','CLAIMED','RESOLVED','REJECTED')),
    resolution      TEXT DEFAULT '',
    resolver_id     INTEGER REFERENCES operators(id),
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_status ON review_cases(status);

CREATE TABLE IF NOT EXISTS operators (
    id              INTEGER PRIMARY KEY,
    login           TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL
                    CHECK(role IN ('ADMIN','OPERATOR','AUDITOR','FINANCE')),
    pass_code       TEXT NOT NULL              -- 虚构平台，仅演示用口令
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
