"""生成虚构样例数据：进出场消息流、无感扣费、六类异常、人工放行与退款。

用法::

    python -m parking.seed            # 已初始化则跳过
    python -m parking.seed --reset    # 删除后重建

所有车牌、人员、订单、金额均为虚构数据。
"""
from __future__ import annotations

import sys

from . import billing, config, evidence, queries
from .auth import Operator
from .db import connect, init_db
from .payment import InMemoryPaymentAdapter
from .service import (ANOMALY_LABELS, RELEASE_REASONS, ParkingService,
                      ServiceError)

FEN = "￥%.2f"


def _ev(svc, msg_uid, plate, lane_name, device_time, conf, note=""):
    filename = f"EV-{msg_uid}.svg"
    evidence.write_evidence(filename, plate, lane_name, device_time, conf, note)
    return filename


def seed(reset: bool = False) -> ParkingService:
    config.ensure_dirs()
    if reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
        for p in config.DB_PATH.parent.glob("parking.db-*"):
            p.unlink(missing_ok=True)
    conn = connect()
    init_db(conn)
    if conn.execute("SELECT COUNT(*) FROM lots").fetchone()[0] > 0:
        return ParkingService(conn, InMemoryPaymentAdapter())

    svc = ParkingService(conn)
    cur = conn.cursor()

    # ------------------------------------------------------------ 主数据
    cur.execute(
        "INSERT INTO lots(code, name, address, created_at) "
        "VALUES ('M01','青澜中央广场停车场','海市临江区港隆路 88 号 B2/B3','2026-01-01T00:00:00+08:00')")
    lot_id = cur.lastrowid
    lanes = {}
    for code, name, direction in [
        ("A-EN-01", "B2-A 区入口", "ENTRY"),
        ("A-EX-01", "B2-A 区出口", "EXIT"),
        ("B-EN-01", "B3-B 区入口", "ENTRY"),
        ("B-EX-01", "B3-B 区出口", "EXIT"),
    ]:
        cur.execute(
            "INSERT INTO lanes(lot_id, code, name, direction, status) VALUES (?,?,?,?,'IDLE')",
            (lot_id, code, name, direction))
        lanes[code] = cur.lastrowid

    # 规则版本 v1（年初口径）
    cur.execute(
        "INSERT INTO rule_versions(lot_id, version, valid_from, valid_to, free_minutes, "
        "unit_minutes, unit_price_fen, daily_cap_fen, note) "
        "VALUES (?,1,'2026-01-01T00:00:00+08:00','2026-09-01T00:00:00+08:00',"
        "30,60,500,5000,'v1：免费30分钟，5元/小时，自然日封顶50元')",
        (lot_id,))
    # v2（9 月起调价，按入场时间选用）
    cur.execute(
        "INSERT INTO rule_versions(lot_id, version, valid_from, valid_to, free_minutes, "
        "unit_minutes, unit_price_fen, daily_cap_fen, note) "
        "VALUES (?,2,'2026-09-01T00:00:00+08:00',NULL,"
        "15,30,300,6000,'v2：免费15分钟，3元/半小时，自然日封顶60元')",
        (lot_id,))

    vehicles = {}
    for plate, color, owner, mtype, mname, token in [
        ("沪AD88026", "GREEN", "林知微", "ETP", "无感支付(ETC联名) ****88026", "OK:ETP-88026"),
        ("沪AF22169", "BLUE", "沈砚", "CREDIT", "虚构银行信用卡 ****22169", "OK:CRD-22169"),
        ("沪AE77K05", "GREEN", "顾长宁", "WALLET", "商场电子钱包 余额充足", "OK:WLT-77K05"),
        ("沪AB55T30", "BLUE", "陆见山", "CREDIT", "虚构银行信用卡 ****5530", "FAIL:CRD-55T30"),
        ("沪AG90D12", "GREEN", "周知珩", "ETP", "无感支付(ETC联名) ****9012", "OK:ETP-90D12"),
        ("沪AC66M88", "BLUE", "许棠", "WALLET", "商场电子钱包 余额充足", "OK:WLT-66M88"),
    ]:
        cur.execute(
            "INSERT INTO vehicles(plate, plate_color, owner_name) VALUES (?,?,?)",
            (plate, color, owner))
        vid = cur.lastrowid
        vehicles[plate] = vid
        cur.execute(
            "INSERT INTO payment_methods(vehicle_id, method_type, display_name, token, "
            "valid, bound_since) VALUES (?,?,?,?,1,'2026-01-05T09:00:00+08:00')",
            (vid, mtype, mname, token))

    # 优惠券
    coupons = {}
    for code, vid, ctype, amount, vf, vt in [
        ("CP-FREE120", vehicles["沪AD88026"], "FREE_MINUTES", 120,
         "2026-09-01T00:00:00+08:00", "2026-09-30T23:59:59+08:00"),
        ("CP-FIX0500", vehicles["沪AF22169"], "FIXED", 500,
         "2026-09-01T00:00:00+08:00", "2026-09-30T23:59:59+08:00"),
        ("CP-OLD0060", vehicles["沪AD88026"], "FREE_MINUTES", 60,
         "2026-07-01T00:00:00+08:00", "2026-08-31T23:59:59+08:00"),
    ]:
        cur.execute(
            "INSERT INTO coupons(code, vehicle_id, coupon_type, amount, valid_from, valid_to) "
            "VALUES (?,?,?,?,?,?)", (code, vid, ctype, amount, vf, vt))
        coupons[code] = cur.lastrowid

    operators = {}
    for login, name, role, code in [
        ("admin", "贺衡", "ADMIN", "admin123"),
        ("fang", "方屿（B2岗亭）", "OPERATOR", "op123"),
        ("shen", "申澄（稽核）", "AUDITOR", "au123"),
        ("qian", "钱泊（财务）", "FINANCE", "fi123"),
    ]:
        cur.execute(
            "INSERT INTO operators(login, display_name, role, pass_code) VALUES (?,?,?,?)",
            (login, name, role, code))
        operators[role] = Operator(cur.lastrowid, login, name, role)
    conn.commit()

    op_oper = operators["OPERATOR"]
    op_admin = operators["ADMIN"]
    op_fin = operators["FINANCE"]

    results: list[tuple[str, dict]] = []

    def send(uid, lane, dt, direction, plate=None, conf=0.98, vehicle_id=None,
             coupon=None, note=""):
        lane_row = conn.execute("SELECT name FROM lanes WHERE id = ?",
                                (lanes[lane],)).fetchone()
        img = _ev(svc, uid, plate, lane_row["name"], dt, conf, note)
        r = svc.ingest_message(
            msg_uid=uid, lane_code=lane, device_time=dt, direction=direction,
            raw_plate=plate, confidence=conf, vehicle_id=vehicle_id,
            coupon_code=coupon, plate_image=img, note=note)
        results.append((uid, r))
        return r

    # ---------------- 场景 1：跨日停车（8/31 入场走 v1 快照，分段+封顶）
    send("CAM-A-20260831-0017", "A-EN-01", "2026-08-31T21:10:00+08:00",
         "ENTRY", "沪AE77K05", 0.99)
    r = send("CAM-A-20260901-0042", "A-EX-01", "2026-09-01T09:40:00+08:00",
             "EXIT", "沪AE77K05", 0.97)

    # ---------------- 场景 2：免费券全额抵扣，0 元放行
    send("CAM-B-20260910-0103", "B-EN-01", "2026-09-10T13:00:00+08:00",
         "ENTRY", "沪AD88026", 0.98)
    send("CAM-B-20260910-0156", "B-EX-01", "2026-09-10T14:08:00+08:00",
         "EXIT", "沪AD88026", 0.96, coupon="CP-FREE120")

    # ---------------- 场景 3：立减券抵扣后支付，随后退款
    send("CAM-A-20260912-0088", "A-EN-01", "2026-09-12T10:00:00+08:00",
         "ENTRY", "沪AF22169", 0.99)
    r3 = send("CAM-A-20260912-0204", "A-EX-01", "2026-09-12T13:34:00+08:00",
              "EXIT", "沪AF22169", 0.98, coupon="CP-FIX0500")

    # ---------------- 场景 4：支付失败进入复核（道闸不落杆放行外的异常）
    send("CAM-B-20260918-0266", "B-EN-01", "2026-09-18T19:20:00+08:00",
         "ENTRY", "沪AB55T30", 0.95)
    send("CAM-B-20260918-0301", "B-EX-01", "2026-09-18T21:05:00+08:00",
         "EXIT", "沪AB55T30", 0.93)

    # ---------------- 场景 5：优惠券失效，按无券计费
    send("CAM-A-20260922-0330", "A-EN-01", "2026-09-22T18:40:00+08:00",
         "ENTRY", "沪AD88026", 0.98)
    send("CAM-A-20260922-0358", "A-EX-01", "2026-09-22T20:05:00+08:00",
         "EXIT", "沪AD88026", 0.97, coupon="CP-OLD0060")

    # ---------------- 场景 6：车牌识别不一致 -> 复核 -> 人工放行
    send("CAM-B-20260920-0210", "B-EN-01", "2026-09-20T08:30:00+08:00",
         "ENTRY", "沪AG90D12", 0.99)
    r6 = send("CAM-B-20260920-0298", "B-EX-01", "2026-09-20T17:52:00+08:00",
              "EXIT", "沪AG90DI2", 0.62, vehicle_id=vehicles["沪AG90D12"],
              note="OCR 将 12 误识为 I2，车主月卡扫码确认身份")

    # ---------------- 场景 7：正常结算后重复出场，不重复扣费
    send("CAM-A-20260923-0401", "A-EN-01", "2026-09-23T09:30:00+08:00",
         "ENTRY", "沪AC66M88", 0.99)
    send("CAM-A-20260923-0447", "A-EX-01", "2026-09-23T11:02:00+08:00",
         "EXIT", "沪AC66M88", 0.98)
    # 摄像头重复推送：同 uid 去重 + 新 uid 的二次出场拦截
    dup_same = svc.ingest_message(
        msg_uid="CAM-A-20260923-0447", lane_code="A-EX-01",
        device_time="2026-09-23T11:02:00+08:00", direction="EXIT",
        raw_plate="沪AC66M88", confidence=0.98)
    send("CAM-A-20260923-0448", "A-EX-01", "2026-09-23T11:14:00+08:00",
         "EXIT", "沪AC66M88", 0.81, note="车辆已离场，摄像头重复抓拍")

    # ---------------- 场景 8：无牌车入场与出场
    send("CAM-B-20260924-0501", "B-EN-01", "2026-09-24T10:00:00+08:00",
         "ENTRY", None, 0.31, note="无牌车，发放纸质临停凭证 P-20260924-07")
    r8 = send("CAM-B-20260924-0566", "B-EX-01", "2026-09-24T12:20:00+08:00",
              "EXIT", None, 0.28, note="出场仍未识别车牌")

    # ------------------------------------------------------- 复核处理
    def case_id_of(uid):
        return conn.execute("SELECT id FROM review_cases WHERE msg_id = "
                            "(SELECT id FROM lane_messages WHERE msg_uid = ?) "
                            "ORDER BY id LIMIT 1", (uid,)).fetchone()[0]

    # 跨日：自动分段复核无误
    svc.resolve_case(op_oper, case_id_of("CAM-A-20260901-0042"),
                     "跨日两段：8/31 170 分钟扣 3 单位 15.00 元；9/1 580 分钟 10 单位触顶 50.00 元，快照 v1，复核无误")
    # 券失效：告知顾客
    svc.resolve_case(op_oper, case_id_of("CAM-A-20260922-0358"),
                     "CP-OLD0060 已于 8/31 到期，按无券收取 9.00 元，已向顾客出示有效期截图")
    # 牌异：核对后人工放行（必须选原因 + 关联证据）
    case6 = case_id_of("CAM-B-20260920-0298")
    svc.resolve_case(op_oper, case6,
                     "比对入场/出场抓拍与月卡扫码账户，确认为同一车辆沪AG90D12，OCR 尾号误识")
    ev6 = conn.execute(
        "SELECT plate_image FROM lane_messages WHERE msg_uid = 'CAM-B-20260920-0298'").fetchone()[0]
    svc.create_release(op_oper, r6["session_id"], "PLATE_CONFIRMED",
                       "双抓拍比对车架与月卡账户一致，系统识别尾号偏差，当场放行", ev6)
    # 无牌车：凭纸质凭证人工放行
    case8 = case_id_of("CAM-B-20260924-0566")
    svc.resolve_case(op_oper, case8,
                     "车主出示纸质临停凭证 P-20260924-07，与入场登记笔迹/时间一致")
    no_plate_session = conn.execute(
        "SELECT id FROM sessions WHERE plate IS NULL ORDER BY id DESC LIMIT 1").fetchone()[0]
    ev8 = conn.execute(
        "SELECT plate_image FROM lane_messages WHERE msg_uid = 'CAM-B-20260924-0566'").fetchone()[0]
    svc.create_release(op_oper, no_plate_session, "NO_PLATE_TICKET",
                       "凭纸质临停凭证 P-20260924-07 核对放行，凭证扫描件附后", ev8)

    # ------------------------------------------------------- 退款（一单一次）
    svc.refund_order(op_fin, r3["order_id"],
                     "顾客在 4 层商户前台已凭消费小票减免停车费，出场又被无感扣费，核实后全额退款")
    # 财务重复提交 -> 幂等返回原退款单
    refund_again, idempotent = svc.refund_order(
        op_fin, r3["order_id"], "重复提交的退款申请")
    assert idempotent, "退款应当幂等"

    # v3 新规则（10 月生效）：只进版本台账，不影响已结算订单快照
    svc.create_rule_version(
        op_admin, lot_id, free_minutes=20, unit_minutes=30, unit_price_fen=250,
        daily_cap_fen=5500, valid_from="2026-10-01T00:00:00+08:00",
        note="v3：免费20分钟，2.5元/半小时，自然日封顶55元（促销季）")

    conn.commit()
    return svc


def report(svc: ParkingService) -> str:
    conn = svc.conn
    lines = ["=" * 68, "  商场停车无感扣费异常复核平台 · 样例生成完毕（数据均为虚构）",
             "=" * 68]
    lines.append("\n[停车会话 / 订单快照]")
    for s in queries.sessions(conn):
        snap = f"v{s['rule_snapshot']['rule']['version']}" if s.get("rule_snapshot") else "-"
        amt = (FEN % (s["amount_fen"] / 100)) if s["amount_fen"] is not None else "   -    "
        exit_part = s["exit_time"][5:16] if s["exit_time"] else "    在场内   "
        lines.append(
            f"  #{s['id']:<2} {s['plate'] or '无牌车':<10} {s['entry_time'][5:16]} "
            f"-> {exit_part} 金额 {amt:>8} 规则快照 {snap:<3} {s['status']}")
    lines.append("\n[异常复核队列]")
    for c in queries.cases(conn):
        lines.append(f"  {c['case_no']} [{c['label']['short']}] {c['status']:<8} "
                     f"{(c['plate'] or '无牌车'):<10} {c['label']['full'][:34]}")
    lines.append("\n[人工放行]")
    for r in queries.releases(conn):
        lines.append(f"  {r['release_no']} {(r['plate'] or '无牌车'):<10} {r['reason_code']:<16} "
                     f"证据 {r['evidence_file']} 操作人 {r['operator_name']}")
    lines.append("\n[退款台账]")
    for r in queries.refunds(conn):
        lines.append(f"  {r['refund_no']} 订单 {r['order_no']} "
                     f"退款 {FEN % (r['amount_fen']/100)} 操作人 {r['operator_name']}")
    st = queries.stats(conn)
    lines.append(f"\n[汇总] 车道 {st['lanes_total']} 条（告警 {st['lanes_alert']}），"
                 f"在场 {st['inside']} 辆，未结复核 {st['open_cases']} 件，"
                 f"已支付订单 {st['paid_orders']} 笔，"
                 f"收入 {FEN % (st['paid_amount_fen']/100)}，"
                 f"退款 {FEN % (st['refund_amount_fen']/100)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    svc = seed(reset="--reset" in argv)
    print(report(svc))
    print(f"\n数据库: {config.DB_PATH}\n证据目录: {config.EVIDENCE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
