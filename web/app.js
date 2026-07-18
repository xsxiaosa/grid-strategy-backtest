// 独立前端仅通过本机 JSON API 读写策略和报告，不直接访问第三方行情源。
"use strict";

const form = document.querySelector("#strategy-form");
const runButton = document.querySelector("#run-button");
const resetButton = document.querySelector("#reset-button");
const copyJsonButton = document.querySelector("#copy-json-button");
const restoreJsonButton = document.querySelector("#restore-json-button");
const jsonDialog = document.querySelector("#json-dialog");
const jsonInput = document.querySelector("#json-input");
const jsonDialogError = document.querySelector("#json-dialog-error");
const confirmRestoreButton = document.querySelector("#confirm-restore-button");
const message = document.querySelector("#message");
const reportContainer = document.querySelector("#report");
const historyContainer = document.querySelector("#history");
let activeChartCleanups = [];

/** 内置指定方案，用于用户主动恢复，不会覆盖服务端最近保存配置。 */
const DEFAULT_CONFIG = {
  symbol: "588000", days: 30, lower_bound: 1.45, upper_bound: 2.05, base_price: 1.74,
  rise_trigger_percent: 2.5, sell_pullback_percent: 20, fall_trigger_percent: 2.5,
  buy_rebound_percent: 30, order_amount: 3000, buy_price_offset: 0.001,
  sell_price_offset: 0.001, initial_capital: 30000, initial_position_percent: 50,
  commission_rate: 0.00025, minimum_commission: 0, lot_size: 100
};

/**
 * 请求 JSON API，并把服务端错误消息转换为 JavaScript Error。
 *
 * @param {string} path API 相对路径。
 * @param {RequestInit} [options] 可选 Fetch 请求参数。
 * @returns {Promise<object>} 已解析的 JSON 根对象。
 */
async function requestJson(path, options) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.message || `请求失败：${response.status}`);
  return payload;
}

/**
 * 将策略 JSON 写入同名表单字段。
 *
 * @param {object} config 服务端配置或内置默认方案。
 * @returns {void}
 */
function fillForm(config) {
  Object.entries(config).forEach(([name, value]) => {
    const control = form.elements.namedItem(name);
    if (control) control.value = String(value);
  });
}

/**
 * 从表单读取策略字段，并将除证券代码外的字段转换为数字。
 *
 * @returns {object} 可直接提交到回测 API 的策略对象。
 */
function readForm() {
  const values = Object.fromEntries(new FormData(form).entries());
  Object.keys(values).forEach((name) => {
    if (name !== "symbol") values[name] = Number(values[name]);
  });
  return values;
}

/**
 * 将文本写入系统剪贴板，并在浏览器剪贴板接口不可用时使用兼容方案。
 *
 * @param {string} text 需要写入剪贴板的完整文本。
 * @returns {Promise<void>} 文本成功写入剪贴板后结束。
 * @throws {Error} 浏览器拒绝剪贴板权限且兼容复制也失败时抛出。
 */
async function writeClipboardText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }

  // 本地非安全上下文使用临时文本域兼容旧浏览器，操作完成后立即移除。
  const temporaryInput = document.createElement("textarea");
  temporaryInput.value = text;
  temporaryInput.setAttribute("readonly", "");
  temporaryInput.style.position = "fixed";
  temporaryInput.style.opacity = "0";
  document.body.append(temporaryInput);
  temporaryInput.select();
  const copied = document.execCommand("copy");
  temporaryInput.remove();
  if (!copied) throw new Error("浏览器未授予剪贴板写入权限");
}

/**
 * 校验当前表单后，将全部回测参数格式化为 JSON 并写入剪贴板。
 *
 * @returns {Promise<void>} 复制完成并向用户显示结果后结束。
 */
async function copyParametersAsJson() {
  if (!form.reportValidity()) return;
  copyJsonButton.disabled = true;
  try {
    const text = JSON.stringify(readForm(), null, 2);
    await writeClipboardText(text);
    showMessage("全部回测参数已复制为 JSON。", "normal");
  } catch (error) {
    showMessage(`参数 JSON 复制失败：${error.message}`, "error");
  } finally {
    copyJsonButton.disabled = false;
  }
}

/**
 * 打开 JSON 还原对话框，并清理上一次输入和错误状态。
 *
 * @returns {void}
 */
function openJsonRestoreDialog() {
  jsonInput.value = "";
  jsonDialogError.textContent = "";
  jsonDialogError.hidden = true;
  jsonDialog.showModal();
  jsonInput.focus();
}

/**
 * 解析用户粘贴的 JSON，将已识别参数还原到表单并校验字段约束。
 *
 * @returns {void}
 */
function restoreParametersFromJson() {
  const previousConfig = readForm();
  try {
    const parsed = JSON.parse(jsonInput.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("JSON 顶层必须是参数对象");
    }
    const supportedNames = new Set(Array.from(form.elements).map((control) => control.name).filter(Boolean));
    const recognizedConfig = Object.fromEntries(Object.entries(parsed).filter(([name]) => supportedNames.has(name)));
    if (Object.keys(recognizedConfig).length === 0) {
      throw new Error("JSON 中没有可识别的回测参数");
    }
    fillForm(recognizedConfig);
    if (!form.checkValidity()) {
      fillForm(previousConfig);
      throw new Error("JSON 中的参数不符合表单范围，请检查代码、天数和数值边界");
    }
    jsonDialog.close();
    showMessage(`已从 JSON 还原 ${Object.keys(recognizedConfig).length} 个参数，请确认后开始回测。`, "normal");
  } catch (error) {
    jsonDialogError.textContent = `还原失败：${error.message}`;
    jsonDialogError.hidden = false;
    jsonInput.focus();
  }
}

/**
 * 显示加载、错误或普通状态消息。
 *
 * @param {string} text 面向用户的消息文本；空字符串表示清空。
 * @param {"loading"|"error"|"normal"} [kind] 消息视觉类型。
 * @returns {void}
 */
function showMessage(text, kind = "normal") {
  message.replaceChildren();
  if (!text) return;
  const item = document.createElement("div");
  item.className = `message ${kind}`;
  item.textContent = text;
  message.append(item);
}

/**
 * 格式化人民币金额并保留两位小数。
 *
 * @param {number} value 原始金额。
 * @returns {string} 带千位分隔的金额字符串。
 */
function money(value) {
  return Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * 格式化已经按百分数表达的比例。
 *
 * @param {number} value 原始百分数。
 * @returns {string} 带正负号和四位小数的百分比。
 */
function percent(value) {
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(4)}%`;
}

/**
 * 格式化 ISO 时间为浏览器当前时区的中文日期时间。
 *
 * @param {string} value ISO-8601 时间。
 * @returns {string} 中文本地日期时间。
 */
function dateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 创建含标题和数值的绩效指标卡。
 *
 * @param {string} title 指标名称。
 * @param {string} value 已格式化的指标值。
 * @param {number} [signValue] 用于决定盈亏颜色的原始数值。
 * @returns {HTMLElement} 指标卡元素。
 */
function createMetric(title, value, signValue) {
  const card = document.createElement("div");
  card.className = "metric";
  const label = document.createElement("span");
  label.textContent = title;
  const strong = document.createElement("strong");
  strong.textContent = value;
  if (typeof signValue === "number") strong.className = signValue >= 0 ? "positive" : "negative";
  card.append(label, strong);
  return card;
}

/**
 * 创建行情与起点区域的单个详情格。
 *
 * @param {string} label 详情字段名称。
 * @param {string|number} value 详情字段值。
 * @returns {HTMLElement} 详情格元素。
 */
function createDetail(label, value) {
  const item = document.createElement("div");
  item.className = "detail";
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = String(value);
  item.append(name, content);
  return item;
}

/**
 * 创建包含 K 线、成交标记和双收益曲线的图表区域。
 *
 * @param {object} result 完整回测报告。
 * @returns {HTMLElement} 可直接插入报告的图表面板。
 */
function createChartsPanel(result) {
  activeChartCleanups.forEach((cleanup) => cleanup());
  activeChartCleanups = [];
  const panel = document.createElement("section");
  panel.className = "panel report-panel chart-panel";
  const heading = document.createElement("div");
  heading.className = "chart-heading";
  const titleBox = document.createElement("div");
  const title = document.createElement("h2");
  title.textContent = "价格、成交与收益轨迹";
  const subtitle = document.createElement("p");
  subtitle.className = "muted chart-subtitle";
  subtitle.textContent = "滚轮或缩放按钮调整时间范围；左右键可逐分钟查看。";
  titleBox.append(title, subtitle);
  const controls = document.createElement("div");
  controls.className = "chart-controls";
  controls.setAttribute("role", "group");
  controls.setAttribute("aria-label", "图表时间范围");
  heading.append(titleBox, controls);

  const candleShell = document.createElement("div");
  candleShell.className = "canvas-shell";
  const candleCanvas = document.createElement("canvas");
  candleCanvas.className = "financial-canvas candle-canvas";
  candleCanvas.tabIndex = 0;
  candleCanvas.setAttribute("role", "img");
  candleCanvas.setAttribute("aria-label", `588000 分钟 K 线图，共 ${result.chart_data?.length || 0} 个数据点，三角形标记买入和卖出成交。`);
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;
  candleShell.append(candleCanvas, tooltip);

  const candleLegend = createLegend([
    { className: "legend-candle-up", text: "上涨（实体）" },
    { className: "legend-candle-down", text: "下跌（空心）" },
    { className: "legend-buy", text: "买入 ▲" },
    { className: "legend-sell", text: "卖出 ▼" }
  ]);
  const profitHeader = document.createElement("div");
  profitHeader.className = "profit-chart-header";
  const profitTitle = document.createElement("h3");
  profitTitle.textContent = "累计收益对比（元）";
  const profitLegend = createLegend([
    { className: "legend-grid", text: "网格收益 · 实线" },
    { className: "legend-hold", text: "直接持有 · 虚线" }
  ]);
  profitHeader.append(profitTitle, profitLegend);
  const profitCanvas = document.createElement("canvas");
  profitCanvas.className = "financial-canvas profit-canvas";
  profitCanvas.tabIndex = 0;
  profitCanvas.setAttribute("role", "img");
  profitCanvas.setAttribute("aria-label", `累计收益对比图。网格最终收益 ${money(result.grid.profit)} 元，直接持有最终收益 ${money(result.buy_and_hold.profit)} 元。`);
  const summary = document.createElement("p");
  summary.className = "sr-chart-summary";
  summary.textContent = `图表摘要：行情从 ${dateTime(result.market_data.started_at)} 到 ${dateTime(result.market_data.ended_at)}；共有 ${result.grid.buy_count} 次买入、${result.grid.sell_count} 次卖出。网格最终收益 ${money(result.grid.profit)} 元，直接持有最终收益 ${money(result.buy_and_hold.profit)} 元。`;
  panel.append(heading, candleLegend, candleShell, profitHeader, profitCanvas, summary);

  const data = result.chart_data || [];
  if (!data.length) {
    candleShell.replaceChildren(document.createTextNode("该历史报告没有图表数据，请重新运行一次回测。"));
    profitCanvas.hidden = true;
    return panel;
  }
  let visibleCount = Math.min(data.length, 241 * 5);
  let visibleStart = Math.max(0, data.length - visibleCount);
  let visibleEnd = data.length;
  let hoverIndex = null;
  const timestampIndexes = new Map(data.map((point, index) => [point.timestamp, index]));
  const rangeButtons = [];

  const zoomStatus = document.createElement("span");
  zoomStatus.className = "zoom-status";
  zoomStatus.setAttribute("aria-live", "polite");
  controls.append(zoomStatus);

  /** 根据当前时间范围重绘两张 Canvas 图表。 */
  const redraw = () => {
    visibleCount = visibleEnd - visibleStart;
    zoomStatus.textContent = `显示 ${visibleCount} 分钟`;
    drawCandlestickChart(candleCanvas, data, result.trades || [], timestampIndexes, visibleStart, visibleEnd, hoverIndex);
    drawProfitChart(profitCanvas, data, visibleStart, visibleEnd);
  };

  /** 更新快捷范围按钮的选中状态，非预设缩放范围不强行标记。 */
  const updateRangeButtons = () => {
    rangeButtons.forEach(({ button, count }) => {
      const active = visibleStart === Math.max(0, data.length - Math.min(data.length, count)) && visibleEnd === data.length;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  };

  /** 设置以行情末尾为终点的快捷可见范围。 */
  const setTrailingRange = (count) => {
    visibleCount = Math.min(data.length, count);
    visibleStart = Math.max(0, data.length - visibleCount);
    visibleEnd = data.length;
    hoverIndex = null;
    tooltip.hidden = true;
    updateRangeButtons();
    redraw();
  };

  /** 围绕指定分钟缩放可见时间窗口，并限制最少显示 20 分钟。 */
  const zoomAt = (factor, anchorIndex = Math.floor((visibleStart + visibleEnd) / 2)) => {
    const oldCount = visibleEnd - visibleStart;
    const newCount = Math.max(20, Math.min(data.length, Math.round(oldCount * factor)));
    if (newCount === oldCount) return;
    const anchorRatio = (anchorIndex - visibleStart) / Math.max(1, oldCount - 1);
    let nextStart = Math.round(anchorIndex - anchorRatio * (newCount - 1));
    nextStart = Math.max(0, Math.min(data.length - newCount, nextStart));
    visibleStart = nextStart;
    visibleEnd = nextStart + newCount;
    hoverIndex = null;
    tooltip.hidden = true;
    updateRangeButtons();
    redraw();
  };

  [{ label: "1日", count: 241 }, { label: "5日", count: 241 * 5 }, { label: "全部", count: data.length }].forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `range-button ${option.count === visibleCount ? "active" : ""}`;
    button.textContent = option.label;
    button.setAttribute("aria-pressed", String(option.count === visibleCount));
    button.addEventListener("click", () => setTrailingRange(option.count));
    rangeButtons.push({ button, count: option.count });
    controls.append(button);
  });

  [{ label: "放大", title: "放大 K 线时间范围", factor: 0.6 }, { label: "缩小", title: "缩小 K 线时间范围", factor: 1.6 }].forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "range-button zoom-button";
    button.textContent = option.label;
    button.title = option.title;
    button.setAttribute("aria-label", option.title);
    button.addEventListener("click", () => zoomAt(option.factor));
    controls.append(button);
  });
  const resetZoomButton = document.createElement("button");
  resetZoomButton.type = "button";
  resetZoomButton.className = "range-button zoom-button";
  resetZoomButton.textContent = "重置";
  resetZoomButton.setAttribute("aria-label", "重置为全部行情范围");
  resetZoomButton.addEventListener("click", () => setTrailingRange(data.length));
  controls.append(resetZoomButton);
  updateRangeButtons();

  /** 将鼠标横坐标转换为当前可见区间内的原始分钟索引。 */
  const indexFromClientX = (clientX) => {
    const rect = candleCanvas.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left - 58) / Math.max(1, rect.width - 76)));
    return Math.min(visibleEnd - 1, visibleStart + Math.round(ratio * Math.max(0, visibleEnd - visibleStart - 1)));
  };
  const updateTooltip = (index, clientX, clientY) => {
    hoverIndex = index;
    const point = data[index];
    tooltip.textContent = `${dateTime(point.timestamp)}  开 ${point.open.toFixed(3)}  高 ${point.high.toFixed(3)}  低 ${point.low.toFixed(3)}  收 ${point.close.toFixed(3)}  网格 ${money(point.grid_profit)}  持有 ${money(point.hold_profit)}`;
    tooltip.hidden = false;
    const rect = candleShell.getBoundingClientRect();
    tooltip.style.left = `${Math.max(8, Math.min(rect.width - 310, clientX - rect.left + 12))}px`;
    tooltip.style.top = `${Math.max(8, clientY - rect.top - 52)}px`;
    redraw();
  };
  candleCanvas.addEventListener("pointermove", (event) => updateTooltip(indexFromClientX(event.clientX), event.clientX, event.clientY));
  candleCanvas.addEventListener("pointerleave", () => { hoverIndex = null; tooltip.hidden = true; redraw(); });
  candleCanvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomAt(event.deltaY < 0 ? 0.8 : 1.25, indexFromClientX(event.clientX));
  }, { passive: false });
  candleCanvas.addEventListener("keydown", (event) => {
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomAt(0.6, hoverIndex ?? Math.floor((visibleStart + visibleEnd) / 2));
      return;
    }
    if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomAt(1.6, hoverIndex ?? Math.floor((visibleStart + visibleEnd) / 2));
      return;
    }
    if (event.key === "0") {
      event.preventDefault();
      setTrailingRange(data.length);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    hoverIndex = hoverIndex ?? visibleEnd - 1;
    hoverIndex = Math.max(visibleStart, Math.min(visibleEnd - 1, hoverIndex + (event.key === "ArrowLeft" ? -1 : 1)));
    const rect = candleCanvas.getBoundingClientRect();
    const ratio = (hoverIndex - visibleStart) / Math.max(1, visibleEnd - visibleStart - 1);
    updateTooltip(hoverIndex, rect.left + 58 + ratio * (rect.width - 76), rect.top + 70);
  });
  const observer = new ResizeObserver(redraw);
  observer.observe(panel);
  activeChartCleanups.push(() => observer.disconnect());
  requestAnimationFrame(redraw);
  return panel;
}

/**
 * 创建图表图例，并用形状、线型和文字共同表达系列含义。
 *
 * @param {{className:string,text:string}[]} items 图例样式和文本列表。
 * @returns {HTMLElement} 图例容器。
 */
function createLegend(items) {
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  items.forEach((item) => {
    const entry = document.createElement("span");
    const swatch = document.createElement("i");
    swatch.className = item.className;
    const text = document.createTextNode(item.text);
    entry.append(swatch, text);
    legend.append(entry);
  });
  return legend;
}

/**
 * 按最多 500 根蜡烛聚合可见分钟数据，保留真实开高低收。
 *
 * @param {object[]} data 完整分钟图表数据。
 * @param {number} start 可见区间起始索引。
 * @param {number} end 可见区间结束索引，不包含该位置。
 * @returns {object[]} 含原始起止索引的聚合蜡烛数组。
 */
function aggregateCandles(data, start, end) {
  const size = Math.max(1, Math.ceil((end - start) / 500));
  const candles = [];
  for (let index = start; index < end; index += size) {
    const sliceEnd = Math.min(end, index + size);
    const slice = data.slice(index, sliceEnd);
    candles.push({ startIndex: index, endIndex: sliceEnd, timestamp: slice[0].timestamp, open: slice[0].open, close: slice[slice.length - 1].close, high: Math.max(...slice.map((item) => item.high)), low: Math.min(...slice.map((item) => item.low)) });
  }
  return candles;
}

/**
 * 准备高分屏 Canvas 绘图上下文并清空上一帧。
 *
 * @param {HTMLCanvasElement} canvas 目标画布。
 * @param {number} cssHeight 画布 CSS 高度。
 * @returns {{context:CanvasRenderingContext2D,width:number,height:number}} 已缩放绘图环境。
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
 * 绘制可见区间 K 线、价格刻度、十字线和买卖成交点。
 *
 * @param {HTMLCanvasElement} canvas K 线画布。
 * @param {object[]} data 完整分钟数据。
 * @param {object[]} trades 模拟成交列表。
 * @param {Map<string,number>} timestampIndexes 时间戳到分钟索引映射。
 * @param {number} start 可见起始索引。
 * @param {number} end 可见结束索引。
 * @param {number|null} hoverIndex 当前键盘或鼠标选中索引。
 * @returns {void}
 */
function drawCandlestickChart(canvas, data, trades, timestampIndexes, start, end, hoverIndex) {
  const { context, width, height } = prepareCanvas(canvas, 410);
  const margin = { left: 58, right: 18, top: 18, bottom: 32 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const visible = data.slice(start, end);
  const priceLow = Math.min(...visible.map((item) => item.low));
  const priceHigh = Math.max(...visible.map((item) => item.high));
  const pricePadding = Math.max(0.001, (priceHigh - priceLow) * 0.06);
  const minimum = priceLow - pricePadding;
  const maximum = priceHigh + pricePadding;
  const y = (price) => margin.top + (maximum - price) / Math.max(0.000001, maximum - minimum) * plotHeight;
  drawGrid(context, margin, plotWidth, plotHeight, minimum, maximum, 5, (value) => value.toFixed(3));
  const candles = aggregateCandles(data, start, end);
  const step = plotWidth / candles.length;
  const bodyWidth = Math.max(1, Math.min(8, step * 0.68));
  candles.forEach((candle, position) => {
    const x = margin.left + (position + 0.5) * step;
    const openY = y(candle.open); const closeY = y(candle.close); const highY = y(candle.high); const lowY = y(candle.low);
    const bullish = candle.close >= candle.open;
    context.strokeStyle = bullish ? "#26a69a" : "#ef5350";
    context.lineWidth = 1;
    context.beginPath(); context.moveTo(x, highY); context.lineTo(x, lowY); context.stroke();
    const top = Math.min(openY, closeY); const bodyHeight = Math.max(1, Math.abs(openY - closeY));
    if (bullish) {
      context.fillStyle = "#26a69a";
      context.fillRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
    } else {
      context.fillStyle = "#111720";
      context.fillRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
      context.strokeRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
    }
  });
  trades.forEach((trade) => {
    const index = timestampIndexes.get(trade.timestamp);
    if (index === undefined || index < start || index >= end) return;
    const x = margin.left + (index - start + 0.5) / Math.max(1, end - start) * plotWidth;
    const markerY = y(trade.price) + (trade.side === "BUY" ? 14 : -14);
    drawTradeMarker(context, x, markerY, trade.side);
  });
  if (hoverIndex !== null && hoverIndex >= start && hoverIndex < end) {
    const point = data[hoverIndex];
    const x = margin.left + (hoverIndex - start + 0.5) / Math.max(1, end - start) * plotWidth;
    context.save();
    context.setLineDash([4, 4]); context.strokeStyle = "rgba(226,232,240,.48)";
    context.beginPath(); context.moveTo(x, margin.top); context.lineTo(x, margin.top + plotHeight); context.moveTo(margin.left, y(point.close)); context.lineTo(margin.left + plotWidth, y(point.close)); context.stroke();
    context.restore();
  }
  drawTimeLabels(context, data, start, end, margin, plotWidth, height);
}

/**
 * 绘制网格实线和直接持有虚线累计收益曲线。
 *
 * @param {HTMLCanvasElement} canvas 收益对比画布。
 * @param {object[]} data 完整分钟收益数据。
 * @param {number} start 可见起始索引。
 * @param {number} end 可见结束索引。
 * @returns {void}
 */
function drawProfitChart(canvas, data, start, end) {
  const { context, width, height } = prepareCanvas(canvas, 270);
  const margin = { left: 58, right: 18, top: 16, bottom: 32 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const visible = data.slice(start, end);
  const values = visible.flatMap((item) => [item.grid_profit, item.hold_profit, 0]);
  const minimum = Math.min(...values); const maximum = Math.max(...values);
  const padding = Math.max(1, (maximum - minimum) * 0.08);
  const low = minimum - padding; const high = maximum + padding;
  const y = (value) => margin.top + (high - value) / Math.max(0.000001, high - low) * plotHeight;
  drawGrid(context, margin, plotWidth, plotHeight, low, high, 5, (value) => money(value));
  const stride = Math.max(1, Math.ceil(visible.length / 1000));
  drawSeries(context, data, start, end, stride, "grid_profit", "#41e6a1", [], margin, plotWidth, y);
  drawSeries(context, data, start, end, stride, "hold_profit", "#f7c66a", [7, 5], margin, plotWidth, y);
  if (low < 0 && high > 0) {
    context.save(); context.strokeStyle = "rgba(226,232,240,.32)"; context.setLineDash([2, 3]); context.beginPath(); context.moveTo(margin.left, y(0)); context.lineTo(margin.left + plotWidth, y(0)); context.stroke(); context.restore();
  }
  drawTimeLabels(context, data, start, end, margin, plotWidth, height);
}

/**
 * 绘制带价格或收益刻度的低对比网格。
 *
 * @param {CanvasRenderingContext2D} context 绘图上下文。
 * @param {object} margin 图表边距。
 * @param {number} plotWidth 绘图区宽度。
 * @param {number} plotHeight 绘图区高度。
 * @param {number} minimum 纵轴最小值。
 * @param {number} maximum 纵轴最大值。
 * @param {number} divisions 网格分段数。
 * @param {(value:number)=>string} formatter 刻度格式化函数。
 * @returns {void}
 */
function drawGrid(context, margin, plotWidth, plotHeight, minimum, maximum, divisions, formatter) {
  context.font = "12px ui-monospace, SFMono-Regular, Consolas, monospace";
  context.textAlign = "right"; context.textBaseline = "middle";
  for (let index = 0; index <= divisions; index += 1) {
    const ratio = index / divisions; const y = margin.top + ratio * plotHeight;
    context.strokeStyle = "rgba(141,155,171,.14)"; context.beginPath(); context.moveTo(margin.left, y); context.lineTo(margin.left + plotWidth, y); context.stroke();
    context.fillStyle = "#8d9bab"; context.fillText(formatter(maximum - ratio * (maximum - minimum)), margin.left - 7, y);
  }
}

/**
 * 绘制单条累计收益折线，支持实线或虚线编码。
 *
 * @param {CanvasRenderingContext2D} context 绘图上下文。
 * @param {object[]} data 完整图表数据。
 * @param {number} start 可见起始索引。
 * @param {number} end 可见结束索引。
 * @param {number} stride 抽样步长。
 * @param {string} field 收益字段名。
 * @param {string} color 折线颜色。
 * @param {number[]} dash Canvas 虚线数组；空数组表示实线。
 * @param {object} margin 图表边距。
 * @param {number} plotWidth 绘图区宽度。
 * @param {(value:number)=>number} y 纵轴坐标转换函数。
 * @returns {void}
 */
function drawSeries(context, data, start, end, stride, field, color, dash, margin, plotWidth, y) {
  context.save(); context.strokeStyle = color; context.lineWidth = 2; context.setLineDash(dash); context.beginPath();
  let started = false;
  for (let index = start; index < end; index += stride) {
    const x = margin.left + (index - start) / Math.max(1, end - start - 1) * plotWidth;
    const pointY = y(data[index][field]);
    if (!started) { context.moveTo(x, pointY); started = true; } else context.lineTo(x, pointY);
  }
  const lastX = margin.left + plotWidth; const lastY = y(data[end - 1][field]); context.lineTo(lastX, lastY); context.stroke(); context.restore();
}

/**
 * 绘制买入上三角或卖出下三角，并附带中文方向文字。
 *
 * @param {CanvasRenderingContext2D} context 绘图上下文。
 * @param {number} x 标记中心横坐标。
 * @param {number} y 标记中心纵坐标。
 * @param {string} side `BUY` 或 `SELL`。
 * @returns {void}
 */
function drawTradeMarker(context, x, y, side) {
  const buy = side === "BUY";
  context.save(); context.fillStyle = buy ? "#60a5fa" : "#f7c66a"; context.strokeStyle = "#080b10"; context.lineWidth = 2;
  context.beginPath();
  if (buy) { context.moveTo(x, y - 7); context.lineTo(x - 7, y + 6); context.lineTo(x + 7, y + 6); }
  else { context.moveTo(x, y + 7); context.lineTo(x - 7, y - 6); context.lineTo(x + 7, y - 6); }
  context.closePath(); context.fill(); context.stroke();
  context.font = "bold 11px Microsoft YaHei, sans-serif"; context.textAlign = "center"; context.textBaseline = buy ? "top" : "bottom"; context.fillText(buy ? "买" : "卖", x, buy ? y + 9 : y - 9); context.restore();
}

/**
 * 在横轴左右两端显示可见时间范围。
 *
 * @param {CanvasRenderingContext2D} context 绘图上下文。
 * @param {object[]} data 完整图表数据。
 * @param {number} start 可见起始索引。
 * @param {number} end 可见结束索引。
 * @param {object} margin 图表边距。
 * @param {number} plotWidth 绘图区宽度。
 * @param {number} height Canvas CSS 高度。
 * @returns {void}
 */
function drawTimeLabels(context, data, start, end, margin, plotWidth, height) {
  context.fillStyle = "#8d9bab"; context.font = "12px ui-monospace, SFMono-Regular, Consolas, monospace"; context.textBaseline = "bottom";
  context.textAlign = "left"; context.fillText(dateTime(data[start].timestamp), margin.left, height - 3);
  context.textAlign = "right"; context.fillText(dateTime(data[end - 1].timestamp), margin.left + plotWidth, height - 3);
}

/**
 * 用结构化 DOM 渲染完整回测报告，避免把行情名称当作 HTML 注入。
 *
 * @param {object} result 服务端返回的完整回测报告。
 * @returns {void}
 */
function renderReport(result) {
  reportContainer.replaceChildren();
  reportContainer.hidden = false;
  const winner = result.comparison.winner === "GRID" ? "网格策略" : result.comparison.winner === "BUY_AND_HOLD" ? "直接持有" : "收益持平";
  const verdict = document.createElement("div");
  verdict.className = `verdict ${result.comparison.winner === "GRID" ? "" : "hold"}`;
  const verdictTitle = document.createElement("h2");
  verdictTitle.textContent = `本区间表现更优：${winner}`;
  const verdictText = document.createElement("p");
  verdictText.textContent = `网格相对直接持有的超额收益为 ${money(result.comparison.excess_profit)} 元，超额收益率 ${percent(result.comparison.excess_return_percent)}。`;
  verdict.append(verdictTitle, verdictText);

  const metrics = document.createElement("div");
  metrics.className = "metric-grid";
  metrics.append(
    createMetric("网格收益", `${money(result.grid.profit)} 元`, result.grid.profit),
    createMetric("直接持有收益", `${money(result.buy_and_hold.profit)} 元`, result.buy_and_hold.profit),
    createMetric("网格成交", `${result.grid.trade_count} 笔`),
    createMetric("累计佣金", `${money(result.grid.commission)} 元`)
  );
  const chartsPanel = createChartsPanel(result);

  const detailsPanel = document.createElement("section");
  detailsPanel.className = "panel report-panel";
  const detailsTitle = document.createElement("h2");
  detailsTitle.textContent = "行情与组合结果";
  const details = document.createElement("div");
  details.className = "details";
  details.append(
    createDetail("证券", `${result.market_data.symbol} ${result.market_data.name}`),
    createDetail("有效分钟数", result.market_data.bar_count),
    createDetail("实际行情区间", `${dateTime(result.market_data.started_at)} 至 ${dateTime(result.market_data.ended_at)}`),
    createDetail("首末价格", `${result.market_data.first_price.toFixed(3)} → ${result.market_data.last_price.toFixed(3)}`),
    createDetail("共同初始组合", `${result.initial.shares} 份 + 现金 ${money(result.initial.cash)} 元`),
    createDetail("最大回撤", `网格 ${percent(result.grid.max_drawdown_percent)} / 持有 ${percent(result.buy_and_hold.max_drawdown_percent)}`),
    createDetail("报告编号", result.report_id),
    createDetail("报告 JSON", result.report_file || "历史报告文件"),
    createDetail("行情来源", result.market_data.source)
  );
  detailsPanel.append(detailsTitle, details);

  const tradesPanel = document.createElement("section");
  tradesPanel.className = "panel report-panel";
  const tradesTitle = document.createElement("h2");
  tradesTitle.textContent = "模拟成交明细";
  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["时间", "方向", "成交价", "数量", "成交额", "佣金", "交易后现金", "交易后持仓", "持仓市值", "总资产", "累计收益", "累计收益率", "原网格基准", "触发极值"].forEach((text) => {
    const th = document.createElement("th"); th.textContent = text; headRow.append(th);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  result.trades.forEach((trade) => body.append(createTradeRow(trade)));
  table.append(head, body);
  tableWrap.append(table);
  tradesPanel.append(tradesTitle, tableWrap);

  const notesPanel = document.createElement("section");
  notesPanel.className = "panel report-panel";
  const notesTitle = document.createElement("h2");
  notesTitle.textContent = "回测口径与限制";
  notesPanel.append(notesTitle);
  if (result.warnings.length) notesPanel.append(createList(result.warnings, "warning-list"));
  notesPanel.append(createList(result.assumptions, "assumption-list"));
  reportContainer.append(verdict, metrics, chartsPanel, detailsPanel, tradesPanel, notesPanel);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  reportContainer.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
}

/**
 * 创建单笔模拟成交表格行。
 *
 * @param {object} trade 服务端返回的成交记录。
 * @returns {HTMLTableRowElement} 完整成交行。
 */
function createTradeRow(trade) {
  const row = document.createElement("tr");
  const valueOrDash = (value, formatter = String) => value === undefined || value === null ? "—" : formatter(value);
  const values = [
    dateTime(trade.timestamp),
    trade.side,
    trade.price,
    trade.quantity,
    money(trade.gross_amount),
    money(trade.commission),
    valueOrDash(trade.cash_after, money),
    valueOrDash(trade.shares_after),
    valueOrDash(trade.position_value_after, money),
    valueOrDash(trade.total_assets_after, money),
    valueOrDash(trade.profit_after, money),
    valueOrDash(trade.return_percent_after, percent),
    trade.reference_price_before,
    trade.trigger_extreme
  ];
  values.forEach((value, index) => {
    const cell = document.createElement("td");
    if (index === 1) {
      const tag = document.createElement("span");
      tag.className = `tag ${value === "BUY" ? "buy" : "sell"}`;
      tag.textContent = value === "BUY" ? "买入" : "卖出";
      cell.append(tag);
    } else {
      cell.textContent = String(value);
      if ((index === 10 || index === 11) && value !== "—") {
        cell.className = Number(index === 10 ? trade.profit_after : trade.return_percent_after) >= 0 ? "positive" : "negative";
      }
    }
    row.append(cell);
  });
  return row;
}

/**
 * 将字符串数组转换为指定样式的无序列表。
 *
 * @param {string[]} items 需要展示的说明文本。
 * @param {string} className 列表 CSS 类名。
 * @returns {HTMLUListElement} 完整说明列表。
 */
function createList(items, className) {
  const list = document.createElement("ul");
  list.className = className;
  items.forEach((text) => {
    const item = document.createElement("li"); item.textContent = text; list.append(item);
  });
  return list;
}

/**
 * 读取并渲染最近保存的 JSON 报告摘要。
 *
 * @returns {Promise<void>} 历史区域完成刷新后结束。
 */
async function loadHistory() {
  try {
    const payload = await requestJson("/api/backtests");
    historyContainer.replaceChildren();
    if (!payload.data.length) {
      const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "尚无回测报告。"; historyContainer.append(empty); return;
    }
    payload.data.forEach((item) => historyContainer.append(createHistoryItem(item)));
  } catch (error) {
    historyContainer.textContent = `历史报告读取失败：${error.message}`;
  }
}

/**
 * 创建一个可点击查看完整内容的历史报告摘要。
 *
 * @param {object} item 服务端返回的历史摘要。
 * @returns {HTMLElement} 历史报告行。
 */
function createHistoryItem(item) {
  const row = document.createElement("div");
  row.className = "history-item";
  const identity = document.createElement("strong"); identity.textContent = `${item.symbol} · ${dateTime(item.created_at)}`;
  const grid = document.createElement("span"); grid.textContent = `网格 ${money(item.grid_profit)} 元`;
  const hold = document.createElement("span"); hold.textContent = `持有 ${money(item.hold_profit)} 元`;
  const button = document.createElement("button"); button.className = "button secondary"; button.type = "button"; button.textContent = "查看";
  button.addEventListener("click", async () => {
    try { showMessage("正在读取历史 JSON 报告……", "loading"); renderReport(await requestJson(`/api/backtests/${encodeURIComponent(item.report_id)}`)); showMessage(""); }
    catch (error) { showMessage(error.message, "error"); }
  });
  row.append(identity, grid, hold, button);
  return row;
}

/**
 * 初始化服务端配置和历史报告，并注册用户交互。
 *
 * @returns {Promise<void>} 页面初始化完成后结束。
 */
async function initialize() {
  try {
    const savedConfig = await requestJson("/api/config");
    // 优化页通过会话存储传递一组完整配置；读取后立即清除，避免以后刷新重复覆盖。
    const pendingText = sessionStorage.getItem("grid-backtest-pending-config");
    if (pendingText) {
      sessionStorage.removeItem("grid-backtest-pending-config");
      fillForm({ ...savedConfig, ...JSON.parse(pendingText) });
      showMessage("已载入参数优化结果，可以直接开始详细回测。", "normal");
    } else {
      fillForm(savedConfig);
    }
  }
  catch (error) { fillForm(DEFAULT_CONFIG); showMessage(`配置读取失败，已使用指定方案：${error.message}`, "error"); }
  await loadHistory();
}

resetButton.addEventListener("click", () => fillForm(DEFAULT_CONFIG));
copyJsonButton.addEventListener("click", () => void copyParametersAsJson());
restoreJsonButton.addEventListener("click", openJsonRestoreDialog);
confirmRestoreButton.addEventListener("click", restoreParametersFromJson);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  runButton.disabled = true;
  showMessage("正在分段提取分钟行情并执行回测，请稍候……", "loading");
  try {
    const result = await requestJson("/api/backtests", { method: "POST", body: JSON.stringify(readForm()) });
    renderReport(result);
    showMessage("回测完成，配置、行情缓存和完整报告均已保存为 JSON。", "normal");
    await loadHistory();
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    runButton.disabled = false;
  }
});

void initialize();
