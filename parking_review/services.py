"""核心业务服务：消息接入、去重、计费结算、异常复核、人工放行、退款。

所有方法在单个 SQLite 连接内以事务方式运行；权限在服务层强制校验。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import billing, timeutil
from .config import can
from .db import dumps, insert, query_all, query_one
from .evidence import make_evidence
from .payment import gateway


def _levenshtein(a: str, b: str) -> int:
    """标准编辑距离，用于出入口车牌单字符误识的候选匹配。"""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

# ---------------------------------------------------------------- 常量

EXCEPTION_META: dict[str, dict[str, str]] = {
    "plate_mismatch": {"tag": "车牌不符", "title": "入场出场车牌识别不一致"},
    "duplicate_exit": {"tag": "重复出场", "title": "重复出场消息"},
    "payment_failed": {"tag": "扣费失败", "title": "自动扣费失败"},
    "no_plate": {"tag": "无牌车", "title": "无牌车需人工核对"},
    "cross_day": {"tag": "跨日停车", "title": "跨日停车复核"},
    "coupon_invalid": {"tag": "券失效", "title": "优惠券失效"},
}

# 跨日 / 券失效属于事后复核，不阻断当次结算；其余类型拦截出场
ADVISORY_TYPES = {"cross_day", "coupon_invalid"}

RELEASE_REASONS: dict[str, str] = {
    "plate_uncertain": "车牌无法确认，人工核对后放行",
    "device_fault": "车道设备故障导致记录异常",
    "driver_appeal": "车主现场申诉，核验后放行",
    "special_vehicle": "军警/应急等特殊车辆放行",
    "token_verified": "无牌车扫码凭据人工核验通过",
    "duplicate_confirmed": "重复出场确认拦截，不予二次扣费",
}

REFUND_REASONS: dict[str, str] = {
    "wrong_charge": "计费规则适用错误",
    "device_error": "设备识别异常导致误扣",
    "duplicate_charge": "重复扣费退款",
    "service_complaint": "客诉协商退款",
}


class ServiceError(Exception):
    """业务规则错误（会展示给操作员）。"""


class PermissionDenied(ServiceError):
    pass


@dataclass
class IngestResult:
    event_id: int
    duplicated_message: bool = False
    session_id: int | None = None
    order_id: int | None = None
    case_ids: list[int] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------- 辅助

def _device_time(conn: sqlite3.Connection, lane_id: int, server_time: str) -> str:
    lane = query_one(conn, "SELECT * FROM lane WHERE id = ?", (lane_id,))
    if lane is None:
        raise ServiceError(f"车道 {lane_id} 不存在")
    return timeutil.shift(server_time, seconds=int(lane["device_clock_offset_s"]))


def _require_role(staff: sqlite3.Row, permission: str) -> None:
    if not can(staff["role"], permission):
        raise PermissionDenied(
            f"岗位「{staff['role']}」无权执行该操作（需要 {permission}）"
        )


def _open_case(
    conn: sqlite3.Connection,
    *,
    type_: str,
    summary: str,
    session_id: int | None = None,
    entry_event_id: int | None = None,
    exit_event_id: int | None = None,
    order_id: int | None = None,
    plate_in: str | None = None,
    plate_out: str | None = None,
    opened_at: str,
) -> int:
    meta = EXCEPTION_META[type_]
    code = f"EX-{uuid.uuid4().hex[:8].upper()}"
    return insert(
        conn,
        "exception_case",
        code=code,
        type=type_,
        session_id=session_id,
        entry_event_id=entry_event_id,
        exit_event_id=exit_event_id,
        order_id=order_id,
        plate_in=plate_in,
        plate_out=plate_out,
        short_tag=meta["tag"],
        summary=summary,
        opened_at=opened_at,
    )


def _evidence_path(evidence_dir: Path, message_uid: str, flagged: bool) -> Path:
    sub = "review" if flagged else "normal"
    return Path(evidence_dir) / sub / f"{message_uid}.png"


def _only_advisory(conn: sqlite3.Connection, case_ids: list[int]) -> bool:
    if not case_ids:
        return True
    rows = conn.execute(
        f"SELECT type FROM exception_case WHERE id IN ({','.join('?' * len(case_ids))})",
        case_ids,
    ).fetchall()
    return all(r["type"] in ADVISORY_TYPES for r in rows)


# ---------------------------------------------------------------- 计费结算

def _settle(
    conn: sqlite3.Connection,
    *,
    session: sqlite3.Row,
    exit_event_id: int,
    exit_time: str,
    exit_lane_id: int,
    coupon_code: str | None,
    opened_cases: list[int],
) -> int:
    """对在场会话发起计费与自动扣费，返回订单 id。失败也会生成 failed 订单并入复核。"""
    lot = query_one(conn, "SELECT * FROM parking_lot WHERE id = ?", (session["lot_id"],))
    rule = billing.select_rule_version(conn, session["lot_id"], session["entry_time"])
    fee = billing.compute_fee(session["entry_time"], exit_time, rule)

    coupon_row: sqlite3.Row | None = None
    discount = 0
    if coupon_code:
        coupon_row = query_one(
            conn, "SELECT * FROM coupon WHERE code = ?", (coupon_code.strip(),)
        )
        if coupon_row is None:
            cid = _open_case(
                conn, type_="coupon_invalid",
                summary=(
                    f"优惠券 {coupon_code} 不存在，"
                    f"已按原价 {billing.yuan(fee['base_fee_cents'])} 元结算"
                ),
                session_id=session["id"], exit_event_id=exit_event_id,
                opened_at=exit_time,
            )
            opened_cases.append(cid)
            coupon_row = None
        else:
            try:
                billing.validate_coupon(coupon_row, exit_time, session["plate"])
                discount = billing.apply_coupon(fee["base_fee_cents"], coupon_row)
            except billing.CouponInvalid as exc:
                cid = _open_case(
                    conn, type_="coupon_invalid",
                    summary=(
                        f"{exc.detail}；已不使用该券，"
                        f"按原价 {billing.yuan(fee['base_fee_cents'])} 元结算"
                    ),
                    session_id=session["id"], exit_event_id=exit_event_id,
                    opened_at=exit_time,
                )
                opened_cases.append(cid)
                coupon_row = None
                discount = 0

    snapshot = billing.build_snapshot(
        rule=rule,
        lot_name=lot["name"],
        plate=session["plate"],
        entry_time=session["entry_time"],
        exit_time=exit_time,
        fee_detail=fee,
        coupon=coupon_row,
        discount_cents=discount,
    )

    vehicle = None
    if session["vehicle_id"]:
        vehicle = query_one(conn, "SELECT * FROM vehicle WHERE id = ?", (session["vehicle_id"],))

    order_id = insert(
        conn,
        "payment_order",
        order_no=f"PK{uuid.uuid4().hex[:10].upper()}",
        session_id=session["id"],
        lot_id=session["lot_id"],
        plate=session["plate"],
        rule_version_id=rule.id,
        billing_snapshot=dumps(snapshot),
        duration_minutes=fee["duration_minutes"],
        amount_cents=fee["base_fee_cents"],
        discount_cents=discount,
        coupon_id=coupon_row["id"] if coupon_row else None,
        coupon_code=coupon_row["code"] if coupon_row else None,
        payable_cents=fee["base_fee_cents"] - discount,
        status="created",
        payment_method=vehicle["payment_method"] if vehicle else None,
        created_at=exit_time,
    )

    # 跨日停车：无论是否支付成功都进入复核（核对逐日封顶明细）
    if fee["cross_day"]:
        days = len(fee["slices"])
        cid = _open_case(
            conn, type_="cross_day",
            summary=(
                f"车辆 {session['plate'] or '无牌'} 跨 {days} 个自然日停车，"
                f"共 {fee['duration_minutes']} 分钟，按日封顶合计 "
                f"{billing.yuan(fee['base_fee_cents'])} 元，请核对逐日切片"
            ),
            session_id=session["id"], exit_event_id=exit_event_id, order_id=order_id,
            plate_in=session["plate"], plate_out=session["plate"],
            opened_at=exit_time,
        )
        opened_cases.append(cid)

    # 必须绑定有效支付方式才能自动扣费
    if vehicle is None or not vehicle["payment_method"] or not vehicle["auto_pay_enabled"]:
        conn.execute(
            "UPDATE payment_order SET status='failed', pay_attempts=0, "
            "fail_reason=? WHERE id=?",
            ("车辆未绑定有效支付方式，无法无感扣费", order_id),
        )
        cid = _open_case(
            conn, type_="payment_failed",
            summary=(
                f"车辆 {session['plate'] or '无牌'} 出场应扣 "
                f"{billing.yuan(fee['base_fee_cents'] - discount)} 元，"
                "但未绑定有效支付方式，自动扣费未发起"
            ),
            session_id=session["id"], exit_event_id=exit_event_id, order_id=order_id,
            plate_in=session["plate"], plate_out=session["plate"],
            opened_at=exit_time,
        )
        opened_cases.append(cid)
        conn.execute(
            "UPDATE parking_session SET status='flagged', exit_time=?, "
            "exit_lane_id=?, order_id=? WHERE id=?",
            (exit_time, exit_lane_id, order_id, session["id"]),
        )
        return order_id

    result = gateway.pay(
        f"ORDER-{order_id}",
        vehicle["payment_method"],
        vehicle["gateway_script"],
        fee["base_fee_cents"] - discount,
    )
    if result.success:
        conn.execute(
            "UPDATE payment_order SET status='paid', pay_attempts=pay_attempts+1, "
            "paid_at=? WHERE id=?",
            (exit_time, order_id),
        )
        if coupon_row:
            conn.execute(
                "UPDATE coupon SET status='used', used_order_id=?, used_at=? WHERE id=?",
                (order_id, exit_time, coupon_row["id"]),
            )
        session_status = "settled"
    else:
        conn.execute(
            "UPDATE payment_order SET status='failed', pay_attempts=pay_attempts+1, "
            "fail_reason=? WHERE id=?",
            (result.reason, order_id),
        )
        cid = _open_case(
            conn, type_="payment_failed",
            summary=(
                f"车辆 {session['plate']} 出场应扣 "
                f"{billing.yuan(fee['base_fee_cents'] - discount)} 元，"
                f"渠道返回：{result.reason}"
            ),
            session_id=session["id"], exit_event_id=exit_event_id, order_id=order_id,
            plate_in=session["plate"], plate_out=session["plate"],
            opened_at=exit_time,
        )
        opened_cases.append(cid)
        session_status = "flagged"

    conn.execute(
        "UPDATE parking_session SET status=?, exit_time=?, exit_lane_id=?, order_id=? WHERE id=?",
        (session_status, exit_time, exit_lane_id, order_id, session["id"]),
    )
    return order_id


# ---------------------------------------------------------------- 消息接入

def ingest_event(
    conn: sqlite3.Connection,
    *,
    lane_id: int,
    direction: str,
    server_time: str,
    evidence_dir: str | Path,
    plate: str | None = None,
    confidence: float = 0.98,
    no_plate_token: str | None = None,
    coupon_code: str | None = None,
    message_uid: str | None = None,
) -> IngestResult:
    """接收一条车道识别消息。同一 message_uid 重复投递只处理一次。"""
    device_time = _device_time(conn, lane_id, server_time)
    lane = query_one(conn, "SELECT * FROM lane WHERE id = ?", (lane_id,))
    uid = message_uid or f"MSG-{uuid.uuid4().hex[:12].upper()}"

    # 消息幂等：相同 message_uid 视为重发，直接返回首次处理结果
    existing = query_one(conn, "SELECT * FROM lane_event WHERE message_uid = ?", (uid,))
    if existing is not None:
        conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
        return IngestResult(
            event_id=existing["id"], duplicated_message=True,
            session_id=existing["session_id"],
            note=f"消息 {uid} 已处理过（{existing['processed_as']}），幂等忽略",
        )

    flagged = confidence < 0.9
    ev_path = _evidence_path(evidence_dir, uid, flagged or plate is None)
    make_evidence(
        ev_path, lane_code=lane["code"], direction=direction,
        device_time=device_time, plate=plate, confidence=confidence,
        flagged=flagged,
    )

    opened_cases: list[int] = []
    result = IngestResult(event_id=-1, case_ids=opened_cases)

    # ------------------------------------------------------------- 入场
    if direction == "entry":
        dup_session = None
        if plate:
            dup_session = query_one(
                conn,
                "SELECT * FROM parking_session WHERE lot_id=? AND plate=? "
                "AND status IN ('in_park','flagged') ORDER BY entry_time DESC LIMIT 1",
                (lane["lot_id"], plate),
            )
        event_id = insert(
            conn, "lane_event",
            message_uid=uid, lane_id=lane_id, direction="entry",
            device_time=device_time, received_at=server_time,
            plate=plate, confidence=confidence, no_plate_token=no_plate_token,
            evidence_path=str(ev_path), processed_as="entry_opened",
        )
        result.event_id = event_id
        if dup_session is not None:
            conn.execute(
                "UPDATE lane_event SET processed_as='ignored', "
                "note='该车牌已有未结在场会话，入场消息忽略' WHERE id=?",
                (event_id,),
            )
            result.note = "重复入场，已忽略"
        else:
            vehicle = (
                query_one(conn, "SELECT * FROM vehicle WHERE plate = ?", (plate,))
                if plate else None
            )
            session_id = insert(
                conn, "parking_session",
                lot_id=lane["lot_id"],
                vehicle_id=vehicle["id"] if vehicle else None,
                plate=plate,
                no_plate_token=no_plate_token,
                entry_lane_id=lane_id,
                entry_time=device_time,
                status="flagged" if plate is None else "in_park",
                created_at=device_time,
            )
            conn.execute(
                "UPDATE lane_event SET session_id=? WHERE id=?", (session_id, event_id)
            )
            result.session_id = session_id
            if plate is None:
                cid = _open_case(
                    conn, type_="no_plate",
                    summary=(
                        f"入场车道 {lane['name']} 未识别到车牌"
                        + (f"，扫码凭据 {no_plate_token}" if no_plate_token else "，且无扫码凭据")
                        + "，需人工核对"
                    ),
                    session_id=session_id, entry_event_id=event_id,
                    opened_at=device_time,
                )
                opened_cases.append(cid)
        conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
        conn.commit()
        return result

    # ------------------------------------------------------------- 出场
    event_id = insert(
        conn, "lane_event",
        message_uid=uid, lane_id=lane_id, direction="exit",
        device_time=device_time, received_at=server_time,
        plate=plate, confidence=confidence, no_plate_token=no_plate_token,
        evidence_path=str(ev_path), processed_as="received",
    )
    result.event_id = event_id

    open_session: sqlite3.Row | None = None
    if plate:
        open_session = query_one(
            conn,
            "SELECT * FROM parking_session WHERE lot_id=? AND plate=? "
            "AND status IN ('in_park','flagged') ORDER BY entry_time DESC LIMIT 1",
            (lane["lot_id"], plate),
        )
    elif no_plate_token:
        open_session = query_one(
            conn,
            "SELECT * FROM parking_session WHERE lot_id=? AND no_plate_token=? "
            "AND status IN ('in_park','flagged') ORDER BY entry_time DESC LIMIT 1",
            (lane["lot_id"], no_plate_token),
        )

    if open_session is None and plate:
        # ① 重复出场：30 分钟内已结算且当前无新在场会话 → 拦截，绝不二次扣费
        recent = query_one(
            conn,
            """
            SELECT ps.*, le.device_time AS last_exit, le.id AS last_exit_event
            FROM parking_session ps
            JOIN lane_event le ON le.session_id = ps.id AND le.direction='exit'
            WHERE ps.lot_id=? AND ps.plate=? AND ps.status IN ('settled','released')
            ORDER BY le.device_time DESC LIMIT 1
            """,
            (lane["lot_id"], plate),
        )
        if recent is not None:
            gap = timeutil.minutes_between(recent["last_exit"], device_time)
            if 0 <= gap <= 30:
                conn.execute(
                    "UPDATE lane_event SET processed_as='duplicate', session_id=?, "
                    "duplicate_of=?, note=? WHERE id=?",
                    (recent["id"], recent["last_exit_event"],
                     f"距上次出场仅 {gap} 分钟，判定重复出场，未重复扣费", event_id),
                )
                _open_case(
                    conn, type_="duplicate_exit",
                    summary=(
                        f"车辆 {plate} 在 {device_time} 再次触发出场识别，距上次正常出场 "
                        f"{recent['last_exit']} 仅 {gap} 分钟；系统已拦截，未产生第二笔扣费"
                    ),
                    session_id=recent["id"], exit_event_id=event_id,
                    plate_in=plate, plate_out=plate, opened_at=device_time,
                )
                conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
                conn.commit()
                result.session_id = recent["id"]
                result.note = "重复出场已拦截，未重复扣费"
                return result

        # ② 车牌识别不一致：在在场会话中匹配车牌最相似的一辆
        #    （出入口 OCR 常见单字符误识，按编辑距离挑候选；无相近候选则不挂单）
        candidates = query_all(
            conn,
            "SELECT * FROM parking_session WHERE lot_id=? "
            "AND status IN ('in_park','flagged') AND plate IS NOT NULL",
            (lane["lot_id"],),
        )
        candidates = [
            c for c in candidates
            if c["plate"] and c["entry_time"] <= device_time
        ]
        candidate = None
        if candidates:
            best = min(candidates, key=lambda c: _levenshtein(c["plate"], plate))
            if _levenshtein(best["plate"], plate) <= max(2, len(plate) // 3):
                candidate = best
        if candidate is not None:
            entry_ev = query_one(
                conn,
                "SELECT id FROM lane_event WHERE session_id=? AND direction='entry' "
                "ORDER BY device_time LIMIT 1",
                (candidate["id"],),
            )
            cid = _open_case(
                conn, type_="plate_mismatch",
                summary=(
                    f"出场识别车牌 {plate}（置信度 {confidence:.2f}）与入场登记车牌 "
                    f"{candidate['plate']} 不一致，已拦截自动扣费，请人工核对抓拍"
                ),
                session_id=candidate["id"],
                entry_event_id=entry_ev["id"] if entry_ev else None,
                exit_event_id=event_id, plate_in=candidate["plate"], plate_out=plate,
                opened_at=device_time,
            )
            opened_cases.append(cid)
            conn.execute(
                "UPDATE lane_event SET processed_as='exception', session_id=?, "
                "note='车牌不一致，已挂复核' WHERE id=?",
                (candidate["id"], event_id),
            )
            conn.execute(
                "UPDATE parking_session SET status='flagged' WHERE id=?",
                (candidate["id"],),
            )
            result.session_id = candidate["id"]
            conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
            conn.commit()
            return result

        # ③ 场内有在场车辆但没有车牌相近的候选：同样拦截扣费、挂车牌异常人工找车
        if candidates:
            _open_case(
                conn, type_="plate_mismatch",
                summary=(
                    f"出场识别车牌 {plate}（置信度 {confidence:.2f}）在场内无对应在场会话，"
                    "且与全部在场车牌均不相近，已拦截自动扣费，请人工核对车辆去向"
                ),
                exit_event_id=event_id, plate_out=plate, opened_at=device_time,
            )
            conn.execute(
                "UPDATE lane_event SET processed_as='exception', "
                "note='出场车牌无匹配会话，挂车牌复核' WHERE id=?",
                (event_id,),
            )
            result.note = "车牌无匹配，挂起复核"
        else:
            # 场内查无任何会话：视为道闸误触发，仅登记消息，不扣费
            conn.execute(
                "UPDATE lane_event SET processed_as='ignored', "
                "note='场内无任何在场会话，未扣费' WHERE id=?",
                (event_id,),
            )
            result.note = "场内无在场会话，消息仅登记"
        conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
        conn.commit()
        return result

    # 无牌车出场（含无匹配凭据 / 入场即无牌的会话）→ 人工核对，系统不自动扣费
    if open_session is None or open_session["plate"] is None:
        if open_session is not None:
            summary = (
                f"无牌车（凭据 {open_session['no_plate_token'] or '无'}）驶离出场车道 "
                f"{lane['name']}，需人工核对后放行，系统未扣费"
            )
        else:
            summary = (
                f"出场车道 {lane['name']} 未识别到车牌"
                + (f"，扫码凭据 {no_plate_token} 未匹配到在场会话" if no_plate_token else "，无扫码凭据")
                + "，道闸保持拦截等待人工处理"
            )
        cid = _open_case(
            conn, type_="no_plate", summary=summary,
            session_id=open_session["id"] if open_session else None,
            exit_event_id=event_id, opened_at=device_time,
        )
        opened_cases.append(cid)
        conn.execute(
            "UPDATE lane_event SET processed_as='exception', "
            + ("session_id=?, " if open_session else "")
            + "note='无牌车出场，挂复核' WHERE id=?",
            ((open_session["id"],) if open_session else ()) + (event_id,),
        )
        conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
        conn.commit()
        result.session_id = open_session["id"] if open_session else None
        return result

    # 正常出场：计费 + 自动扣费
    conn.execute(
        "UPDATE lane_event SET session_id=? WHERE id=?", (open_session["id"], event_id)
    )
    order_id = _settle(
        conn,
        session=open_session,
        exit_event_id=event_id,
        exit_time=device_time,
        exit_lane_id=lane_id,
        coupon_code=coupon_code,
        opened_cases=opened_cases,
    )
    conn.execute(
        "UPDATE lane_event SET processed_as=? WHERE id=?",
        ("exit_settled" if _only_advisory(conn, opened_cases) else "exception", event_id),
    )
    result.session_id = open_session["id"]
    result.order_id = order_id
    conn.execute("UPDATE lane SET last_device_time=? WHERE id=?", (device_time, lane_id))
    conn.commit()
    return result


# ---------------------------------------------------------------- 重试扣费

def retry_payment(
    conn: sqlite3.Connection, *, order_id: int, staff: sqlite3.Row
) -> int:
    _require_role(staff, "case:review")
    order = query_one(conn, "SELECT * FROM payment_order WHERE id=?", (order_id,))
    if order is None:
        raise ServiceError("订单不存在")
    if order["status"] != "failed":
        raise ServiceError(f"订单状态为 {order['status']}，无需重试")
    vehicle = query_one(
        conn,
        "SELECT v.* FROM vehicle v JOIN parking_session ps ON ps.vehicle_id=v.id "
        "WHERE ps.id=?",
        (order["session_id"],),
    )
    if vehicle is None or not vehicle["payment_method"]:
        raise ServiceError("车辆仍未绑定有效支付方式，请先完成绑定或走人工放行")
    result = gateway.pay(
        f"ORDER-{order_id}", vehicle["payment_method"],
        vehicle["gateway_script"], order["payable_cents"],
    )
    if not result.success:
        conn.execute(
            "UPDATE payment_order SET pay_attempts=pay_attempts+1, fail_reason=? WHERE id=?",
            (result.reason, order_id),
        )
        conn.commit()
        raise ServiceError(f"重试仍失败：{result.reason}")
    paid_at = timeutil.now_iso()
    conn.execute(
        "UPDATE payment_order SET status='paid', pay_attempts=pay_attempts+1, "
        "fail_reason=NULL, paid_at=? WHERE id=?",
        (paid_at, order_id),
    )
    if order["coupon_id"]:
        conn.execute(
            "UPDATE coupon SET status='used', used_order_id=?, used_at=? WHERE id=?",
            (order_id, paid_at, order["coupon_id"]),
        )
    conn.execute(
        "UPDATE parking_session SET status='settled' WHERE id=?", (order["session_id"],)
    )
    conn.execute(
        "UPDATE exception_case SET status='resolved', resolved_at=?, resolved_by=?, "
        "resolution='重试扣费成功，订单已支付' WHERE type='payment_failed' AND order_id=? "
        "AND status IN ('open','in_review')",
        (paid_at, staff["id"], order_id),
    )
    conn.commit()
    return order_id


# ---------------------------------------------------------------- 复核处置

def review_case(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    staff: sqlite3.Row,
    action: str,
    resolution: str = "",
) -> None:
    """通用复核动作：claim(认领) / dismiss(核实无异议关闭)。"""
    _require_role(staff, "case:review")
    case = query_one(conn, "SELECT * FROM exception_case WHERE id=?", (case_id,))
    if case is None:
        raise ServiceError("复核单不存在")
    ts = timeutil.now_iso()
    if action == "claim":
        if case["status"] == "open":
            conn.execute(
                "UPDATE exception_case SET status='in_review', resolved_by=? WHERE id=?",
                (staff["id"], case_id),
            )
    elif action == "dismiss":
        conn.execute(
            "UPDATE exception_case SET status='resolved', resolved_at=?, resolved_by=?, "
            "resolution=? WHERE id=?",
            (ts, staff["id"], resolution or "复核确认无异议，归档结案", case_id),
        )
    else:
        raise ServiceError(f"未知复核动作 {action}")
    conn.commit()


def settle_mismatch_as_entry_plate(
    conn: sqlite3.Connection, *, case_id: int, staff: sqlite3.Row
) -> int:
    """车牌不一致：复核员确认以入场车牌为准，补结算（仅产生这一次正常扣费）。"""
    _require_role(staff, "case:review")
    case = query_one(conn, "SELECT * FROM exception_case WHERE id=?", (case_id,))
    if case is None or case["type"] != "plate_mismatch":
        raise ServiceError("复核单不存在或类型不符")
    exit_event = query_one(conn, "SELECT * FROM lane_event WHERE id=?", (case["exit_event_id"],))
    session = query_one(conn, "SELECT * FROM parking_session WHERE id=?", (case["session_id"],))
    if session["exit_time"] is not None:
        raise ServiceError("该会话已结算或已放行，不能重复处理")
    opened: list[int] = []
    order_id = _settle(
        conn, session=session, exit_event_id=exit_event["id"],
        exit_time=exit_event["device_time"], exit_lane_id=exit_event["lane_id"],
        coupon_code=None, opened_cases=opened,
    )
    conn.execute(
        "UPDATE lane_event SET processed_as='exit_settled', "
        "note='复核后按入场车牌结算' WHERE id=?",
        (exit_event["id"],),
    )
    ts = timeutil.now_iso()
    conn.execute(
        "UPDATE exception_case SET status='resolved', resolved_at=?, resolved_by=?, "
        "resolution=? WHERE id=?",
        (ts, staff["id"], f"确认以入场车牌 {case['plate_in']} 为准，订单已补结算", case_id),
    )
    conn.commit()
    return order_id


# ---------------------------------------------------------------- 人工放行

def create_manual_release(
    conn: sqlite3.Connection,
    *,
    staff: sqlite3.Row,
    reason_code: str,
    reason_detail: str,
    evidence_path: str,
    case_id: int | None = None,
    session_id: int | None = None,
    lane_id: int | None = None,
) -> int:
    _require_role(staff, "release:create")
    if reason_code not in RELEASE_REASONS:
        raise ServiceError("必须选择放行原因")
    if not reason_detail.strip():
        raise ServiceError("必须填写放行情况说明")
    ep = Path(evidence_path)
    if not ep.is_file():
        raise ServiceError("必须关联本地识别证据文件，且文件需存在")

    case = None
    if case_id is not None:
        case = query_one(conn, "SELECT * FROM exception_case WHERE id=?", (case_id,))
        if case is None:
            raise ServiceError("复核单不存在")
        session_id = case["session_id"]
        exit_ev = query_one(
            conn, "SELECT lane_id FROM lane_event WHERE id=?", (case["exit_event_id"],)
        )
        lane_id = exit_ev["lane_id"] if exit_ev else None

    if session_id is None:
        raise ServiceError("缺少停车会话，无法放行")
    session = query_one(conn, "SELECT * FROM parking_session WHERE id=?", (session_id,))
    if session is None:
        raise ServiceError("停车会话不存在")
    if lane_id is None:
        lane_id = session["exit_lane_id"] or session["entry_lane_id"]

    release_id = insert(
        conn, "manual_release",
        release_no=f"RF{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id, lane_id=lane_id,
        reason_code=reason_code, reason_detail=reason_detail.strip(),
        evidence_path=str(ep), staff_id=staff["id"],
        created_at=timeutil.now_iso(),
    )
    ts = timeutil.now_iso()
    if session["exit_time"] is None:
        conn.execute(
            "UPDATE parking_session SET status='released', exit_time=?, exit_lane_id=? WHERE id=?",
            (ts, lane_id, session_id),
        )
    else:
        conn.execute(
            "UPDATE parking_session SET status='released' WHERE id=?", (session_id,)
        )
    if case is not None:
        conn.execute(
            "UPDATE exception_case SET status='resolved', resolved_at=?, resolved_by=?, "
            "resolution=?, linked_release_id=? WHERE id=?",
            (ts, staff["id"], f"人工放行：{RELEASE_REASONS[reason_code]}", release_id, case["id"]),
        )
    conn.commit()
    return release_id


# ---------------------------------------------------------------- 退款

def refund_order(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    staff: sqlite3.Row,
    reason_code: str,
    reason_detail: str = "",
) -> tuple[int, bool]:
    """整单退款。同一订单仅允许一次；重复提交幂等返回既有退款单。"""
    _require_role(staff, "refund:create")
    if reason_code not in REFUND_REASONS:
        raise ServiceError("必须选择退款原因")
    order = query_one(conn, "SELECT * FROM payment_order WHERE id=?", (order_id,))
    if order is None:
        raise ServiceError("订单不存在")

    existing = query_one(conn, "SELECT * FROM refund_record WHERE order_id=?", (order_id,))
    if existing is not None:
        # 退款幂等：同一订单只保留一笔台账，重复提交直接返回
        return existing["id"], True

    if order["status"] != "paid":
        raise ServiceError(f"订单状态为 {order['status']}，仅已支付订单可退款")

    result = gateway.refund(f"ORDER-{order_id}", order["payable_cents"])
    ts = timeutil.now_iso()
    refund_id = insert(
        conn, "refund_record",
        refund_no=f"RFD{uuid.uuid4().hex[:10].upper()}",
        order_id=order_id, amount_cents=order["payable_cents"],
        reason_code=reason_code, reason_detail=reason_detail.strip(),
        staff_id=staff["id"],
        status="success" if result.success else "failed",
        gateway_refund_no=result.gateway_no,
        fail_reason=result.reason,
        created_at=ts,
    )
    if result.success:
        conn.execute(
            "UPDATE payment_order SET status='refunded' WHERE id=?", (order_id,)
        )
        conn.execute(
            "UPDATE exception_case SET linked_refund_id=? WHERE order_id=?",
            (refund_id, order_id),
        )
    conn.commit()
    return refund_id, False


# ---------------------------------------------------------------- 规则发布

def publish_rule_version(
    conn: sqlite3.Connection,
    *,
    lot_id: int,
    staff: sqlite3.Row,
    effective_from: str,
    free_minutes: int,
    hour_fee_cents: int,
    daily_cap_cents: int,
    note: str = "",
) -> int:
    _require_role(staff, "rule:publish")
    if free_minutes < 0 or hour_fee_cents < 0 or daily_cap_cents < 0:
        raise ServiceError("费率不能为负数")
    if daily_cap_cents < hour_fee_cents:
        raise ServiceError("单日封顶金额不应低于单小时费用")
    last = query_one(
        conn, "SELECT MAX(version) AS v FROM rule_version WHERE lot_id=?", (lot_id,)
    )
    version = (last["v"] or 0) + 1
    conn.execute(
        "UPDATE rule_version SET effective_to=? WHERE lot_id=? AND effective_to IS NULL",
        (effective_from, lot_id),
    )
    rid = insert(
        conn, "rule_version",
        lot_id=lot_id, version=version, effective_from=effective_from,
        effective_to=None, free_minutes=free_minutes,
        hour_fee_cents=hour_fee_cents, daily_cap_cents=daily_cap_cents,
        published_by=staff["login"], note=note,
    )
    conn.commit()
    return rid
