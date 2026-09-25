"""单条命令：重置本地数据 → 生成进出场/扣费/复核样例 → 启动 Web。

    python3 -m parking_review.demo            # 生成样例并启动（默认端口 8000）
    python3 -m parking_review.demo --seed-only
    python3 -m parking_review.demo --port 8080

所有车牌、优惠券、订单号、金额均为虚构数据。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config, timeutil
from .db import connect, init_db, insert, query_one
from .payment import gateway
from .services import (
    create_manual_release,
    ingest_event,
    refund_order,
    review_case,
)


# (login, 姓名, 岗位, PIN)
STAFF = [
    ("monitor01", "闻澜", "monitor", "1001"),
    ("reviewer01", "沈确", "reviewer", "2002"),
    ("cashier01", "安穗", "cashier", "3003"),
    ("admin01", "贺钧", "admin", "9009"),
]

# (车牌, 品牌车型, 支付方式, 渠道脚本)
VEHICLES = [
    ("京A-F0001", "虚构·云岚 S1 白色轿车", "alipay", "ok"),
    ("京A-F0002", "虚构·川楝 X3 灰色 SUV", "wechat", "ok"),
    ("京A-F0003", "虚构·涧石 L7 蓝色 MPV", "unionpay", "fail_once"),
    ("京A-F0004", "虚构·云岚 S5 黑色轿车", "unionpay", "declined"),
    ("京A-F0005", "虚构·青骊 M2 红色轿车", None, "ok"),
    ("沪C-T2046", "虚构·川楝 X1 白色 SUV", "alipay", "ok"),
    ("粤B-9X306", "虚构·涧石 L9 银色 MPV", "alipay", "ok"),
    ("川A-77Q88", "虚构·青骊 M8 黑色 SUV", "alipay", "ok"),
    ("津A-D5520", "虚构·云岚 S3 金色轿车", "wechat", "ok"),
    ("浙A-2K777", "虚构·川楝 X5 绿色 SUV", "alipay", "ok"),
    ("苏E-6H021", "虚构·涧石 L2 白色轿车", "alipay", "ok"),
    ("渝A-G0731", "虚构·青骊 M5 灰色轿车", "alipay", "ok"),
    ("鄂A-3F819", "虚构·云岚 S1 红色轿车", "alipay", "ok"),
    ("京A-F0099", "虚构·川楝 X7 蓝色 SUV", "wechat", "ok"),
]

# (编号, 车道名, 方向, 设备时钟偏移秒)
LANES = [
    ("E1", "北地库入口", "entry", 60),
    ("E2", "南地面入口", "entry", -30),
    ("X1", "北中央缴费出口", "exit", 20),
    ("X2", "南潮汐出口", "exit", -45),
]


def reset_data() -> None:
    if config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def seed() -> Path:
    """生成完整样例，返回数据库路径。"""
    reset_data()
    conn = connect(config.DB_PATH)
    init_db(conn)
    gateway.reset()

    ts0 = "2026-01-01T00:00:00"
    lot_id = insert(
        conn, "parking_lot",
        code="MALL-YSL", name="云杉里购物中心地下停车场",
        city="虚构市杉湾区云杉路 88 号", created_at=ts0,
    )

    lane_ids: dict[str, int] = {}
    for code, name, direction, offset in LANES:
        lane_ids[code] = insert(
            conn, "lane",
            lot_id=lot_id, code=code, name=name, direction=direction,
            device_clock_offset_s=offset, status="online",
        )

    staff_ids: dict[str, int] = {}
    for login, name, role, pin in STAFF:
        staff_ids[login] = insert(
            conn, "staff", login=login, name=name, role=role, pin=pin
        )

    # 规则版本：v1（1-7 月）→ v2（8 月起）。旧订单保存 v1 快照，不随 v2 重算
    insert(
        conn, "rule_version",
        lot_id=lot_id, version=1, effective_from="2026-01-01T00:00:00",
        effective_to="2026-08-01T00:00:00",
        free_minutes=15, hour_fee_cents=600, daily_cap_cents=6000,
        published_by="admin01", note="开业初版：免费 15 分钟，6 元/小时，单日封顶 60 元",
    )
    insert(
        conn, "rule_version",
        lot_id=lot_id, version=2, effective_from="2026-08-01T00:00:00",
        effective_to=None,
        free_minutes=30, hour_fee_cents=800, daily_cap_cents=8000,
        published_by="admin01", note="现行版：免费 30 分钟，8 元/小时，单日封顶 80 元",
    )

    for plate, model, method, script in VEHICLES:
        insert(
            conn, "vehicle",
            plate=plate, plate_color="blue", brand_model=model,
            payment_method=method,
            payment_bound_at="2026-08-02T10:00:00" if method else None,
            auto_pay_enabled=1, gateway_script=script,
            created_at="2026-08-02T10:00:00",
        )

    # 优惠券：有效现金券 / 过期券 / 有效折扣券 / 作废券
    insert(
        conn, "coupon",
        code="CP-WEL-500", plate="浙A-2K777", type="cash", amount=500,
        valid_from="2026-09-01T00:00:00", valid_to="2026-09-30T23:59:59",
        status="unused",
    )
    insert(
        conn, "coupon",
        code="CP-OLD-1000", plate="津A-D5520", type="cash", amount=1000,
        valid_from="2026-07-01T00:00:00", valid_to="2026-08-31T23:59:59",
        status="unused",
    )
    insert(
        conn, "coupon",
        code="CP-VIP-80", plate="京A-F0002", type="discount", amount=20,
        valid_from="2026-09-01T00:00:00", valid_to="2026-12-31T23:59:59",
        status="unused",
    )
    insert(
        conn, "coupon",
        code="CP-VOID-300", plate=None, type="cash", amount=300,
        valid_from="2026-09-01T00:00:00", valid_to="2026-09-30T23:59:59",
        status="void",
    )
    conn.commit()

    reviewer = query_one(conn, "SELECT * FROM staff WHERE id=?", (staff_ids["reviewer01"],))
    cashier = query_one(conn, "SELECT * FROM staff WHERE id=?", (staff_ids["cashier01"],))
    ev_dir = config.EVIDENCE_DIR

    def server_time(lane_code: str, want_device: str) -> str:
        """按车道时钟偏移反推平台接收时间，保证设备时间精确。"""
        offset = dict((c, o) for c, _, _, o in LANES)[lane_code]
        return timeutil.shift(want_device, seconds=-offset)

    def event(lane: str, direction: str, want_device: str, **kw):
        return ingest_event(
            conn,
            lane_id=lane_ids[lane], direction=direction,
            server_time=server_time(lane, want_device),
            evidence_dir=ev_dir, **kw,
        )

    # ---------------- 历史订单（演示规则快照不重算） ----------------
    # v1 时代订单：7 月 28 日，按 v1（15 分钟免费 / 6 元每小时）
    event("E1", "entry", "2026-07-28T10:05:00", plate="苏E-6H021",
          message_uid="MSG-HIS-0001-E")
    event("X1", "exit", "2026-07-28T12:35:00", plate="苏E-6H021",
          message_uid="MSG-HIS-0001-X")

    # 跨版本边界：7 月 31 日 23:40 入场，按入场时间适用 v1
    event("E1", "entry", "2026-07-31T23:40:00", plate="渝A-G0731",
          message_uid="MSG-HIS-0002-E")
    r = event("X1", "exit", "2026-08-01T00:50:00", plate="渝A-G0731",
              message_uid="MSG-HIS-0002-X")
    # 跨日单事后复核 → 归档
    cross_case = conn.execute(
        "SELECT id FROM exception_case WHERE session_id=? AND type='cross_day'",
        (r.session_id,),
    ).fetchone()
    review_case(conn, case_id=cross_case["id"], staff=reviewer,
                action="dismiss", resolution="跨日切片与封顶核对无误，归档")

    # 历史退款单：9 月 20 日设备误识别重复扣费，收费员整单退款
    event("E2", "entry", "2026-09-20T14:02:00", plate="鄂A-3F819",
          message_uid="MSG-HIS-0003-E")
    r = event("X2", "exit", "2026-09-20T16:12:00", plate="鄂A-3F819",
              message_uid="MSG-HIS-0003-X")
    refund_order(conn, order_id=r.order_id, staff=cashier,
                 reason_code="device_error", reason_detail="出口地感重复触发导致误扣，核实后全额退款")

    # 历史人工放行：9 月 22 日无牌车扫码入场，凭据核验通过后放行
    r = event("E1", "entry", "2026-09-22T19:06:00", plate=None,
              no_plate_token="NP-9K12-OLD", message_uid="MSG-HIS-0004-E")
    entry_ev = query_one(
        conn, "SELECT evidence_path FROM lane_event WHERE id=?", (r.event_id,)
    )
    create_manual_release(
        conn, case_id=r.case_ids[0], staff=reviewer,
        reason_code="token_verified",
        reason_detail="无牌车扫码凭据 NP-9K12-OLD 与商场访客登记一致，核验出场照片后放行",
        evidence_path=entry_ev["evidence_path"],
    )

    # ---------------- 2026-09-25 当日样例（故意乱序发送，页面按设备时间排序） ----------------
    # 1) 跨 3 日停车：23 日晚入场，25 日晨出场，逐日封顶
    event("E1", "entry", "2026-09-23T21:10:00", plate="川A-77Q88",
          message_uid="MSG-D25-0001-E")

    # 2) 优惠券失效：券 8 月底已过期
    event("E2", "entry", "2026-09-25T08:20:00", plate="津A-D5520",
          message_uid="MSG-D25-0002-E")

    # 3) 未绑定支付方式车辆
    event("E1", "entry", "2026-09-25T09:00:00", plate="京A-F0005",
          message_uid="MSG-D25-0003-E")

    # 4) 正常短停（免费时长内，0 元）
    event("E1", "entry", "2026-09-25T08:50:00", plate="京A-F0001",
          message_uid="MSG-D25-0004-E")

    # 5) 车牌不一致：入场 沪C-T2046，出场误识别 沪C-T20A6
    event("E2", "entry", "2026-09-25T07:15:00", plate="沪C-T2046",
          message_uid="MSG-D25-0005-E")

    # 6) 重复出场车辆：先入场
    event("E1", "entry", "2026-09-25T08:02:00", plate="粤B-9X306",
          message_uid="MSG-D25-0006-E")

    # 7) 首次扣费失败车辆（fail_once）
    event("E1", "entry", "2026-09-25T08:40:00", plate="京A-F0003",
          message_uid="MSG-D25-0007-E")

    # 8) 签约注销车辆（始终失败）
    event("E2", "entry", "2026-09-25T09:10:00", plate="京A-F0004",
          message_uid="MSG-D25-0008-E")

    # 9) 正常停车（微信，2 小时档）
    event("E2", "entry", "2026-09-25T08:30:00", plate="京A-F0002",
          message_uid="MSG-D25-0009-E")

    # 10) 有效优惠券车辆
    event("E1", "entry", "2026-09-25T09:05:00", plate="浙A-2K777",
          message_uid="MSG-D25-0010-E")

    # 11) 无牌车扫码入场（滞留场内，等待处理）
    event("E1", "entry", "2026-09-25T10:10:00", plate=None,
          no_plate_token="NP-7F3A-NEW", confidence=0.0,
          message_uid="MSG-D25-0011-E")

    # 12) 在场未出车辆
    event("E2", "entry", "2026-09-25T10:42:00", plate="京A-F0099",
          message_uid="MSG-D25-0012-E")

    # ---- 出场消息 ----
    # 免费时长边界：22 分钟，0 元
    event("X1", "exit", "2026-09-25T09:12:00", plate="京A-F0001",
          message_uid="MSG-D25-0004-X")

    # 消息重复投递：同一 uid 发两遍，只允许产生一次结算
    event("X1", "exit", "2026-09-25T10:05:00", plate="京A-F0002",
          message_uid="MSG-D25-0009-X")
    dup = event("X1", "exit", "2026-09-25T10:05:00", plate="京A-F0002",
                message_uid="MSG-D25-0009-X")
    assert dup.duplicated_message, "同 uid 出场消息必须幂等"

    # 首次扣费失败 → 异常队列（可在界面重试，第二次成功）
    event("X2", "exit", "2026-09-25T09:40:00", plate="京A-F0003",
          message_uid="MSG-D25-0007-X")

    # 未绑支付方式 → 扣费失败异常
    event("X1", "exit", "2026-09-25T09:55:00", plate="京A-F0005",
          message_uid="MSG-D25-0003-X")

    # 车牌识别不一致 → 拦截，不扣费
    event("X2", "exit", "2026-09-25T09:47:00", plate="沪C-T20A6",
          confidence=0.71, message_uid="MSG-D25-0005-X")

    # 正常扣费后 8 分钟再次出场 → 重复出场拦截，不二次扣费
    event("X1", "exit", "2026-09-25T09:55:00", plate="粤B-9X306",
          message_uid="MSG-D25-0006-X")
    event("X1", "exit", "2026-09-25T10:03:00", plate="粤B-9X306",
          message_uid="MSG-D25-0006-X2")

    # 无牌车出场，凭据无匹配会话 → 无牌车复核
    event("X1", "exit", "2026-09-25T09:30:00", plate=None,
          no_plate_token="NP-ZZZZ-UNK", confidence=0.0,
          message_uid="MSG-D25-0013-X")

    # 跨日车出场 → 跨日复核（不阻断），按日封顶收费
    event("X1", "exit", "2026-09-25T09:40:00", plate="川A-77Q88",
          message_uid="MSG-D25-0001-X")

    # 优惠券过期 → 券失效复核，按原价扣
    event("X2", "exit", "2026-09-25T10:02:00", plate="津A-D5520",
          coupon_code="CP-OLD-1000", message_uid="MSG-D25-0002-X")

    # 有效现金券：800 减 500，实付 3 元
    event("X1", "exit", "2026-09-25T10:20:00", plate="浙A-2K777",
          coupon_code="CP-WEL-500", message_uid="MSG-D25-0010-X")

    # 签约注销 → 扣费失败异常
    event("X2", "exit", "2026-09-25T10:25:00", plate="京A-F0004",
          message_uid="MSG-D25-0008-X")

    conn.commit()

    counts = {
        row["type"]: row["n"]
        for row in conn.execute(
            "SELECT type, COUNT(*) n FROM exception_case GROUP BY type"
        )
    }
    conn.close()
    return config.DB_PATH, counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="停车无感扣费异常复核平台（虚构演示数据）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seed-only", action="store_true", help="只生成样例不启动服务")
    args = parser.parse_args(argv)

    db_path, counts = seed()
    print("=" * 64)
    print(" 商场停车无感扣费异常复核平台 · 本地演示已生成（数据均为虚构）")
    print("=" * 64)
    print(f" SQLite       : {db_path}")
    print(f" 证据图片目录 : {config.EVIDENCE_DIR}")
    print(" 异常复核单   : " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())
    ))
    print(" 演示岗位 PIN : 监控员 1001 / 复核员 2002 / 收费员 3003 / 管理员 9009")
    if args.seed_only:
        return 0

    from .web import serve

    print(f"\n 启动地址     : http://{args.host}:{args.port}/")
    print(" Ctrl+C 退出\n")
    serve(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
