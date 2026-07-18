// 历史对比页只读取本机 JSON API，并在浏览器端完成两组报告的归一化和绘图。
"use strict";

const reportASelect = document.querySelector("#report-a-select");
const reportBSelect = document.querySelector("#report-b-select");
const reportAMeta = document.querySelector("#report-a-meta");
const reportBMeta = document.querySelector("#report-b-meta");
const refreshHistoryButton = document.querySelector("#refresh-history-button");
const compareMessage = document.querySelector("#compare-message");
const comparisonDashboard = document.querySelector("#comparison-dashboard");
const comparisonVerdict = document.querySelector("#comparison-verdict");
const comparisonMetrics = document.querySelector("#comparison-metrics");
const overlayKlineChart = document.querySelector("#overlay-kline-chart");
const overlayReturnChart = document.querySelector("#overlay-return-chart");
const klineChartShell = document.querySelector("#kline-chart-shell");
const returnChartShell = document.querySelector("#return-chart-shell");
const klineTooltip = document.querySelector("#kline-tooltip");
const returnTooltip = document.querySelector("#return-tooltip");
const klineSummary = document.querySelector("#kline-summary");
const returnSummary = document.querySelector("#return-summary");
const comparisonZoomStatus = document.querySelector("#comparison-zoom-status");
const comparisonZoomRanges = document.querySelectorAll(".comparison-zoom-range");
const comparisonZoomIn = document.querySelector("#comparison-zoom-in");
const comparisonZoomOut = document.querySelector("#comparison-zoom-out");
const comparisonZoomReset = document.querySelector("#comparison-zoom-reset");
const klineLegendA = document.querySelector("#kline-legend-a");
const klineLegendB = document.querySelector("#kline-legend-b");
const returnLegendA = document.querySelector("#return-legend-a");
const returnLegendB = document.querySelector("#return-legend-b");

// 缓存完整报告，避免切换下拉框时重复读取同一个 JSON 文件。
const reportCache = new Map();
let reportSummaries = [];
let chartCleanup = () => {};
let chartAbortController = null;
let comparisonRequestSequence = 0;

/**
 * 请求 JSON API 并统一抛出服务端返回的中文错误。
 *
 * @param {string} path API 相对路径。
 * @returns {Promise<object>} 已解析的 JSON 对象。
 */
async function requestJson(path) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" } });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `请求失败：${response.status}`);
  return payload;
}

/**
 * 将可能为空的数值转为安全的有限数字。
 *
 * @param {unknown} value 服务端返回的原始值。
 * @param {number} fallback 数据缺失时使用的后备值。
 * @returns {number} 可用于计算和绘图的有限数字。
 */
function numberOr(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * 格式化人民币金额，保留两位小数以便两组数据稳定对齐。
 *
 * @param {unknown} value 金额原始值。
 * @returns {string} 本地化金额文本。
 */
function money(value) {
  return numberOr(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * 格式化百分比并显式显示正负号，避免仅依赖颜色传达方向。
 *
 * @param {unknown} value 百分比原始值。
 * @returns {string} 含正负号和百分号的文本。
 */
function percent(value) {
  const parsed = numberOr(value);
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(2)}%`;
}

/**
 * 将 ISO 时间转换为适合历史选择器和提示框阅读的中文时间。
 *
 * @param {unknown} value ISO 时间或空值。
 * @returns {string} 本地化时间文本。
 */
function dateTime(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 显示对比页的加载、普通提示或错误信息。
 *
 * @param {string} text 面向用户的中文提示。
 * @param {"normal"|"loading"|"error"} kind 提示的语义类型。
 * @returns {void}
 */
function showMessage(text, kind = "normal") {
  compareMessage.textContent = text;
  compareMessage.className = text ? `comparison-message ${kind}` : "comparison-message";
}

/**
 * 用历史摘要填充一个报告选择框，并保留当前仍然有效的选中项。
 *
 * @param {HTMLSelectElement} select 需要填充的选择框。
 * @param {string} currentId 当前选中的报告编号。
 * @param {number} fallbackIndex 没有当前编号时使用的默认索引。
 * @returns {void}
 */
function fillReportSelect(select, currentId, fallbackIndex) {
  select.replaceChildren();
  reportSummaries.forEach((item, index) => {
    const option = document.createElement("option");
    option.value = item.report_id;
    option.textContent = `${item.symbol || "未知标的"} · ${dateTime(item.created_at)} · 网格 ${money(item.grid_profit)} 元`;
    select.append(option);
    if (item.report_id === currentId || (!currentId && index === fallbackIndex)) option.selected = true;
  });
  select.disabled = reportSummaries.length === 0;
}

/**
 * 从摘要或完整报告生成选择器下方的简短说明。
 *
 * @param {object} report 报告摘要或完整报告。
 * @returns {string} 说明文本。
 */
function reportMetaText(report) {
  if (!report) return "请选择一组历史报告。";
  const market = report.market_data || {};
  const grid = report.grid || {};
  const bars = market.bar_count ? `${market.bar_count} 根K线` : "K线数量未知";
  const returnText = report.grid ? percent(grid.return_percent) : `${money(report.grid_profit)} 元`;
  return `${market.symbol || report.symbol || "未知标的"} · ${dateTime(market.started_at || report.created_at)} 至 ${dateTime(market.ended_at)} · ${bars} · 网格 ${returnText}`;
}

/**
 * 从历史摘要中查找指定报告，供尚未加载完整 JSON 时先渲染基本信息。
 *
 * @param {string} reportId 报告编号。
 * @returns {object|null} 对应摘要或空值。
 */
function findSummary(reportId) {
  return reportSummaries.find((item) => item.report_id === reportId) || null;
}

/**
 * 更新两个选择器下方的报告摘要信息。
 *
 * @param {object|null} reportA 报告 A 摘要或完整对象。
 * @param {object|null} reportB 报告 B 摘要或完整对象。
 * @returns {void}
 */
function renderSelectionMeta(reportA, reportB) {
  reportAMeta.textContent = reportMetaText(reportA);
  reportBMeta.textContent = reportMetaText(reportB);
}

/**
 * 读取并缓存一份历史报告的完整内容。
 *
 * @param {string} reportId 报告编号。
 * @returns {Promise<object>} 完整回测报告。
 */
async function loadReport(reportId) {
  if (!reportId) throw new Error("请选择需要对比的历史报告");
  if (!reportCache.has(reportId)) {
    reportCache.set(reportId, requestJson(`/api/backtests/${encodeURIComponent(reportId)}`));
  }
  return reportCache.get(reportId);
}

/**
 * 为一个报告建立归一化 K 线和收益曲线数据。
 *
 * @param {object} report 完整回测报告。
 * @param {string} label 图例名称。
 * @param {string} color 系列颜色。
 * @returns {{label:string,color:string,candles:object[],returns:object[],report:object}} 可绘制的系列。
 */
function buildSeries(report, label, color) {
  const points = Array.isArray(report.chart_data) ? report.chart_data : [];
  const firstClose = numberOr(points[0]?.close, numberOr(report.market_data?.first_price, 1));
  const initialAssets = numberOr(report.initial?.assets, 0);
  const base = firstClose || 1;
  const candles = points.map((point, index) => ({
    index,
    timestamp: point.timestamp,
    open: numberOr(point.open) / base * 100,
    high: numberOr(point.high) / base * 100,
    low: numberOr(point.low) / base * 100,
    close: numberOr(point.close) / base * 100,
    actualClose: numberOr(point.close),
    gridReturn: initialAssets ? numberOr(point.grid_profit) / initialAssets * 100 : 0,
  }));
  return {
    label,
    color,
    candles,
    returns: candles.map((candle) => ({ index: candle.index, timestamp: candle.timestamp, value: candle.gridReturn })),
    report,
  };
}

/**
 * 创建高分屏 Canvas 绘图环境并清理上一帧内容。
 *
 * @param {HTMLCanvasElement} canvas 目标画布。
 * @param {number} cssHeight 画布 CSS 高度。
 * @returns {{context:CanvasRenderingContext2D,width:number,height:number}} 绘图上下文和尺寸。
 */
function prepareCanvas(canvas, cssHeight) {
  const width = Math.max(320, canvas.clientWidth || 900);
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(cssHeight * ratio);
  canvas.style.height = `${cssHeight}px`;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, cssHeight);
  return { context, width, height: cssHeight };
}

/**
 * 绘制统一的网格、纵轴刻度和横轴进度标签。
 *
 * @param {CanvasRenderingContext2D} context Canvas 绘图上下文。
 * @param {object} margin 图表边距。
 * @param {number} plotWidth 绘图区宽度。
 * @param {number} plotHeight 绘图区高度。
 * @param {number} minimum 纵轴最小值。
 * @param {number} maximum 纵轴最大值。
 * @param {(value:number)=>string} formatter 刻度格式化函数。
 * @returns {void}
 */
function drawChartGrid(context, margin, plotWidth, plotHeight, minimum, maximum, formatter, viewStart = 0, viewEnd = 1) {
  context.font = "12px ui-monospace, SFMono-Regular, Consolas, monospace";
  context.lineWidth = 1;
  for (let index = 0; index <= 5; index += 1) {
    const ratio = index / 5;
    const y = margin.top + ratio * plotHeight;
    const value = maximum - ratio * (maximum - minimum);
    context.strokeStyle = "rgba(141,155,171,.18)";
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(margin.left + plotWidth, y);
    context.stroke();
    context.fillStyle = "#8d9bab";
    context.fillText(formatter(value), 8, y + 4);
  }
  [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
    const x = margin.left + ratio * plotWidth;
    context.fillStyle = "#8d9bab";
    context.fillText(`${Math.round((viewStart + ratio * (viewEnd - viewStart)) * 100)}%`, x - 10, margin.top + plotHeight + 25);
  });
}

/**
 * 绘制两组按首根价格归一化的重叠 K 线，并在当前悬停位置显示参考线。
 *
 * @param {HTMLCanvasElement} canvas K 线画布。
 * @param {object[]} seriesList 两组归一化系列。
 * @param {number|null} hoverRatio 当前悬停的回测进度比例。
 * @returns {void}
 */
function drawOverlayKline(canvas, seriesList, viewStart = 0, viewEnd = 1, hoverRatio = null) {
  const { context, width, height } = prepareCanvas(canvas, 410);
  const margin = { left: 64, right: 18, top: 18, bottom: 38 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const viewSpan = Math.max(0.000001, viewEnd - viewStart);
  const allCandles = seriesList.flatMap((series) => series.candles.filter((_, index) => {
    const ratio = index / Math.max(1, series.candles.length - 1);
    return ratio >= viewStart && ratio <= viewEnd;
  }));
  if (!allCandles.length) return;
  const low = Math.min(...allCandles.map((candle) => candle.low), 100);
  const high = Math.max(...allCandles.map((candle) => candle.high), 100);
  const padding = Math.max(1, (high - low) * 0.08);
  const minimum = low - padding;
  const maximum = high + padding;
  const y = (value) => margin.top + (maximum - value) / Math.max(0.000001, maximum - minimum) * plotHeight;
  drawChartGrid(context, margin, plotWidth, plotHeight, minimum, maximum, (value) => value.toFixed(1), viewStart, viewEnd);
  const maxLength = Math.max(...seriesList.map((series) => series.candles.filter((_, index) => {
    const ratio = index / Math.max(1, series.candles.length - 1);
    return ratio >= viewStart && ratio <= viewEnd;
  }).length));
  const bodyWidth = Math.max(2, Math.min(9, plotWidth / Math.max(1, maxLength) * 0.55));
  const offsets = seriesList.length > 1 ? [-bodyWidth * 0.46, bodyWidth * 0.46] : [0];
  seriesList.forEach((series, seriesIndex) => {
    series.candles.forEach((candle, index) => {
      const ratio = index / Math.max(1, series.candles.length - 1);
      if (ratio < viewStart || ratio > viewEnd) return;
      const x = margin.left + (ratio - viewStart) / viewSpan * plotWidth + offsets[seriesIndex];
      const openY = y(candle.open);
      const closeY = y(candle.close);
      const highY = y(candle.high);
      const lowY = y(candle.low);
      const bullish = candle.close >= candle.open;
      context.strokeStyle = series.color;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(x, highY);
      context.lineTo(x, lowY);
      context.stroke();
      const top = Math.min(openY, closeY);
      const bodyHeight = Math.max(1, Math.abs(openY - closeY));
      if (bullish) {
        context.globalAlpha = 0.72;
        context.fillStyle = series.color;
        context.fillRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
        context.globalAlpha = 1;
      } else {
        context.fillStyle = "#0b1017";
        context.fillRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
        context.strokeRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
      }
    });
  });
  const baselineY = y(100);
  context.save();
  context.setLineDash([5, 5]);
  context.strokeStyle = "rgba(237,242,247,.58)";
  context.beginPath();
  context.moveTo(margin.left, baselineY);
  context.lineTo(margin.left + plotWidth, baselineY);
  context.stroke();
  if (hoverRatio !== null) {
    const hoverX = margin.left + (hoverRatio - viewStart) / viewSpan * plotWidth;
    context.strokeStyle = "rgba(96,165,250,.8)";
    context.beginPath();
    context.moveTo(hoverX, margin.top);
    context.lineTo(hoverX, margin.top + plotHeight);
    context.stroke();
  }
  context.restore();
}

/**
 * 绘制两组网格收益百分比曲线，并显示零收益基线。
 *
 * @param {HTMLCanvasElement} canvas 收益画布。
 * @param {object[]} seriesList 两组归一化系列。
 * @param {number|null} hoverRatio 当前悬停的回测进度比例。
 * @returns {void}
 */
function drawOverlayReturns(canvas, seriesList, viewStart = 0, viewEnd = 1, hoverRatio = null) {
  const { context, width, height } = prepareCanvas(canvas, 290);
  const margin = { left: 64, right: 18, top: 18, bottom: 38 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const viewSpan = Math.max(0.000001, viewEnd - viewStart);
  const visibleSeries = seriesList.map((series) => series.returns.filter((_, index) => {
    const ratio = index / Math.max(1, series.returns.length - 1);
    return ratio >= viewStart && ratio <= viewEnd;
  }));
  const values = visibleSeries.flatMap((points) => points.map((point) => point.value));
  if (!values.length) return;
  const minimum = Math.min(...values, 0);
  const maximum = Math.max(...values, 0);
  const padding = Math.max(0.5, (maximum - minimum) * 0.12);
  const chartMinimum = minimum - padding;
  const chartMaximum = maximum + padding;
  const y = (value) => margin.top + (chartMaximum - value) / Math.max(0.000001, chartMaximum - chartMinimum) * plotHeight;
  drawChartGrid(context, margin, plotWidth, plotHeight, chartMinimum, chartMaximum, (value) => `${value.toFixed(1)}%`, viewStart, viewEnd);
  seriesList.forEach((series, seriesIndex) => {
    context.strokeStyle = series.color;
    context.lineWidth = 2.2;
    context.beginPath();
    const firstVisibleIndex = series.returns.findIndex((_, candidateIndex) => {
      const candidateRatio = candidateIndex / Math.max(1, series.returns.length - 1);
      return candidateRatio >= viewStart && candidateRatio <= viewEnd;
    });
    series.returns.forEach((point, index) => {
      const ratio = index / Math.max(1, series.returns.length - 1);
      if (ratio < viewStart || ratio > viewEnd) return;
      const x = margin.left + (ratio - viewStart) / viewSpan * plotWidth;
      const pointY = y(point.value);
      if (index === firstVisibleIndex) context.moveTo(x, pointY);
      else context.lineTo(x, pointY);
    });
    context.stroke();
  });
  context.save();
  context.setLineDash([5, 5]);
  context.strokeStyle = "rgba(237,242,247,.58)";
  context.beginPath();
  context.moveTo(margin.left, y(0));
  context.lineTo(margin.left + plotWidth, y(0));
  context.stroke();
  if (hoverRatio !== null) {
    const hoverX = margin.left + (hoverRatio - viewStart) / viewSpan * plotWidth;
    context.strokeStyle = "rgba(96,165,250,.8)";
    context.beginPath();
    context.moveTo(hoverX, margin.top);
    context.lineTo(hoverX, margin.top + plotHeight);
    context.stroke();
  }
  context.restore();
}

/**
 * 从鼠标横坐标计算 0 到 1 的统一回测进度。
 *
 * @param {MouseEvent} event 指针事件。
 * @param {HTMLCanvasElement} canvas 发生事件的画布。
 * @returns {number} 限制在 0 到 1 之间的进度比例。
 */
function progressFromEvent(event, canvas, viewStart = 0, viewEnd = 1) {
  const rect = canvas.getBoundingClientRect();
  const position = Math.max(0, Math.min(1, (event.clientX - rect.left - 64) / Math.max(1, rect.width - 82)));
  return viewStart + position * (viewEnd - viewStart);
}

/**
 * 将缩放区间限制在 0 到 1，并保证始终保留可读的最小窗口。
 *
 * @param {number} start 缩放区间起点。
 * @param {number} end 缩放区间终点。
 * @returns {{start:number,end:number}} 修正后的缩放区间。
 */
function normalizeViewRange(start, end) {
  const minimumSpan = 0.08;
  let nextStart = Math.max(0, Math.min(1, numberOr(start)));
  let nextEnd = Math.max(0, Math.min(1, numberOr(end, 1)));
  if (nextEnd < nextStart) [nextStart, nextEnd] = [nextEnd, nextStart];
  if (nextEnd - nextStart < minimumSpan) {
    const center = (nextStart + nextEnd) / 2;
    nextStart = Math.max(0, center - minimumSpan / 2);
    nextEnd = Math.min(1, nextStart + minimumSpan);
    nextStart = Math.max(0, nextEnd - minimumSpan);
  }
  return { start: nextStart, end: nextEnd };
}

/**
 * 围绕指定回测进度放大或缩小共享图表窗口。
 *
 * @param {number} start 当前窗口起点。
 * @param {number} end 当前窗口终点。
 * @param {number} factor 小于 1 表示放大，大于 1 表示缩小。
 * @param {number} anchorRatio 缩放锚点对应的完整回测进度。
 * @returns {{start:number,end:number}} 缩放后的窗口。
 */
function zoomViewRange(start, end, factor, anchorRatio = (start + end) / 2) {
  const span = end - start;
  const nextSpan = Math.max(0.08, Math.min(1, span * factor));
  if (Math.abs(nextSpan - span) < 0.000001) return { start, end };
  const anchor = Math.max(start, Math.min(end, anchorRatio));
  const anchorPosition = (anchor - start) / Math.max(0.000001, span);
  let nextStart = anchor - anchorPosition * nextSpan;
  nextStart = Math.max(0, Math.min(1 - nextSpan, nextStart));
  return normalizeViewRange(nextStart, nextStart + nextSpan);
}

/**
 * 更新缩放控件的文字和快捷范围按钮选中状态。
 *
 * @param {number} start 当前窗口起点。
 * @param {number} end 当前窗口终点。
 * @returns {void}
 */
function updateZoomControls(start, end) {
  const span = end - start;
  comparisonZoomStatus.textContent = span >= 0.999 ? "显示全部区间" : `显示 ${Math.round(span * 100)}% 区间`;
  comparisonZoomRanges.forEach((button) => {
    const buttonStart = numberOr(button.dataset.zoomStart);
    const buttonEnd = numberOr(button.dataset.zoomEnd, 1);
    const active = Math.abs(buttonStart - start) < 0.001 && Math.abs(buttonEnd - end) < 0.001;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

/**
 * 读取最接近指定进度的 K 线点，并格式化为对比提示框文本。
 *
 * @param {object[]} seriesList 两组归一化系列。
 * @param {number} ratio 当前进度比例。
 * @returns {string} 可直接显示的多行提示文本。
 */
function comparisonTooltipText(seriesList, ratio) {
  return seriesList.map((series) => {
    const index = Math.min(series.candles.length - 1, Math.round(ratio * Math.max(0, series.candles.length - 1)));
    const candle = series.candles[index];
    if (!candle) return `${series.label}：暂无数据`;
    return `${series.label}  ${dateTime(candle.timestamp)}\n收盘 ${candle.actualClose.toFixed(3)} · 归一化 ${candle.close.toFixed(2)} · 网格 ${percent(candle.gridReturn)}`;
  }).join("\n");
}

/**
 * 将提示框定位到图表附近，避免遮挡鼠标当前指针。
 *
 * @param {HTMLElement} tooltip 提示框元素。
 * @param {HTMLElement} shell 图表外壳。
 * @param {MouseEvent} event 指针事件。
 * @param {string} text 提示框文本。
 * @returns {void}
 */
function showTooltip(tooltip, shell, event, text) {
  tooltip.textContent = text;
  tooltip.hidden = false;
  const shellRect = shell.getBoundingClientRect();
  tooltip.style.left = `${Math.max(8, Math.min(shellRect.width - 310, event.clientX - shellRect.left + 12))}px`;
  tooltip.style.top = `${Math.max(8, event.clientY - shellRect.top - 76)}px`;
}

/**
 * 创建指标卡，按 A、B、差值三列呈现同一指标。
 *
 * @param {string} label 指标名称。
 * @param {string} valueA A 组显示值。
 * @param {string} valueB B 组显示值。
 * @param {string} difference 两组差值。
 * @returns {HTMLElement} 指标卡元素。
 */
function createMetric(label, valueA, valueB, difference) {
  const card = document.createElement("article");
  card.className = "comparison-metric-card";
  const title = document.createElement("h3");
  title.textContent = label;
  const values = document.createElement("div");
  values.className = "comparison-metric-values";
  [["A", valueA, "metric-a"], ["B", valueB, "metric-b"], ["差值", difference, "metric-difference"]].forEach(([name, value, className]) => {
    const item = document.createElement("div");
    item.className = className;
    const strong = document.createElement("strong");
    strong.textContent = value;
    const caption = document.createElement("span");
    caption.textContent = name;
    item.append(strong, caption);
    values.append(item);
  });
  card.append(title, values);
  return card;
}

/**
 * 渲染两组报告的结论卡和关键指标。
 *
 * @param {object} reportA 完整报告 A。
 * @param {object} reportB 完整报告 B。
 * @returns {void}
 */
function renderMetrics(reportA, reportB) {
  const gridA = reportA.grid || {};
  const gridB = reportB.grid || {};
  const compare = (key, formatter) => formatter(numberOr(gridA[key]) - numberOr(gridB[key]));
  const winner = numberOr(gridA.return_percent) === numberOr(gridB.return_percent)
    ? "两组网格收益相同"
    : numberOr(gridA.return_percent) > numberOr(gridB.return_percent) ? "报告 A 的网格收益更高" : "报告 B 的网格收益更高";
  comparisonVerdict.replaceChildren();
  comparisonVerdict.className = `comparison-verdict panel ${numberOr(gridA.return_percent) >= numberOr(gridB.return_percent) ? "a-winner" : "b-winner"}`;
  const kicker = document.createElement("p");
  kicker.className = "section-kicker";
  kicker.textContent = "COMPARISON RESULT";
  const title = document.createElement("h2");
  title.id = "comparison-verdict-title";
  title.textContent = winner;
  const copy = document.createElement("p");
  copy.textContent = `报告 A：${reportA.market_data?.symbol || "未知标的"}，最终网格收益 ${percent(gridA.return_percent)}；报告 B：${reportB.market_data?.symbol || "未知标的"}，最终网格收益 ${percent(gridB.return_percent)}。`;
  comparisonVerdict.append(kicker, title, copy);

  comparisonMetrics.replaceChildren(
    createMetric("网格收益率", percent(gridA.return_percent), percent(gridB.return_percent), compare("return_percent", percent)),
    createMetric("网格收益", `${money(gridA.profit)} 元`, `${money(gridB.profit)} 元`, `${compare("profit", money)} 元`),
    createMetric("最大回撤", percent(gridA.max_drawdown_percent), percent(gridB.max_drawdown_percent), compare("max_drawdown_percent", percent)),
    createMetric("成交次数", `${numberOr(gridA.trade_count)} 次`, `${numberOr(gridB.trade_count)} 次`, `${compare("trade_count", (value) => `${value > 0 ? "+" : ""}${value}`)} 次`),
    createMetric("平均仓位", percent(gridA.average_position_percent), percent(gridB.average_position_percent), compare("average_position_percent", percent)),
    createMetric("直接持有收益率", percent(reportA.buy_and_hold?.return_percent), percent(reportB.buy_and_hold?.return_percent), `${percent(numberOr(reportA.buy_and_hold?.return_percent) - numberOr(reportB.buy_and_hold?.return_percent))}`),
  );
}

/**
 * 渲染两组完整报告的图表、图例和无障碍摘要。
 *
 * @param {object} reportA 完整报告 A。
 * @param {object} reportB 完整报告 B。
 * @returns {void}
 */
function renderComparison(reportA, reportB) {
  chartCleanup();
  chartAbortController = new AbortController();
  const eventOptions = { signal: chartAbortController.signal };
  const seriesList = [buildSeries(reportA, "报告 A", "#60a5fa"), buildSeries(reportB, "报告 B", "#f7c66a")];
  klineLegendA.textContent = `${reportA.market_data?.symbol || "报告 A"} · A`;
  klineLegendB.textContent = `${reportB.market_data?.symbol || "报告 B"} · B`;
  returnLegendA.textContent = `${reportA.market_data?.symbol || "报告 A"} · 网格收益`;
  returnLegendB.textContent = `${reportB.market_data?.symbol || "报告 B"} · 网格收益`;
  klineSummary.textContent = `K线图摘要：${seriesList[0].label}最终归一化收盘 ${seriesList[0].candles.at(-1)?.close.toFixed(2) || "未知"}，${seriesList[1].label}最终归一化收盘 ${seriesList[1].candles.at(-1)?.close.toFixed(2) || "未知"}。`;
  returnSummary.textContent = `收益图摘要：${seriesList[0].label}最终网格收益 ${percent(reportA.grid?.return_percent)}，${seriesList[1].label}最终网格收益 ${percent(reportB.grid?.return_percent)}。`;
  renderMetrics(reportA, reportB);
  comparisonDashboard.hidden = false;
  let viewStart = 0;
  let viewEnd = 1;
  let hoverRatio = null;
  updateZoomControls(viewStart, viewEnd);
  const redraw = () => {
    drawOverlayKline(overlayKlineChart, seriesList, viewStart, viewEnd, hoverRatio);
    drawOverlayReturns(overlayReturnChart, seriesList, viewStart, viewEnd, hoverRatio);
  };
  const setViewRange = (start, end) => {
    const normalized = normalizeViewRange(start, end);
    viewStart = normalized.start;
    viewEnd = normalized.end;
    hoverRatio = null;
    klineTooltip.hidden = true;
    returnTooltip.hidden = true;
    updateZoomControls(viewStart, viewEnd);
    redraw();
  };
  const zoomAt = (factor, anchorRatio = (viewStart + viewEnd) / 2) => {
    const next = zoomViewRange(viewStart, viewEnd, factor, anchorRatio);
    setViewRange(next.start, next.end);
  };
  const handleMove = (event, tooltip, shell) => {
    hoverRatio = progressFromEvent(event, overlayKlineChart, viewStart, viewEnd);
    showTooltip(tooltip, shell, event, comparisonTooltipText(seriesList, hoverRatio));
    redraw();
  };
  const handleLeave = (tooltip) => {
    hoverRatio = null;
    tooltip.hidden = true;
    redraw();
  };
  const handleWheel = (event) => {
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 0.72 : 1.38, progressFromEvent(event, overlayKlineChart, viewStart, viewEnd));
  };
  const handleKeydown = (event) => {
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomAt(0.72, hoverRatio ?? (viewStart + viewEnd) / 2);
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomAt(1.38, hoverRatio ?? (viewStart + viewEnd) / 2);
    } else if (event.key === "0") {
      event.preventDefault();
      setViewRange(0, 1);
    }
  };
  comparisonZoomRanges.forEach((button) => button.addEventListener("click", () => setViewRange(button.dataset.zoomStart, button.dataset.zoomEnd), eventOptions));
  comparisonZoomIn.addEventListener("click", () => zoomAt(0.72), eventOptions);
  comparisonZoomOut.addEventListener("click", () => zoomAt(1.38), eventOptions);
  comparisonZoomReset.addEventListener("click", () => setViewRange(0, 1), eventOptions);
  klineChartShell.addEventListener("pointermove", (event) => handleMove(event, klineTooltip, klineChartShell), eventOptions);
  klineChartShell.addEventListener("pointerleave", () => handleLeave(klineTooltip), eventOptions);
  returnChartShell.addEventListener("pointermove", (event) => handleMove(event, returnTooltip, returnChartShell), eventOptions);
  returnChartShell.addEventListener("pointerleave", () => handleLeave(returnTooltip), eventOptions);
  klineChartShell.addEventListener("wheel", handleWheel, { ...eventOptions, passive: false });
  returnChartShell.addEventListener("wheel", handleWheel, { ...eventOptions, passive: false });
  overlayKlineChart.addEventListener("keydown", handleKeydown, eventOptions);
  overlayReturnChart.addEventListener("keydown", handleKeydown, eventOptions);
  const resizeObserver = new ResizeObserver(redraw);
  resizeObserver.observe(comparisonDashboard);
  chartCleanup = () => {
    resizeObserver.disconnect();
    chartAbortController?.abort();
    chartAbortController = null;
  };
  requestAnimationFrame(redraw);
}

/**
 * 读取历史报告摘要并初始化两个选择器。
 *
 * @returns {Promise<void>} 历史数据加载完成后结束。
 */
async function loadHistory() {
  refreshHistoryButton.disabled = true;
  showMessage("正在读取历史报告……", "loading");
  try {
    const payload = await requestJson("/api/backtests");
    reportSummaries = Array.isArray(payload.data) ? payload.data : [];
    const currentA = reportASelect.value;
    const currentB = reportBSelect.value;
    fillReportSelect(reportASelect, currentA, 0);
    fillReportSelect(reportBSelect, currentB, Math.min(1, Math.max(0, reportSummaries.length - 1)));
    if (reportSummaries.length < 2) {
      comparisonDashboard.hidden = true;
      renderSelectionMeta(findSummary(reportASelect.value), null);
      showMessage("至少需要两组历史回测报告，完成两次回测后即可进行叠加对比。", "normal");
      return;
    }
    await compareSelectedReports();
  } catch (error) {
    comparisonDashboard.hidden = true;
    showMessage(`历史报告读取失败：${error.message}`, "error");
  } finally {
    refreshHistoryButton.disabled = false;
  }
}

/**
 * 读取当前选中的两组报告并刷新对比视图，较新的请求会覆盖旧请求。
 *
 * @returns {Promise<void>} 对比视图刷新完成后结束。
 */
async function compareSelectedReports() {
  const requestSequence = ++comparisonRequestSequence;
  const summaryA = findSummary(reportASelect.value);
  const summaryB = findSummary(reportBSelect.value);
  renderSelectionMeta(summaryA, summaryB);
  if (!reportASelect.value || !reportBSelect.value) {
    comparisonDashboard.hidden = true;
    showMessage("请选择两组历史报告。", "normal");
    return;
  }
  if (reportASelect.value === reportBSelect.value) {
    comparisonDashboard.hidden = true;
    showMessage("报告 A 和报告 B 不能选择同一组数据，请更换其中一组。", "error");
    return;
  }
  showMessage("正在读取两组完整报告并绘制对比图……", "loading");
  try {
    const [reportA, reportB] = await Promise.all([loadReport(reportASelect.value), loadReport(reportBSelect.value)]);
    if (requestSequence !== comparisonRequestSequence) return;
    renderSelectionMeta(reportA, reportB);
    renderComparison(reportA, reportB);
    showMessage("对比完成：K 线已按首根收盘价归一化，收益曲线按初始资产计算。", "normal");
  } catch (error) {
    comparisonDashboard.hidden = true;
    showMessage(`对比数据读取失败：${error.message}`, "error");
  }
}

// 下拉框变更和刷新按钮均使用同一条异步加载路径，保持状态反馈一致。
reportASelect.addEventListener("change", () => void compareSelectedReports());
reportBSelect.addEventListener("change", () => void compareSelectedReports());
refreshHistoryButton.addEventListener("click", () => void loadHistory());
void loadHistory();
