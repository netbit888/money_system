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
// persons 只缓存「当前页」个体（分页/搜索由服务端完成），总量看 personsTotal
let persons = [];
let personsTotal = 0;
let selectedId = null;
// 选中个体（详情面板数据源）与其母体名：按需从 GET /person/{id} 拉取
let selectedPerson = null;
let selectedParentName = null;
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
    reproInheritRatio: p.reproInheritRatio ?? 0.5,
    attrs: p.attrs || {},
    needs: (p.needs || []).map(n => ({ key: n.key, amount: n.amount })),
    rules: (p.rules || []).map(r => normAsk(r)),
    birthRound: p.birthRound ?? null,
    dependent: p.dependent !== false,
    // null = 继承基础设置里的全局养育期
    weanMinRounds: (p.weanMinRounds === null || p.weanMinRounds === undefined) ? null : p.weanMinRounds,
    weanMaxRounds: (p.weanMaxRounds === null || p.weanMaxRounds === undefined) ? null : p.weanMaxRounds
  };
}

function normalizeDefaults(d) {
  return {
    metabolism: normMetabolism(d.metabolism, "食物"),
    income: normMetabolism(d.income, "钱"),
    canReproduce: d.canReproduce !== false,
    reproThresholdMult: d.reproThresholdMult ?? 2.0,
    reproInheritMult: d.reproInheritMult ?? 1.0,
    reproInheritRatio: d.reproInheritRatio ?? 0.5,
    weanMinRounds: d.weanMinRounds ?? 3,
    weanMaxRounds: d.weanMaxRounds ?? 8,
    attrs: d.attrs || {},
    needs: (d.needs || []).map(n => ({ key: n.key, amount: n.amount })),
    rules: (d.rules || []).map(r => normAsk(r)),
    perishable_resources: (d.perishable_resources || []).map(r => String(r)),
    adaptive_pricing: d.adaptive_pricing === true,
    price_adjust_alpha: Number(d.price_adjust_alpha) || 0.1
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

function toggleEconChart() {
  const wrap = document.getElementById("econChartWrap");
  const btn = document.getElementById("econToggleBtn");
  if (!wrap || !btn) return;
  if (wrap.style.display === "none") {
    wrap.style.display = "";
    btn.textContent = "隐藏经济指标";
  } else {
    wrap.style.display = "none";
    btn.textContent = "显示经济指标";
  }
  fetchHistoryAndRender();
}

// ===================== 历史曲线（SVG 折线图）=====================
// 可滑动时间窗口：只拉取 [end-window, end] 区间，避免回合数暴涨后全量绘制。
let histWindow = 100;        // 窗口大小（回合）
let histFollowLatest = true; // 跟随最新：滑块贴最右，自动跑时随回合滚动

function _historyWrapVisible() {
  const wrap = document.getElementById("historyChartWrap");
  return wrap && wrap.style.display !== "none";
}

function _econWrapVisible() {
  const wrap = document.getElementById("econChartWrap");
  return wrap && wrap.style.display !== "none";
}

function updateHistRangeLabel(start, end) {
  const el = document.getElementById("histRangeLabel");
  if (el) el.textContent = `显示回合 ${start} ~ ${end}（共 ${historyData.length} 点）`;
}

function onHistWindowChange() {
  const sel = document.getElementById("histWindow");
  histWindow = parseInt(sel.value) || 100;
  fetchHistoryAndRender();
}

function onHistFollowChange() {
  const cb = document.getElementById("histFollow");
  histFollowLatest = cb.checked;
  if (histFollowLatest) {
    const slider = document.getElementById("histPos");
    if (slider) slider.value = round;
  }
  fetchHistoryAndRender();
}

function onHistSlider() {
  // 用户拖动滑块 = 主动离开"跟随最新"，锁定到所选历史区间
  histFollowLatest = false;
  const cb = document.getElementById("histFollow");
  if (cb) cb.checked = false;
  fetchHistoryAndRender();
}

async function fetchHistoryAndRender() {
  if (!_historyWrapVisible() && !_econWrapVisible()) return;
  try {
    const slider = document.getElementById("histPos");
    if (slider) {
      slider.max = Math.max(round, 1);
      if (histFollowLatest) {
        slider.value = round;
      } else if (parseInt(slider.value) > round) {
        slider.value = round;
      }
    }
    const end = slider ? parseInt(slider.value) : round;
    const start = Math.max(0, end - histWindow);
    const r = await fetch(`/history?from=${start}&to=${end}`);
    if (!r.ok) throw new Error('获取历史失败');
    const j = await r.json();
    historyData = j.history || [];
    if (_historyWrapVisible()) renderHistoryChart();
    if (_econWrapVisible()) renderEconChart();
    updateHistRangeLabel(start, end);
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
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="background:#fafafa; max-width:${W}px; font-family:inherit; font-size:10px;">`;

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

// ===================== 经济指标图（双 Y 轴）=====================
// 左轴：0~1 指标（基尼/HHI/失业率/满足率）；右轴：价格指数（首非空点=100 归一）。
function renderEconChart() {
  const box = document.getElementById("econChart");
  if (!box) return;
  if (!historyData.length) { box.innerHTML = ""; return; }

  const opt = {
    price_index: document.getElementById("ecPriceIndex")?.checked,
    gini: document.getElementById("ecGini")?.checked,
    hhi_wealth: document.getElementById("ecHhiWealth")?.checked,
    hhi_pop: document.getElementById("ecHhiPop")?.checked,
    unemployment: document.getElementById("ecUnemployment")?.checked,
    met_rate: document.getElementById("ecMetRate")?.checked,
  };

  const n = historyData.length;

  // 价格指数归一：首个非空点 = 100
  let base = null;
  for (const h of historyData) {
    const v = h.metrics ? h.metrics.price_index : null;
    if (base === null && v != null && v > 0) { base = v; break; }
  }
  const series = [];
  if (opt.price_index) {
    const data = historyData.map(h => {
      const v = h.metrics ? h.metrics.price_index : null;
      return (v == null || base == null) ? null : v / base * 100;
    });
    series.push({ name: "价格指数", data, color: "#c0392b", axis: "right" });
  }
  function addRatio(key, name, color) {
    if (!opt[key]) return;
    series.push({
      name, color, axis: "left",
      data: historyData.map(h => (h.metrics ? h.metrics[key] : null)),
    });
  }
  addRatio("gini", "基尼系数", "#2980b9");
  addRatio("hhi_wealth", "财富HHI", "#8e44ad");
  addRatio("hhi_pop", "人口HHI", "#16a085");
  addRatio("unemployment", "失业率", "#d35400");
  addRatio("met_rate", "满足率", "#27ae60");

  if (!series.length) { box.innerHTML = "<i>勾选上方指标以查看</i>"; return; }

  // 取值范围
  const leftVals = [], rightVals = [];
  series.forEach(s => s.data.forEach(v => { if (v != null) (s.axis === "right" ? rightVals : leftVals).push(v); }));
  const lMin = Math.min(0, ...(leftVals.length ? leftVals : [0]));
  const lMaxRaw = leftVals.length ? Math.max(...leftVals) : 1;
  const lMax = lMaxRaw <= lMin ? lMin + 1 : lMaxRaw;
  const rMin = rightVals.length ? Math.min(...rightVals) : 0;
  const rMaxRaw = rightVals.length ? Math.max(...rightVals) : 1;
  const rMax = rMaxRaw <= rMin ? rMin + 1 : rMaxRaw;

  const W = 700, H = 280, padL = 50, padR = 50, padT = 10, padB = 30;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const xScale = (i) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yLeft = (v) => padT + plotH - ((v - lMin) / (lMax - lMin)) * plotH;
  const yRight = (v) => padT + plotH - ((v - rMin) / (rMax - rMin)) * plotH;

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="background:#fafafa; max-width:${W}px; font-family:inherit; font-size:10px;">`;

  // 左轴刻度（4 格）
  for (let k = 0; k <= 4; k++) {
    const v = lMin + (lMax - lMin) * k / 4;
    const y = yLeft(v);
    svg += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#eee"/>`;
    svg += `<text x="${padL - 4}" y="${y + 3}" text-anchor="end" fill="#3498db">${fmtNum(v)}</text>`;
  }
  // 右轴刻度（价格指数）
  for (let k = 0; k <= 4; k++) {
    const v = rMin + (rMax - rMin) * k / 4;
    const y = yRight(v);
    svg += `<text x="${W - padR + 4}" y="${y + 3}" text-anchor="start" fill="#c0392b">${fmtNum(v)}</text>`;
  }

  // X 刻度
  const xStep = Math.max(1, Math.floor(n / 8));
  for (let i = 0; i < n; i += xStep) {
    svg += `<line x1="${xScale(i)}" y1="${padT + plotH}" x2="${xScale(i)}" y2="${padT + plotH + 4}" stroke="#999"/>`;
    svg += `<text x="${xScale(i)}" y="${padT + plotH + 16}" text-anchor="middle" fill="#666">${historyData[i].round}</text>`;
  }
  svg += `<text x="${W / 2}" y="${H - 2}" text-anchor="middle" fill="#666">回合</text>`;
  svg += `<text x="${padL - 4}" y="${padT - 1}" text-anchor="end" fill="#3498db">0~1</text>`;
  svg += `<text x="${W - padR + 4}" y="${padT - 1}" text-anchor="start" fill="#c0392b">指数</text>`;

  // 曲线
  for (const s of series) {
    let path = "";
    let started = false;
    s.data.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const x = xScale(i), y = (s.axis === "right" ? yRight(v) : yLeft(v));
      path += (started ? " L" : "M") + x + "," + y;
      started = true;
    });
    svg += `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="1.8"/>`;
  }

  // 图例（含最新实际值）
  let lx = padL, ly = padT + 2;
  for (const s of series) {
    let last = null;
    for (let i = n - 1; i >= 0; i--) { if (s.data[i] != null) { last = s.data[i]; break; } }
    const label = s.name + (last != null ? ` ${fmtNum(last)}` : "");
    svg += `<line x1="${lx}" y1="${ly + 4}" x2="${lx + 16}" y2="${ly + 4}" stroke="${s.color}" stroke-width="2"/>`;
    svg += `<text x="${lx + 20}" y="${ly + 8}" fill="${s.color}">${label}</text>`;
    lx += label.length * 8 + 24;
    if (lx > W - 60) { lx = padL; ly += 14; }
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
  if (_historyWrapVisible() || _econWrapVisible()) {
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
// 注：renderPersonList / renderDetail / refreshSelected 由管理页提供；
//     首页无此函数时通过 typeof 检查跳过。renderPieChart 在两页均存在（已做 null 检查）

// 回合响应不再携带全量 persons，这里按「当前可见面板」补拉：
//   管理页 → 当前页个体 + 选中个体；首页 → 只刷新饼图（不发额外请求）
async function refreshPersonViews() {
  if (document.getElementById("personList")) {
    if (typeof renderPersonList === 'function') await renderPersonList();
  } else {
    renderPieChart();
  }
  const panel = document.getElementById("detailPanel");
  if (panel && panel.style.display !== "none" && typeof refreshSelected === 'function') {
    await refreshSelected();
  }
}

async function nextRound() {
  if (_busy) return;
  if ((lastSummary.total || 0) === 0) { alert("请先创建个体"); return; }
  _setBusy(true);
  try {
    const r = await api("POST", "/next-round");
    personsTotal = (r.summary && r.summary.total) || 0;
    round = r.summary.round;
    lastSummary = r.summary;
    const rn = document.getElementById("roundNum");
    if (rn) rn.textContent = round;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    await refreshPersonViews();
    _refreshHistoryIfVisible();
  } catch (e) { alert("下一回合失败：" + e.message); if (_autoRunning) stopAuto(); }
  finally { _setBusy(false); }
}

// ===================== 自动运行（回合模块，前端驱动）=====================
// 播放/暂停切换；每分钟回合数由 autoRate 输入控制，间隔 = 60000 / 速率。
// 串行执行：等上一拍 nextRound 真正完成后，再排下一拍（递归 setTimeout，杜绝重叠）。
let _autoRunning = false;
let _autoTimer = null;

function toggleAuto() {
  if (_autoRunning) stopAuto();
  else startAuto();
}

function updateAutoBtn() {
  const btn = document.getElementById("autoBtn");
  if (!btn) return;
  if (_autoRunning) {
    btn.textContent = "⏸ 暂停";
    btn.style.background = "#e74c3c";
  } else {
    btn.textContent = "▶ 自动运行";
    btn.style.background = "#5cb85c";
  }
}

function startAuto() {
  if ((lastSummary.total || 0) === 0) { alert("请先创建个体，再开始自动运行"); return; }
  _autoRunning = true;
  updateAutoBtn();
  autoStep();
}

function stopAuto() {
  _autoRunning = false;
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  updateAutoBtn();
}

function autoStep() {
  if (!_autoRunning) return;
  nextRound().then(() => {
    if (!_autoRunning) return;   // 执行期间已被暂停
    let rate = parseFloat(document.getElementById("autoRate")?.value);
    if (!isFinite(rate) || rate <= 0) rate = 60;
    const interval = 60000 / rate;   // 实际速率受 nextRound 处理耗时限制
    _autoTimer = setTimeout(autoStep, interval);
  });
}

async function calculate() {
  if (_busy) return;
  if ((lastSummary.total || 0) === 0) { alert("请先创建个体"); return; }
  _setBusy(true);
  try {
    const r = await api("POST", "/calculate");
    personsTotal = (r.summary && r.summary.total) || 0;
    lastSummary = r.summary;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    await refreshPersonViews();
  } catch (e) { alert("交换计算失败：" + e.message); }
  finally { _setBusy(false); }
}

async function nextRoundAndCalculate() {
  if (_busy) return;
  if ((lastSummary.total || 0) === 0) { alert("请先创建个体"); return; }
  _setBusy(true);
  try {
    const r = await api("POST", "/next-and-calc");
    personsTotal = (r.summary && r.summary.total) || 0;
    round = r.summary.round;
    lastSummary = r.summary;
    const rn = document.getElementById("roundNum");
    if (rn) rn.textContent = round;
    const log = document.getElementById("log");
    if (log) log.textContent = r.log;
    renderGovernment(r.government);
    await refreshPersonViews();
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
    persons = [];
    personsTotal = (r.summary && r.summary.total) || 0;
    round = r.summary.round;
    lastSummary = r.summary;
    selectedId = null;
    selectedPerson = null;
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
  persons = [];
  personsTotal = (s.summary && s.summary.total) || 0;
  round = s.round;
  lastSummary = s.summary;
  const rn = document.getElementById("roundNum");
  if (rn) rn.textContent = round;
  renderGovernment(s.government);
  if (typeof renderAll === 'function') renderAll();
  else renderPieChart();
}
