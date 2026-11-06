/* LinIoT 控制台逻辑：hash 路由 + WebSocket 实时刷新 + Canvas 图表（零依赖） */
"use strict";
function cssVar(n,d){ try { var v=getComputedStyle(document.documentElement).getPropertyValue(n); v=v?v.trim():v; return v||d; } catch(e){ return d; } }
(function(){
  function applyTheme(t){
    document.documentElement.setAttribute('data-theme',t);
    try{ localStorage.setItem('theme',t); }catch(e){}
    var b=document.getElementById('themeToggle');
    if(b) b.textContent=(t==='dark'?'\u2600\ufe0f \u4eae\u8272':'\u{1F319} \u6697\u8272');
  }
  var t='light';
  try{ t=localStorage.getItem('theme')||'light'; }catch(e){}
  applyTheme(t);
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',function(){ applyTheme(t); bindBtn(); });
  } else { bindBtn(); }
  function bindBtn(){
    var btn=document.getElementById('themeToggle');
    if(!btn) return;
    btn.addEventListener('click',function(){
      t=(t==='dark'?'light':'dark');
      applyTheme(t);
      var redrawn=false;
      try{ if(typeof drawChart==='function'){ drawChart(); redrawn=true; } }catch(e){}
      if(!redrawn){ try{ location.reload(); }catch(e){} }
    });
  }
})();

const S = {
  devices: [], overview: {}, alerts: [],
  deviceGroupFilter: "", overviewGroupFilter: "",
  health: { mqtt: false, ai: false },
  ws: null, wsOk: false,
  route: "overview", routeId: null,
  aiHistory: [],
};

/* ------------------------------------------------------------------ 工具 */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 2200);
}

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return resp.json();
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function ago(ts) {
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

const METRIC_STYLE = {
  temperature: { color: "#f59e0b", unit: "°C", label: "温度" },
  humidity: { color: cssVar('--accent', '#2f7bf6'), unit: "%", label: "湿度" },
  co2: { color: "#8b5cf6", unit: "ppm", label: "CO₂" },
  pm25: { color: "#ec4899", unit: "μg/m³", label: "PM2.5" },
};
const metricStyle = (m) => METRIC_STYLE[m] || { color: "#14b8a6", unit: "", label: m };

/* 设备类型扁平图标（浅色卡片风格） */
const DEVICE_ICON = {
  sensor: { fill: "#34b3a0", glyph: '<path d="M7 12.5V9.8a3.4 3.4 0 1 1 4 0v2.7l-1 1.4H8l-1-1.4z" fill="#fff"/>' },
  light: { fill: cssVar('--orange', '#f5a623'), glyph: '<path d="M10 4.5A3.5 3.5 0 0 0 6.5 8c0 1.2.6 2.2 1.5 2.8v1.7h4V10.8c.9-.6 1.5-1.6 1.5-2.8A3.5 3.5 0 0 0 10 4.5z" fill="#fff"/><rect x="8" y="13.5" width="4" height="1.4" rx=".7" fill="#fff" opacity=".85"/>' },
  switch: { fill: cssVar('--accent', '#2f7bf6'), glyph: '<rect x="4.5" y="8" width="11" height="5" rx="2.5" fill="#fff"/><circle cx="7" cy="10.5" r="1.5" fill="#2f7bf6"/>' },
  generic: { fill: "#7b8aa5", glyph: '<rect x="6" y="6" width="8" height="8" rx="1.5" fill="#fff"/><path d="M8 8.5h4M8 10.5h4" stroke="#7b8aa5" stroke-width="1.1" stroke-linecap="round"/>' },
};
function deviceIcon(type, size = 24) {
  const icon = DEVICE_ICON[type] || DEVICE_ICON.generic;
  return `<svg class="dev-icon" width="${size}" height="${size}" viewBox="0 0 20 20" aria-hidden="true">` +
         `<rect width="20" height="20" rx="6" fill="${icon.fill}"/>${icon.glyph}</svg>`;
}

/* ------------------------------------------------------------------ 图表 */
function drawChart(canvas, points, color) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);
  if (!points || points.length < 2) {
    ctx.fillStyle = cssVar('--muted', '#8a99b5'); ctx.font = "12px sans-serif";
    ctx.fillText("等待数据…", w / 2 - 30, h / 2);
    return;
  }
  const values = points.map((p) => p.value);
  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < 1e-9) { min -= 1; max += 1; }
  const pad = (max - min) * 0.12;
  min -= pad; max += pad;
  const padL = 42, padR = 10, padT = 10, padB = 18;
  const iw = w - padL - padR, ih = h - padT - padB;
  const x = (i) => padL + (iw * i) / (points.length - 1);
  const y = (v) => padT + ih - ((v - min) * ih) / (max - min);

  ctx.strokeStyle = "#e8eef7"; ctx.fillStyle = cssVar('--muted', '#8a99b5'); ctx.font = "10px ui-monospace, Menlo";
  ctx.textAlign = "right";
  for (let g = 0; g <= 3; g++) {
    const gy = padT + (ih * g) / 3;
    const gv = max - ((max - min) * g) / 3;
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(w - padR, gy); ctx.stroke();
    ctx.fillText(gv.toFixed(1), padL - 6, gy + 3);
  }
  ctx.textAlign = "left";
  ctx.fillText(fmtTime(points[0].ts), padL, h - 4);
  ctx.textAlign = "right";
  ctx.fillText(fmtTime(points[points.length - 1].ts), w - padR, h - 4);

  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(x(i), y(p.value)) : ctx.moveTo(x(i), y(p.value))));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.stroke();
  ctx.lineTo(x(points.length - 1), padT + ih); ctx.lineTo(x(0), padT + ih); ctx.closePath();
  const grad = ctx.createLinearGradient(0, padT, 0, padT + ih);
  grad.addColorStop(0, color + "3d"); grad.addColorStop(1, color + "00");
  ctx.fillStyle = grad; ctx.fill();

  const last = points[points.length - 1];
  ctx.beginPath(); ctx.arc(x(points.length - 1), y(last.value), 3.5, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.fill();
}

/* ------------------------------------------------------------------ WebSocket */
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { setWS(true); };
  ws.onclose = () => { setWS(false); setTimeout(connectWS, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    handleEvent(msg);
  };
}
function setWS(ok) {
  S.wsOk = ok;
  const dot = $("#ws-dot"), text = $("#ws-text");
  dot.className = "dot " + (ok ? "on" : "off");
  text.textContent = ok ? "实时连接" : "已断开，重试中";
}

const refreshCurrent = debounce(() => render(true), 400);

function handleEvent(msg) {
  const { type, data } = msg;
  if (type === "hello") {
    S.devices = data.devices; S.overview = data.overview;
    refreshCurrent();
  } else if (type === "device") {
    const i = S.devices.findIndex((d) => d.id === data.id);
    if (i >= 0) S.devices[i] = data; else S.devices.push(data);
    if (["devices", "device"].includes(S.route)) refreshCurrent();
  } else if (type === "device_deleted") {
    S.devices = S.devices.filter((d) => d.id !== data.id);
    if (["devices", "device"].includes(S.route)) {
      if (S.route === "device" && S.routeId === data.id) location.hash = "#/devices";
      else refreshCurrent();
    }
  } else if (type === "telemetry") {
    if (["overview", "device"].includes(S.route)) refreshCurrent();
  } else if (type === "alert") {
    S.alerts.unshift(data);
    toast(`新告警：${data.message}`);
    if (["overview", "rules"].includes(S.route)) refreshCurrent();
  } else if (type === "alert_diagnosis") {
    const alert = S.alerts.find((a) => a.id === data.id);
    if (alert) alert.diagnosis = data.diagnosis;
    if (["overview", "rules"].includes(S.route)) refreshCurrent();
  } else if (type === "scene_fired") {
    toast(`场景「${data.name}」已执行：${data.action_device_id}.${data.action_property}`);
    if (S.route === "scenes") refreshCurrent();
  }
}

/* ------------------------------------------------------------------ 路由 */
function parseHash() {
  const hash = location.hash.replace(/^#\//, "") || "overview";
  const [route, id] = hash.split("/");
  S.route = route; S.routeId = id || null;
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === route));
  render();
}
window.addEventListener("hashchange", parseHash);

async function render(keepScroll) {
  const view = $("#view");
  const scroll = view.scrollHeight - view.scrollTop - view.clientHeight < 60;
  const builders = { overview: viewOverview, devices: viewDevices, device: viewDevice,
                     rules: viewRules, scenes: viewScenes, ai: viewAI };
  (builders[S.route] || viewOverview)(view);
  if (keepScroll && scroll) view.scrollTop = view.scrollHeight;
}

function renderAlerts(alerts) {
  if (!alerts.length) return `<div class="empty">暂无告警</div>`;
  return alerts.map((a) => `
    <div class="alert-item"><span class="alert-time">${fmtTime(a.ts)}</span>
      <div><span class="alert-msg">${esc(a.message)}</span>${a.diagnosis
        ? `<div class="muted" style="margin-top:2px">AI 诊断：${esc(a.diagnosis)}</div>` : ""}
      </div></div>`).join("");
}

/* ------------------------------------------------------------------ 概览 */
async function viewOverview(view) {
  const [overview, series, alerts] = await Promise.all([
    api("/api/overview"), api("/api/overview/series?limit=120"), api("/api/alerts?limit=8"),
  ]);
  S.overview = overview; S.alerts = alerts;
  const entries = Object.entries(series);
  const groups = [...new Set(entries.map(([, v]) => v.group || "").filter(Boolean))].sort();
  const visibleEntries = S.overviewGroupFilter
    ? entries.filter(([, v]) => v.group === S.overviewGroupFilter)
    : entries;
  const onlineIn = (list) => list.filter(([, v]) => v.online).length;
  const groupOptions = [`<option value="">全部分组</option>`]
    .concat(groups.map((g) =>
      `<option value="${esc(g)}"${g === S.overviewGroupFilter ? " selected" : ""}>${esc(g)}</option>`))
    .join("");
  const groupBar = groups.length ? `
    <div class="card mb14"><div class="chart-card-head">
      <div class="chart-title">按分组查看</div>
      <select id="overview-group">${groupOptions}</select></div></div>` : "";
  const cards = [
    ["设备", `${onlineIn(visibleEntries)}<span class="muted" style="font-size:14px"> / ${visibleEntries.length} 在线</span>`,
      S.overviewGroupFilter ? `分组「${esc(S.overviewGroupFilter)}」` : "注册设备总数"],
    ["数据点", overview.telemetry_points, "累计遥测记录"],
    ["24h 告警", overview.alerts_24h, "近 24 小时触发"],
    ["规则", overview.rules, "已配置阈值规则"],
  ];
  const chartCards = visibleEntries.flatMap(([deviceId, dev]) =>
    Object.entries(dev.metrics).map(([metric, points]) => `
      <div class="card chart-card" data-chart="${esc(deviceId)}:${esc(metric)}">
        <div class="chart-card-head">
          <div class="chart-title">${deviceIcon(dev.type, 20)} ${esc(dev.name)} · ${metricStyle(metric).label}</div>
          <div><span class="chart-latest" style="color:${metricStyle(metric).color}">
            ${points.length ? points[points.length - 1].value : "--"}</span>
            <span class="chart-unit">${metricStyle(metric).unit}</span></div>
        </div>
        <canvas class="chart"></canvas>
      </div>`)).join("");

  view.innerHTML = `
    <div class="page-title">设备可视化</div>
    <div class="page-sub">EQUIPMENT VISUALIZATION · 设备、数据与告警的实时总览（WebSocket 推送，无需刷新）</div>
    <div class="stat-grid">${cards.map(([label, value, extra]) => `
      <div class="card"><div class="stat-label">${label}</div>
      <div class="stat-value">${value}</div><div class="stat-extra">${extra}</div></div>`).join("")}
    </div>
    ${groupBar}
    ${chartCards ? `<div class="chart-grid">${chartCards}</div>`
      : (S.overviewGroupFilter ? `<div class="card mb14"><div class="empty">该分组暂无设备数据</div></div>` : "")}
    <div class="card">
      <div class="chart-card-head"><div class="chart-title">最近告警</div>
      <a class="back-link" href="#/rules">全部规则 →</a></div>
      ${renderAlerts(alerts)}
    </div>`;
  const overviewFilter = $("#overview-group");
  if (overviewFilter) overviewFilter.addEventListener("change", () => {
    S.overviewGroupFilter = overviewFilter.value;
    render();
  });
  paintCharts();
}

function paintCharts() {
  document.querySelectorAll("[data-chart]").forEach((card) => {
    const [deviceId, metric] = card.dataset.chart.split(":");
    api(`/api/devices/${encodeURIComponent(deviceId)}/telemetry?metric=${encodeURIComponent(metric)}&limit=120`)
      .then((points) => drawChart(card.querySelector("canvas"), points, metricStyle(metric).color))
      .catch(() => {});
  });
}

/* ------------------------------------------------------------------ 设备 */
async function viewDevices(view) {
  S.devices = await api("/api/devices");
  const groupOf = (d) => (d.meta && d.meta.group) || "";
  const groups = [...new Set(S.devices.map(groupOf).filter(Boolean))].sort();
  const visible = S.deviceGroupFilter
    ? S.devices.filter((d) => groupOf(d) === S.deviceGroupFilter)
    : S.devices;
  const groupOptions = [`<option value="">全部分组</option>`]
    .concat(groups.map((g) =>
      `<option value="${esc(g)}"${g === S.deviceGroupFilter ? " selected" : ""}>${esc(g)}</option>`))
    .join("");
  const rows = visible.map((d) => `
    <tr class="row-link" data-open="${esc(d.id)}">
      <td class="mono">${esc(d.id)}</td>
      <td>${deviceIcon(d.type)} ${esc(d.name)}</td>
      <td>${groupOf(d) ? `<span class="pill type">${esc(groupOf(d))}</span>` : '<span class="muted">—</span>'}</td>
      <td><span class="pill type">${esc(d.type)}</span></td>
      <td><span class="pill ${d.online ? "online" : "offline"}">${d.online ? "在线" : "离线"}</span></td>
      <td class="mono">${d.last_seen ? ago(d.last_seen) : "—"}</td>
      <td>
        <button class="link" data-open="${esc(d.id)}">详情</button>
        <button class="link danger" data-del="${esc(d.id)}" style="color:#f85149">删除</button>
      </td>
    </tr>`).join("");
  view.innerHTML = `
    <div class="page-title">设备管理</div>
    <div class="page-sub">${S.deviceGroupFilter
      ? `分组「${esc(S.deviceGroupFilter)}」共 ${visible.length} / ${S.devices.length} 台设备`
      : `共 ${S.devices.length} 台设备；设备可通过 MQTT 或 HTTP 上报数据`}</div>
    <div class="card mb14">
      <form id="device-form" class="form-grid">
        <div class="form-item"><label>设备 ID（可选，留空自动生成）</label>
          <input name="id" placeholder="如 sensor-1" pattern="[a-z0-9][a-z0-9_-]*"></div>
        <div class="form-item"><label>名称</label>
          <input name="name" required placeholder="如 客厅温湿度传感器"></div>
        <div class="form-item"><label>分组（可选）</label>
          <input name="group" maxlength="32" placeholder="如 一楼 / 机房"></div>
        <div class="form-item"><label>类型</label>
          <select name="type">
            <option value="sensor">sensor（传感器）</option>
            <option value="light">light（灯）</option>
            <option value="switch">switch（开关）</option>
            <option value="generic">generic（通用）</option>
          </select></div>
        <button class="primary" type="submit">注册设备</button>
      </form>
    </div>
    <div class="card">
      ${S.devices.length ? `<div class="chart-card-head">
          <div class="chart-title">设备列表</div>
          <select id="group-filter">${groupOptions}</select></div>
        ${visible.length ? `<table>
        <thead><tr><th>ID</th><th>名称</th><th>分组</th><th>类型</th><th>状态</th><th>最后上报</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody></table>`
        : `<div class="empty">该分组暂无设备</div>`}`
      : `<div class="empty">还没有设备。试试 <code class="mono">liniot simulate</code> 一键生成虚拟设备。</div>`}
    </div>`;

  $("#device-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = { name: fd.get("name"), type: fd.get("type") };
    if (fd.get("id")) body.id = fd.get("id");
    if (fd.get("group")) body.group = fd.get("group");
    try {
      await api("/api/devices", { method: "POST", body });
      toast("设备已注册"); render();
    } catch (e) { toast(`注册失败：${e.message}`); }
  });
  const groupFilter = $("#group-filter");
  if (groupFilter) groupFilter.addEventListener("change", () => {
    S.deviceGroupFilter = groupFilter.value;
    render();
  });
  view.querySelectorAll("[data-del]").forEach((btn) =>
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm(`确认删除设备 ${btn.dataset.del}？其数据与规则将一并删除。`)) return;
      await api(`/api/devices/${btn.dataset.del}`, { method: "DELETE" });
      toast("已删除"); render();
    }));
  view.querySelectorAll("[data-open]").forEach((el) =>
    el.addEventListener("click", () => { location.hash = `#/device/${el.dataset.open}`; }));
}

/* ------------------------------------------------------------------ 设备详情 */
async function viewDevice(view) {
  const id = S.routeId;
  let device;
  try { device = await api(`/api/devices/${encodeURIComponent(id)}`); }
  catch (e) { view.innerHTML = `<div class="empty">设备不存在：<a class="back-link" href="#/devices">返回列表</a></div>`; return; }

  const props = Object.entries(device.properties || {}).map(([key, value]) => {
    if (typeof value === "boolean") {
      return `<div class="prop-item"><span class="prop-name">${esc(key)}</span>
        <button class="toggle ${value ? "on" : ""}" data-prop="${esc(key)}"
          data-value="${!value}" title="点击切换"></button>
        <span class="muted">${value ? "开" : "关"}</span></div>`;
    }
    return `<div class="prop-item"><span class="prop-name">${esc(key)}</span>
      <input style="width:90px" data-prop-input="${esc(key)}" value="${esc(value)}">
      <button class="link" data-prop-set="${esc(key)}">设置</button></div>`;
  }).join("");

  view.innerHTML = `
    <a class="back-link" href="#/devices">← 返回设备列表</a>
    <div class="page-title" style="margin-top:10px">${deviceIcon(device.type, 26)} ${esc(device.name)}
      ${device.meta && device.meta.group ? `<span class="pill type">${esc(device.meta.group)}</span>` : ""}
      <span class="pill type">${esc(device.type)}</span>
      <span class="pill ${device.online ? "online" : "offline"}">${device.online ? "在线" : "离线"}</span>
    </div>
    <div class="page-sub mono">${esc(device.id)} · 最后上报 ${device.last_seen ? ago(device.last_seen) : "—"}</div>
    ${props ? `<div class="card mb14"><div class="chart-title mb14">设备属性（下发命令 → 设备确认）</div>
      <div class="props">${props}</div></div>` : ""}
    <div class="card">
      <div class="chart-card-head"><div class="chart-title">遥测数据</div>
        <span class="head-actions"><span id="metric-tabs"></span>
          <span id="agg-tabs"></span>
          <a id="export-csv" class="link" hidden download>导出 CSV</a></span></div>
      <canvas id="device-chart" class="chart" style="height:240px"></canvas>
    </div>`;

  const tabs = $("#metric-tabs");
  const chartCanvas = $("#device-chart");
  let metrics = [];
  try {
    const series = await api("/api/overview/series?limit=1");
    metrics = Object.keys((series[id] || {}).metrics || {});
  } catch (e) { metrics = []; }
  // 聚合视图：null = 原始点；"1m"/"5m"/"15m"/"1h" = 服务端窗口降采样
  const aggTabs = $("#agg-tabs");
  let currentMetric = null, currentWindow = null;
  const setAgg = (w) => {
    currentWindow = (currentWindow === w) ? null : w;
    aggTabs.querySelectorAll("button").forEach((x) => (x.style.color = ""));
    if (currentWindow) {
      const btn = aggTabs.querySelector(`[data-agg="${currentWindow}"]`);
      if (btn) btn.style.color = "#3b82f6";
    }
  };
  async function loadMetric(metric) {
    currentMetric = metric;
    if (currentWindow) {
      const buckets = await api(`/api/devices/${encodeURIComponent(id)}/telemetry/aggregate`
        + `?metric=${encodeURIComponent(metric)}&window=${currentWindow}`);
      drawChart(chartCanvas, buckets.map((b) => ({ ts: b.ts, value: b.avg })),
                metricStyle(metric).color);
    } else {
      const points = await api(`/api/devices/${encodeURIComponent(id)}/telemetry`
        + `?metric=${encodeURIComponent(metric)}&limit=300`);
      drawChart(chartCanvas, points, metricStyle(metric).color);
    }
  }
  // 导出按钮跟随当前选中的指标，有数据才显示
  const exportLink = $("#export-csv");
  const setExport = (metric) => {
    exportLink.href = `/api/devices/${encodeURIComponent(id)}/telemetry/export`
      + `?metric=${encodeURIComponent(metric)}`;
  };

  tabs.innerHTML = metrics.length
    ? metrics.map((m) => `<button class="link" data-metric="${esc(m)}"
        style="${m === (metrics[0] || "") ? "color:#3b82f6" : ""}">${metricStyle(m).label}</button>`).join("")
    : `<span class="muted">暂无遥测数据</span>`;
  aggTabs.innerHTML = metrics.length
    ? ["1m", "5m", "15m", "1h"].map((w) =>
        `<button class="link" data-agg="${w}" title="按 ${w} 窗口聚合（avg）">${w}</button>`).join("")
    : "";
  if (metrics.length) { setExport(metrics[0]); exportLink.hidden = false; loadMetric(metrics[0]); }
  tabs.querySelectorAll("[data-metric]").forEach((b) =>
    b.addEventListener("click", () => {
      tabs.querySelectorAll("button").forEach((x) => (x.style.color = ""));
      b.style.color = "#3b82f6";
      setExport(b.dataset.metric);
      loadMetric(b.dataset.metric);
    }));
  aggTabs.querySelectorAll("[data-agg]").forEach((b) =>
    b.addEventListener("click", () => {
      setAgg(b.dataset.agg);
      if (currentMetric) loadMetric(currentMetric);
    }));

  view.querySelectorAll(".toggle").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/devices/${encodeURIComponent(id)}/properties`,
        { method: "POST", body: { properties: { [btn.dataset.prop]: btn.dataset.value === "true" } } });
      toast(`已下发：${btn.dataset.prop} = ${btn.dataset.value === "true"}`);
    }));
  view.querySelectorAll("[data-prop-set]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const input = view.querySelector(`[data-prop-input="${btn.dataset.propSet}"]`);
      let value = input.value;
      if (value !== "" && !isNaN(Number(value))) value = Number(value);
      await api(`/api/devices/${encodeURIComponent(id)}/properties`,
        { method: "POST", body: { properties: { [btn.dataset.propSet]: value } } });
      toast(`已下发：${btn.dataset.propSet} = ${value}`);
    }));
}

/* ------------------------------------------------------------------ 规则 */
async function viewRules(view) {
  const [devices, rules, alerts] = await Promise.all([
    api("/api/devices"), api("/api/rules"), api("/api/alerts?limit=20"),
  ]);
  const nameOf = (id) => {
    const d = devices.find((x) => x.id === id);
    return d ? d.name : id;
  };
  const rows = rules.map((r) => `
    <tr>
      <td class="mono">#${r.id}</td>
      <td>${r.device_id ? `${esc(nameOf(r.device_id))} <span class="mono muted">(${esc(r.device_id)})</span>` : "所有设备"}</td>
      <td>${esc(r.metric)}</td>
      <td class="mono">${esc(r.op)} ${r.threshold}</td>
      <td>${r.note ? esc(r.note) : '<span class="muted">—</span>'}</td>
      <td><button class="link" data-toggle="${r.id}" data-on="${r.enabled ? 1 : 0}">
        ${r.enabled ? "已启用" : "已停用"}</button></td>
      <td><button class="link" data-delm="${r.id}" style="color:#f85149">删除</button></td>
    </tr>`).join("");
  const deviceOptions = [`<option value="">所有设备</option>`]
    .concat(devices.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}（${esc(d.id)}）</option>`))
    .join("");

  view.innerHTML = `
    <div class="page-title">规则与告警</div>
    <div class="page-sub">阈值规则在数据上报时实时判定，上升沿触发一次告警，可推送 Webhook</div>
    <div class="card mb14">
      <form id="rule-form" class="form-grid">
        <div class="form-item"><label>设备</label><select name="device_id">${deviceOptions}</select></div>
        <div class="form-item"><label>指标</label><input name="metric" required placeholder="temperature"></div>
        <div class="form-item"><label>条件</label><select name="op">
          <option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option><option>==</option>
        </select></div>
        <div class="form-item"><label>阈值</label><input name="threshold" type="number" step="any" required placeholder="30"></div>
        <div class="form-item"><label>备注（可选）</label><input name="note" placeholder="如 高温告警"></div>
        <button class="primary" type="submit">新建规则</button>
      </form>
    </div>
    <div class="card mb14">
      ${rules.length ? `<table>
        <thead><tr><th>ID</th><th>设备</th><th>指标</th><th>条件</th><th>备注</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">还没有规则，例如：温度 > 30 时告警</div>`}
    </div>
    <div class="card">
      <div class="chart-title mb14">告警历史</div>
      ${renderAlerts(alerts)}
    </div>`;

  $("#rule-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    try {
      await api("/api/rules", { method: "POST", body: {
        device_id: fd.get("device_id") || null,
        metric: fd.get("metric"), op: fd.get("op"),
        threshold: Number(fd.get("threshold")), note: fd.get("note"),
      } });
      toast("规则已创建"); render();
    } catch (e) { toast(`创建失败：${e.message}`); }
  });
  view.querySelectorAll("[data-toggle]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/rules/${btn.dataset.toggle}`,
        { method: "PATCH", body: { enabled: btn.dataset.on !== "1" } });
      render();
    }));
  view.querySelectorAll("[data-delm]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/rules/${btn.dataset.delm}`, { method: "DELETE" });
      toast("规则已删除"); render();
    }));
}

/* ------------------------------------------------------------------ 场景联动 */
async function viewScenes(view) {
  const [devices, scenes] = await Promise.all([
    api("/api/devices"), api("/api/scenes"),
  ]);
  const nameOf = (id) => {
    const d = devices.find((x) => x.id === id);
    return d ? d.name : id;
  };
  const deviceOptions = (includeAny) =>
    (includeAny ? [`<option value="">任意设备</option>`] : [])
      .concat(devices.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}（${esc(d.id)}）</option>`))
      .join("");
  const fmtValue = (v) => (typeof v === "string" ? `"${v}"` : JSON.stringify(v));
  const rows = scenes.map((s) => {
    let actionValue;
    try { actionValue = JSON.parse(s.action_value); } catch (e) { actionValue = s.action_value; }
    return `
    <tr>
      <td class="mono">#${s.id}</td>
      <td>${esc(s.name)}</td>
      <td>${s.source_device_id ? esc(nameOf(s.source_device_id)) : "任意设备"}
        · <span class="mono">${esc(s.metric)} ${esc(s.op)} ${s.threshold}</span></td>
      <td>${esc(nameOf(s.action_device_id))}.
        <span class="mono">${esc(s.action_property)} = ${esc(fmtValue(actionValue))}</span></td>
      <td class="mono">${s.cooldown}s</td>
      <td class="mono muted">${s.last_fired ? ago(s.last_fired) : "从未"}</td>
      <td><button class="link" data-toggle="${s.id}" data-on="${s.enabled ? 1 : 0}">
        ${s.enabled ? "已启用" : "已停用"}</button></td>
      <td><button class="link" data-del="${s.id}" style="color:#e5484d">删除</button></td>
    </tr>`;
  }).join("");

  view.innerHTML = `
    <div class="page-title">场景联动</div>
    <div class="page-sub">当 [设备指标满足条件] 则 [对另一设备执行动作]；冷却窗口防止命令震荡</div>
    <div class="card mb14">
      <form id="scene-form" class="form-grid">
        <div class="form-item"><label>场景名称</label>
          <input name="name" required placeholder="如 高温自动开灯"></div>
        <div class="form-item"><label>触发设备</label><select name="source_device_id">${deviceOptions(true)}</select></div>
        <div class="form-item"><label>触发指标</label><input name="metric" required placeholder="temperature"></div>
        <div class="form-item"><label>条件</label><select name="op">
          <option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option><option>==</option>
        </select></div>
        <div class="form-item"><label>阈值</label><input name="threshold" type="number" step="any" required placeholder="28"></div>
        <div class="form-item"><label>执行设备</label><select name="action_device_id">${deviceOptions(false)}</select></div>
        <div class="form-item"><label>动作属性</label><input name="action_property" required placeholder="power"></div>
        <div class="form-item"><label>动作值</label><input name="action_value" required placeholder="true / 80 / off"></div>
        <div class="form-item"><label>冷却（秒）</label><input name="cooldown" type="number" value="60" min="1"></div>
        <button class="primary" type="submit">创建场景</button>
      </form>
    </div>
    <div class="card">
      ${scenes.length ? `<table>
        <thead><tr><th>ID</th><th>名称</th><th>触发条件</th><th>执行动作</th>
        <th>冷却</th><th>最近触发</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody></table>`
      : `<div class="empty">还没有场景，例如：温度 &gt; 28 时打开智能灯-1</div>`}
    </div>`;

  $("#scene-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const raw = String(fd.get("action_value")).trim();
    let value = raw;
    if (raw === "true") value = true;
    else if (raw === "false") value = false;
    else if (raw !== "" && !isNaN(Number(raw))) value = Number(raw);
    try {
      await api("/api/scenes", { method: "POST", body: {
        name: fd.get("name"),
        source_device_id: fd.get("source_device_id") || null,
        metric: fd.get("metric"), op: fd.get("op"), threshold: Number(fd.get("threshold")),
        action_device_id: fd.get("action_device_id"),
        action_property: fd.get("action_property"),
        action_value: value, cooldown: Number(fd.get("cooldown")) || 60,
      } });
      toast("场景已创建"); render();
    } catch (e) { toast(`创建失败：${e.message}`); }
  });
  view.querySelectorAll("[data-toggle]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/scenes/${btn.dataset.toggle}`,
        { method: "PATCH", body: { enabled: btn.dataset.on !== "1" } });
      render();
    }));
  view.querySelectorAll("[data-del]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/scenes/${btn.dataset.del}`, { method: "DELETE" });
      toast("场景已删除"); render();
    }));
}

/* ------------------------------------------------------------------ AI 助手 */
async function viewAI(view) {
  view.innerHTML = `
    <div class="page-title">AI 助手</div>
    <div class="page-sub">用自然语言查询设备数据、下发控制命令
      ${S.health.ai ? "（已连接大模型）" : "（当前为本地规则模式，配置 LINIOT_AI_API_KEY 后启用大模型）"}</div>
    <div class="chat-wrap card">
      <div class="chat-box" id="chat-box">
        <div class="msg bot">你好，我是 LinIoT 智能助手，可以查询设备数据、下发控制命令。<span class="msg-meta">
          试试：「温度是多少」「打开所有的灯」「设备概览」</span></div>
      </div>
      <div class="chat-hints">
        ${["温度是多少？", "湿度怎么样？", "打开所有的灯", "关闭智能灯-1", "设备概览"]
          .map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
      </div>
      <div class="chat-input-row">
        <input id="chat-input" placeholder="输入消息，回车发送…" maxlength="500">
        <button class="primary" id="chat-send">发送</button>
      </div>
    </div>`;

  const box = $("#chat-box"), input = $("#chat-input");
  const addMsg = (role, text, meta) => {
    const div = document.createElement("div");
    div.className = `msg ${role}`;
    div.textContent = text;
    if (meta) {
      const m = document.createElement("span");
      m.className = "msg-meta"; m.textContent = meta;
      div.appendChild(m);
    }
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    return div;
  };
  const send = async (text) => {
    if (!text.trim()) return;
    addMsg("user", text);
    input.value = "";
    const thinking = addMsg("bot", "思考中…");
    try {
      const history = S.aiHistory.slice(-6);
      const result = await api("/api/ai/chat", { method: "POST",
        body: { message: text, history } });
      thinking.textContent = result.reply;
      const meta = document.createElement("span");
      meta.className = "msg-meta";
      meta.textContent = result.mode === "llm"
        ? `LLM · ${result.actions.length} 个动作已执行` : "本地规则模式";
      thinking.appendChild(meta);
      S.aiHistory.push({ role: "user", content: text },
                       { role: "assistant", content: result.reply });
    } catch (e) {
      thinking.textContent = `出错了：${e.message}`;
    }
    box.scrollTop = box.scrollHeight;
  };
  $("#chat-send").addEventListener("click", () => send(input.value));
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") send(input.value); });
  view.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => send(chip.textContent)));
  input.focus();
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  try {
    S.health = await api("/api/health");
    const mqttBadge = $("#badge-mqtt"), aiBadge = $("#badge-ai");
    mqttBadge.classList.toggle("on", S.health.mqtt);
    mqttBadge.title = S.health.mqtt ? "MQTT 通道已启用" : "MQTT 未配置（HTTP 通道可用）";
    aiBadge.classList.toggle("on", S.health.ai);
    aiBadge.title = S.health.ai ? "AI 大模型已连接" : "AI 本地规则模式";
  } catch (e) { /* 服务未就绪时静默 */ }
  parseHash();
  connectWS();
  // WebSocket 不可用时退化为 3 秒轮询
  setInterval(() => { if (!S.wsOk) render(true); }, 3000);
  window.addEventListener("resize", debounce(() => {
    if (S.route === "overview") paintCharts();
    if (S.route === "device") render(true);
  }, 200));
}
boot();
