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
    price_adjust_alpha: Number(d.price_adjust_alpha) || 0.1,
    max_population: d.max_population ?? 300000
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

// ===================== ECharts 实例管理 =====================
function _echart(elId) {
  const el = document.getElementById(elId);
  if (!el || typeof echarts === "undefined") return null;
  let c = echarts.getInstanceByDom(el);
  if (!c) c = echarts.init(el);
  c.resize();
  return c;
}

function _echartDispose(elId) {
  if (typeof echarts === "undefined") return;
  const el = document.getElementById(elId);
  if (!el) return;
  const c = echarts.getInstanceByDom(el);
  if (c) c.dispose();
}

function renderHistoryChart() {
  const box = document.getElementById("historyChart");
  if (!box) return;
  if (typeof echarts === "undefined") { box.innerHTML = "<i>图表库未加载</i>"; return; }
  if (!historyData.length) {
    _echartDispose("historyChart");
    box.innerHTML = "<i>无历史数据，先跑几个回合</i>";
    return;
  }

  const showTotal = document.getElementById("histShowTotal") ? document.getElementById("histShowTotal").checked : true;
  const showGroups = document.getElementById("histShowGroups") ? document.getElementById("histShowGroups").checked : false;
  const showResources = document.getElementById("histShowResources") ? document.getElementById("histShowResources").checked : false;

  const xData = historyData.map(h => h.round);
  const series = [];
  if (showTotal) {
    series.push({ name: "总人口", type: "line", smooth: true, lineStyle: { width: 2 }, data: historyData.map(h => h.total) });
  }
  if (showGroups) {
    const groupNames = new Set();
    historyData.forEach(h => Object.keys(h.groups || {}).forEach(g => groupNames.add(g)));
    for (const g of groupNames) {
      series.push({ name: g, type: "line", smooth: true, data: historyData.map(h => (h.groups || {})[g] || 0) });
    }
  }
  if (showResources) {
    const resNames = new Set();
    historyData.forEach(h => Object.keys(h.resource_totals || {}).forEach(r => resNames.add(r)));
    for (const r of resNames) {
      series.push({ name: r, type: "line", yAxisIndex: 1, lineStyle: { type: "dashed" }, data: historyData.map(h => (h.resource_totals || {})[r] || 0) });
    }
  }

  if (!series.length) {
    _echartDispose("historyChart");
    box.innerHTML = "<i>请至少勾选一项</i>";
    return;
  }

  _echart("historyChart").setOption({
    color: ["#2c3e50", "#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c", "#e67e22", "#8e44ad", "#16a085", "#d35400", "#27ae60", "#c0392b", "#2980b9", "#f1c40f"],
    tooltip: { trigger: "axis" },
    legend: { type: "scroll", top: 0, textStyle: { fontSize: 12 } },
    grid: { left: 60, right: 16, top: 32, bottom: 56 },
    xAxis: { type: "category", data: xData, name: "回合" },
    yAxis: [
      { type: "value", name: "个体数" },
      { type: "value", name: "资源量" },
    ],
    dataZoom: [
      { type: "inside", start: 0, end: 100 },
      { type: "slider", start: 0, end: 100, height: 16, bottom: 8 },
    ],
    series,
  }, true);
}

// ===================== 经济指标图（ECharts 双 Y 轴）=====================
// 左轴：0~1 指标（基尼/HHI/失业率/满足率）；右轴：价格指数（首非空点=100 归一）。
function renderEconChart() {
  const box = document.getElementById("econChart");
  if (!box) return;
  if (typeof echarts === "undefined") { box.innerHTML = "<i>图表库未加载</i>"; return; }
  if (!historyData.length) {
    _echartDispose("econChart");
    box.innerHTML = "";
    return;
  }

  const opt = {
    price_index: document.getElementById("ecPriceIndex")?.checked,
    gini: document.getElementById("ecGini")?.checked,
    hhi_wealth: document.getElementById("ecHhiWealth")?.checked,
    hhi_pop: document.getElementById("ecHhiPop")?.checked,
    unemployment: document.getElementById("ecUnemployment")?.checked,
    met_rate: document.getElementById("ecMetRate")?.checked,
  };

  const xData = historyData.map(h => h.round);

  // 价格指数归一：首个非空点 = 100
  let base = null;
  for (const h of historyData) {
    const v = h.metrics ? h.metrics.price_index : null;
    if (base === null && v != null && v > 0) { base = v; break; }
  }

  const series = [];
  if (opt.price_index) {
    series.push({
      name: "价格指数", type: "line", yAxisIndex: 1,
      data: historyData.map(h => {
        const v = h.metrics ? h.metrics.price_index : null;
        return (v == null || base == null) ? null : +(v / base * 100).toFixed(2);
      }),
    });
  }
  function addRatio(key, name) {
    if (!opt[key]) return;
    series.push({ name, type: "line", yAxisIndex: 0, data: historyData.map(h => (h.metrics ? h.metrics[key] : null)) });
  }
  addRatio("gini", "基尼系数");
  addRatio("hhi_wealth", "财富HHI");
  addRatio("hhi_pop", "人口HHI");
  addRatio("unemployment", "失业率");
  addRatio("met_rate", "满足率");

  if (!series.length) {
    _echartDispose("econChart");
    box.innerHTML = "<i>勾选上方指标以查看</i>";
    return;
  }

  _echart("econChart").setOption({
    color: ["#c0392b", "#2980b9", "#8e44ad", "#16a085", "#d35400", "#27ae60"],
    tooltip: { trigger: "axis" },
    legend: { type: "scroll", top: 0, textStyle: { fontSize: 12 } },
    grid: { left: 60, right: 60, top: 32, bottom: 56 },
    xAxis: { type: "category", data: xData, name: "回合" },
    yAxis: [
      { type: "value", name: "0~1", min: 0 },
      { type: "value", name: "指数" },
    ],
    dataZoom: [
      { type: "inside", start: 0, end: 100 },
      { type: "slider", start: 0, end: 100, height: 16, bottom: 8 },
    ],
    series,
  }, true);
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

// ===================== 人口上限进度 =====================
function renderPopCap() {
  const curEl = document.getElementById("popCurrent");
  if (!curEl) return;
  const cur = lastSummary.total || 0;
  const cap = defaultSettings.max_population;
  curEl.textContent = cur;
  const maxEl = document.getElementById("popCapMax");
  const fill = document.getElementById("popCapFill");
  const hint = document.getElementById("popCapHint");
  if (cap > 0) {
    if (maxEl) maxEl.textContent = cap;
    const pct = Math.min(100, cur / cap * 100);
    if (fill) {
      fill.style.width = pct + "%";
      fill.style.background = pct >= 100 ? "#e74c3c" : (pct >= 90 ? "#f39c12" : "#3498db");
    }
    if (hint) hint.textContent = pct >= 100 ? "　已达上限，繁殖与新增已停止" : "";
  } else {
    if (maxEl) maxEl.textContent = "不限";
    if (fill) fill.style.width = "0%";
    if (hint) hint.textContent = "";
  }
}

// ===================== 回合耗时 =====================
function renderRoundMs(ms) {
  const el = document.getElementById("roundMs");
  if (!el) return;
  if (ms === undefined || ms === null) {
    el.textContent = "—";
    el.style.color = "";
    return;
  }
  const v = Number(ms);
  el.textContent = v.toFixed(1) + " ms";
  el.style.color = v > 2000 ? "#e74c3c" : "";
}

// ===================== 扇形图（用后端 summary.groups）=====================
function renderPieChart() {
  renderPopCap();
  const box = document.getElementById("pieChart");
  if (!box) return;
  if (typeof echarts === "undefined") { box.innerHTML = "<i>图表库未加载</i>"; return; }
  const groups = lastSummary.groups || {};
  const entries = Object.entries(groups).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((s, e) => s + e[1], 0);
  if (total === 0) {
    _echartDispose("pieChart");
    box.innerHTML = "<i>无个体</i>";
    return;
  }

  _echart("pieChart").setOption({
    color: ["#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c", "#e67e22", "#34495e"],
    tooltip: { trigger: "item", formatter: "{b}：{c}（{d}%）" },
    legend: { orient: "vertical", right: 8, top: "middle", textStyle: { fontSize: 12 } },
    series: [{
      type: "pie",
      radius: ["40%", "70%"],
      center: ["38%", "50%"],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: "#fff", borderWidth: 1 },
      label: { show: false },
      emphasis: { label: { show: true, fontWeight: "bold" } },
      data: entries.map(([name, count]) => ({ name, value: count })),
    }],
  }, true);
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
    renderRoundMs(r.duration_ms);
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
    renderRoundMs(r.duration_ms);
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
    renderRoundMs(r.duration_ms);
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
