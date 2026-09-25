/* 商场停车无感扣费异常复核平台 —— 前端（原生 JS，无第三方依赖） */
"use strict";

const state = {
  token: localStorage.getItem("parking_token") || "",
  me: null,
  view: "lanes",
  constants: { anomaly_labels: [], release_reasons: [] },
  lanes: [],
  laneFilter: null,
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fen = (v) => (v === null || v === undefined) ? "—"
  : "￥" + (v / 100).toFixed(2);
const shortTime = (s) => s ? s.replace("T", " ").slice(0, 16) : "—";
const can = (action) => state.me && state.me.permissions.includes(action);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: Object.assign(
      { "Content-Type": "application/json" },
      state.token ? { "X-Auth-Token": state.token } : {}),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  setTimeout(() => { t.className = "toast"; }, 3200);
}

/* --------------------------------------------------------------- 登录 */
async function login() {
  const [u, p] = [$("#loginUser").value.trim(), $("#loginPass").value];
  try {
    const d = await api("/api/login", { method: "POST", body: { login: u, pass_code: p } });
    state.token = d.token;
    localStorage.setItem("parking_token", state.token);
    await loadMe();
    toast(`欢迎，${d.display_name}`);
    switchView(state.view);
  } catch (e) { toast(e.message, true); }
}

function logout() {
  state.token = ""; state.me = null;
  localStorage.removeItem("parking_token");
  applyAuthUI();
}

async function loadMe() {
  state.me = await api("/api/me");
  applyAuthUI();
}

function applyAuthUI() {
  if (state.me) {
    $("#loggedOut").classList.add("hidden");
    $("#loggedIn").classList.remove("hidden");
    $("#whoName").textContent = state.me.display_name;
    $("#whoRole").textContent = `${state.me.role_name} ${state.me.role}`;
  } else {
    $("#loggedOut").classList.remove("hidden");
    $("#loggedIn").classList.add("hidden");
  }
  document.body.classList.toggle("authed", !!state.me);
}

/* --------------------------------------------------------------- 视图切换 */
async function switchView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view));
  document.querySelectorAll("main > section").forEach((s) =>
    s.classList.toggle("hidden", s.id !== `view-${view}`));
  if (!state.me) return;
  const renderers = {
    lanes: renderLanes, records: renderRecords, cases: renderCases,
    rules: renderRules, releases: renderReleases, refunds: renderRefunds,
  };
  await renderers[view]();
}

async function refreshAll() {
  await switchView(state.view);
  if (state.view !== "cases") refreshCaseCount();
}

async function refreshCaseCount() {
  try {
    const cs = await api("/api/cases?status=OPEN");
    const n = cs.length;
    const el = $("#tabCasesN");
    el.textContent = n;
    el.className = "n" + (n ? "" : " zero");
  } catch { /* ignore */ }
}

function anomalyTag(codeOrObj) {
  if (!codeOrObj) return "";
  const code = typeof codeOrObj === "string" ? codeOrObj : codeOrObj.code;
  const lab = state.constants.anomaly_labels.find((a) => a.code === code);
  const short = lab ? lab.short : code;
  const full = lab ? lab.full : code;
  return `<span class="tag ${code}" title="${esc(full)}">${esc(short)}</span>`;
}
function statusPill(s) {
  return s ? `<span class="status ${esc(s)}">${esc(s)}</span>` : "";
}

/* --------------------------------------------------------------- 车道监控 */
async function renderLanes() {
  const [{ lanes, lots }, stats, msgs] = await Promise.all([
    api("/api/lanes"), api("/api/stats"),
    api(state.laneFilter ? `/api/lane-messages?lane_id=${state.laneFilter}`
        : "/api/lane-messages?lane_id=0"),
  ]);
  state.lanes = lanes;

  $("#statsBar").innerHTML = [
    ["车道总数", stats.lanes_total], ["告警车道", stats.lanes_alert, "alert"],
    ["在场车辆", stats.inside], ["待复核", stats.open_cases, stats.open_cases ? "alert" : ""],
    ["已支付订单", stats.paid_orders],
    ["累计收入", fen(stats.paid_amount_fen / 100)],
    ["累计退款", fen(stats.refund_amount_fen / 100)],
  ].map(([k, v, cls]) =>
    `<div class="stat ${cls || ""}"><div class="k">${k}</div><div class="v">${v}</div></div>`
  ).join("");

  // 车道下拉
  $("#msgLane").innerHTML =
    `<option value="">全部车道（默认显示首条车道）</option>` +
    lanes.map((l) => `<option value="${l.id}" ${state.laneFilter === l.id ? "selected" : ""}>${esc(l.name)}</option>`).join("");

  // 横向车道带：按停车场分组
  const wrap = $("#lotsWrap");
  wrap.innerHTML = lots.map((lot) => {
    const ls = lanes.filter((l) => l.lot_id === lot.id);
    const cells = ls.map((l) => {
      const cls = l.status === "ALERT" ? "alert" : (l.status === "PASS" ? "ok" : "");
      return `<div class="lane-cell ${cls}" data-lane="${l.id}">
        <div class="lane-head">
          <span class="lane-dir ${l.direction}">${l.direction === "ENTRY" ? "入口" : "出口"}</span>
          <span class="lane-name">${esc(l.name)}</span>
        </div>
        <div class="lane-code">${esc(l.code)} · #${l.seq}</div>
        <div class="lane-event">${esc(l.last_event || "待机中")}</div>
        <div class="lane-time">${l.device_time ? shortTime(l.device_time) : "无消息"}</div>
        <div class="lane-strip"></div>
      </div>`;
    }).join("");
    return `<div class="lot-title"><h2>${esc(lot.name)}</h2>
      <span class="muted">${esc(lot.address)}</span></div>
      <div class="lane-band">${cells}</div>`;
  }).join("");
  wrap.querySelectorAll(".lane-cell").forEach((c) =>
    c.addEventListener("click", () => {
      state.laneFilter = Number(c.dataset.lane);
      $("#msgLane").value = state.laneFilter;
      renderLaneMessages();
    }));

  await renderLaneMessages();
}

async function renderLaneMessages() {
  const laneId = state.laneFilter || state.lanes[0]?.id || 0;
  const msgs = await api(`/api/lane-messages?lane_id=${laneId}`);
  $("#msgBody").innerHTML = msgs.length ? msgs.map((m) => `
    <tr>
      <td class="mono">${shortTime(m.device_time)}</td>
      <td>${m.direction === "ENTRY" ? "入场" : "出场"}</td>
      <td class="mono">${esc(m.raw_plate || "无牌车")}</td>
      <td class="mono">${(m.confidence || 0).toFixed(2)}</td>
      <td class="mono muted" style="font-size:12px">${esc(m.msg_uid)}</td>
      <td>${m.dup_of_msg_uid
        ? `<span class="status FAILED">重复→${esc(m.dup_of_msg_uid.slice(-8))}</span>`
        : statusPill(m.processed ? "PASS" : "INSIDE")}
        ${m.note ? `<div class="muted" style="font-size:12px">${esc(m.note)}</div>` : ""}
      </td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="muted" style="padding:18px">该车道暂无设备消息</td></tr>`;
}

/* --------------------------------------------------------------- 停车记录 */
async function renderRecords() {
  const filter = $("#recFilter").value;
  const rows = await api("/api/sessions" + (filter ? `?status=${filter}` : ""));
  $("#recBody").innerHTML = rows.map((s) => `
    <tr class="clickable" data-session="${s.id}">
      <td class="mono">#${s.id}</td>
      <td class="mono"><b>${esc(s.plate || "无牌车")}</b></td>
      <td class="mono">${shortTime(s.entry_time)}</td>
      <td>${esc(s.entry_lane_name || "")}</td>
      <td class="mono">${shortTime(s.exit_time)}</td>
      <td>${esc(s.exit_lane_name || "")}</td>
      <td class="mono">${s.exit_time ? durationLabel(s.entry_time, s.exit_time) : "—"}</td>
      <td class="num">${fen(s.amount_fen)}</td>
      <td>${s.rule_snapshot ? `v${s.rule_snapshot.rule.version}` : "—"}</td>
      <td>${statusPill(s.status)}</td>
      <td>${s.anomaly ? anomalyTag(s.anomaly) : ""}</td>
    </tr>`).join("") ||
    `<tr><td colspan="11" class="muted" style="padding:18px">暂无停车记录</td></tr>`;
  $("#recBody").querySelectorAll("tr.clickable").forEach((tr) =>
    tr.addEventListener("click", () => openSession(Number(tr.dataset.session))));
}

function durationLabel(a, b) {
  const mins = Math.max(0, Math.round((new Date(b) - new Date(a)) / 60000));
  if (mins < 60) return `${mins} 分`;
  return `${Math.floor(mins / 60)} 时 ${mins % 60} 分`;
}

/* --------------------------------------------------------------- 异常队列 */
async function renderCases() {
  const filter = $("#caseFilter").value;
  const rows = await api("/api/cases" + (filter ? `?status=${filter}` : ""));
  $("#caseBody").innerHTML = rows.map((c) => `
    <tr>
      <td class="mono">${esc(c.case_no)}</td>
      <td>
        <div class="mono"><b>${esc(c.plate || "无牌车")}</b></div>
        <div class="muted" style="font-size:12px">会话 #${c.session_id ?? "—"}</div>
      </td>
      <td>${anomalyTag(c.label)}</td>
      <td style="max-width:340px"><span class="fulltext">${esc(c.detail)}</span></td>
      <td class="mono">${esc(c.order_no || "—")}</td>
      <td class="num">${fen(c.amount_fen)}</td>
      <td>${statusPill(c.status)}</td>
      <td>${caseActions(c)}</td>
    </tr>`).join("") ||
    `<tr><td colspan="8" class="muted" style="padding:18px">队列为空</td></tr>`;
  bindCaseActions(rows);
  refreshCaseCount();
}

function caseActions(c) {
  if (c.status !== "OPEN") {
    return c.resolution ? `<span class="muted" style="font-size:12px">${esc(c.resolution.slice(0, 26))}…</span>` : "—";
  }
  const btns = [];
  if (can("review.resolve")) {
    btns.push(`<button class="ghost" data-act="resolve" data-case="${c.id}">复核处理</button>`);
  }
  if (c.code === "PAY_FAILED" && c.order_id && can("payment.bind") && c.vehicle_id) {
    btns.push(`<button data-act="bind-retry" data-order="${c.order_id}" data-vehicle="${c.vehicle_id}">补绑支付并重试</button>`);
  } else if (c.code === "PAY_FAILED" && c.order_id && can("review.resolve")) {
    btns.push(`<button data-act="retry" data-order="${c.order_id}">重试扣款</button>`);
  }
  if (c.session_id && can("release.create") &&
      ["PLATE_MISMATCH", "NO_PLATE"].includes(c.code)) {
    btns.push(`<button class="danger" data-act="release" data-session="${c.session_id}">人工放行</button>`);
  }
  return btns.join(" ") || `<span class="muted">当前岗位仅可查看</span>`;
}

function bindCaseActions(rows) {
  $("#caseBody").querySelectorAll("button[data-act]").forEach((b) => {
    b.addEventListener("click", async () => {
      const act = b.dataset.act;
      try {
        if (act === "resolve") {
          const note = prompt("请输入复核处理说明：");
          if (note === null) return;
          await api(`/api/cases/${b.dataset.case}/resolve`,
            { method: "POST", body: { resolution: note } });
          toast("案件已处理");
        } else if (act === "retry") {
          const d = await api(`/api/orders/${b.dataset.order}/retry`, { method: "POST", body: {} });
          toast(d.message || "重试扣款成功");
        } else if (act === "bind-retry") {
          await api(`/api/vehicles/${b.dataset.vehicle}/bind-method`, {
            method: "POST",
            body: { method_type: "WALLET", display_name: "岗亭补绑电子钱包（模拟）",
                    token: "OK:WLT-" + Date.now().toString().slice(-8) },
          });
          const d = await api(`/api/orders/${b.dataset.order}/retry`, { method: "POST", body: {} });
          toast(d.message || "补绑后扣款成功");
        } else if (act === "release") {
          openSession(Number(b.dataset.session), true);
          return;
        }
        renderCases();
      } catch (e) { toast(e.message, true); }
    });
  });
}

/* --------------------------------------------------------------- 规则版本 */
async function renderRules() {
  const [rules, lots] = await Promise.all([api("/api/rules"), api("/api/lots")]);
  $("#ruleBody").innerHTML = rules.map((r) => `
    <tr>
      <td>${esc(r.lot_name)}</td>
      <td><b>v${r.version}</b>${r.valid_to ? "" : ' <span class="pill-inline">当前生效</span>'}</td>
      <td class="mono">${shortTime(r.valid_from)}</td>
      <td class="mono">${r.valid_to ? shortTime(r.valid_to) : "—"}</td>
      <td>${r.free_minutes} 分</td>
      <td>${r.unit_minutes} 分 / ${fen(r.unit_price_fen)}</td>
      <td class="num">${fen(r.unit_price_fen)}</td>
      <td class="num">${r.daily_cap_fen ? fen(r.daily_cap_fen) : "不封顶"}</td>
      <td class="muted">${esc(r.note)}</td>
    </tr>`).join("");
  $("#nrLot").innerHTML = lots.map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("");
  $("#newRulePanel").style.display = can("rule.create") ? "" : "none";
}

async function submitNewRule() {
  try {
    await api("/api/rules", { method: "POST", body: {
      lot_id: Number($("#nrLot").value),
      valid_from: $("#nrFrom").value,
      free_minutes: Number($("#nrFree").value),
      unit_minutes: Number($("#nrUnit").value),
      unit_price_fen: Number($("#nrPrice").value),
      daily_cap_fen: Number($("#nrCap").value),
      note: $("#nrNote").value,
    }});
    toast("新版本已登记，旧版本同时关闭；已结算订单仍按其快照金额");
    renderRules();
  } catch (e) { toast(e.message, true); }
}

/* --------------------------------------------------------------- 人工放行 */
async function renderReleases() {
  const rows = await api("/api/releases");
  $("#releaseBody").innerHTML = rows.map((r) => `
    <tr>
      <td class="mono">${esc(r.release_no)}</td>
      <td class="mono">${shortTime(r.created_at)}</td>
      <td class="mono"><b>${esc(r.plate || "无牌车")}</b></td>
      <td>${esc(r.lane_name)}</td>
      <td><span class="tag" style="background:var(--teal-deep)">${esc(r.reason_code)}</span></td>
      <td style="max-width:300px">${esc(r.reason_detail)}</td>
      <td><a href="/evidence/${encodeURIComponent(r.evidence_file)}" target="_blank"
           class="mono" style="font-size:12px">${esc(r.evidence_file)}</a></td>
      <td>${esc(r.operator_name)}</td>
    </tr>`).join("") ||
    `<tr><td colspan="8" class="muted" style="padding:18px">暂无人工放行记录</td></tr>`;
}

/* --------------------------------------------------------------- 退款台账 */
async function renderRefunds() {
  const rows = await api("/api/refunds");
  $("#refundBody").innerHTML = rows.map((r) => `
    <tr>
      <td class="mono">${esc(r.refund_no)}</td>
      <td class="mono">${shortTime(r.created_at)}</td>
      <td class="mono">${esc(r.order_no)}</td>
      <td class="mono">${esc(r.plate || "")}</td>
      <td class="num">${fen(r.amount_fen)}</td>
      <td style="max-width:320px">${esc(r.reason)}</td>
      <td>${esc(r.operator_name)}</td>
      <td><span class="status ${r.status}">${r.status === "SUCCESS" ? "退款成功" : r.status}</span></td>
    </tr>`).join("") ||
    `<tr><td colspan="8" class="muted" style="padding:18px">暂无退款记录</td></tr>`;
}

/* --------------------------------------------------------------- 会话抽屉 */
async function openSession(id, focusRelease = false) {
  const s = await api(`/api/sessions/${id}`);
  $("#drawerTitle").textContent = `会话 #${s.id} · ${s.plate || "无牌车"}`;
  const body = $("#drawerBody");

  const orderCards = s.orders.map((o) => {
    const snap = o.rule_snapshot_json.rule;
    const detail = o.fee_detail_json;
    const daySegs = detail.day_segments.map((d) => `
      <div class="dayseg">${d.date}：停车 ${d.segment_minutes} 分，免费 ${d.free_minutes_used} 分，
        计 ${d.units} 个单位${d.capped ? "（触封顶）" : ""} → ${fen(d.fee_fen)}</div>`).join("");
    const couponLine = detail.coupon
      ? `<div class="dayseg" style="color:${detail.coupon.applied ? "var(--ok)" : "var(--alert)"}">
           券 ${esc(detail.coupon.code)}：${detail.coupon.applied ? "已抵扣" : "失效未抵扣"}</div>`
      : "";
    const refunded = s.refunded_order_ids?.includes(o.id);
    return `
      <div class="section-t">订单 ${esc(o.order_no)} · 计费快照 v${snap.version}
        <span class="pill-inline">快照不可重算</span></div>
      <dl class="kv">
        <dt>进出场</dt><dd class="mono">${shortTime(o.entry_time)} → ${shortTime(o.exit_time)}（${o.duration_min} 分钟${detail.cross_day ? "，跨日" : ""}）</dd>
        <dt>快照口径</dt><dd>免费 ${snap.free_minutes} 分 · ${snap.unit_minutes} 分/${fen(snap.unit_price_fen)} · 封顶 ${snap.daily_cap_fen ? fen(snap.daily_cap_fen) : "无"}</dd>
        <dt>分段明细</dt><dd>${daySegs}${couponLine}</dd>
        <dt>券前 / 应付</dt><dd class="mono">${fen(o.amount_before_coupon_fen)} / <b>${fen(o.amount_fen)}</b></dd>
        <dt>支付状态</dt><dd>${statusPill(o.pay_status)} <span class="muted">${esc(o.pay_message || "")}</span></dd>
      </dl>
      <div style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap">
        ${o.pay_status === "FAILED" && s.vehicle_id && can("payment.bind")
          ? `<button data-act="bind-retry-order" data-order="${o.id}" data-vehicle="${s.vehicle_id}">补绑有效支付并重试</button>`
          : (o.pay_status === "FAILED" && can("review.resolve")
          ? `<button data-act="retry-order" data-order="${o.id}">重试扣款</button>` : "")}
        ${o.pay_status === "SUCCESS" && o.amount_fen > 0 && can("refund.create")
          ? `<button class="danger" data-act="refund" data-order="${o.id}">发起退款（一单一次）</button>`
          : ""}
      </div>`;
  }).join("");

  const reasonOpts = state.constants.release_reasons.map((r) =>
    `<option value="${r.code}">${r.code} — ${esc(r.full)}</option>`).join("");

  body.innerHTML = `
    <dl class="kv">
      <dt>当前状态</dt><dd>${statusPill(s.status)}</dd>
      <dt>入场</dt><dd class="mono">${shortTime(s.entry_time)} · ${esc(s.entry_lane_name || "")}</dd>
      <dt>出场</dt><dd class="mono">${shortTime(s.exit_time || "—")} · ${esc(s.exit_lane_name || "")}</dd>
      <dt>异常</dt><dd>${s.anomaly ? anomalyTag(s.anomaly) + esc(s.anomaly.full) : "无"}</dd>
      ${s.anomaly_detail ? `<dt>异常说明</dt><dd>${esc(s.anomaly_detail)}</dd>` : ""}
    </dl>

    <div class="section-t">入场 → 结算 时间线</div>
    <ul class="timeline">
      <li><div class="t">${shortTime(s.entry_time)} 入场</div>
        <div class="d">${esc(s.entry_lane_name || "")} 设备登记</div></li>
      ${s.exit_time ? `<li class="${s.status === "MANUAL_RELEASED" ? "muted-tl" : ""}">
        <div class="t">${shortTime(s.exit_time)} 出场</div>
        <div class="d">${esc(s.exit_lane_name || "")} 设备识别</div></li>` : ""}
      ${s.orders.map((o) => `<li class="${o.pay_status === "FAILED" ? "muted-tl" : ""}">
        <div class="t">${shortTime(o.created_at)} 订单 ${esc(o.order_no)}</div>
        <div class="d">${fen(o.amount_fen)} · ${o.pay_status} · 快照 v${o.rule_snapshot_json.rule.version}</div></li>`).join("")}
      ${s.releases.map((r) => `<li class="muted-tl"><div class="t">${shortTime(r.created_at)} 人工放行 ${esc(r.release_no)}</div>
        <div class="d">${esc(r.reason_code)} · ${esc(r.operator_name)}</div></li>`).join("")}
    </ul>

    <div class="section-t">识别证据（本地文件）</div>
    <div class="ev-row">
      ${s.entry_evidence ? evidenceFig(s.entry_evidence, "入场抓拍") : ""}
      ${s.exit_evidence ? evidenceFig(s.exit_evidence, "出场抓拍") : ""}
    </div>

    ${orderCards}

    ${s.releases.length ? `<div class="section-t">人工放行记录</div>` +
      s.releases.map((r) => `<dl class="kv">
        <dt>${esc(r.release_no)}</dt><dd>${esc(r.reason_code)} — ${esc(r.reason_detail)}
          <div class="muted">证据：<a href="/evidence/${encodeURIComponent(r.evidence_file)}" target="_blank">${esc(r.evidence_file)}</a> · ${esc(r.operator_name)}</div></dd></dl>`).join("") : ""}

    ${can("release.create") && s.status !== "MANUAL_RELEASED" && s.exit_time ? `
      <div class="section-t">人工放行（必须选原因 + 关联证据）</div>
      <div class="form-grid" id="releaseForm">
        <label class="field wide">放行原因<select id="rfReason">${reasonOpts}</select></label>
        <label class="field wide">完整说明<textarea id="rfDetail" placeholder="请填写核对经过与现场情况"></textarea></label>
        <label class="field wide">识别证据文件<select id="rfEvidence"></select></label>
        <div class="wide"><button class="danger" data-act="release-submit" data-session="${s.id}">确认人工放行</button></div>
      </div>` : ""}
  `;

  // 证据下拉：优先本会话出场/入场证据
  if (can("release.create")) {
    const sel = $("#rfEvidence");
    if (sel) {
      const files = await api("/api/evidence");
      const preferred = [s.exit_evidence, s.entry_evidence].filter(Boolean);
      sel.innerHTML =
        preferred.map((f) => `<option value="${esc(f)}">${esc(f)}（本会话）</option>`).join("") +
        files.filter((f) => !preferred.includes(f.file)).map((f) =>
          `<option value="${esc(f.file)}">${esc(f.file)}</option>`).join("");
    }
  }

  body.querySelectorAll("button[data-act]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        if (b.dataset.act === "retry-order") {
          const d = await api(`/api/orders/${b.dataset.order}/retry`, { method: "POST", body: {} });
          toast(d.message || "扣款成功");
        } else if (b.dataset.act === "bind-retry-order") {
          await api(`/api/vehicles/${b.dataset.vehicle}/bind-method`, {
            method: "POST",
            body: { method_type: "WALLET", display_name: "岗亭补绑电子钱包（模拟）",
                    token: "OK:WLT-" + Date.now().toString().slice(-8) },
          });
          const d = await api(`/api/orders/${b.dataset.order}/retry`, { method: "POST", body: {} });
          toast(d.message || "补绑后扣款成功");
        } else if (b.dataset.act === "refund") {
          const reason = prompt("请输入退款原因（同一订单仅可退一次）：");
          if (reason === null) return;
          const d = await api(`/api/orders/${b.dataset.order}/refund`,
            { method: "POST", body: { reason } });
          toast(d.idempotent ? "该订单已退过款，幂等返回原退款单" : "退款成功");
        } else if (b.dataset.act === "release-submit") {
          const d = await api(`/api/sessions/${b.dataset.session}/release`, {
            method: "POST", body: {
              reason_code: $("#rfReason").value,
              reason_detail: $("#rfDetail").value,
              evidence_file: $("#rfEvidence").value,
            }});
          toast(`放行单 MF 已生成（#${d.id}）`);
          closeDrawer();
          refreshAll();
          return;
        }
        closeDrawer();
        openSession(id);
        refreshAll();
      } catch (e) { toast(e.message, true); }
    }));

  $("#drawer").classList.add("open");
  $("#mask").classList.add("open");
  if (focusRelease) document.getElementById("rfReason")?.focus();
}

function evidenceFig(file, caption) {
  return `<figure><img class="ev-thumb" src="/evidence/${encodeURIComponent(file)}" alt="${esc(caption)}">
    <figcaption><a href="/evidence/${encodeURIComponent(file)}" target="_blank">${esc(file)}</a><br>${esc(caption)}</figcaption></figure>`;
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#mask").classList.remove("open");
}

/* --------------------------------------------------------------- 初始化 */
async function init() {
  state.constants = await api("/api/constants");
  $("#btnLogin").addEventListener("click", login);
  $("#loginPass").addEventListener("keydown", (e) => e.key === "Enter" && login());
  $("#btnLogout").addEventListener("click", logout);
  $("#quickAccount").addEventListener("change", () => {
    const [u, p] = $("#quickAccount").value.split("|");
    $("#loginUser").value = u; $("#loginPass").value = p;
  });
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchView(t.dataset.view)));
  $("#msgLane").addEventListener("change", () => {
    state.laneFilter = Number($("#msgLane").value) || null;
    renderLaneMessages();
  });
  $("#recFilter").addEventListener("change", renderRecords);
  $("#caseFilter").addEventListener("change", renderCases);
  $("#btnNewRule").addEventListener("click", submitNewRule);
  $("#drawerX").addEventListener("click", closeDrawer);
  $("#mask").addEventListener("click", closeDrawer);

  if (state.token) {
    try {
      await loadMe();
    } catch { logout(); }
  }
  applyAuthUI();
  if (state.me) switchView("lanes");
  refreshCaseCount();
  setInterval(refreshCaseCount, 15000);
}

init();
