"""停车业务核心：消息接入、会话结算、异常复核、人工放行、退款、规则版本。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import billing
from .auth import Operator
from . import config
from .payment import InMemoryPaymentAdapter

# 异常短标签 -> (短标签显示, 完整原因)
ANOMALY_LABELS = {
    "PLATE_MISMATCH": ("牌异", "车牌识别不一致：出场车牌与入场登记不一致，需人工核对"),
    "DUP_EXIT": ("重出", "重复出场：该车辆已有完成的出场结算，不得重复扣费"),
    "PAY_FAILED": ("付败", "支付失败：无感扣费未成功或缺少有效支付方式"),
    "NO_PLATE": ("无牌", "无牌车：出场未识别到车牌，无法自动匹配入场记录"),
    "CROSS_DAY": ("跨日", "跨日停车：停车时段跨越自然日，需复核分段计费与封顶"),
    "COUPON_INVALID": ("券失", "优惠券失效：出示的优惠券不在有效期内，已按无券计费"),
}

# 人工放行原因（必须选择）
RELEASE_REASONS = {
    "PLATE_CONFIRMED": "人工核对车牌一致，系统识别偏差放行",
    "NO_PLATE_TICKET": "无牌车凭纸质临停凭证核对放行",
    "SYSTEM_FAULT": "车道设备故障，登记证据后放行",
    "EMERGENCY": "应急放行（消防/救护/公务）",
    "ALREADY_PAID": "已在其他渠道缴费，凭凭证放行",
    "DISPUTE": "计费争议，先行放行后复核",
}

OPEN_STATUSES = ("INSIDE", "MATCH_REVIEW", "REVIEW_HOLD")
SETTLED_STATUSES = ("PAID", "MANUAL_RELEASED", "CLOSED")


class ServiceError(Exception):
    """业务校验错误。"""


class NotFound(ServiceError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _seq(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}").fetchone()[0]


class ParkingService:
    def __init__(self, conn: sqlite3.Connection, adapter: InMemoryPaymentAdapter | None = None):
        self.conn = conn
        self.adapter = adapter or InMemoryPaymentAdapter()

    # ---------------------------------------------------------------- 规则
    def active_rule(self, lot_id: int, when: datetime) -> billing.Rule:
        row = self.conn.execute(
            "SELECT * FROM rule_versions WHERE lot_id = ? "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY version DESC LIMIT 1",
            (lot_id, when.isoformat(timespec="seconds"), when.isoformat(timespec="seconds")),
        ).fetchone()
        if row is None:
            raise ServiceError(f"停车场 {lot_id} 在 {when} 没有生效的计费规则版本")
        return billing.Rule(
            id=row["id"], version=row["version"], free_minutes=row["free_minutes"],
            unit_minutes=row["unit_minutes"], unit_price_fen=row["unit_price_fen"],
            daily_cap_fen=row["daily_cap_fen"],
        )

    def create_rule_version(self, operator: Operator, lot_id: int, *,
                            free_minutes: int, unit_minutes: int,
                            unit_price_fen: int, daily_cap_fen: int,
                            valid_from: str, note: str = "") -> int:
        operator.require("rule.create")
        dt = billing.parse_dt(valid_from)
        if unit_minutes <= 0 or unit_price_fen < 0 or free_minutes < 0 or daily_cap_fen < 0:
            raise ServiceError("规则参数不合法")
        version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM rule_versions WHERE lot_id = ?",
            (lot_id,),
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE rule_versions SET valid_to = ? WHERE lot_id = ? AND valid_to IS NULL",
            (valid_from, lot_id),
        )
        cur = self.conn.execute(
            "INSERT INTO rule_versions(lot_id, version, valid_from, valid_to, "
            "free_minutes, unit_minutes, unit_price_fen, daily_cap_fen, note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (lot_id, version, valid_from, None, free_minutes, unit_minutes,
             unit_price_fen, daily_cap_fen, note or f"第 {version} 版规则，{dt:%Y-%m-%d %H:%M} 生效"),
        )
        self.conn.commit()
        return cur.lastrowid

    # ------------------------------------------------------------ 消息接入
    def ingest_message(self, *, msg_uid: str, lane_code: str, device_time: str,
                       direction: str, raw_plate: str | None = None,
                       confidence: float = 0.0, vehicle_id: int | None = None,
                       coupon_code: str | None = None,
                       plate_image: str | None = None,
                       note: str = "") -> dict:
        """接入一条车道设备消息。返回处理结果；重复 uid 直接返回不重复处理。"""
        lane = self.conn.execute(
            "SELECT * FROM lanes WHERE code = ?", (lane_code,),
        ).fetchone()
        if lane is None:
            raise NotFound(f"车道 {lane_code} 不存在")
        billing.parse_dt(device_time)  # 校验格式

        dup = self.conn.execute(
            "SELECT msg_uid, processed FROM lane_messages WHERE msg_uid = ?",
            (msg_uid,),
        ).fetchone()
        if dup is not None:
            return {"duplicate": True, "msg_uid": msg_uid,
                    "first_uid": dup["msg_uid"], "direction": direction}

        cur = self.conn.execute(
            "INSERT INTO lane_messages(msg_uid, lane_id, device_time, direction, "
            "raw_plate, confidence, vehicle_id, plate_image, coupon_code, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (msg_uid, lane["id"], device_time, direction, raw_plate, confidence,
             vehicle_id, plate_image, coupon_code, note),
        )
        msg_id = cur.lastrowid
        if direction == "ENTRY":
            result = self._handle_entry(msg_id, lane, device_time, raw_plate,
                                        confidence, vehicle_id, plate_image)
        else:
            result = self._handle_exit(msg_id, lane, device_time, raw_plate,
                                       confidence, vehicle_id, coupon_code)
        self._bump_lane(lane, device_time, result["lane_event"], result["lane_status"])
        self.conn.commit()
        result["duplicate"] = False
        result["msg_uid"] = msg_uid
        return result

    def _bump_lane(self, lane, device_time: str, event: str, status: str) -> None:
        self.conn.execute(
            "UPDATE lanes SET device_time = ?, last_event = ?, status = ?, seq = seq + 1 "
            "WHERE id = ?",
            (device_time, event, status, lane["id"]),
        )

    def _resolve_vehicle(self, raw_plate, vehicle_id):
        if vehicle_id:
            return self.conn.execute(
                "SELECT * FROM vehicles WHERE id = ?", (vehicle_id,),
            ).fetchone()
        if raw_plate:
            return self.conn.execute(
                "SELECT * FROM vehicles WHERE plate = ? AND status = 'NORMAL'",
                (raw_plate,),
            ).fetchone()
        return None

    def _handle_entry(self, msg_id, lane, device_time, raw_plate,
                      confidence, vehicle_id, plate_image) -> dict:
        vehicle = self._resolve_vehicle(raw_plate, vehicle_id)
        # 低置信度不自动绑定（仍登记车牌，出场需复核）
        if vehicle is not None and confidence < 0.70 and raw_plate != vehicle["plate"]:
            vehicle = None
        sid = self.conn.execute(
            "INSERT INTO sessions(lot_id, plate, vehicle_id, entry_lane_id, entry_time, "
            "entry_msg_uid, status) VALUES (?,?,?,?,?,?, 'INSIDE')",
            (lane["lot_id"],
             vehicle["plate"] if vehicle else raw_plate,
             vehicle["id"] if vehicle else None,
             lane["id"], device_time,
             self.conn.execute("SELECT msg_uid FROM lane_messages WHERE id = ?",
                               (msg_id,)).fetchone()[0]),
        ).lastrowid
        self.conn.execute(
            "UPDATE lane_messages SET session_id = ?, vehicle_id = ?, processed = 1, "
            "plate_image_exit = NULL WHERE id = ?",
            (sid, vehicle["id"] if vehicle else None, msg_id),
        )
        label = f"入场 {vehicle['plate'] if vehicle else (raw_plate or '无牌车')}"
        return {"session_id": sid, "lane_event": label, "lane_status": "PASS"}

    def _open_case(self, code: str, detail: str, *, session_id=None,
                   order_id=None, msg_id=None) -> int:
        case_no = f"RC{self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM review_cases').fetchone()[0]:06d}"
        cur = self.conn.execute(
            "INSERT INTO review_cases(case_no, session_id, order_id, msg_id, code, detail, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (case_no, session_id, order_id, msg_id, code, detail, now_iso()),
        )
        if session_id:
            self.conn.execute(
                "UPDATE sessions SET anomaly_code = ?, anomaly_detail = ?, "
                "status = CASE WHEN status = 'INSIDE' THEN 'MATCH_REVIEW' ELSE status END "
                "WHERE id = ?",
                (code, detail, session_id),
            )
        return cur.lastrowid

    def _handle_exit(self, msg_id, lane, device_time, raw_plate,
                     confidence, vehicle_id, coupon_code) -> dict:
        self.conn.execute(
            "UPDATE lane_messages SET vehicle_id = ? WHERE id = ?",
            (vehicle_id, msg_id),
        )

        # 1) 可信车辆标识（月卡/凭证扫码）优先匹配在库会话
        session = None
        mismatch = False
        if vehicle_id:
            session = self.conn.execute(
                "SELECT * FROM sessions WHERE vehicle_id = ? AND exit_time IS NULL "
                "ORDER BY entry_time DESC LIMIT 1",
                (vehicle_id,),
            ).fetchone()
            if session is not None and raw_plate and session["plate"] != raw_plate:
                mismatch = True

        # 2) 按出场车牌精确匹配未结束会话
        if session is None and raw_plate:
            session = self.conn.execute(
                "SELECT * FROM sessions WHERE plate = ? AND exit_time IS NULL "
                "ORDER BY entry_time DESC LIMIT 1",
                (raw_plate,),
            ).fetchone()

        # 3) 匹配成功
        if session is not None:
            if mismatch:
                detail = (f"出场识别车牌「{raw_plate}」与入场登记「{session['plate']}」"
                          f"（车辆ID {vehicle_id}）不一致，置信度 {confidence:.2f}，道闸保持落杆")
                self._fill_exit(session["id"], lane["id"], device_time, msg_id)
                self.conn.execute(
                    "UPDATE sessions SET status = 'MATCH_REVIEW', anomaly_code = 'PLATE_MISMATCH', "
                    "anomaly_detail = ? WHERE id = ?",
                    (detail, session["id"]),
                )
                case_id = self._open_case("PLATE_MISMATCH", detail,
                                          session_id=session["id"], msg_id=msg_id)
                return {"session_id": session["id"], "case_id": case_id,
                        "lane_event": f"牌异拦截 {raw_plate}", "lane_status": "ALERT",
                        "anomaly": "PLATE_MISMATCH"}
            return self._settle(session, lane, device_time, msg_id, coupon_code, raw_plate)

        # 4) 无牌车：尝试关联同场唯一在库无牌会话，交人工凭证复核
        if not raw_plate:
            open_no_plate = self.conn.execute(
                "SELECT * FROM sessions WHERE lot_id = ? AND plate IS NULL "
                "AND exit_time IS NULL ORDER BY entry_time DESC LIMIT 1",
                (lane["lot_id"],),
            ).fetchone()
            if open_no_plate is not None:
                self._fill_exit(open_no_plate["id"], lane["id"], device_time, msg_id)
                detail = (f"{lane['name']} 于 {device_time} 出场车辆未识别到车牌，"
                          f"已关联无牌入场会话 {open_no_plate['id']}（{open_no_plate['entry_time']}），"
                          f"须凭纸质临停凭证人工核对")
                self.conn.execute(
                    "UPDATE sessions SET status = 'MATCH_REVIEW', anomaly_code = 'NO_PLATE', "
                    "anomaly_detail = ? WHERE id = ?",
                    (detail, open_no_plate["id"]),
                )
                case_id = self._open_case("NO_PLATE", detail,
                                          session_id=open_no_plate["id"], msg_id=msg_id)
                return {"session_id": open_no_plate["id"], "case_id": case_id,
                        "lane_event": "无牌车待凭证", "lane_status": "ALERT",
                        "anomaly": "NO_PLATE"}
            detail = f"{lane['name']} 于 {device_time} 出场车辆未识别到车牌，且无在库无牌入场记录"
            case_id = self._open_case("NO_PLATE", detail, msg_id=msg_id)
            return {"case_id": case_id, "lane_event": "无牌车拦截",
                    "lane_status": "ALERT", "anomaly": "NO_PLATE"}

        # 5) 重复出场：车牌对应最近会话已完成出场
        last_session = self.conn.execute(
            "SELECT * FROM sessions WHERE plate = ? AND exit_time IS NOT NULL "
            "ORDER BY exit_time DESC LIMIT 1",
            (raw_plate,),
        ).fetchone()
        if last_session is not None:
            detail = (f"车牌「{raw_plate}」重复出场消息：该车已于 {last_session['exit_time']} "
                      f"出场（会话 {last_session['id']}，状态 {last_session['status']}），"
                      f"本次不产生新订单、不重复扣费")
            case_id = self._open_case("DUP_EXIT", detail,
                                      session_id=last_session["id"], msg_id=msg_id)
            self.conn.execute(
                "UPDATE lane_messages SET session_id = ?, dup_of_msg_uid = ? WHERE id = ?",
                (last_session["id"], last_session["exit_msg_uid"], msg_id),
            )
            return {"session_id": last_session["id"], "case_id": case_id,
                    "lane_event": f"重复出场 {raw_plate}", "lane_status": "ALERT",
                    "anomaly": "DUP_EXIT"}

        # 6) 识别不一致且找不到任何记录
        detail = f"出场车牌「{raw_plate}」无对应入场记录（置信度 {confidence:.2f}），疑似识别错误"
        case_id = self._open_case("PLATE_MISMATCH", detail, msg_id=msg_id)
        return {"case_id": case_id, "lane_event": f"牌异拦截 {raw_plate}",
                "lane_status": "ALERT", "anomaly": "PLATE_MISMATCH"}

    def _fill_exit(self, session_id: int, lane_id: int, device_time: str, msg_id: int) -> None:
        msg_uid = self.conn.execute(
            "SELECT msg_uid, plate_image FROM lane_messages WHERE id = ?", (msg_id,),
        ).fetchone()
        self.conn.execute(
            "UPDATE sessions SET exit_lane_id = ?, exit_time = ?, exit_msg_uid = ? WHERE id = ?",
            (lane_id, device_time, msg_uid["msg_uid"], session_id),
        )
        self.conn.execute(
            "UPDATE lane_messages SET session_id = ?, processed = 1 WHERE id = ?",
            (session_id, msg_id),
        )

    def _load_coupon(self, coupon_code, vehicle_id, exit_dt):
        if not coupon_code:
            return None, None
        row = self.conn.execute(
            "SELECT * FROM coupons WHERE code = ?", (coupon_code,),
        ).fetchone()
        if row is None:
            return None, f"优惠券 {coupon_code} 不存在"
        if vehicle_id and row["vehicle_id"] != vehicle_id:
            return None, f"优惠券 {coupon_code} 不属于本车辆"
        coupon = billing.Coupon(
            id=row["id"], code=row["code"], coupon_type=row["coupon_type"],
            amount=row["amount"],
            valid_from=billing.parse_dt(row["valid_from"]),
            valid_to=billing.parse_dt(row["valid_to"]),
        )
        if not coupon.usable_at(exit_dt):
            return coupon, (f"优惠券 {coupon_code} 已失效：有效期 "
                            f"{row['valid_from']} 至 {row['valid_to']}，出场时间 {exit_dt:%Y-%m-%d %H:%M}")
        return coupon, None

    def _settle(self, session, lane, device_time, msg_id, coupon_code, raw_plate) -> dict:
        exit_dt = billing.parse_dt(device_time)
        entry_dt = billing.parse_dt(session["entry_time"])
        rule = self.active_rule(session["lot_id"], entry_dt)  # 按入场时间选版本

        coupon, coupon_problem = self._load_coupon(
            coupon_code, session["vehicle_id"], exit_dt,
        )
        if coupon is not None and coupon_problem:
            # 失效券仍记录进快照明细，但不抵扣
            fee = billing.compute_fee(entry_dt, exit_dt, rule,
                                      coupon=coupon, coupon_honored=False)
        else:
            fee = billing.compute_fee(entry_dt, exit_dt, rule, coupon=coupon)

        seq = _seq(self.conn, "orders")
        order_no = f"PO{exit_dt:%Y%m%d}{seq:05d}"
        snapshot = billing.snapshot_fee(fee, rule)
        cur = self.conn.execute(
            "INSERT INTO orders(order_no, session_id, lot_id, vehicle_id, plate, "
            "entry_time, exit_time, duration_min, rule_version_id, rule_snapshot, "
            "fee_detail, amount_fen, amount_before_coupon_fen, coupon_id, "
            "pay_status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'CREATED', ?)",
            (order_no, session["id"], session["lot_id"], session["vehicle_id"],
             session["plate"] or raw_plate, session["entry_time"], device_time,
             fee["duration_minutes"], rule.id, snapshot,
             json.dumps(fee, ensure_ascii=False, sort_keys=True),
             fee["amount_fen"], fee["amount_before_coupon_fen"],
             coupon.id if coupon else None, now_iso()),
        )
        order_id = cur.lastrowid
        self._fill_exit(session["id"], lane["id"], device_time, msg_id)
        self.conn.execute("UPDATE sessions SET rule_version_id = ? WHERE id = ?",
                          (rule.id, session["id"]))

        anomalies = []
        if fee["cross_day"]:
            days = "、".join(d["date"] for d in fee["day_segments"])
            cid = self._open_case(
                "CROSS_DAY",
                f"会话 {session['id']} 跨日停车（{days}），共 {fee['duration_minutes']} 分钟，"
                f"已按自然日分段计费并适用每日封顶，应付 {fee['amount_fen']} 分，请复核",
                session_id=session["id"], order_id=order_id, msg_id=msg_id,
            )
            anomalies.append(("CROSS_DAY", cid))
        if coupon_problem:
            cid = self._open_case(
                "COUPON_INVALID", coupon_problem + f"，订单 {order_no} 已按无券计费",
                session_id=session["id"], order_id=order_id, msg_id=msg_id,
            )
            anomalies.append(("COUPON_INVALID", cid))

        method = None
        if session["vehicle_id"]:
            method = self.conn.execute(
                "SELECT * FROM payment_methods WHERE vehicle_id = ? AND valid = 1 "
                "ORDER BY id DESC LIMIT 1",
                (session["vehicle_id"],),
            ).fetchone()

        if method is None:
            self.conn.execute(
                "UPDATE orders SET pay_message = ? WHERE id = ?",
                ("车辆未绑定有效支付方式，需人工处理或人工放行", order_id),
            )
            cid = self._open_case(
                "PAY_FAILED",
                f"订单 {order_no} 无法无感扣费：车辆未绑定有效支付方式，道闸保持落杆",
                session_id=session["id"], order_id=order_id, msg_id=msg_id,
            )
            anomalies.append(("PAY_FAILED", cid))
            self.conn.execute("UPDATE sessions SET status = 'REVIEW_HOLD' WHERE id = ?",
                              (session["id"],))
            self.conn.execute(
                "UPDATE review_cases SET session_id = ? WHERE id = ?",
                (session["id"], cid),
            )
            return {"session_id": session["id"], "order_id": order_id,
                    "order_no": order_no, "amount_fen": fee["amount_fen"],
                    "case_id": cid, "lane_event": "支付方式缺失",
                    "lane_status": "ALERT", "anomaly": "PAY_FAILED"}

        result = self.adapter.charge(order_no, method["token"], fee["amount_fen"])
        if result.success:
            pay_status = "SKIPPED_FREE" if fee["amount_fen"] == 0 else "SUCCESS"
            self.conn.execute(
                "UPDATE orders SET pay_status = ?, pay_method_id = ?, paid_at = ?, "
                "pay_message = ? WHERE id = ?",
                (pay_status, method["id"], now_iso(), result.message, order_id),
            )
            if coupon and not coupon_problem:
                self.conn.execute(
                    "UPDATE coupons SET status = 'USED', used_order_id = ? WHERE id = ?",
                    (order_id, coupon.id),
                )
            if anomalies:
                self.conn.execute("UPDATE sessions SET status = 'PAID' WHERE id = ?",
                                  (session["id"],))
            else:
                self.conn.execute(
                    "UPDATE sessions SET status = 'PAID', anomaly_code = NULL, "
                    "anomaly_detail = NULL WHERE id = ?",
                    (session["id"],),
                )
            return {"session_id": session["id"], "order_id": order_id,
                    "order_no": order_no, "amount_fen": fee["amount_fen"],
                    "lane_event": f"扣费 {fee['amount_fen']/100:.2f} 元放行",
                    "lane_status": "PASS",
                    "anomaly": anomalies[0][0] if anomalies else None}

        self.conn.execute(
            "UPDATE orders SET pay_status = 'FAILED', pay_method_id = ?, pay_message = ? "
            "WHERE id = ?",
            (method["id"], result.message, order_id),
        )
        cid = self._open_case(
            "PAY_FAILED",
            f"订单 {order_no} 扣款失败：{result.message}，道闸保持落杆，可在绑定有效方式后重试",
            session_id=session["id"], order_id=order_id, msg_id=msg_id,
        )
        anomalies.append(("PAY_FAILED", cid))
        self.conn.execute("UPDATE sessions SET status = 'REVIEW_HOLD' WHERE id = ?",
                          (session["id"],))
        return {"session_id": session["id"], "order_id": order_id,
                "order_no": order_no, "amount_fen": fee["amount_fen"],
                "case_id": cid, "lane_event": "支付失败",
                "lane_status": "ALERT", "anomaly": "PAY_FAILED"}

    # ----------------------------------------------------- 支付方式与重试
    def bind_payment_method(self, vehicle_id: int, method_type: str,
                            display_name: str, token: str) -> int:
        if self.conn.execute("SELECT 1 FROM vehicles WHERE id = ?",
                             (vehicle_id,)).fetchone() is None:
            raise NotFound("车辆不存在")
        cur = self.conn.execute(
            "INSERT INTO payment_methods(vehicle_id, method_type, display_name, token, "
            "valid, bound_since) VALUES (?,?,?,?,1,?)",
            (vehicle_id, method_type, display_name, token, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def retry_charge(self, operator: Operator, order_id: int) -> dict:
        operator.require("review.resolve")
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise NotFound("订单不存在")
        if order["pay_status"] not in ("FAILED", "CREATED"):
            raise ServiceError(f"订单状态 {order['pay_status']} 不允许重试扣款")
        method = None
        if order["vehicle_id"]:
            method = self.conn.execute(
                "SELECT * FROM payment_methods WHERE vehicle_id = ? AND valid = 1 "
                "ORDER BY id DESC LIMIT 1",
                (order["vehicle_id"],),
            ).fetchone()
        if method is None:
            raise ServiceError("车辆仍未绑定有效支付方式")
        result = self.adapter.charge(order["order_no"], method["token"], order["amount_fen"])
        if not result.success:
            self.conn.execute(
                "UPDATE orders SET pay_message = ? WHERE id = ?",
                (result.message, order_id),
            )
            self.conn.commit()
            raise ServiceError(f"重试扣款仍失败：{result.message}")
        pay_status = "SKIPPED_FREE" if order["amount_fen"] == 0 else "SUCCESS"
        self.conn.execute(
            "UPDATE orders SET pay_status = ?, pay_method_id = ?, paid_at = ?, "
            "pay_message = ? WHERE id = ?",
            (pay_status, method["id"], now_iso(), result.message, order_id),
        )
        self.conn.execute(
            "UPDATE sessions SET status = 'PAID' WHERE id = ?", (order["session_id"],),
        )
        self.conn.commit()
        return {"success": True, "order_no": order["order_no"], "message": result.message}

    # ------------------------------------------------------------ 复核案件
    def resolve_case(self, operator: Operator, case_id: int, resolution: str,
                     verdict: str = "RESOLVED") -> None:
        operator.require("review.resolve")
        if verdict not in ("RESOLVED", "REJECTED"):
            raise ServiceError("verdict 必须是 RESOLVED 或 REJECTED")
        if not resolution.strip():
            raise ServiceError("复核处理必须填写处理说明")
        case = self.conn.execute("SELECT * FROM review_cases WHERE id = ?",
                                 (case_id,)).fetchone()
        if case is None:
            raise NotFound("复核案件不存在")
        if case["status"] not in ("OPEN", "CLAIMED"):
            raise ServiceError("案件已处理")
        self.conn.execute(
            "UPDATE review_cases SET status = ?, resolution = ?, resolver_id = ?, "
            "resolved_at = ? WHERE id = ?",
            (verdict, resolution, operator.id, now_iso(), case_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------ 人工放行
    def create_release(self, operator: Operator, session_id: int,
                       reason_code: str, reason_detail: str,
                       evidence_file: str) -> int:
        operator.require("release.create")
        if reason_code not in RELEASE_REASONS:
            raise ServiceError("必须选择有效的放行原因")
        if not reason_detail.strip():
            raise ServiceError("放行必须填写完整说明")
        session = self.conn.execute("SELECT * FROM sessions WHERE id = ?",
                                    (session_id,)).fetchone()
        if session is None:
            raise NotFound("会话不存在")
        # 证据必须是本地证据目录内真实存在的文件（禁止路径穿越）
        from pathlib import Path
        ev_path = Path(config.EVIDENCE_DIR) / Path(evidence_file).name
        if Path(evidence_file).name != evidence_file or not ev_path.is_file():
            raise ServiceError("必须关联已上传的本地识别证据文件")
        lane_id = session["exit_lane_id"] or session["entry_lane_id"]
        seq = _seq(self.conn, "manual_releases")
        release_no = f"MF{seq:06d}"
        cur = self.conn.execute(
            "INSERT INTO manual_releases(release_no, session_id, lane_id, reason_code, "
            "reason_detail, evidence_file, operator_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (release_no, session_id, lane_id, reason_code, reason_detail,
             evidence_file, operator.id, now_iso()),
        )
        self.conn.execute(
            "UPDATE sessions SET status = 'MANUAL_RELEASED', closed_at = ? WHERE id = ?",
            (now_iso(), session_id),
        )
        lane = self.conn.execute("SELECT * FROM lanes WHERE id = ?", (lane_id,)).fetchone()
        self._bump_lane(lane, now_iso(),
                        f"人工放行 {release_no}（{RELEASE_REASONS[reason_code]}）", "PASS")
        self.conn.commit()
        return cur.lastrowid

    # ---------------------------------------------------------------- 退款
    def refund_order(self, operator: Operator, order_id: int, reason: str) -> tuple[dict, bool]:
        """对订单执行退款。一单最多一次：重复请求直接返回既有记录（幂等）。

        返回 (退款记录, 是否为幂等命中)。
        """
        operator.require("refund.create")
        if not reason.strip():
            raise ServiceError("退款必须填写原因")
        order = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise NotFound("订单不存在")

        existing = self.conn.execute(
            "SELECT * FROM refunds WHERE order_id = ?", (order_id,),
        ).fetchone()
        if existing is not None:
            return dict(existing), True

        if order["pay_status"] != "SUCCESS":
            raise ServiceError(f"订单支付状态为 {order['pay_status']}，不可退款")
        if order["amount_fen"] <= 0:
            raise ServiceError("0 元订单无需退款")

        token = ""
        if order["pay_method_id"]:
            token = self.conn.execute(
                "SELECT token FROM payment_methods WHERE id = ?",
                (order["pay_method_id"],),
            ).fetchone()["token"]
        seq = _seq(self.conn, "refunds")
        refund_no = f"RF{seq:06d}"
        result = self.adapter.refund(refund_no, order["order_no"], token, order["amount_fen"])
        if not result.success:
            raise ServiceError(f"退款通道失败：{result.message}")
        cur = self.conn.execute(
            "INSERT INTO refunds(refund_no, order_id, amount_fen, reason, operator_id, "
            "status, pay_message, created_at) VALUES (?,?,?,?,?,'SUCCESS',?,?)",
            (refund_no, order_id, order["amount_fen"], reason, operator.id,
             result.message, now_iso()),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM refunds WHERE id = ?",
                                (cur.lastrowid,)).fetchone()
        return dict(row), False
