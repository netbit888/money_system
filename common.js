/* ===================== 经济系统公共脚本 ===================== */

// ===================== 全局状态（前端缓存）=====================
let defaultSettings = {
  metabolism: { res: "食物", amount: 1 },
  income: { res: "钱", amount: 1 },
  canReproduce: true,
  attrs: {},
  needs: [],
  rules: []
};
let persons = [];
let selectedId = null;
let round = 0;
let lastSummary = { round: 0, total: 0, groups: {} };

// 历史曲线数据
let historyData = [];

// 操作互斥锁（防止连点导致数据竞态）
let _busy = false;

// ===================== API 调用封装 =====================
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* 无 JSON 体 */ }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}

// ===================== 工具函数 =====================
function fmtNum(v) {
  const n = Number(v);
  if (!isFinite(n)) return "0";
  const abs = Math.abs(n);
  // 中式单位：万=1e4, 亿=1e8, 兆=1e12
  if (abs >= 1e12) return (n / 1e12).toFixed(2) + "兆";
  if (abs >= 1e8)  return (n / 1e8).toFixed(2) + "亿";
  if (abs >= 1e4)  return (n / 1e4).toFixed(2) + "万";
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(2);
}

function getBaseName(name) {
  const idx = name.indexOf("#");
  return idx > 0 ? name.substring(0, idx) : name;
}

function normMetabolism(m, fbRes) {
  const fb = (typeof fbRes === "string" && fbRes.trim()) ? fbRes.trim() : "食物";
  if (m && typeof m === "object") {
    return { res: String(m.res == null ? "" : m.res).trim() || fb, amount: Math.max(0, Number(m.amount) || 0) };
  }
  if (typeof m === "string" && m.trim()) return { res: m.trim(), amount: 1 };
  return { res: fb, amount: 1 };
}

function normAsk(r) {
  let sell = "", buy = "", rate = 0;
  if (r && typeof r === "object") {
    if (r.sell !== undefined && r.buy !== undefined) { sell = r.sell; buy = r.buy; rate = r.rate; }
    else if (r.res1 !== undefined && r.res2 !== undefined) { sell = r.res1; buy = r.res2; rate = r.rate; }
    else if (r.from !== undefined && r.to !== undefined) { sell = r.from; buy = r.to; rate = r.rate; }
  }
  return { sell: String(sell || "").trim(), buy: String(buy || "").trim(), rate: Number(rate) || 0 };
}

function normalizePerson(p) {
  // 后端返回的 person 已经是规范结构，这里仅做兜底
  return {
    id: p.id,
    parentId: p.parentId ?? null,
    name: p.name || "未命名",
    metabolism: normMetabolism(p.metabolism, "食物"),
    income: normMetabolism(p.income, "钱"),
    canReproduce: p.canReproduce !== false,
    reproThresholdMult: p.reproThresholdMult ?? 2.0,
    reproInheritMult: p.reproInheritMult ?? 1.0,
    attrs: p.attrs || {},
    needs: (p.needs || []).map(n => ({ key: n.key, amount: n.amount })),
    rules: (p.rules || []).map(r => normAsk(r))
  };
}

function normalizeDefaults(d) {
  return {
    metabolism: normMetabolism(d.metabolism, "食物"),
    income: normMetabolism(d.income, "钱"),
    canReproduce: d.canReproduce !== false,
    reproThresholdMult: d.reproThresholdMult ?? 2.0,
    reproInheritMult: d.reproInheritMult ?? 1.0,
    attrs: d.attrs || {},
    needs: (d.needs || []).map(n => ({ key: n.key, amount: n.amount })),
    rules: (d.rules || []).map(r => normAsk(r)),
    perishable_resources: (d.perishable_resources || []).map(r => String(r))
  };
}

// ===================== 操作互斥锁 =====================
function _setBusy(b) {
  _busy = b;
  document.querySelectorAll('button[data-lock]').forEach(btn => btn.disabled = b);
}

// ===================== 显示切换（两页共用元素） =====================
function toggleLog() {
  const logEl = document.getElementById("log");
  const btn = document.getElementById("logToggleBtn");
  if (!logEl || !btn) return;
  if (logEl.style.display === "none") { logEl.style.display = ""; btn.textContent = "隐藏日志"; }
  else { logEl.style.display = "none"; btn.textContent = "显示日志"; }
}

function togglePieChart() {
  const wrap = document.getElementById("pieChartWrap");
  const btn = document.getElementById("pieToggleBtn");
  if (!wrap || !btn) return;
  if (wrap.style.display === "none") { wrap.style.display = ""; btn.textContent = "隐藏扇形图"; }
  else { wrap.style.display = "none"; btn.textContent = "显示扇形图"; }
}

function toggleHistoryChart() {
  const wrap = document.getElementById("historyChartWrap");
  const btn = document.getElementById("historyToggleBtn");
  if (!wrap || !btn) return;
  if (wrap.style.display === "none") {
    wrap.style.display = "";
    btn.textContent = "隐藏历史曲线";
    fetchHistoryAndRender();
  } else {
    wrap.style.display = "none";
    btn.textContent = "显示历史曲线";
  }
}

// ===================== 历史曲线（SVG 折线图）=====================
async function fetchHistoryAndRender() {
  try {
    const r = await fetch('/history?from=0');
    if (!r.ok) throw new Error('获取历史失败');
    const j = await r.json();
    historyData = j.history || [];
    renderHistoryChart();
  } catch (e) {
    const box = document.getElementById("historyChart");
    if (box) box.innerHTML = `<i style="color:#c00;">${e.message}</i>`;
  }
}

function renderHistoryChart() {
  const box = document.getElementById("historyChart");
  if (!box) return;
  if (!historyData.length) {
    box.innerHTML = "<i>无历史数据，先跑几个回合</i>";
    return;
  }

  const showTotalEl = document.getElementById("histShowTotal");
  const showGroupsEl = document.getElementById("histShowGroups");
  const showResourcesEl = document.getElementById("histShowResources");
  const showTotal = showTotalEl ? showTotalEl.checked : true;
  const showGroups = showGroupsEl ? showGroupsEl.checked : false;
  const showResources = showResourcesEl ? showResourcesEl.checked : false;

  // 收集所有曲线
  const series = [];
  if (showTotal) {
    series.push({ name: "总人口", data: historyData.map(h => h.total), color: "#2c3e50", dashed: false });
  }
  if (showGroups) {
    const groupColors = ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c", "#e67e22", "#34495e"];
    const groupNames = new Set();
    historyData.forEach(h => Object.keys(h.groups || {}).forEach(g => groupNames.add(g)));
    let ci = 0;
    for (const g of groupNames) {
      series.push({
        name: g,
        data: historyData.map(h => (h.groups || {})[g] || 0),
        color: groupColors[ci % groupColors.length],
        dashed: false,
      });
      ci++;
    }
  }
  if (showResources) {
    const resColors = ["#8e44ad", "#16a085", "#d35400", "#27ae60", "#c0392b", "#2980b9", "#f1c40f"];
    const resNames = new Set();
    historyData.forEach(h => Object.keys(h.resource_totals || {}).forEach(r => resNames.add(r)));
    let ci = 0;
    for (const r of resNames) {
      series.push({
        name: r,
        data: historyData.map(h => (h.resource_totals || {})[r] || 0),
        color: resColors[ci % resColors.length],
        dashed: true,
      });
      ci++;
    }
  }

  if (!series.length) {
    box.innerHTML = "<i>请至少勾选一项</i>";
    return;
  }

  // 计算 Y 轴范围
  let yMin = Infinity, yMax = -Infinity;
  for (const s of series) {
    for (const v of s.data) {
      if (v < yMin) yMin = v;
      if (v > yMax) yMax = v;
    }
  }
  if (yMin === Infinity) { yMin = 0; yMax = 1; }
  if (yMin > 0) yMin = 0;
  if (yMax === yMin) yMax = yMin + 1;

  const W = 700, H = 280, padL = 50, padR = 10, padT = 10, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const n = historyData.length;
  const xScale = (i) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yScale = (v) => padT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  // Y 轴刻度（4 格）
  const yTicks = [];
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * i / 4;
    yTicks.push({ v, y: yScale(v) });
  }

  // X 轴刻度（最多 8 个）
  const xStep = Math.max(1, Math.floor(n / 8));
  const xTicks = [];
  for (let i = 0; i < n; i += xStep) {
    xTicks.push({ i, label: historyData[i].round, x: xScale(i) });
  }

  // 构建 SVG
  let svg = `<svg width="${W}" height="${H}" style="background:#fafafa; font-family:monospace; font-size:10px;">`;

  // Y 网格 + 刻度
  for (const t of yTicks) {
    svg += `<line x1="${padL}" y1="${t.y}" x2="${W - padR}" y2="${t.y}" stroke="#eee" stroke-width="1"/>`;
    svg += `<text x="${padL - 4}" y="${t.y + 3}" text-anchor="end" fill="#666">${fmtNum(t.v)}</text>`;
  }

  // X 刻度
  for (const t of xTicks) {
    svg += `<line x1="${t.x}" y1="${padT + plotH}" x2="${t.x}" y2="${padT + plotH + 4}" stroke="#999"/>`;
    svg += `<text x="${t.x}" y="${padT + plotH + 16}" text-anchor="middle" fill="#666">${t.label}</text>`;
  }
  svg += `<text x="${W / 2}" y="${H - 2}" text-anchor="middle" fill="#666">回合</text>`;

  // 曲线
  for (const s of series) {
    let path = "";
    s.data.forEach((v, i) => {
      const x = xScale(i), y = yScale(v);
      path += (i === 0 ? "M" : " L") + x + "," + y;
    });
    const dash = s.dashed ? ' stroke-dasharray="4,2"' : '';
    svg += `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="1.8"${dash}/>`;
  }

  // 图例
  let lx = padL, ly = padT + 2;
  for (const s of series) {
    const dash = s.dashed ? ' stroke-dasharray="4,2"' : '';
    svg += `<line x1="${lx}" y1="${ly + 4}" x2="${lx + 16}" y2="${ly + 4}" stroke="${s.color}" stroke-width="2"${dash}/>`;
    svg += `<text x="${lx + 20}" y="${ly + 8}" fill="${s.color}">${s.name}</text>`;
    lx += s.name.length * 8 + 50;
    if (lx > W - 80) { lx = padL; ly += 14; }
  }

  svg += "</svg>";
  box.innerHTML = svg;
}

async function clearHistory() {
  if (!confirm("确定清空所有历史数据？")) return;
  try {
    await fetch('/clear-history', { method: 'POST' });
    historyData = [];
    renderHistoryChart();
  } catch (e) {
    alert(e.message);
  }
}

function _refreshHistoryIfVisible() {
  const wrap = document.getElementById("historyChartWrap");
  if (wrap && wrap.style.display !== "none") {
    fetchHistoryAndRender();
  }
}

// ===================== 扇形图（用后端 summary.groups）=====================
function renderPieChart() {
  const box = document.getElementById("pieChart");
  if (!box) return;
  const groups = lastSummary.groups || {};
  const entries = Object.entries(groups).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, e) => s + e[1], 0);
  if (total === 0) { box.innerHTML = "<i>无个体</i>"; return; }
  const colors = ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c", "#e67e22", "#34495e"];
  const cx = 80, cy = 80, r = 65;
  let startAngle = -Math.PI / 2;
  let paths = "", legend = "";
  entries.forEach((e, i) => {
    const [name, count] = e;
    const pct = count / total;
    const endAngle = startAngle + pct * Math.PI * 2;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const large = (endAngle - startAngle) > Math.PI ? 1 : 0;
    const color = colors[i % colors.length];
    if (pct >= 1) {
      paths += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" stroke="#fff" stroke-width="1"/>`;
    } else {
      paths += `<path d="M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} Z" fill="${color}" stroke="#fff" stroke-width="1"/>`;
    }
    legend += `<div style="display:flex;align-items:center;margin:2px 0;">
      <span style="display:inline-block;width:12px;height:12px;background:${color};margin-right:6px;border-radius:2px;"></span>
      <span style="font-size:13px;">${name}：${count}（${(pct * 100).toFixed(0)}%）</span>
    </div>`;
    startAngle = endAngle;
  });
  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
      <svg width="160" height="160" viewBox="0 0 160 160">${paths}<text x="${cx}" y="${cy + 5}" text-anchor="middle" font-size="16" font-weight="bold" fill="#333">${total}</text></svg>
      <div style="flex:1;min-width:120px;">${legend}</div>
    </div>
  `;
}

// ===================== 政府面板 =====================
function _fmtGovDict(d) {
  if (!d || Object.keys(d).length === 0) return "—";
  return Object.entries(d).map(([k, v]) => `${k}: ${fmtNum(v)}`).join("，");
}

function renderGovernment(gov) {
  if (!gov) return;
  const rateInput = document.getElementById("govTaxRate");
  if (rateInput && document.activeElement !== rateInput) {
    rateInput.value = gov.tax_rate;
  }
  const tr = document.getElementById("govTreasury");
  if (tr) tr.textContent = _fmtGovDict(gov.treasury);
  const co = document.getElementById("govCollected");
  if (co) co.textContent = _fmtGovDict(gov.total_collected);
}

// ===================== 政府税率应用 =====================
async function applyTaxRate() {
  if (_busy) return;
  const rate = parseFloat(document.getElementById("govTaxRate").value);
  if (isNaN(rate) || rate < 0 || rate > 1) {
    alert("税率必须在 0~1 之间");
    return;
  }
  _setBusy(true);
  try {
    const r = await api("PUT", "/government", { tax_rate: rate });
    renderGovernment(r.government);
  } catch (e) {
    alert("设置税率失败：" + e.message);
  } finally {
    _setBusy(false);
  }
}

// ===================== 回合 / 交换计算 =====================
// 注：renderPersonList / renderDetail 由管理页提供；首页无此函数时通过 typeof 检查跳过
//     renderPieChart 在两页均存在（已做 null 检查）
async function nextRound() {
  if (_busy) return;
  if (persons.length === 0) { alert("请先创建个体"); return; }
  _setBusy(true);
  try {
    const r = await api("POST", "/next-round");
    persons = r.persons.map(normalizePerson);
    round = r.summary.round;
    lastSummary = r.summary;
    const rn = document.getElementById("roundNum");
    if (rn) rn.textContent = round;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    if (typeof renderPersonList === 'function') renderPersonList();  // 内部会调 renderPieChart
    else renderPieChart();
    if (typeof renderDetail === 'function' && selectedId !== null) renderDetail();
    _refreshHistoryIfVisible();
  } catch (e) { alert("下一回合失败：" + e.message); }
  finally { _setBusy(false); }
}

async function calculate() {
  if (_busy) return;
  if (persons.length === 0) { alert("请先创建个体"); return; }
  _setBusy(true);
  try {
    const r = await api("POST", "/calculate");
    persons = r.persons.map(normalizePerson);
    lastSummary = r.summary;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    if (typeof renderPersonList === 'function') renderPersonList();
    else renderPieChart();
    if (typeof renderDetail === 'function' && selectedId !== null) renderDetail();
  } catch (e) { alert("交换计算失败：" + e.message); }
  finally { _setBusy(false); }
}

async function nextRoundAndCalculate() {
  if (_busy) return;
  if (persons.length === 0) { alert("请先创建个体"); return; }
  _setBusy(true);
  try {
    const r = await api("POST", "/next-and-calc");
    persons = r.persons.map(normalizePerson);
    round = r.summary.round;
    lastSummary = r.summary;
    const rn = document.getElementById("roundNum");
    if (rn) rn.textContent = round;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    if (typeof renderPersonList === 'function') renderPersonList();
    else renderPieChart();
    if (typeof renderDetail === 'function' && selectedId !== null) renderDetail();
    _refreshHistoryIfVisible();
  } catch (e) { alert("操作失败：" + e.message); }
  finally { _setBusy(false); }
}

async function resetRound() {
  if (_busy) return;
  if (!confirm("确定要重置回合吗？\n\n回合数将归零，并从 config 文件夹重新加载基础设置和个体列表。\n\n⚠️ 当前所有修改都会被覆盖。")) return;
  _setBusy(true);
  try {
    const r = await api("POST", "/reset");
    defaultSettings = normalizeDefaults(r.defaults);
    persons = r.persons.map(normalizePerson);
    round = r.summary.round;
    lastSummary = r.summary;
    selectedId = null;
    const detailPanel = document.getElementById("detailPanel");
    if (detailPanel) detailPanel.style.display = "none";
    const rn = document.getElementById("roundNum");
    if (rn) rn.textContent = round;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    if (typeof renderAll === 'function') renderAll();
    else renderPieChart();
    _refreshHistoryIfVisible();
  } catch (e) { alert("重置失败：" + e.message); }
  finally { _setBusy(false); }
}

// ===================== 启动：拉取后端状态 =====================
// 各页可通过定义自己的 renderAll() 来扩展渲染逻辑（管理页实现个体列表/详情渲染）
async function loadState() {
  const s = await api("GET", "/state");
  defaultSettings = normalizeDefaults(s.defaults);
  persons = s.persons.map(normalizePerson);
  round = s.round;
  lastSummary = s.summary;
  const rn = document.getElementById("roundNum");
  if (rn) rn.textContent = round;
  renderGovernment(s.government);
  if (typeof renderAll === 'function') renderAll();
  else renderPieChart();
}
