"""Web 界面与操作端点（仅标准库）。

页面：
  /            车道监控（横向车道带）
  /records     停车记录（入场 → 结算时间线）
  /cases       异常队列（短标签 + 完整原因）
  /rules       规则版本
  /releases    人工放行台账
  /refunds     退款台账
  /orders/<id> 订单与计费快照
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import sqlite3
import urllib.parse
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path

from . import billing, config, timeutil
from .db import connect, loads, query_all, query_one
from .services import (
    EXCEPTION_META,
    REFUND_REASONS,
    RELEASE_REASONS,
    PermissionDenied,
    ServiceError,
    create_manual_release,
    publish_rule_version,
    refund_order,
    retry_payment,
    review_case,
    settle_mismatch_as_entry_plate,
)
from .timeutil import FMT

# ---------------------------------------------------------------- 配色

C_BG = "#d9d9d7"       # 浅混凝土灰
C_PANEL = "#efefed"
C_INK = "#1a1a1a"      # 黑
C_TEAL = "#14b8a6"     # 青绿
C_TEAL_D = "#0d9488"
C_WARN = "#c8502f"
C_MUTED = "#6f6f6c"
C_LINE = "#c4c4c1"

STATUS_LABEL = {
    "in_park": "在场",
    "settled": "已结算",
    "released": "人工放行",
    "flagged": "复核中",
    "created": "待扣费",
    "paid": "已支付",
    "failed": "扣费失败",
    "refunded": "已退款",
    "partially_refunded": "部分退款",
    "closed": "已关闭",
    "open": "待复核",
    "in_review": "复核中",
    "resolved": "已处置",
    "dismissed": "已驳回",
}

PROCESSED_LABEL = {
    "received": "已接收",
    "entry_opened": "入场开单",
    "exit_settled": "出场结算",
    "duplicate": "重复拦截",
    "ignored": "已忽略",
    "exception": "挂起复核",
}

CASE_TYPE_COLOR = {
    "plate_mismatch": "#b45309",
    "duplicate_exit": "#7c3aed",
    "payment_failed": C_WARN,
    "no_plate": "#475569",
    "cross_day": "#0369a1",
    "coupon_invalid": "#be185d",
}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def yuan(cents) -> str:
    return billing.yuan(int(cents or 0))


def fmt_dt(s: str | None) -> str:
    if not s:
        return "—"
    return s.replace("T", " ").replace("-", "/")[2:]


# ---------------------------------------------------------------- 签名 cookie

def _secret() -> bytes:
    path = config.DATA_DIR / ".session_secret"
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    path.write_bytes(secret)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def _sign(staff_id: int) -> str:
    msg = str(staff_id).encode()
    return f"{staff_id}.{hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:16]}"


def _unsign(token: str) -> int | None:
    try:
        sid, sig = token.split(".", 1)
        expect = hmac.new(_secret(), sid.encode(), hashlib.sha256).hexdigest()[:16]
        if hmac.compare_digest(sig, expect):
            return int(sid)
    except (ValueError, AttributeError):
        return None
    return None


# ---------------------------------------------------------------- 页面框架

CSS = f"""
:root {{
  --bg:{C_BG}; --panel:{C_PANEL}; --ink:{C_INK}; --teal:{C_TEAL};
  --teal-d:{C_TEAL_D}; --warn:{C_WARN}; --muted:{C_MUTED}; --line:{C_LINE};
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.55 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
}}
header.top {{
  background:var(--ink); color:#f2f2f0; padding:0 24px;
  display:flex; align-items:stretch; gap:4px; height:52px;
}}
header.top .brand {{
  font-weight:800; letter-spacing:.08em; color:var(--teal);
  display:flex; align-items:center; margin-right:20px; white-space:nowrap;
}}
header.top nav {{ display:flex; gap:2px; }}
header.top nav a {{
  color:#cfcfcc; text-decoration:none; padding:0 16px;
  display:flex; align-items:center; border-bottom:3px solid transparent;
}}
header.top nav a.active {{ color:#fff; border-bottom-color:var(--teal); background:#262626; }}
header.top .who {{ margin-left:auto; display:flex; align-items:center; gap:10px; font-size:12.5px; }}
header.top .who b {{ color:var(--teal); }}
.wrap {{ max-width:1280px; margin:22px auto; padding:0 22px; }}
h1 {{ font-size:19px; margin:0 0 4px; }}
.sub {{ color:var(--muted); font-size:12.5px; margin-bottom:18px; }}
.panel {{
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:16px 18px; margin-bottom:18px;
}}
.panel h2 {{ font-size:15px; margin:0 0 12px; display:flex; align-items:center; gap:8px; }}
.panel h2::before {{ content:""; width:8px; height:16px; background:var(--teal); border-radius:2px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th {{
  text-align:left; font-weight:700; color:#3c3c3a; background:#e2e2df;
  padding:7px 10px; border-bottom:2px solid var(--ink); white-space:nowrap;
}}
td {{ padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
tr:hover td {{ background:#e8e8e5; }}
.amt {{
  width:7.25rem; min-width:7.25rem; text-align:right; white-space:nowrap;
  font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  font-variant-numeric:tabular-nums; letter-spacing:-.01em;
}}
.amt.zero {{ color:var(--muted); }}
.amt.bad {{ color:var(--warn); font-weight:700; }}
.amt.ok {{ color:var(--teal-d); font-weight:700; }}
a {{ color:var(--teal-d); }}
.btn {{
  display:inline-block; border:1px solid var(--ink); background:var(--ink);
  color:#fff; padding:4px 12px; border-radius:6px; font-size:12.5px;
  cursor:pointer; text-decoration:none;
}}
.btn.teal {{ background:var(--teal-d); border-color:var(--teal-d); }}
.btn.ghost {{ background:transparent; color:var(--ink); }}
.btn.danger {{ background:var(--warn); border-color:var(--warn); }}
.btn:disabled,.btn.disabled {{ opacity:.4; cursor:not-allowed; }}
.tag {{
  display:inline-block; padding:1px 8px; border-radius:999px; font-size:11.5px;
  font-weight:700; color:#fff; white-space:nowrap;
}}
.pill {{
  display:inline-block; padding:1px 8px; border-radius:4px; font-size:11.5px;
  border:1px solid var(--line); background:#fff; white-space:nowrap;
}}
.pill.bad {{ color:var(--warn); border-color:var(--warn); }}
.pill.ok {{ color:var(--teal-d); border-color:var(--teal-d); }}
.pill.ink {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
.fulltext {{ color:#333331; }}
.kpi {{ display:flex; gap:14px; margin-bottom:18px; flex-wrap:wrap; }}
.kpi .card {{
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:12px 18px; min-width:130px;
}}
.kpi .card .n {{ font-size:24px; font-weight:800; font-family:ui-monospace,monospace; }}
.kpi .card .l {{ font-size:12px; color:var(--muted); }}
/* 横向车道带 */
.lanebands {{ display:flex; flex-direction:column; gap:10px; }}
.laneband {{
  display:flex; align-items:stretch; background:var(--panel);
  border:1px solid var(--line); border-radius:10px; overflow:hidden;
}}
.laneband .side {{ width:8px; }}
.laneband .side.online {{ background:var(--teal); }}
.laneband .side.offline {{ background:var(--warn); }}
.laneband .side.maintenance {{ background:#a8a8a4; }}
.laneband .body {{ flex:1; display:flex; align-items:center; gap:18px; padding:12px 16px; }}
.laneband .code {{
  font-family:ui-monospace,monospace; font-weight:800; font-size:16px;
  background:var(--ink); color:var(--teal); border-radius:6px; padding:4px 10px;
}}
.laneband .meta {{ min-width:150px; }}
.laneband .meta .nm {{ font-weight:700; }}
.laneband .meta .dir {{ font-size:12px; color:var(--muted); }}
.laneband .stream {{ flex:1; display:flex; gap:8px; align-items:center; overflow-x:auto; }}
.evtchip {{
  border:1px dashed var(--line); border-radius:6px; padding:3px 8px;
  font-size:12px; white-space:nowrap; background:#fff;
}}
.evtchip b {{ font-family:ui-monospace,monospace; }}
.dot {{ display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; }}
.dot.online {{ background:var(--teal); }}
.dot.offline {{ background:var(--warn); }}
.dot.maintenance {{ background:#a8a8a4; }}
/* 时间线 */
.tl {{ display:flex; align-items:center; gap:8px; white-space:nowrap; font-size:12.5px; }}
.tl .stop {{ font-family:ui-monospace,monospace; }}
.tl .arrow {{ color:var(--teal-d); font-weight:800; }}
.flow {{ font-family:ui-monospace,monospace; color:var(--muted); }}
/* 表单 */
form.inline {{ display:inline; }}
label.f {{ display:block; font-size:12.5px; margin:8px 0; }}
label.f span {{ display:block; color:var(--muted); margin-bottom:3px; }}
input[type=text],input[type=password],input[type=datetime-local],select,textarea {{
  width:100%; padding:6px 9px; border:1px solid #a8a8a4; border-radius:6px;
  background:#fff; font:inherit; color:var(--ink);
}}
textarea {{ min-height:64px; resize:vertical; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:0 18px; }}
.flash {{ padding:9px 14px; border-radius:8px; margin-bottom:14px; font-size:13px; }}
.flash.err {{ background:#f7e3dc; border:1px solid var(--warn); color:#7a2b14; }}
.flash.ok {{ background:#dff3ef; border:1px solid var(--teal); color:#0b5c53; }}
.evthumb {{ width:300px; border:2px solid var(--ink); border-radius:6px; }}
.snap {{
  background:#fff; border:1px solid var(--line); border-radius:8px;
  padding:12px 14px; font-family:ui-monospace,monospace; font-size:12.5px;
  white-space:pre-wrap;
}}
.muted {{ color:var(--muted); }}
.right {{ text-align:right; }}
.nowrap {{ white-space:nowrap; }}
.loginbox {{ max-width:380px; margin:8vh auto; }}
.evradio {{ display:flex; align-items:center; gap:8px; margin:6px 0; font-size:12.5px; }}
.evradio img {{ width:150px; border:1px solid var(--ink); border-radius:4px; }}
"""


def shell(title: str, body: str, staff, active: str = "", flash: tuple[str, str] | None = None) -> bytes:
    nav_items = [
        ("/", "车道监控"),
        ("/records", "停车记录"),
        ("/cases", "异常队列"),
        ("/rules", "规则版本"),
        ("/releases", "人工放行"),
        ("/refunds", "退款台账"),
    ]
    nav_html = "".join(
        f'<a href="{u}"{" class=active" if u == active else ""}>{n}</a>'
        for u, n in nav_items
    )
    if staff:
        who = (
            f'<span>{config.ROLE_NAMES[staff["role"]]} <b>{esc(staff["name"])}</b>'
            f'（{esc(staff["login"])}）</span>'
            f'<a class="btn ghost" style="color:#eee;border-color:#555" href="/logout">切换岗位</a>'
        )
    else:
        who = '<a class="btn teal" href="/login">岗位登录</a>'
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{esc(msg)}</div>'
    doc = f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 停车无感扣费复核平台</title>
<style>{CSS}</style></head>
<body>
<header class="top">
  <div class="brand">PARK·REVIEW 停车复核台</div>
  <nav>{nav_html}</nav>
  <div class="who">{who}</div>
</header>
<main class="wrap">{flash_html}{body}</main>
</body></html>"""
    return doc.encode("utf-8")


# ---------------------------------------------------------------- 查询辅助

def _staff_from_cookie(handler) -> object:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get("staff")
    if morsel is None:
        return None
    sid = _unsign(morsel.value)
    if sid is None:
        return None
    conn = connect(config.DB_PATH)
    try:
        row = query_one(conn, "SELECT * FROM staff WHERE id=? AND active=1", (sid,))
        return row
    finally:
        conn.close()


def _order_badge(status: str) -> str:
    cls = "ok" if status == "paid" else "bad" if status == "failed" else ""
    return f'<span class="pill {cls}">{STATUS_LABEL.get(status, status)}</span>'


def _case_tag(t: str) -> str:
    return (
        f'<span class="tag" style="background:{CASE_TYPE_COLOR[t]}">'
        f'{EXCEPTION_META[t]["tag"]}</span>'
    )


# ---------------------------------------------------------------- 页面：车道监控

def page_lanes(conn, staff) -> str:
    lot = query_one(conn, "SELECT * FROM parking_lot ORDER BY id LIMIT 1")
    lanes = query_all(conn, "SELECT * FROM lane ORDER BY direction DESC, code")
    open_cases = query_one(conn, "SELECT COUNT(*) n FROM exception_case WHERE status!='resolved'")["n"]
    in_park = query_one(conn, "SELECT COUNT(*) n FROM parking_session WHERE status IN ('in_park','flagged')")["n"]
    today_paid = query_one(
        conn,
        "SELECT COUNT(*) n, COALESCE(SUM(payable_cents),0) s FROM payment_order "
        "WHERE status='paid' AND substr(created_at,1,10)='2026-09-25'"
    )
    bands = []
    for lane in lanes:
        chips = query_all(
            conn,
            "SELECT plate, direction, device_time, processed_as FROM lane_event "
            "WHERE lane_id=? ORDER BY device_time DESC LIMIT 6",
            (lane["id"],),
        )
        chip_html = "".join(
            f'<span class="evtchip">{fmt_dt(c["device_time"])[6:]} '
            f'<b>{esc(c["plate"] or "无牌")}</b> '
            f'{PROCESSED_LABEL.get(c["processed_as"], c["processed_as"])}</span>'
            for c in chips
        ) or '<span class="muted">暂无识别消息</span>'
        arrow = "入场 ▸" if lane["direction"] == "entry" else "◂ 出场"
        off = lane["device_clock_offset_s"]
        off_txt = f"设备时钟偏移 {off:+d}s" if off else "设备时钟同步"
        lane_status_txt = {"online": "在线", "offline": "离线", "maintenance": "维护中"}[lane["status"]]
        bands.append(f"""
        <div class="laneband">
          <div class="side {lane['status']}"></div>
          <div class="body">
            <div class="code">{esc(lane['code'])}</div>
            <div class="meta">
              <div class="nm">{esc(lane['name'])}</div>
              <div class="dir">{arrow} · <span class="dot {lane['status']}"></span>
                {lane_status_txt} · {off_txt}</div>
            </div>
            <div class="stream">{chip_html}</div>
            <div class="nowrap muted" style="font-family:ui-monospace,monospace;font-size:12px">
              最新设备时间<br><b style="color:var(--ink)">{fmt_dt(lane['last_device_time'])}</b>
            </div>
          </div>
        </div>""")
    return f"""
    <h1>车道监控 · {esc(lot['name'])}</h1>
    <div class="sub">车道状态以横向车道带展示，消息按车道设备时间排序（设备时间存在独立时钟偏移）。</div>
    <div class="kpi">
      <div class="card"><div class="n">{len(lanes)}</div><div class="l">车道总数</div></div>
      <div class="card"><div class="n">{in_park}</div><div class="l">在场/待核车辆</div></div>
      <div class="card"><div class="n" style="color:var(--warn)">{open_cases}</div><div class="l">未结复核单</div></div>
      <div class="card"><div class="n">{today_paid['n']}</div><div class="l">当日已支付订单</div></div>
      <div class="card"><div class="n" style="color:var(--teal-d)">¥{yuan(today_paid['s'])}</div><div class="l">当日无感扣费金额（虚构）</div></div>
    </div>
    <div class="panel"><h2>车道设备状态带</h2><div class="lanebands">{''.join(bands)}</div></div>
    """


# ---------------------------------------------------------------- 页面：停车记录

def page_records(conn, staff) -> str:
    rows = query_all(
        conn,
        """
        SELECT ps.*, le.code AS entry_code, lx.code AS exit_code,
               po.order_no, po.payable_cents, po.status AS order_status, po.amount_cents,
               po.discount_cents, po.id AS order_id
        FROM parking_session ps
        JOIN lane le ON le.id = ps.entry_lane_id
        LEFT JOIN lane lx ON lx.id = ps.exit_lane_id
        LEFT JOIN payment_order po ON po.id = ps.order_id
        ORDER BY ps.entry_time
        """,
    )
    trs = []
    for r in rows:
        plate = esc(r["plate"] or "无牌车")
        if r["no_plate_token"] and not r["plate"]:
            plate += f' <span class="pill">{esc(r["no_plate_token"])}</span>'
        dur = "—"
        if r["exit_time"]:
            m = int((timeutil.parse(r["exit_time"]) - timeutil.parse(r["entry_time"])).total_seconds() // 60)
            dur = f"{m // 60} 小时 {m % 60} 分" if m >= 60 else f"{m} 分"
        cases = query_all(
            conn,
            "SELECT type, status FROM exception_case WHERE session_id=? ORDER BY id",
            (r["id"],),
        )
        case_html = " ".join(_case_tag(c["type"]) for c in cases) or '<span class="muted">—</span>'
        order_link = (
            f'<a href="/orders/{r["order_id"]}">{esc(r["order_no"])}</a>'
            if r["order_id"] else '<span class="muted">未生成订单</span>'
        )
        amt_cls = "zero" if (r["payable_cents"] or 0) == 0 else ("bad" if r["order_status"] == "failed" else "ok" if r["order_status"] == "paid" else "")
        amt = (
            f'<span class="amt {amt_cls}">¥{yuan(r["payable_cents"])}</span>'
            if r["payable_cents"] is not None else '<span class="amt muted">—</span>'
        )
        trs.append(f"""
        <tr>
          <td class="nowrap">{plate}</td>
          <td><div class="tl">
            <span class="stop">{esc(r['entry_code'])} {fmt_dt(r['entry_time'])}</span>
            <span class="arrow">━━▶</span>
            <span class="stop">{esc(r['exit_code'] or '—')} {fmt_dt(r['exit_time'])}</span>
          </div></td>
          <td class="nowrap">{dur}</td>
          <td class="nowrap">{order_link}<br>{_order_badge(r['order_status']) if r['order_status'] else ''}</td>
          <td class="right">{amt}</td>
          <td>{case_html}</td>
          <td class="nowrap"><a href="/cases?session={r['id']}">会话复核</a></td>
        </tr>""")
    return f"""
    <h1>停车记录</h1>
    <div class="sub">车辆记录严格按入场时间排列；每行呈现「入场 → 出场 → 结算」全过程，金额列固定宽度。</div>
    <div class="panel"><h2>停车会话（{len(rows)} 条）</h2>
    <table><thead><tr>
      <th>车辆</th><th>入场 → 出场（车道/设备时间）</th><th>停车时长</th>
      <th>订单</th><th class="right">实付金额</th><th>关联异常</th><th></th>
    </tr></thead><tbody>{''.join(trs)}</tbody></table></div>
    """


# ---------------------------------------------------------------- 页面：异常队列

def page_cases(conn, staff, session_filter: str | None = None) -> str:
    where, params = "", []
    if session_filter:
        where, params = "WHERE c.session_id=?", [session_filter]
    cases = query_all(
        conn,
        f"""
        SELECT c.*, s.plate AS s_plate, o.order_no
        FROM exception_case c
        LEFT JOIN parking_session s ON s.id=c.session_id
        LEFT JOIN payment_order o ON o.id=c.order_id
        {where}
        ORDER BY CASE c.status WHEN 'open' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END,
                 c.opened_at
        """,
        params,
    )
    groups: dict[str, list[sqlite3.Row]] = {"open": [], "in_review": [], "resolved": []}
    for c in cases:
        groups[c["status"] if c["status"] in groups else "resolved"].append(c)

    def case_row(c: sqlite3.Row) -> str:
        acts = []
        plate = esc(c["plate_in"] or c["s_plate"] or "—")
        if c["plate_out"] and c["plate_out"] != c["plate_in"]:
            plate += f' <span class="flow">→</span> {esc(c["plate_out"])}'
        if staff and c["status"] != "resolved":
            if config.can(staff["role"], "case:review"):
                if c["status"] == "open":
                    acts.append(
                        f'<form class="inline" method="post" action="/cases/{c["id"]}/claim">'
                        f'<button class="btn ghost">认领</button></form>'
                    )
                if c["type"] == "plate_mismatch":
                    acts.append(
                        f'<form class="inline" method="post" action="/cases/{c["id"]}/settle-mismatch">'
                        f'<button class="btn teal">按入场车牌补结算</button></form>'
                    )
                if c["type"] == "payment_failed" and c["order_id"]:
                    acts.append(
                        f'<form class="inline" method="post" action="/orders/{c["order_id"]}/retry">'
                        f'<button class="btn teal">重试扣费</button></form>'
                    )
                acts.append(
                    f'<form class="inline" method="post" action="/cases/{c["id"]}/dismiss">'
                    f'<button class="btn ghost">核实归档</button></form>'
                )
            if config.can(staff["role"], "release:create"):
                acts.append(f'<a class="btn" href="/releases?case_id={c["id"]}#create">人工放行</a>')
            if not acts:
                acts.append('<span class="muted">当前岗位仅可查看</span>')
        elif not staff:
            acts.append('<a class="btn ghost" href="/login">登录处理</a>')
        order_html = (
            f'<a href="/orders/{c["order_id"]}">{esc(c["order_no"])}</a>'
            if c["order_id"] else '<span class="muted">未扣费</span>'
        )
        owner = ""
        if c["resolved_by"]:
            who = query_one(conn, "SELECT name,role FROM staff WHERE id=?", (c["resolved_by"],))
            owner = f'<div class="muted" style="font-size:12px">处置人：{esc(who["name"])}（{config.ROLE_NAMES[who["role"]]}） · {esc(c["resolution"] or "")}</div>'
        return f"""
        <tr>
          <td class="nowrap">{_case_tag(c['type'])}
            <div class="fulltext" style="margin-top:4px;max-width:340px">{esc(EXCEPTION_META[c['type']]['title'])}</div></td>
          <td class="nowrap">{plate}<div class="muted" style="font-size:12px">{esc(c['code'])}</div></td>
          <td style="max-width:420px"><div class="fulltext">{esc(c['summary'])}</div>{owner}</td>
          <td class="nowrap">{fmt_dt(c['opened_at'])}<br>{order_html}</td>
          <td class="nowrap"><span class="pill {'ink' if c['status']=='in_review' else 'ok' if c['status']=='resolved' else 'bad'}">{STATUS_LABEL[c['status']]}</span></td>
          <td class="nowrap">{" ".join(acts)}</td>
        </tr>"""

    def table(rows_: list) -> str:
        if not rows_:
            return '<p class="muted">无</p>'
        return (
            '<table><thead><tr><th>异常（短标签 / 全称）</th><th>车辆</th>'
            '<th>完整原因与处置</th><th>发生时间 / 订单</th><th>状态</th><th>操作</th>'
            f'</tr></thead><tbody>{"".join(case_row(c) for c in rows_)}</tbody></table>'
        )

    counts = {k: len(v) for k, v in groups.items()}
    return f"""
    <h1>异常复核队列</h1>
    <div class="sub">车牌不一致、重复出场、支付失败、无牌车、跨日停车与优惠券失效分别入队；短标签用于快速分拣，悬读列为完整文字。</div>
    <div class="kpi">
      <div class="card"><div class="n" style="color:var(--warn)">{counts['open']}</div><div class="l">待复核</div></div>
      <div class="card"><div class="n">{counts['in_review']}</div><div class="l">复核中</div></div>
      <div class="card"><div class="n" style="color:var(--teal-d)">{counts['resolved']}</div><div class="l">已处置</div></div>
    </div>
    <div class="panel"><h2>待复核（{counts['open']}）</h2>{table(groups['open'])}</div>
    <div class="panel"><h2>复核中（{counts['in_review']}）</h2>{table(groups['in_review'])}</div>
    <div class="panel"><h2>已处置归档（{counts['resolved']}）</h2>{table(groups['resolved'])}</div>
    """


# ---------------------------------------------------------------- 页面：规则版本

def page_rules(conn, staff) -> str:
    lot = query_one(conn, "SELECT * FROM parking_lot ORDER BY id LIMIT 1")
    rules = query_all(
        conn, "SELECT * FROM rule_version WHERE lot_id=? ORDER BY version DESC", (lot["id"],)
    )
    trs = []
    for r in rules:
        current = r["effective_to"] is None
        trs.append(f"""
        <tr>
          <td class="nowrap"><b>v{r['version']}</b> {'' if not current else '<span class="pill ok">现行</span>'}</td>
          <td class="nowrap">{fmt_dt(r['effective_from'])}<br><span class="muted">至 {fmt_dt(r['effective_to'])}</span></td>
          <td class="right"><span class="amt">{r['free_minutes']} 分钟</span></td>
          <td class="right"><span class="amt">¥{yuan(r['hour_fee_cents'])}/时</span></td>
          <td class="right"><span class="amt">¥{yuan(r['daily_cap_cents'])}/日</span></td>
          <td>{esc(r['note'] or '')}<div class="muted" style="font-size:12px">发布：{esc(r['published_by'] or '—')}</div></td>
        </tr>""")
    can_pub = staff and config.can(staff["role"], "rule:publish")
    form = ""
    if can_pub:
        form = """
        <div class="panel" id="publish"><h2>发布新版本（管理员）</h2>
        <form method="post" action="/rules/publish">
          <div class="grid2">
            <label class="f"><span>生效时间</span><input type="datetime-local" name="effective_from" step="1" required></label>
            <label class="f"><span>免费时长（分钟）</span><input type="text" name="free_minutes" value="30" required></label>
            <label class="f"><span>每小时费用（元）</span><input type="text" name="hour_fee_yuan" value="8.00" required></label>
            <label class="f"><span>单日封顶（元）</span><input type="text" name="daily_cap_yuan" value="80.00" required></label>
          </div>
          <label class="f"><span>版本说明</span><input type="text" name="note" placeholder="例：节假日临时费率"></label>
          <button class="btn teal">发布新版本</button>
          <span class="muted" style="margin-left:10px">新版本仅影响生效后入场的车辆；历史订单保留计费快照，不重算。</span>
        </form></div>"""
    elif staff:
        form = '<div class="panel"><h2>发布新版本</h2><p class="muted">仅管理员可发布规则版本。</p></div>'
    return f"""
    <h1>计费规则版本</h1>
    <div class="sub">按车辆入场时间选择适用版本；已结算订单保存完整计费快照（含逐日切片），规则修改不触发重算。</div>
    <div class="panel"><h2>{esc(lot['name'])} · 版本时间线（{len(rules)} 个版本）</h2>
    <table><thead><tr><th>版本</th><th>生效区间</th><th class="right">免费时长</th>
      <th class="right">小时费率</th><th class="right">单日封顶</th><th>说明</th></tr></thead>
    <tbody>{''.join(trs)}</tbody></table></div>
    {form}
    """


# ---------------------------------------------------------------- 页面：人工放行

def page_releases(conn, staff, preselect: int | None = None) -> str:
    rows = query_all(
        conn,
        """
        SELECT mr.*, s.name AS staff_name, s.role AS staff_role,
               l.code AS lane_code, ps.plate
        FROM manual_release mr
        JOIN staff s ON s.id=mr.staff_id
        JOIN lane l ON l.id=mr.lane_id
        LEFT JOIN parking_session ps ON ps.id=mr.session_id
        ORDER BY mr.id DESC
        """,
    )
    trs = []
    for r in rows:
        trs.append(f"""
        <tr>
          <td class="nowrap">{esc(r['release_no'])}<div class="muted" style="font-size:12px">{fmt_dt(r['created_at'])}</div></td>
          <td class="nowrap">{esc(r['plate'] or '无牌车')}<div class="muted" style="font-size:12px">车道 {esc(r['lane_code'])}</div></td>
          <td class="nowrap"><span class="tag" style="background:#374151">{esc(RELEASE_REASONS[r['reason_code']])}</span></td>
          <td>{esc(r['reason_detail'])}</td>
          <td><a href="/evidence/release/{r['id']}" target="_blank">查看证据图片</a><div class="muted" style="font-size:11px">{esc(Path(r['evidence_path']).name)}</div></td>
          <td class="nowrap">{esc(r['staff_name'])}<div class="muted" style="font-size:12px">{config.ROLE_NAMES[r['staff_role']]}</div></td>
        </tr>""")

    form = '<p class="muted">登录复核员/管理员账号后可创建人工放行。</p>'
    if staff and config.can(staff["role"], "release:create"):
        open_cases = query_all(
            conn,
            """
            SELECT c.*, ps.plate FROM exception_case c
            LEFT JOIN parking_session ps ON ps.id=c.session_id
            WHERE c.status!='resolved' ORDER BY c.opened_at
            """,
        )
        opts = '<option value="">— 选择未结复核单 —</option>' + "".join(
            f'<option value="{c["id"]}"{" selected" if preselect == c["id"] else ""}>'
            f'{esc(c["code"])}｜{EXCEPTION_META[c["type"]]["tag"]}｜{esc(c["plate_in"] or c["plate"] or "无牌")}</option>'
            for c in open_cases
        )
        evidence_block = ""
        if preselect:
            c = query_one(conn, "SELECT * FROM exception_case WHERE id=?", (preselect,))
            evs = []
            for eid in (c["entry_event_id"], c["exit_event_id"]):
                if eid:
                    ev = query_one(conn, "SELECT * FROM lane_event WHERE id=?", (eid,))
                    if ev:
                        evs.append(ev)
            radios = "".join(
                f'<label class="evradio"><input type="radio" name="evidence_path" value="{esc(ev["evidence_path"])}" required>'
                f'<img src="/evidence/event/{ev["id"]}"><span>'
                f'{"入场" if ev["direction"]=="entry" else "出场"}抓拍 · {esc(ev["device_time"])}'
                f' · 识别 {esc(ev["plate"] or "无牌")}（{ev["confidence"]:.2f}）</span></label>'
                for ev in evs
            ) or '<p class="muted">该复核单暂无抓拍证据，不可放行。</p>'
            evidence_block = f'<div id="create"><label class="f"><span>关联识别证据（必选）</span></label>{radios}</div>'
        reason_opts = "".join(
            f'<option value="{k}">{esc(v)}</option>' for k, v in RELEASE_REASONS.items()
        )
        form = f"""
        <form method="post" action="/releases/create">
          <label class="f"><span>复核单（必选）</span><select name="case_id" onchange="if(this.value)location.href='/releases?case_id='+this.value+'#create'">{opts}</select></label>
          {evidence_block}
          <label class="f"><span>放行原因（必选）</span><select name="reason_code">{reason_opts}</select></label>
          <label class="f"><span>情况说明（必填）</span><textarea name="reason_detail" placeholder="记录核对过程、现场情况与处置依据"></textarea></label>
          <button class="btn teal">{'确认选择证据并' if preselect else '先选择复核单'}提交放行</button>
        </form>"""
    elif staff:
        form = '<p class="muted">当前岗位无人工放行权限。</p>'

    return f"""
    <h1>人工放行台账</h1>
    <div class="sub">人工放行必须选择原因并关联本地抓拍证据；放行后复核单自动结案并写入台账，留痕可追溯。</div>
    <div class="panel"><h2>放行记录（{len(rows)}）</h2>
      <table><thead><tr><th>放行单号/时间</th><th>车辆/车道</th><th>原因</th>
        <th>完整说明</th><th>证据</th><th>操作人</th></tr></thead>
      <tbody>{''.join(trs)}</tbody></table></div>
    <div class="panel"><h2>新建人工放行</h2>{form}</div>
    """


# ---------------------------------------------------------------- 页面：退款

def page_refunds(conn, staff) -> str:
    orders = query_all(
        conn,
        """
        SELECT po.*, r.id AS refund_id, r.status AS refund_status
        FROM payment_order po
        LEFT JOIN refund_record r ON r.order_id=po.id
        WHERE po.payable_cents > 0
        ORDER BY po.id DESC
        """,
    )
    refunds = query_all(
        conn,
        """
        SELECT rr.*, s.name AS staff_name, s.role AS staff_role, po.plate, po.order_no
        FROM refund_record rr
        JOIN staff s ON s.id=rr.staff_id
        JOIN payment_order po ON po.id=rr.order_id
        ORDER BY rr.id DESC
        """,
    )
    can_refund = staff and config.can(staff["role"], "refund:create")
    otrs = []
    for o in orders:
        snap = loads(o["billing_snapshot"])
        rule_v = f'v{snap["rule"]["version"]}'
        if o["refund_id"]:
            action = '<span class="pill ok">已退款（一单一次）</span>'
        elif o["status"] == "paid":
            if can_refund:
                opts = "".join(
                    f'<option value="{k}">{esc(v)}</option>' for k in REFUND_REASONS
                )
                action = (
                    f'<form class="inline" method="post" action="/refunds/create" '
                    f'style="display:flex;gap:6px;align-items:center">'
                    f'<input type="hidden" name="order_id" value="{o["id"]}">'
                    f'<select name="reason_code" style="width:auto">{opts}</select>'
                    f'<button class="btn danger">整单退款</button></form>'
                )
            else:
                action = '<span class="muted">需收费员/管理员</span>'
        else:
            action = _order_badge(o["status"])
        disc = f'<div class="muted" style="font-size:11.5px">券减 ¥{yuan(o["discount_cents"])}</div>' if o["discount_cents"] else ""
        otrs.append(f"""
        <tr>
          <td class="nowrap"><a href="/orders/{o['id']}">{esc(o['order_no'])}</a><div class="muted" style="font-size:12px">{esc(o['plate'] or '无牌')} · {rule_v}</div></td>
          <td class="right"><span class="amt">{yuan(o['amount_cents'])}</span></td>
          <td class="right"><span class="amt zero">−{yuan(o['discount_cents'])}</span>{disc}</td>
          <td class="right"><span class="amt {'ok' if o['status']=='paid' else 'bad' if o['status']=='failed' else ''}">¥{yuan(o['payable_cents'])}</span></td>
          <td class="nowrap">{fmt_dt(o['created_at'])}</td>
          <td class="nowrap">{action}</td>
        </tr>""")

    rtrs = []
    for r in refunds:
        rtrs.append(f"""
        <tr>
          <td class="nowrap">{esc(r['refund_no'])}<div class="muted" style="font-size:12px">{fmt_dt(r['created_at'])}</div></td>
          <td class="nowrap">{esc(r['order_no'])}<div class="muted" style="font-size:12px">{esc(r['plate'])}</div></td>
          <td class="right"><span class="amt bad">−¥{yuan(r['amount_cents'])}</span></td>
          <td>{esc(REFUND_REASONS[r['reason_code']])}<div class="muted" style="font-size:12px">{esc(r['reason_detail'] or '')}</div></td>
          <td class="nowrap">{esc(r['staff_name'])}（{config.ROLE_NAMES[r['staff_role']]}）</td>
          <td class="nowrap"><span class="pill ok">成功</span><div class="muted" style="font-size:11px">{esc(r['gateway_refund_no'] or '')}</div></td>
        </tr>""")
    total_refund = sum(int(r["amount_cents"]) for r in refunds)
    return f"""
    <h1>退款台账</h1>
    <div class="sub">同一订单只允许退款一次，重复提交幂等返回既有退款单；退款成功后订单状态置为「已退款」。</div>
    <div class="kpi">
      <div class="card"><div class="n">{len(refunds)}</div><div class="l">退款笔数</div></div>
      <div class="card"><div class="n" style="color:var(--warn)">¥{yuan(total_refund)}</div><div class="l">累计退款金额（虚构）</div></div>
    </div>
    <div class="panel"><h2>订单与退款操作（{len(orders)}）</h2>
      <table><thead><tr><th>订单 / 车辆 / 规则版本</th>
        <th class="right">计费金额</th><th class="right">优惠</th><th class="right">实付（固定列宽）</th>
        <th>结算时间</th><th>退款操作</th></tr></thead>
      <tbody>{''.join(otrs)}</tbody></table></div>
    <div class="panel"><h2>退款流水（{len(refunds)}）</h2>
      <table><thead><tr><th>退款单号/时间</th><th>原订单</th><th class="right">退款金额</th>
        <th>原因/说明</th><th>经办</th><th>渠道结果</th></tr></thead>
      <tbody>{''.join(rtrs)}</tbody></table></div>
    """


# ---------------------------------------------------------------- 页面：订单详情与计费快照

def page_order(conn, staff, order_id: int) -> str:
    o = query_one(conn, "SELECT * FROM payment_order WHERE id=?", (order_id,))
    if o is None:
        return '<div class="panel"><h2>订单不存在</h2></div>'
    snap = loads(o["billing_snapshot"])
    events = query_all(
        conn,
        """
        SELECT le.*, l.code FROM lane_event le JOIN lane l ON l.id=le.lane_id
        WHERE le.session_id=? ORDER BY le.device_time
        """,
        (o["session_id"],),
    )
    cap_badge = '<span class="pill bad">触顶</span>'
    slice_rows = "".join(
        f'<tr><td>第 {s["day_index"]} 日 · {esc(s["date"])}</td>'
        f'<td class="right"><span class="amt">{s["minutes"]} 分</span></td>'
        f'<td class="right"><span class="amt zero">{s["free_deducted_minutes"]} 分</span></td>'
        f'<td class="right"><span class="amt">{s["hour_units"]} 个</span></td>'
        f'<td class="right"><span class="amt zero">¥{yuan(s["raw_fee_cents"])}</span></td>'
        f'<td class="right"><span class="amt {"bad" if s["cap_applied"] else ""}">¥{yuan(s["fee_cents"])}</span></td>'
        f'<td>{cap_badge if s["cap_applied"] else "—"}</td></tr>'
        for s in snap["day_slices"]
    )
    r = snap["rule"]
    rule_box = f"""
    <div class="grid2">
      <div><b>规则版本 v{r['version']}</b>（id={r['rule_version_id']}）
        <div class="muted" style="font-size:12.5px">生效 {esc(r['effective_from'])} ～ {esc(r['effective_to'] or '长期')}</div></div>
      <div class="muted" style="font-size:12.5px">免费 {r['free_minutes']} 分钟 ·
        ¥{yuan(r['hour_fee_cents'])}/小时（不足 1 小时按 1 小时）·
        单日封顶 ¥{yuan(r['daily_cap_cents'])}</div>
    </div>"""
    cp = snap["coupon"]
    coupon_html = (
        f'<p class="muted">未使用优惠券</p>' if not cp else
        f'<p>券 {esc(cp["code"])}（{"现金券" if cp["type"]=="cash" else "折扣券"}）'
        f'有效期 {esc(cp["valid_from"])} ～ {esc(cp["valid_to"])}，抵扣 '
        f'<b class="amt zero" style="display:inline">¥{yuan(snap["discount_cents"])}</b></p>'
    )
    ev_html = "".join(
        f'<div style="display:inline-block;margin:0 14px 10px 0">'
        f'<img class="evthumb" src="/evidence/event/{e["id"]}"><div class="muted" style="font-size:12px">'
        f'{esc(e["code"])} · {"入场" if e["direction"]=="entry" else "出场"} · {fmt_dt(e["device_time"])}'
        f' · {PROCESSED_LABEL.get(e["processed_as"], e["processed_as"])}</div></div>'
        for e in events
    )
    refund = query_one(conn, "SELECT * FROM refund_record WHERE order_id=?", (order_id,))
    return f"""
    <h1>订单 {esc(o['order_no'])}</h1>
    <div class="sub">{_order_badge(o['status'])} 车辆 {esc(o['plate'] or '无牌')} ·
      停车 {o['duration_minutes']} 分钟 {'· <span class="pill">跨日</span>' if snap['cross_day'] else ''}
      {'· <span class="pill bad">券失效复核</span>' if o['id'] and False else ''}</div>
    <div class="panel"><h2>计费快照（不可变）</h2>
      {rule_box}
      <table style="margin:12px 0"><thead><tr><th>自然日切片</th><th class="right">停车分钟</th>
        <th class="right">免费抵扣</th><th class="right">计费小时单位</th>
        <th class="right">封前费用</th><th class="right">计收</th><th>封顶</th></tr></thead>
        <tbody>{slice_rows}</tbody></table>
      {coupon_html}
      <table style="max-width:420px;margin-left:auto">
        <tr><td>基础车费</td><td class="right"><span class="amt">¥{yuan(snap['base_fee_cents'])}</span></td></tr>
        <tr><td>优惠减免</td><td class="right"><span class="amt zero">−¥{yuan(snap['discount_cents'])}</span></td></tr>
        <tr style="border-top:2px solid var(--ink)"><td><b>应收/实收</b></td>
          <td class="right"><span class="amt ok" style="font-size:15px">¥{yuan(snap['payable_cents'])}</span></td></tr>
      </table>
      <p class="muted" style="font-size:12px">{esc(snap['snapshot_note'])}</p>
      {'<p class="flash err">渠道失败原因：' + esc(o['fail_reason'] or '') + f' <a class="btn teal" href="/cases?session={o["session_id"]}">前往复核</a></p>' if o['status']=='failed' else ''}
      {'<p class="flash ok">已退款：' + esc(refund['refund_no']) + '（' + esc(REFUND_REASONS[refund['reason_code']]) + '）</p>' if refund else ''}
    </div>
    <div class="panel"><h2>车道抓拍证据</h2>{ev_html}</div>
    """


# ---------------------------------------------------------------- HTTP 处理

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "ParkingReview/1.0"

    def log_message(self, fmt, *args):  # 安静日志
        pass

    # ---- 基础工具 ----
    def _render(self, title: str, body: str, active: str = "", status: int = 200,
                flash: tuple[str, str] | None = None):
        staff = _staff_from_cookie(self)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(shell(title, body, staff, active, flash))

    def _redirect(self, path: str, flash: tuple[str, str] | None = None):
        loc = path
        if flash:
            loc += ("&" if "?" in path else "?") + "flash_kind=" + flash[0] + "&flash=" + urllib.parse.quote(flash[1])
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def _flash(self, qs: dict):
        if qs.get("flash"):
            return qs.get("flash_kind", ["ok"])[0], qs["flash"][0]
        return None

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def _require(self, permission: str):
        staff = _staff_from_cookie(self)
        if staff is None:
            self._redirect("/login", ("err", "请先使用岗位 PIN 登录"))
            return None
        if not config.can(staff["role"], permission):
            self._redirect("/", ("err", f"岗位「{config.ROLE_NAMES[staff['role']]}」无此操作权限"))
            return None
        return staff

    def _serve_file(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        ctype = "image/png" if path.suffix == ".png" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        flash = self._flash(qs)
        conn = connect(config.DB_PATH)
        try:
            staff = _staff_from_cookie(self)
            if path == "/login":
                body = """
                <div class="panel loginbox"><h2>岗位登录（虚构演示）</h2>
                <form method="post" action="/login">
                  <label class="f"><span>账号</span><input type="text" name="login" placeholder="如 reviewer01" required></label>
                  <label class="f"><span>PIN</span><input type="password" name="pin" placeholder="如 2002" required></label>
                  <button class="btn teal">登录</button>
                </form>
                <p class="muted" style="margin-top:14px;font-size:12.5px">
                演示账号：monitor01/1001 监控员 · reviewer01/2002 复核员<br>
                cashier01/3003 收费员 · admin01/9009 管理员</p></div>"""
                return self._render("登录", body, flash=flash)
            if path == "/logout":
                self.send_response(303)
                self.send_header("Set-Cookie", "staff=; Max-Age=0; Path=/")
                self.send_header("Location", "/login")
                self.end_headers()
                return

            if path == "/":
                return self._render("车道监控", page_lanes(conn, staff), "/", flash=flash)
            if path == "/records":
                return self._render("停车记录", page_records(conn, staff), "/records", flash=flash)
            if path == "/cases":
                sf = qs.get("session", [None])[0]
                return self._render("异常队列", page_cases(conn, staff, sf), "/cases", flash=flash)
            if path == "/rules":
                return self._render("规则版本", page_rules(conn, staff), "/rules", flash=flash)
            if path == "/releases":
                pre = int(qs["case_id"][0]) if qs.get("case_id") else None
                return self._render("人工放行", page_releases(conn, staff, pre), "/releases", flash=flash)
            if path == "/refunds":
                return self._render("退款台账", page_refunds(conn, staff), "/refunds", flash=flash)

            if path.startswith("/orders/"):
                oid = int(path.rsplit("/", 1)[1])
                return self._render("订单", page_order(conn, staff, oid), flash=flash)
            if path.startswith("/evidence/event/"):
                eid = int(path.rsplit("/", 1)[1])
                ev = query_one(conn, "SELECT evidence_path FROM lane_event WHERE id=?", (eid,))
                if not ev:
                    return self.send_error(404)
                return self._serve_file(Path(ev["evidence_path"]))
            if path.startswith("/evidence/release/"):
                rid = int(path.rsplit("/", 1)[1])
                r = query_one(conn, "SELECT evidence_path FROM manual_release WHERE id=?", (rid,))
                if not r:
                    return self.send_error(404)
                return self._serve_file(Path(r["evidence_path"]))
            self.send_error(404)
        finally:
            conn.close()

    # ---- POST ----
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, form = parsed.path, self._form()
        conn = connect(config.DB_PATH)
        try:
            if path == "/login":
                s = query_one(
                    conn, "SELECT * FROM staff WHERE login=? AND pin=? AND active=1",
                    (form.get("login", "").strip(), form.get("pin", "").strip()),
                )
                if s is None:
                    return self._redirect("/login", ("err", "账号或 PIN 错误（均为虚构演示账号）"))
                self.send_response(303)
                self.send_header("Set-Cookie", f"staff={_sign(s['id'])}; Path=/; HttpOnly")
                self.send_header("Location", "/")
                self.end_headers()
                return
            if path == "/logout":
                self.send_response(303)
                self.send_header("Set-Cookie", "staff=; Max-Age=0; Path=/")
                self.send_header("Location", "/login")
                self.end_headers()
                return

            staff = self._require_any()
            if staff is None:
                return

            try:
                if path.startswith("/cases/") and path.endswith("/claim"):
                    st = self._require("case:review")
                    if st is None:
                        return
                    cid = int(path.split("/")[2])
                    review_case(conn, case_id=cid, staff=st, action="claim")
                    return self._redirect("/cases", ("ok", "已认领复核单"))
                if path.startswith("/cases/") and path.endswith("/dismiss"):
                    st = self._require("case:review")
                    if st is None:
                        return
                    cid = int(path.split("/")[2])
                    review_case(conn, case_id=cid, staff=st, action="dismiss",
                                resolution="复核确认无异议，归档结案")
                    return self._redirect("/cases", ("ok", "复核单已归档"))
                if path.startswith("/cases/") and path.endswith("/settle-mismatch"):
                    st = self._require("case:review")
                    if st is None:
                        return
                    cid = int(path.split("/")[2])
                    oid = settle_mismatch_as_entry_plate(conn, case_id=cid, staff=st)
                    return self._redirect(f"/orders/{oid}", ("ok", "已按入场车牌补结算，仅扣一次费"))
                if path.startswith("/orders/") and path.endswith("/retry"):
                    st = self._require("case:review")
                    if st is None:
                        return
                    oid = int(path.split("/")[2])
                    retry_payment(conn, order_id=oid, staff=st)
                    return self._redirect(f"/orders/{oid}", ("ok", "重试扣费成功，订单已支付"))
                if path == "/releases/create":
                    st = self._require("release:create")
                    if st is None:
                        return
                    rid = create_manual_release(
                        conn, staff=st,
                        case_id=int(form["case_id"]) if form.get("case_id") else None,
                        reason_code=form.get("reason_code", ""),
                        reason_detail=form.get("reason_detail", ""),
                        evidence_path=form.get("evidence_path", ""),
                    )
                    return self._redirect("/releases", ("ok", f"人工放行已登记并写入台账"))
                if path == "/refunds/create":
                    st = self._require("refund:create")
                    if st is None:
                        return
                    oid = int(form["order_id"])
                    rid, dup = refund_order(
                        conn, order_id=oid, staff=st,
                        reason_code=form.get("reason_code", ""),
                        reason_detail=form.get("reason_detail", ""),
                    )
                    msg = "该订单已退款，已幂等返回既有退款单" if dup else "整单退款成功，已写入台账"
                    return self._redirect("/refunds", ("ok", msg))
                if path == "/rules/publish":
                    st = self._require("rule:publish")
                    if st is None:
                        return
                    eff = form.get("effective_from", "").replace("T", "T")
                    if len(eff) == 16:
                        eff += ":00"
                    datetime.strptime(eff, FMT)
                    lot = query_one(conn, "SELECT id FROM parking_lot ORDER BY id LIMIT 1")
                    publish_rule_version(
                        conn, lot_id=lot["id"], staff=st, effective_from=eff,
                        free_minutes=int(float(form["free_minutes"])),
                        hour_fee_cents=int(round(float(form["hour_fee_yuan"]) * 100)),
                        daily_cap_cents=int(round(float(form["daily_cap_yuan"]) * 100)),
                        note=form.get("note", ""),
                    )
                    return self._redirect("/rules", ("ok", "新版本已发布，历史订单快照不变"))
                self.send_error(404)
            except (ServiceError, PermissionDenied, ValueError, KeyError) as e:
                back = self.headers.get("Referer", "/")
                return self._redirect(back, ("err", str(e)))
        finally:
            conn.close()

    def _require_any(self):
        staff = _staff_from_cookie(self)
        if staff is None:
            self._redirect("/login", ("err", "请先使用岗位 PIN 登录"))
        return staff


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    if not config.DB_PATH.exists():
        raise SystemExit("数据库不存在，请先运行 python3 -m parking_review.demo --seed-only")
    httpd = ThreadingHTTPServer((host, port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
