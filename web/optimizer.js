"use strict";

const form = document.querySelector("#optimizer-form");
const startButton = document.querySelector("#start-button");
const cancelButton = document.querySelector("#cancel-button");
const messageContainer = document.querySelector("#optimizer-message");
const progressPanel = document.querySelector("#progress-panel");
const progressTitle = document.querySelector("#progress-title");
const progressPercent = document.querySelector("#progress-percent");
const progressTrack = document.querySelector(".progress-track");
const progressFill = document.querySelector("#progress-fill");
const progressCount = document.querySelector("#progress-count");
const progressTime = document.querySelector("#progress-time");
const progressId = document.querySelector("#progress-id");
const resultsContainer = document.querySelector("#optimization-results");
const bestTableContainer = document.querySelector("#best-table");
const worstTableContainer = document.querySelector("#worst-table");
const coarseValueLimitInput = document.querySelector("#coarse-value-limit");
const coarseCombinationCount = document.querySelector("#coarse-combination-count");
const CURRENT_JOB_STORAGE_KEY = "grid-backtest-current-optimization-job";

let baseConfig = null;
let currentJobId = null;
let pollTimer = null;

/**
 * 调用本机 JSON API，并将非成功响应转换为可直接展示的中文错误。
 *
 * @param {string} path API 路径。
 * @param {RequestInit} [options] 可选请求方法、请求头和请求体。
 * @returns {Promise<object>} 已解析的 JSON 对象。
 */
async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.message || `请求失败：HTTP ${response.status}`);
    // 保留状态码，恢复旧任务时只清理服务端已确认不存在的任务编号。
    error.status = response.status;
    throw error;
  }
  return payload;
}

/**
 * 将最近一次优化任务编号保存到浏览器，允许页面跳转或刷新后继续查看。
 *
 * @param {string} jobId 后台返回的优化任务编号。
 * @returns {void}
 */
function rememberCurrentJob(jobId) {
  try {
    localStorage.setItem(CURRENT_JOB_STORAGE_KEY, jobId);
  } catch (error) {
    // 浏览器禁用本地存储时不影响后台任务本身，只放弃跨页面自动恢复能力。
    console.warn("无法保存优化任务编号", error);
  }
}

/**
 * 读取浏览器中最近一次优化任务编号。
 *
 * @returns {string|null} 存在时返回任务编号，否则返回 null。
 */
function readRememberedJob() {
  try {
    return localStorage.getItem(CURRENT_JOB_STORAGE_KEY);
  } catch (error) {
    // 浏览器禁用本地存储时视为没有可恢复任务。
    console.warn("无法读取优化任务编号", error);
    return null;
  }
}

/**
 * 清除服务端已经确认不存在的旧优化任务编号。
 *
 * @returns {void}
 */
function forgetCurrentJob() {
  try {
    localStorage.removeItem(CURRENT_JOB_STORAGE_KEY);
  } catch (error) {
    // 清理失败不会影响当前页面，后续恢复时仍会再次校验任务是否存在。
    console.warn("无法清除优化任务编号", error);
  }
}

/**
 * 在页面状态区域显示普通、加载或错误消息。
 *
 * @param {string} text 需要展示的中文说明；空字符串会清空区域。
 * @param {string} [kind] 可选的 loading 或 error 样式名称。
 * @returns {void}
 */
function showMessage(text, kind = "normal") {
  messageContainer.replaceChildren();
  if (!text) return;
  const message = document.createElement("div");
  message.className = `message ${kind}`;
  message.textContent = text;
  messageContainer.append(message);
}

/**
 * 将普通回测最近保存的固定配置填入优化页面。
 *
 * @param {object} config 服务端返回的完整策略配置。
 * @returns {void}
 */
function fillFixedConfig(config) {
  document.querySelectorAll("[data-config-field]").forEach((input) => {
    input.value = config[input.dataset.configField];
  });
}

/**
 * 将任务快照中的固定配置、搜索范围和采样数量还原到优化表单。
 *
 * @param {object} snapshot 服务端返回的完整任务状态快照。
 * @returns {void}
 */
function fillTaskSettings(snapshot) {
  if (snapshot.config) fillFixedConfig(snapshot.config);
  const rangeInputs = {
    rise_trigger_percent: ["#rise-min", "#rise-max"],
    sell_pullback_percent: ["#sell-min", "#sell-max"],
    fall_trigger_percent: ["#fall-min", "#fall-max"],
    buy_rebound_percent: ["#buy-min", "#buy-max"]
  };
  Object.entries(rangeInputs).forEach(([name, selectors]) => {
    const range = snapshot.ranges?.[name];
    if (!range) return;
    document.querySelector(selectors[0]).value = range.minimum;
    document.querySelector(selectors[1]).value = range.maximum;
  });
  if (Number.isInteger(snapshot.coarse_value_limit)) {
    coarseValueLimitInput.value = snapshot.coarse_value_limit;
    renderCoarseCombinationCount();
  }
}

/**
 * 从固定条件表单读取完整策略配置并保留四个当前信号参数。
 *
 * @returns {object} 可提交给 StrategyConfig 校验器的完整配置。
 */
function readFixedConfig() {
  const config = { ...baseConfig };
  const integerFields = new Set(["days", "lot_size"]);
  document.querySelectorAll("[data-config-field]").forEach((input) => {
    const field = input.dataset.configField;
    config[field] = field === "symbol" ? input.value.trim() : integerFields.has(field) ? Number.parseInt(input.value, 10) : Number(input.value);
  });
  return config;
}

/**
 * 读取并构造四个参数的最小值、最大值闭区间。
 *
 * @returns {object} 字段名与 Python OptimizationRanges 完全一致的范围对象。
 */
function readRanges() {
  return {
    rise_trigger_percent: { minimum: Number(document.querySelector("#rise-min").value), maximum: Number(document.querySelector("#rise-max").value) },
    sell_pullback_percent: { minimum: Number(document.querySelector("#sell-min").value), maximum: Number(document.querySelector("#sell-max").value) },
    fall_trigger_percent: { minimum: Number(document.querySelector("#fall-min").value), maximum: Number(document.querySelector("#fall-max").value) },
    buy_rebound_percent: { minimum: Number(document.querySelector("#buy-min").value), maximum: Number(document.querySelector("#buy-max").value) }
  };
}

/**
 * 在浏览器提交前检查四个范围的数值、边界和先后顺序。
 *
 * @param {object} ranges 四个参数范围。
 * @returns {void}
 * @throws {Error} 任一范围不满足 0 < 最小值 ≤ 最大值 ≤ 100 时抛出。
 */
function validateRanges(ranges) {
  const labels = {
    rise_trigger_percent: "上涨触发",
    sell_pullback_percent: "回落卖出",
    fall_trigger_percent: "下跌触发",
    buy_rebound_percent: "反弹买入"
  };
  Object.entries(ranges).forEach(([name, range]) => {
    if (!Number.isFinite(range.minimum) || !Number.isFinite(range.maximum) || range.minimum <= 0 || range.maximum > 100 || range.minimum > range.maximum) {
      throw new Error(`${labels[name]}范围必须满足 0 < 最小值 ≤ 最大值 ≤ 100`);
    }
  });
}

/**
 * 读取并校验每个参数使用的等比粗采样数量。
 *
 * @returns {number} 位于 2 至 25 之间的整数采样数量。
 * @throws {Error} 输入不是合法整数或超出后端允许范围时抛出。
 */
function readCoarseValueLimit() {
  const value = Number(coarseValueLimitInput.value);
  if (!Number.isInteger(value) || value < 2 || value > 25) {
    throw new Error("等比粗采样值必须是 2 至 25 之间的整数");
  }
  return value;
}

/**
 * 根据当前输入实时更新第一轮四维笛卡尔积的理论组合数。
 *
 * @returns {void}
 */
function renderCoarseCombinationCount() {
  const value = Number(coarseValueLimitInput.value);
  const combinations = Number.isInteger(value) && value >= 2 && value <= 25 ? value ** 4 : 0;
  coarseCombinationCount.textContent = combinations.toLocaleString("zh-CN");
}

/**
 * 切换运行中的按钮、表单禁用和取消入口状态。
 *
 * @param {boolean} running 是否存在排队或计算中的任务。
 * @returns {void}
 */
function setRunning(running) {
  startButton.disabled = running;
  startButton.textContent = running ? "优化计算中…" : "开始三轮优化";
  cancelButton.hidden = !running;
  form.querySelectorAll("input").forEach((input) => { input.disabled = running; });
}

/**
 * 根据任务快照刷新轮次、进度条、组合数、耗时和任务编号。
 *
 * @param {object} snapshot 优化任务状态快照。
 * @returns {void}
 */
function renderProgress(snapshot) {
  progressPanel.hidden = false;
  const total = Math.max(0, Number(snapshot.estimated_total));
  const completed = Math.max(0, Number(snapshot.completed));
  const percent = total > 0 ? Math.min(100, completed / total * 100) : 0;
  progressTitle.textContent = snapshot.round_name || "正在准备";
  progressPercent.textContent = `${percent.toFixed(1)}%`;
  progressFill.style.transform = `scaleX(${percent / 100})`;
  progressTrack.setAttribute("aria-valuenow", percent.toFixed(1));
  progressCount.textContent = `${completed.toLocaleString("zh-CN")} / ${total.toLocaleString("zh-CN")}`;
  progressTime.textContent = `${Number(snapshot.elapsed_seconds).toFixed(2)} 秒`;
  progressId.textContent = snapshot.job_id;
}

/**
 * 以固定小数位展示策略百分比，避免八位归一化值产生无意义尾零。
 *
 * @param {number} value 原始参数或收益百分比。
 * @param {number} [digits] 最多保留的小数位数。
 * @returns {string} 本地化后的百分数文本。
 */
function formatPercent(value, digits = 4) {
  return `${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: digits })}%`;
}

/**
 * 将指定优化结果与固定配置合并后带回普通回测页。
 *
 * @param {object} result 用户选择的一条候选结果。
 * @param {object} config 本次任务使用的完整固定配置。
 * @returns {void}
 */
function applyResult(result, config) {
  const pending = {
    ...config,
    rise_trigger_percent: result.rise_trigger_percent,
    sell_pullback_percent: result.sell_pullback_percent,
    fall_trigger_percent: result.fall_trigger_percent,
    buy_rebound_percent: result.buy_rebound_percent
  };
  sessionStorage.setItem("grid-backtest-pending-config", JSON.stringify(pending));
  window.location.href = "/index.html";
}

/**
 * 构造最优或最差结果的可访问数据表。
 *
 * @param {object[]} results 已按后端确定性规则排序的候选列表。
 * @param {object} config 本任务使用的固定策略配置。
 * @param {string} caption 面向屏幕阅读器的表格标题。
 * @returns {HTMLTableElement} 完整结果表格。
 */
function createResultTable(results, config, caption) {
  const table = document.createElement("table");
  table.className = "optimizer-table";
  const tableCaption = document.createElement("caption");
  tableCaption.className = "sr-chart-summary";
  tableCaption.textContent = caption;
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["排名", "收益率", "超额收益率", "最大回撤", "成交", "佣金（元）", "上涨触发", "回落卖出", "下跌触发", "反弹买入", "操作"].forEach((text) => {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = text;
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  results.forEach((result, index) => {
    const row = document.createElement("tr");
    const values = [
      index + 1,
      formatPercent(result.return_percent),
      formatPercent(result.excess_return_percent),
      formatPercent(result.max_drawdown_percent),
      `${result.trade_count}（买 ${result.buy_count} / 卖 ${result.sell_count}）`,
      Number(result.commission).toFixed(2),
      formatPercent(result.rise_trigger_percent, 2),
      formatPercent(result.sell_pullback_percent, 2),
      formatPercent(result.fall_trigger_percent, 2),
      formatPercent(result.buy_rebound_percent, 2)
    ];
    values.forEach((value, valueIndex) => {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      if (valueIndex === 1) cell.className = Number(result.return_percent) >= 0 ? "positive" : "negative";
      row.append(cell);
    });
    const actionCell = document.createElement("td");
    const applyButton = document.createElement("button");
    applyButton.type = "button";
    applyButton.className = "button secondary compact-button";
    applyButton.textContent = "应用到回测";
    applyButton.addEventListener("click", () => applyResult(result, config));
    actionCell.append(applyButton);
    row.append(actionCell);
    body.append(row);
  });
  table.append(tableCaption, head, body);
  return table;
}

/**
 * 渲染当前任务最好与最差的两张排名表。
 *
 * @param {object} snapshot 含 best、worst 和 config 的任务快照。
 * @returns {void}
 */
function renderResults(snapshot) {
  if (!snapshot.best.length && !snapshot.worst.length) return;
  resultsContainer.hidden = false;
  bestTableContainer.replaceChildren(createResultTable(snapshot.best, snapshot.config, "最终收益率最高的十组迭代结果"));
  worstTableContainer.replaceChildren(createResultTable(snapshot.worst, snapshot.config, "最终收益率最低的十组迭代结果"));
}

/**
 * 停止轮询并恢复表单操作状态。
 *
 * @returns {void}
 */
function stopPolling() {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
  setRunning(false);
}

/**
 * 读取一次任务状态，并根据终态继续或停止定时轮询。
 *
 * @returns {Promise<void>} 当前轮询请求及页面更新完成后结束。
 */
async function pollJob() {
  if (!currentJobId) return;
  try {
    const snapshot = await requestJson(`/api/optimizations/${encodeURIComponent(currentJobId)}`);
    renderProgress(snapshot);
    renderResults(snapshot);
    if (snapshot.status === "completed") {
      stopPolling();
      showMessage(`三轮迭代完成，共评估 ${Number(snapshot.completed).toLocaleString("zh-CN")} 个唯一组合，结果已保存为 JSON。`);
      resultsContainer.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    } else if (snapshot.status === "cancelled") {
      stopPolling();
      showMessage("参数优化已取消，已完成的阶段性排名保留在当前页面。", "error");
    } else if (snapshot.status === "failed") {
      stopPolling();
      showMessage(`参数优化失败：${snapshot.error || "未知错误"}`, "error");
    } else {
      pollTimer = window.setTimeout(pollJob, 750);
    }
  } catch (error) {
    if (error.status === 404) {
      forgetCurrentJob();
      currentJobId = null;
    }
    stopPolling();
    showMessage(`读取优化进度失败：${error.message}`, "error");
  }
}

/**
 * 恢复页面跳转或刷新前保存的优化任务，并按其状态继续轮询或展示最终结果。
 *
 * @returns {Promise<boolean>} 找到并恢复任务时返回 true，没有有效任务时返回 false。
 */
async function restoreCurrentJob() {
  const rememberedJobId = readRememberedJob();
  if (!rememberedJobId) return false;
  currentJobId = rememberedJobId;
  try {
    const snapshot = await requestJson(`/api/optimizations/${encodeURIComponent(currentJobId)}`);
    fillTaskSettings(snapshot);
    renderProgress(snapshot);
    renderResults(snapshot);
    if (snapshot.status === "queued" || snapshot.status === "running") {
      setRunning(true);
      showMessage("已恢复正在运行的参数优化任务，页面会继续自动更新进度。", "loading");
      await pollJob();
    } else if (snapshot.status === "completed") {
      setRunning(false);
      showMessage(`已恢复最近完成的优化任务，共评估 ${Number(snapshot.completed).toLocaleString("zh-CN")} 个唯一组合。`);
    } else if (snapshot.status === "cancelled") {
      setRunning(false);
      showMessage("已恢复最近取消的优化任务，阶段性排名仍保留在页面中。", "error");
    } else {
      setRunning(false);
      showMessage(`最近的参数优化任务失败：${snapshot.error || "未知错误"}`, "error");
    }
    return true;
  } catch (error) {
    currentJobId = null;
    if (error.status === 404) {
      forgetCurrentJob();
      return false;
    }
    throw error;
  }
}

/**
 * 读取默认配置并初始化独立优化页面。
 *
 * @returns {Promise<void>} 默认配置填充完成后结束。
 */
async function initialize() {
  try {
    baseConfig = await requestJson("/api/config");
    fillFixedConfig(baseConfig);
    await restoreCurrentJob();
  } catch (error) {
    showMessage(`无法初始化参数优化页面：${error.message}`, "error");
    startButton.disabled = true;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const ranges = readRanges();
    validateRanges(ranges);
    const coarseValueLimit = readCoarseValueLimit();
    setRunning(true);
    resultsContainer.hidden = true;
    showMessage("正在创建后台任务并获取一次分钟行情……", "loading");
    const snapshot = await requestJson("/api/optimizations", {
      method: "POST",
      body: JSON.stringify({ config: readFixedConfig(), ranges, coarse_value_limit: coarseValueLimit })
    });
    currentJobId = snapshot.job_id;
    rememberCurrentJob(currentJobId);
    renderProgress(snapshot);
    showMessage("后台优化已启动，页面会自动更新每一轮进度。", "loading");
    await pollJob();
  } catch (error) {
    setRunning(false);
    showMessage(error.message, "error");
  }
});

// 输入变化时即时反馈粗搜索规模，帮助用户在精度和耗时之间做选择。
coarseValueLimitInput.addEventListener("input", renderCoarseCombinationCount);

cancelButton.addEventListener("click", async () => {
  if (!currentJobId || !window.confirm("确定取消当前参数优化任务吗？已完成的计算不会写入最终结果文件。")) return;
  cancelButton.disabled = true;
  try {
    await requestJson(`/api/optimizations/${encodeURIComponent(currentJobId)}`, { method: "DELETE" });
    showMessage("已发送取消请求，当前计算批次结束后会停止。", "loading");
  } catch (error) {
    showMessage(`取消任务失败：${error.message}`, "error");
  } finally {
    cancelButton.disabled = false;
  }
});

void initialize();
