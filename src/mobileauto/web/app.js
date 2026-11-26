const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let api = null;
let state = null;
let selectedIndex = null;
let recordFiles = [];
let recordFilter = "all";
let tasksCache = [];
let recordingDraft = [];
let selectingPoint = false;
let selectButtonMode = false;

// 可选能力清单（与 flow_engine.DEFAULT_CAPABILITIES 对齐）
const CAPABILITIES = [
  ["ocr", "OCR识别"], ["action_verify", "动作验证"], ["anti_stuck", "防卡死"],
  ["dedupe", "点击去重"], ["structured_log", "分层日志"], ["end_detect", "结束检测"],
  ["auto_start", "自动启动"], ["entitlement", "结束信息"],
];
const DEFAULT_CAPS = Object.fromEntries(CAPABILITIES.map(([k]) => [k, true]));

function renderCapabilities(caps) {
  const grid = $("#capGrid");
  if (!grid) return;
  const values = { ...DEFAULT_CAPS, ...(caps || {}) };
  grid.innerHTML = "";
  CAPABILITIES.forEach(([key, label]) => {
    const wrap = document.createElement("label");
    wrap.className = "check cap-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!values[key];
    cb.dataset.cap = key;
    cb.addEventListener("change", syncCapsToJson);
    wrap.appendChild(cb);
    wrap.appendChild(document.createTextNode(label));
    grid.appendChild(wrap);
  });
}

function readCapsFromJson() {
  try {
    const data = JSON.parse($("#taskJson").value || "{}");
    return { ...DEFAULT_CAPS, ...(data.capabilities || {}) };
  } catch (e) {
    return { ...DEFAULT_CAPS };
  }
}

function syncCapsToJson() {
  try {
    const data = JSON.parse($("#taskJson").value || "{}");
    const caps = {};
    $$("#capGrid input[data-cap]").forEach((cb) => { caps[cb.dataset.cap] = cb.checked; });
    data.capabilities = caps;
    $("#taskJson").value = JSON.stringify(data, null, 2);
  } catch (e) { /* JSON 无效时不覆盖 */ }
}

function syncCapsFromJson() {
  const caps = readCapsFromJson();
  $$("#capGrid input[data-cap]").forEach((cb) => { cb.checked = !!caps[cb.dataset.cap]; });
}

window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  bindEvents();
  await call("set_stream_quality", !$("#showElements").checked);
  await poll();
  await loadTaskSelect();
  await loadExecution();
  setInterval(poll, 900);
  setInterval(async () => {
    if ($("#page-device").classList.contains("active")) {
      await loadExecution();
      if (!$("#activityList")?.dataset.editing) await loadRecording();
    }
  }, 1500);
});

async function call(name, ...args) {
  if (!api || typeof api[name] !== "function") return null;
  try {
    return await api[name](...args);
  } catch (e) {
    console.error(name, e);
    return null;
  }
}

function bindEvents() {
  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => showPage(btn.dataset.page));
  });

  $("#autoConnect").addEventListener("change", async () => {
    await call("set_auto_connect", $("#autoConnect").checked);
    await poll();
  });
  $("#scanBtn").addEventListener("click", async () => {
    const address = $("#deviceAddress").value.trim();
    await call("remember_address", address);
    await call("connect", "auto", address);
    await poll();
  });
  $("#refreshBtn").addEventListener("click", async () => {
    await call("refresh");
    await poll();
  });
  $("#diagnoseBtn").addEventListener("click", async () => {
    const info = await call("diagnose_device");
    if (!info) return;
    const lines = [
      `adb: ${info.adb || "未找到"}`,
      `adb设备: ${(info.adb_devices || []).join(", ") || "无"}`,
      `本机u2服务: ${info.u2_local ? "可用" : "不可用"}`,
      ...(info.hints || []),
    ];
    $("#diagBox").textContent = lines.join("\n");
    $("#diagBox").classList.remove("hidden");
  });
  $("#disconnectBtn").addEventListener("click", async () => {
    await call("disconnect");
    await poll();
  });
  $("#scrollUpBtn").addEventListener("click", () => call("scroll_page", "up"));
  $("#scrollDownBtn").addEventListener("click", () => call("scroll_page", "down"));
  $("#backBtn").addEventListener("click", () => call("press_back"));
  $("#homeBtn").addEventListener("click", () => call("press_home"));
  $("#recentsBtn").addEventListener("click", () => call("press_recents"));
  $("#showElements").addEventListener("change", async () => {
    await call("set_stream_quality", !$("#showElements").checked);
    renderFromState();
    await poll();
  });
  [$("#filterInput"), $("#hideSystem")].forEach((node) => {
    node.addEventListener("input", renderFromState);
    node.addEventListener("change", renderFromState);
  });

  $("#startTaskBtn").addEventListener("click", async () => {
    const key = $("#taskSelect").value || (state && state.task_key);
    await call("start_task", key || null);
    await loadExecution();
    await poll();
  });
  $("#stopTaskBtn").addEventListener("click", async () => {
    await call("stop_task");
    await poll();
  });

  $("#collectBtn").addEventListener("click", async () => {
    $("#captureResult").innerHTML = '<p class="hint">采集中...</p>';
    const result = await call("capture_page");
    renderCapture(result);
    await poll();
  });
  $("#clearActivityBtn").addEventListener("click", async () => {
    await call("clear_recorded");
    await loadRecording();
    await poll();
  });
  $("#recordToggleBtn").addEventListener("click", async () => {
    const info = await call("get_recording_state");
    const on = !(info && info.recording);
    if (on) await call("start_recording");
    else await call("stop_recording");
    await loadRecording();
  });
  $("#selectPointBtn").addEventListener("click", toggleSelecting);
  $("#selectButtonBtn").addEventListener("click", toggleSelectButton);
  $("#foldLoopBtn").addEventListener("click", async () => {
    const result = await call("fold_recording_loops");
    $("#recordStatus").textContent = result && result.changed
      ? `已折叠 ${(result.loops || []).length} 处循环` : "未发现重复序列";
    await loadRecording();
  });
  bindSelection();
  $("#recordToTaskBtn").addEventListener("click", async () => {
    const name = prompt("新任务名称：", "录制生成的任务");
    if (name === null) return;
    const result = await call("recording_to_task", name, state && state.task_package ? state.task_package : "");
    if (result && result.ok) {
      alert(`已生成任务草稿：${result.task.name}（${result.task.states.length} 步），到「任务列表」编辑。`);
      await loadTasks();
    } else {
      alert("生成失败：" + ((result && result.errors || ["未知原因"]).join("; ")));
    }
  });

  $("#refreshRecordsBtn").addEventListener("click", loadRecords);
  $$(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      recordFilter = btn.dataset.filter;
      $$(".filter-btn").forEach((b) => b.classList.toggle("active", b === btn));
      loadRecords();
    });
  });

  $("#refreshTasksBtn").addEventListener("click", loadTasks);
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTaskTab(btn.dataset.tab));
  });
  $("#exportFlowBtn").addEventListener("click", exportCurrentFlow);
  $("#importFlowBtn").addEventListener("click", () => $("#importFlowFile").click());
  $("#importFlowFile").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) importFlowFile(file);
    e.target.value = "";
  });
  bindFlowZoom();
  $("#newTaskBtn").addEventListener("click", async () => {
    // 每个任务自带规则：新建时内嵌一份空 flow，不引用全局规则名
    $("#taskJson").value = JSON.stringify(
      { key: "", name: "新任务", description: "", app: "com.luna.music", flow: { loop_from: 0, max_loops: 1, steps: [] }, enabled: true, max_loops: 1, capabilities: { ...DEFAULT_CAPS } },
      null, 2
    );
    renderCapabilities(DEFAULT_CAPS);
    $("#taskEditorHint").textContent = "新建任务：请填写 key 和 name 后保存";
    $("#taskErrors").textContent = "";
  });
  $("#saveTaskBtn").addEventListener("click", saveCurrentTask);
  $("#taskJson").addEventListener("input", syncCapsFromJson);
  renderCapabilities(DEFAULT_CAPS);
  $("#deleteTaskBtn").addEventListener("click", async () => {
    const key = $("#taskJson").value ? JSON.parse($("#taskJson").value).key : "";
    if (!key || !confirm(`删除任务「${key}」？`)) return;
    await call("delete_task", key);
    await loadTasks();
    await loadTaskSelect();
    await poll();
  });
  $("#taskSelect").addEventListener("change", async () => {
    await call("select_task", $("#taskSelect").value);
    await poll();
  });

  $("#refreshReportsBtn").addEventListener("click", async () => {
    await loadReports();
    await analyzeCurrentReport();
  });
  $("#analyzeBtn").addEventListener("click", analyzeCurrentReport);
}

function showPage(page) {
  $$(".page").forEach((p) => p.classList.remove("active"));
  $$(".nav-item").forEach((b) => b.classList.remove("active"));
  $(`#page-${page}`).classList.add("active");
  $(`.nav-item[data-page="${page}"]`).classList.add("active");
  if (page === "records") loadRecords();
  if (page === "tasks") loadTasks();
  if (page === "device") {
    loadExecution();
    loadRecording();
  }
  if (page === "tasks" && !$("#taskJson").value.trim()) {
    const first = $(".task-item");
    if (first && first.dataset.key) openTask(first.dataset.key);
  }
  if (page === "analysis") {
    loadReports().then(() => analyzeCurrentReport());
  }
}

async function poll() {
  const next = await call("get_state");
  if (!next) return;
  state = next;
  updateStatus();
  renderFromState();

  const stream = await call("get_stream_status");
  if (stream) {
    $("#streamStatus").textContent = stream.status || "画面未启动";
    const img = $("#liveFrame");
    if (stream.url && img.getAttribute("src") !== stream.url) img.src = stream.url;
  }

}

function updateStatus() {
  const connected = (state.device_status || "").startsWith("已连接");
  $("#deviceStatus").textContent = state.device_status || "未连接";
  if (connected) {
    $("#deviceLabel").textContent = state.device_label || "设备已就绪";
  } else if (state.auto_connect) {
    $("#deviceLabel").textContent = `自动连接中（第 ${state.auto_attempts} 次）`;
  } else {
    $("#deviceLabel").textContent = "自动连接已关闭，请手动扫描";
  }
  if ($("#autoConnect").checked !== !!state.auto_connect) $("#autoConnect").checked = !!state.auto_connect;
  if (state.last_address && !$("#deviceAddress").value) $("#deviceAddress").value = state.last_address;
  $("#scanBtn").disabled = !!state.scanning;
  $("#disconnectBtn").disabled = !connected;
  $("#refreshBtn").disabled = !!state.refreshing || !connected;
  $("#backBtn").disabled = !connected;
  $("#homeBtn").disabled = !connected;
  $("#recentsBtn").disabled = !connected;

  $("#stopTaskBtn").disabled = !state.task_running;
  $("#startTaskBtn").disabled = !!state.task_running;
  $("#collectBtn").disabled = !!state.task_running;
}

function filteredRows() {
  if (!state) return [];
  const keyword = ($("#filterInput").value || "").trim().toLowerCase();
  const hideSystem = $("#hideSystem").checked;

  return (state.rows || []).filter((row) => {
    if (String(row.clickable).toLowerCase() !== "true") return false;
    if (hideSystem && String(row.package || "").startsWith("com.android.systemui")) return false;
    const name = `${row.text || ""} ${row.desc || ""}`;
    const hay = `${name} ${row.resource_id || ""} ${row.class || ""}`.toLowerCase();
    if (keyword && !hay.includes(keyword)) return false;
    return true;
  });
}

function renderFromState() {
  if (!state) return;
  const rows = filteredRows();
  if ($("#elementCount")) $("#elementCount").textContent = `${rows.length} 个可点击元素`;
  const phone = $("#phone");
  if (phone) phone.classList.toggle("hide-elements", !$("#showElements").checked);
  renderList(rows);
  renderPhone(rows);
}

function renderList(rows) {
  const list = $("#buttonList");
  if (!list) return; // 按钮点击已集成到实时画面，独立的按钮面板已取消
  list.innerHTML = "";
  const screen = state && state.screen;
  const sw = screen && screen[0] ? Number(screen[0]) : 0;
  const sh = screen && screen[1] ? Number(screen[1]) : 0;

  rows.slice(0, 400).forEach((row) => {
    const name = row.text || row.desc || (row.resource_id || "").split("/").pop() || "未命名按钮";
    const btn = document.createElement("button");
    btn.className = "click-btn" + (Number(row.index) === Number(selectedIndex) ? " selected" : "");
    btn.dataset.index = row.index;
    btn.title = `点击「${name}」`;
    // 按手机屏幕真实坐标定位，和画面里的红框一一对应
    if (sw && sh && row.cx !== undefined && row.cy !== undefined) {
      btn.style.left = `${(Number(row.cx) / sw) * 100}%`;
      btn.style.top = `${(Number(row.cy) / sh) * 100}%`;
      btn.textContent = name.slice(0, 8);
    } else {
      btn.style.left = "50%";
      btn.style.top = "50%";
      btn.textContent = name.slice(0, 8);
    }
    btn.addEventListener("click", () => {
      selectedIndex = row.index;
      renderFromState();
      confirmClick(row);
    });
    list.appendChild(btn);
  });
}

function renderPhone(rows) {
  const phone = $("#phone");
  phone.querySelectorAll(".phone-element").forEach((node) => node.remove());
  if (!rows.length) return;

  const screen = state.screen || guessScreen(rows);
  if (!screen || !screen[0] || !screen[1]) return;
  const sw = Number(screen[0]);
  const sh = Number(screen[1]);

  rows.slice(0, 200).forEach((row) => {
    const boxW = Number(row.x2) - Number(row.x1);
    const boxH = Number(row.y2) - Number(row.y1);
    if (boxW > sw * 0.9 && boxH > sh * 0.9) return;

    const div = document.createElement("div");
    div.className = "phone-element" + (Number(row.index) === Number(selectedIndex) ? " selected" : "");
    div.dataset.index = row.index;
    div.style.left = `${(Number(row.x1) / sw) * 100}%`;
    div.style.top = `${(Number(row.y1) / sh) * 100}%`;
    div.style.width = `${(boxW / sw) * 100}%`;
    div.style.height = `${(boxH / sh) * 100}%`;
    const label = row.text || row.desc || (row.resource_id || "").split("/").pop() || "";
    if (label) {
      const span = document.createElement("span");
      span.textContent = label.slice(0, 10);
      div.appendChild(span);
    }
    div.title = `${label}\n(${row.cx}, ${row.cy})`;
    div.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (selectButtonMode) {
        await recordPickedButton(row);
      } else {
        selectedIndex = row.index;
        confirmClick(row);
      }
    });
    phone.appendChild(div);
  });
}

function guessScreen(rows) {
  let w = 0;
  let h = 0;
  rows.forEach((row) => {
    w = Math.max(w, Number(row.x2 || 0));
    h = Math.max(h, Number(row.y2 || 0));
  });
  return [w, h];
}

// ---------- 选择已识别按钮作为录制步骤 ----------
function toggleSelectButton() {
  selectButtonMode = !selectButtonMode;
  if (selectButtonMode) {
    selectingPoint = false;
    const overlay = $("#selectOverlay");
    overlay.classList.add("hidden");
    overlay.innerHTML = "";
    $("#selectPointBtn").classList.remove("primary");
    $("#selectPointBtn").textContent = "选点/框选";
  }
  $("#phone").classList.toggle("select-button", selectButtonMode);
  const btn = $("#selectButtonBtn");
  btn.classList.toggle("primary", selectButtonMode);
  btn.textContent = selectButtonMode ? "退出选择" : "选择按钮";
}

async function recordPickedButton(row) {
  const name = row.text || row.desc || (row.resource_id || "").split("/").pop() || "未命名按钮";
  const screen = state.screen || guessScreen([row]);
  const sw = screen && screen[0] ? Number(screen[0]) : 1440;
  const sh = screen && screen[1] ? Number(screen[1]) : 3200;
  const cx = row.cx !== undefined ? Number(row.cx) : (Number(row.x1) + Number(row.x2)) / 2;
  const cy = row.cy !== undefined ? Number(row.cy) : (Number(row.y1) + Number(row.y2)) / 2;
  await call("add_manual_click", Math.round(cx), Math.round(cy), name);
  await loadRecording();
  poll();
}

// ---------- 画面选点 / 框选 ----------
function toggleSelecting() {
  selectingPoint = !selectingPoint;
  if (selectingPoint && selectButtonMode) {
    selectButtonMode = false;
    $("#phone").classList.remove("select-button");
    $("#selectButtonBtn").classList.remove("primary");
    $("#selectButtonBtn").textContent = "选择按钮";
  }
  const overlay = $("#selectOverlay");
  overlay.classList.toggle("hidden", !selectingPoint);
  overlay.innerHTML = "";
  const btn = $("#selectPointBtn");
  btn.classList.toggle("primary", selectingPoint);
  btn.textContent = selectingPoint ? "退出选点" : "选点/框选";
  if (selectingPoint) {
    const hint = document.createElement("div");
    hint.className = "select-hint";
    hint.textContent = "单击=点击该点，拖拽=框选区域中心";
    overlay.appendChild(hint);
    setTimeout(() => {
      if (hint.parentNode) hint.remove();
    }, 2600);
  }
}

function bindSelection() {
  const overlay = $("#selectOverlay");
  let start = null;

  overlay.addEventListener("pointerdown", (e) => {
    if (!selectingPoint) return;
    e.preventDefault();
    overlay.setPointerCapture(e.pointerId);
    start = { x: e.clientX, y: e.clientY };
    overlay.classList.add("dragging");
  });
  overlay.addEventListener("pointermove", (e) => {
    if (!selectingPoint || !start) return;
    const rect = overlay.getBoundingClientRect();
    const x1 = Math.min(start.x, e.clientX) - rect.left;
    const y1 = Math.min(start.y, e.clientY) - rect.top;
    const w = Math.abs(e.clientX - start.x);
    const h = Math.abs(e.clientY - start.y);
    let box = overlay.querySelector(".select-box");
    if (!box) {
      box = document.createElement("div");
      box.className = "select-box";
      box.style.cssText = "position:absolute;border:2px solid #f59e0b;background:rgba(245,158,11,.18);border-radius:3px;pointer-events:none;";
      overlay.appendChild(box);
    }
    box.style.left = `${x1}px`;
    box.style.top = `${y1}px`;
    box.style.width = `${w}px`;
    box.style.height = `${h}px`;
  });
  overlay.addEventListener("pointerup", async (e) => {
    if (!selectingPoint || !start) return;
    const rect = overlay.getBoundingClientRect();
    const dx = Math.abs(e.clientX - start.x);
    const dy = Math.abs(e.clientY - start.y);
    let cx, cy, label;
    if (dx < 6 && dy < 6) {
      cx = (e.clientX - rect.left) / rect.width;
      cy = (e.clientY - rect.top) / rect.height;
      label = "选点点击";
    } else {
      cx = ((Math.min(start.x, e.clientX) + Math.max(start.x, e.clientX)) / 2 - rect.left) / rect.width;
      cy = ((Math.min(start.y, e.clientY) + Math.max(start.y, e.clientY)) / 2 - rect.top) / rect.height;
      label = "框选区域中心";
    }
    start = null;
    overlay.classList.remove("dragging");
    overlay.querySelectorAll(".select-box").forEach((n) => n.remove());

    const screen = state && state.screen;
    if (!screen || !screen[0] || !screen[1]) {
      alert("设备未连接或分辨率未知，无法定位选点");
      return;
    }
    const x = Math.round(cx * Number(screen[0]));
    const y = Math.round(cy * Number(screen[1]));
    await call("click_xy", x, y, false);
    await call("add_manual_click", x, y, label);
    await loadRecording();
    await poll();
  });
}

function selectRow(index) {
  selectedIndex = Number(index);
  $$("#buttonList .click-btn").forEach((btn) => btn.classList.toggle("selected", Number(btn.dataset.index) === selectedIndex));
  $$(".phone-element").forEach((node) => node.classList.toggle("selected", Number(node.dataset.index) === selectedIndex));
}

async function confirmClick(row) {
  await call("click_xy", Number(row.cx), Number(row.cy));
  await poll();
}

function recordMatchesFilter(path) {
  if (recordFilter === "all") return true;
  if (recordFilter === "log") return path.endsWith(".log");
  if (recordFilter === "xml") return path.endsWith(".xml");
  if (recordFilter === "report") return path.includes("report") || path.includes("activity");
  if (recordFilter === "ocr") return path.endsWith(".txt") || path.endsWith(".json");
  return true;
}

async function loadRecords() {
  recordFiles = (await call("list_records")) || [];
  const list = $("#recordList");
  list.innerHTML = "";
  const shown = recordFiles.filter((file) => recordMatchesFilter(file.path));
  $("#recordCount").textContent = `共 ${shown.length} 个文件`;
  shown.forEach((file, index) => {
    const div = document.createElement("div");
    div.className = "record-item";
    div.textContent = `${file.mtime}  ${file.path}`;
    const sizeKb = Math.max(1, Math.round(file.size / 1024));
    div.title = `${file.name} · ${sizeKb} KB`;
    div.addEventListener("click", async () => {
      $$(".record-item").forEach((n) => n.classList.remove("active"));
      div.classList.add("active");
      const content = (await call("read_record", file.path)) || "";
      $("#recordPreview").textContent = content;
    });
    list.appendChild(div);
  });
  if (!shown.length) list.textContent = "该分类暂无文件。";
}

// ---------- 流程视图 / 流通 JSON ----------
function switchTaskTab(tab) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  if (tab !== "json" && !$("#taskJson").value.trim()) {
    const first = $(".task-item");
    if (first && first.dataset.key) openTask(first.dataset.key);
  }
}

let mermaidReady = false;

async function refreshTaskFlow(key) {
  const result = await call("task_mermaid", key);
  if (!result || !result.ok) {
    const msg = ((result && result.errors) || ["流程生成失败"]).join("; ");
    $("#flowGraph").textContent = msg;
    $("#taskErrors").textContent = msg;
    return;
  }
  $("#mermaidCode").textContent = result.mermaid || "";
  await renderFlowGraph(result.mermaid || "");
  renderFlowStats(result.analysis);
}

function initMermaid() {
  if (mermaidReady || !window.mermaid) return;
  window.mermaid.initialize({
    startOnLoad: false,
    theme: "default",
    securityLevel: "loose",
    flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis", nodeSpacing: 45, rankSpacing: 55 },
    themeVariables: { fontSize: "12px" },
  });
  mermaidReady = true;
}

function renderFlowStats(a) {
  const box = $("#flowStats");
  if (!box || !a) return;
  const items = [
    ["模式", a.mode === "priority" ? "状态优先级" : "顺序步骤"],
    ["步骤", a.steps], ["点击", a.clicks], ["循环", `${a.loops} (${a.loop_max || "∞"}轮)`],
    ["结束词", (a.end_keywords || []).slice(0, 2).join("/") || "无"],
  ];
  box.innerHTML = items.map(([k, v]) => `<span class="flow-stat"><b>${k}</b>${v}</span>`).join("");
}

let flowView = { scale: 1, x: 0, y: 0, fit: 1 };

async function renderFlowGraph(code) {
  const box = $("#flowGraph");
  if (!box) return;
  if (!code) {
    box.textContent = "暂无流程节点";
    return;
  }
  if (!window.mermaid) {
    box.textContent = "Mermaid 库未加载：请确认 vendor/mermaid.min.js 存在";
    return;
  }
  initMermaid();
  try {
    const seq = Number(box.dataset.seq || 0) + 1;
    box.dataset.seq = String(seq);
    const { svg } = await window.mermaid.render(`flowchart-${seq}`, code);
    if (Number(box.dataset.seq || 0) === seq) {
      box.innerHTML = `<div class="flow-stage">${svg}</div>`;
      fitFlowGraph();
    }
  } catch (e) {
    box.textContent = `Mermaid 渲染失败：${e.message || e}`;
  }
}

function applyFlowView() {
  const box = $("#flowGraph");
  const svg = box && box.querySelector("svg");
  const stage = box && box.querySelector(".flow-stage");
  if (!svg || !stage) return;
  svg.style.transform = `translate(${flowView.x}px, ${flowView.y}px) scale(${flowView.scale})`;
  stage.style.width = `${(Number(stage.dataset.w || 0) * flowView.scale) + 20}px`;
  stage.style.height = `${(Number(stage.dataset.h || 0) * flowView.scale) + 20}px`;
  $("#flowZoomLabel").textContent = `${Math.round(flowView.scale * 100)}%`;
}

function fitFlowGraph() {
  const box = $("#flowGraph");
  const svg = box && box.querySelector("svg");
  if (!svg) return;
  const raw = svg.getAttribute("viewBox");
  const size = (raw || "").split(/[\s,]+/).map(Number);
  const w = size[2] || svg.getBoundingClientRect().width || 800;
  const h = size[3] || svg.getBoundingClientRect().height || 600;
  const availW = Math.max(200, box.clientWidth - 28);
  const availH = Math.max(160, box.clientHeight - 28);
  const scale = Math.min(availW / w, availH / h, 1.5);
  flowView = { scale: Math.max(0.1, scale), x: 0, y: 0, fit: Math.max(0.1, scale) };
  svg.style.width = `${w}px`;
  svg.style.height = `${h}px`;
  const stage = box.querySelector(".flow-stage");
  if (stage) {
    stage.dataset.w = String(w);
    stage.dataset.h = String(h);
  }
  applyFlowView();
}

function zoomFlow(factor) {
  const box = $("#flowGraph");
  if (!box || !box.querySelector("svg")) return;
  flowView.scale = Math.min(4, Math.max(0.1, flowView.scale * factor));
  applyFlowView();
}

function bindFlowZoom() {
  $("#flowZoomInBtn").addEventListener("click", () => zoomFlow(1.2));
  $("#flowZoomOutBtn").addEventListener("click", () => zoomFlow(1 / 1.2));
  $("#flowFitBtn").addEventListener("click", fitFlowGraph);
  const box = $("#flowGraph");
  box.addEventListener("wheel", (e) => {
    if (!box.querySelector("svg")) return;
    e.preventDefault();
    zoomFlow(e.deltaY < 0 ? 1.1 : 1 / 1.1);
  }, { passive: false });
  let dragging = false;
  let start = null;
  box.addEventListener("pointerdown", (e) => {
    if (!box.querySelector("svg") || e.button !== 0) return;
    dragging = true;
    start = { x: e.clientX - flowView.x, y: e.clientY - flowView.y };
    box.setPointerCapture(e.pointerId);
  });
  box.addEventListener("pointermove", (e) => {
    if (!dragging || !start) return;
    flowView.x = e.clientX - start.x;
    flowView.y = e.clientY - start.y;
    applyFlowView();
  });
  box.addEventListener("pointerup", () => { dragging = false; start = null; });
  box.addEventListener("pointercancel", () => { dragging = false; start = null; });
  window.addEventListener("resize", () => {
    if ($("#tab-flow").classList.contains("active")) fitFlowGraph();
  });
}

async function exportCurrentFlow() {
  const key = $("#taskSelect").value;
  if (!key) {
    $("#taskErrors").textContent = "请先选择一个任务";
    switchTaskTab("json");
    return;
  }
  const result = await call("save_flow_as", key, `${key}.flow.json`);
  if (!result || result.cancelled) return;
  if (!result.ok) {
    $("#taskErrors").textContent = ((result && result.errors) || ["导出失败"]).join("; ");
    return;
  }
  $("#taskErrors").textContent = `已保存 FlowSpec v1 流程 JSON：${result.path}`;
  const preview = await call("export_task_flow", key);
  if (preview && preview.ok) {
    await renderFlowGraph(preview.mermaid || "");
    $("#mermaidCode").textContent = preview.mermaid || "";
  }
}

async function importFlowFile(file) {
  try {
    const text = await file.text();
    const spec = JSON.parse(text);
    const result = await call("import_task_flow", spec);
    if (result && result.ok) {
      $("#taskErrors").textContent = `已导入任务：${result.task.name}`;
      await loadTasks();
      refreshTaskFlow(result.task.key);
      switchTaskTab("flow");
    } else {
      $("#taskErrors").textContent = ((result && result.errors) || ["导入失败"]).join("; ");
      switchTaskTab("json");
    }
  } catch (e) {
    $("#taskErrors").textContent = "流程 JSON 解析失败：" + e.message;
    switchTaskTab("json");
  }
}

// ---------- 任务列表 ----------
async function loadTaskSelect() {
  const list = (await call("list_tasks")) || [];
  const select = $("#taskSelect");
  if (!select) return;
  const current = select.value;
  select.innerHTML = "";
  list.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.key;
    opt.textContent = `${item.name}${item.enabled ? "" : "（停用）"}`;
    select.appendChild(opt);
  });
  const target = state && state.task_key ? state.task_key : current;
  if (target && list.some((t) => t.key === target)) select.value = target;
  else if (list.length) select.value = list[0].key;
}

async function loadTasks() {
  const list = (await call("list_tasks")) || [];
  tasksCache = list;
  const box = $("#taskList");
  box.innerHTML = "";
  if (!list.length) {
    box.textContent = "暂无任务，点「新建任务」添加。";
  }
  list.forEach((item) => {
    const div = document.createElement("div");
    div.className = "task-item";
    div.dataset.key = item.key;
    const head = document.createElement("div");
    head.className = "task-item-head";
    const name = document.createElement("b");
    name.textContent = item.name;
    const toggle = document.createElement("label");
    toggle.className = "check";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!item.enabled;
    cb.addEventListener("change", async (e) => {
      e.stopPropagation();
      await call("set_task_enabled", item.key, cb.checked);
      await loadTasks();
      await loadTaskSelect();
    });
    toggle.appendChild(cb);
    toggle.appendChild(document.createTextNode("启用"));
    head.appendChild(name);
    head.appendChild(toggle);
    const desc = document.createElement("div");
    desc.className = "task-item-desc";
    desc.textContent = item.description || "无描述";
    const meta = document.createElement("div");
    meta.className = "task-item-meta";
    const capCount = item.capabilities ? Object.values(item.capabilities).filter(Boolean).length : 8;
    meta.textContent = `App: ${item.app || "-"} · 规则: ${item.rule || "-"} · 循环: ${item.max_loops} · 引擎: ${item.engine || "步骤流程"} · 能力: ${capCount}/8 · ${item.mtime}`;
    const actions = document.createElement("div");
    actions.className = "task-item-actions";
    const editBtn = document.createElement("button");
    editBtn.className = "btn tiny";
    editBtn.textContent = "编辑";
    editBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await openTask(item.key);
      switchTaskTab("json");
    });
    const runBtn = document.createElement("button");
    runBtn.className = "btn tiny primary";
    runBtn.textContent = "执行";
    runBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await call("select_task", item.key);
      await call("start_task", item.key);
      showPage("device");
      await loadExecution();
      await poll();
    });
    actions.appendChild(editBtn);
    actions.appendChild(runBtn);
    div.appendChild(head);
    div.appendChild(desc);
    div.appendChild(meta);
    div.appendChild(actions);
    div.addEventListener("click", () => openTask(item.key));
    box.appendChild(div);
  });
  await loadTaskSelect();
}

async function openTask(key) {
  if (!key) return;
  const data = await call("load_task", key);
  if (!data) return;
  selectTaskInList(key);
  $("#taskJson").value = JSON.stringify(data, null, 2);
  syncCapsFromJson();
  $("#taskEditorHint").textContent = `正在编辑：${data.name || key}（${key}.json）`;
  $("#taskErrors").textContent = "";
  await refreshTaskFlow(key);
}

function selectTaskInList(key) {
  $$(".task-item").forEach((n) => n.classList.toggle("active", n.dataset.key === key));
}

async function saveCurrentTask() {
  try {
    const data = JSON.parse($("#taskJson").value || "{}");
    if (!data.name) {
      $("#taskErrors").textContent = "任务缺少 name";
      return;
    }
    if (!data.key) data.key = data.name.replace(/[^\w\u4e00-\u9fff-]+/g, "_");
    const result = await call("save_task", data);
    if (result && result.ok) {
      $("#taskErrors").textContent = "已保存";
      await loadTasks();
      await loadTaskSelect();
      refreshTaskFlow(data.key);
    } else {
      $("#taskErrors").textContent = ((result && result.errors) || ["保存失败"]).join("; ");
    }
  } catch (e) {
    $("#taskErrors").textContent = "JSON 解析失败：" + e.message;
  }
  await poll();
}

async function loadExecution() {
  const list = (await call("get_task_progress", 120)) || [];
  const box = $("#executionList");
  if (!box) return;
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<p class="hint">执行任务后，这里会实时显示做了什么。</p>';
    return;
  }
  list.slice().reverse().forEach((item) => {
    const div = document.createElement("div");
    div.className = "execution-item kind-" + (item.kind || "other");
    const dot = document.createElement("span");
    dot.className = "execution-dot";
    const text = document.createElement("span");
    text.className = "execution-text";
    text.textContent = item.detail || item.target || item.kind || "行为";
    const time = document.createElement("span");
    time.className = "execution-time";
    time.textContent = (item.ts || "").slice(-8);
    div.appendChild(dot);
    div.appendChild(text);
    div.appendChild(time);
    box.appendChild(div);
  });
  box.scrollTop = 0;
}

// ---------- 阶段2：回放分析 ----------
async function loadReports(selectName) {
  const list = (await call("list_reports")) || [];
  const select = $("#reportSelect");
  select.innerHTML = "";
  list.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.name;
    opt.textContent = `${item.name}  (${item.mtime})`;
    select.appendChild(opt);
  });
  // 默认选中任务执行日志（回放分析的实际数据源）
  const preferred = selectName || (list.some((i) => i.name === "activity.jsonl") ? "activity.jsonl" : null);
  if (preferred && list.some((item) => item.name === preferred)) select.value = preferred;
}

async function analyzeCurrentReport() {
  const name = $("#reportSelect").value;
  if (!name) return;
  const data = await call("analyze_report", name);
  if (!data) return;
  $("#analysisSummary").textContent =
    `${data.file} · 事件 ${data.total} · 已执行 ${data.acted} · 失败 ${data.failures || 0} · 成功率 ${data.success_rate}%`;
  renderBars("#levelStats", data.levels, data.total);
  renderBars("#resultStats", data.results, data.total);
  renderBars("#sourceStats", data.sources, data.total);
  renderBars("#costStats", data.avg_cost_ms, 0);
  renderEvents(data.recent);
}

function renderBars(selector, values, total) {
  const box = $(selector);
  box.innerHTML = "";
  const entries = Object.entries(values || {});
  if (!entries.length) {
    box.textContent = "暂无数据";
    return;
  }
  const max = Math.max(...entries.map((item) => Number(item[1]) || 0), 1);
  entries.forEach(([key, value]) => {
    const num = Number(value) || 0;
    const row = document.createElement("div");
    row.className = "stat-row";
    const label = document.createElement("span");
    label.textContent = key;
    const track = document.createElement("div");
    track.className = "stat-track";
    const fill = document.createElement("div");
    fill.className = "stat-fill";
    fill.style.width = `${(num / max * 100).toFixed(1)}%`;
    track.appendChild(fill);
    const amount = document.createElement("b");
    amount.textContent = total ? `${num} (${(num / total * 100).toFixed(1)}%)` : String(num);
    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(amount);
    box.appendChild(row);
  });
}

function renderEvents(events) {
  const box = $("#eventList");
  box.innerHTML = "";
  const items = (events || []).slice().reverse();
  if (!items.length) {
    box.textContent = "暂无事件";
    return;
  }
  items.slice(0, 150).forEach((event) => {
    const div = document.createElement("div");
    div.className = "event-item";
    div.textContent = [
      event.ts || "-",
      event.target || "-",
      event.level || "-",
      event.result || "-",
      `${event.cost_ms || "-"}ms`,
    ].join(" | ");
    box.appendChild(div);
  });
}

// ---------- 单帧采集结果 ----------
function renderCapture(result) {
  const box = $("#captureResult");
  if (!result || !result.ok) {
    box.innerHTML = `<p class="hint">采集失败：${(result && result.error) || "未知原因"}</p>`;
    return;
  }
  const img = result.preview
    ? `<img class="capture-shot" src="${result.preview}" alt="采集截图" />`
    : "";
  const ocr = (result.ocr || [])
    .map((it) => `<div class="capture-ocr-item"><b>${it.text}</b><span>${it.score} @(${it.cx},${it.cy})</span></div>`)
    .join("");
  box.innerHTML = `
    <div class="capture-head">采集 ${result.time} · 识别 ${result.ocr_count} 段文字 · ${result.clickable_count} 个按钮</div>
    <div class="capture-body">${img}<div class="capture-text">${ocr || '<p class="hint">未识别到文字</p>'}</div></div>
    <div class="hint">目录：${result.dir}</div>
  `;
}

// ---------- 行为录制 ----------
const ACTION_OPTIONS = [
  ["click_ratio", "点击坐标"], ["click_match", "点击文字"], ["back", "返回键"],
  ["scroll", "页面滚动"], ["wait", "等待"], ["finish", "结束"],
  ["mute_once", "静音一次"], ["close_ad", "关广告"],
];

async function loadRecording() {
  const info = await call("get_recording_state");
  const list = (info && info.steps) || [];
  const on = !!(info && info.recording);
  const toggle = $("#recordToggleBtn");
  toggle.textContent = on ? "停止录制" : "开始录制";
  toggle.classList.toggle("danger", on);
  $("#recordStatus").textContent = on ? "录制中…" : `已录制 ${list.length} 步`;

  const box = $("#activityList");
  if (!box || box.dataset.editing) return;
  recordingDraft = list;
  box.innerHTML = "";
  if (!list.length) {
    box.textContent = "暂无录制步骤。点「开始录制」后点击按钮/采集页面/按系统键；或点「选点/框选」直接在画面上定位。";
    return;
  }

  list.forEach((item, i) => {
    const row = document.createElement("div");
    row.className = "activity-item";

    const no = document.createElement("span");
    no.className = "activity-no";
    no.textContent = `${i + 1}.`;
    row.appendChild(no);

    const detail = document.createElement("input");
    detail.className = "act-detail";
    detail.value = item.label || item.detail || item.target || "";
    detail.addEventListener("focus", () => { box.dataset.editing = "1"; });
    detail.addEventListener("blur", async () => {
      recordingDraft[i].label = detail.value;
      await persistDraft();
      releaseEditing();
    });
    row.appendChild(detail);

    const action = document.createElement("select");
    action.className = "act-action";
    ACTION_OPTIONS.forEach(([val, text]) => {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = text;
      action.appendChild(opt);
    });
    action.value = item.action || "wait";
    action.addEventListener("focus", () => { box.dataset.editing = "1"; });
    action.addEventListener("change", async () => {
      recordingDraft[i].action = action.value;
      await persistDraft();
    });
    action.addEventListener("blur", async () => {
      releaseEditing();
      await loadRecording();
    });
    row.appendChild(action);

    if ((item.action || "wait") === "click_ratio") {
      const ratioWrap = document.createElement("span");
      ratioWrap.className = "act-ratio";
      const ratio = item.ratio || [
        item.x != null ? Number(item.x) / 1440 : 0.5,
        item.y != null ? Number(item.y) / 3200 : 0.5,
      ];
      [0, 1].forEach((idx) => {
        const input = document.createElement("input");
        input.type = "number";
        input.step = "0.001";
        input.min = "0";
        input.max = "1";
        input.value = Number(ratio[idx] || 0).toFixed(3);
        input.title = idx === 0 ? "X 比例" : "Y 比例";
        input.addEventListener("focus", () => { box.dataset.editing = "1"; });
        input.addEventListener("blur", async () => {
          const cur = recordingDraft[i].ratio || [0.5, 0.5];
          cur[idx] = Number(input.value);
          recordingDraft[i].ratio = cur;
          await persistDraft();
          releaseEditing();
        });
        ratioWrap.appendChild(input);
      });
      row.appendChild(ratioWrap);
    }

    const ops = document.createElement("span");
    ops.className = "activity-ops";
    const mk = (text, title, fn) => {
      const b = document.createElement("button");
      b.className = "btn tiny ghost";
      b.textContent = text;
      b.title = title;
      b.addEventListener("click", async (e) => {
        e.preventDefault();
        await fn();
      });
      return b;
    };
    ops.appendChild(mk("试", "按当前坐标试点击", () =>
      call("click_ratio", (recordingDraft[i].ratio || [0.5, 0.5])[0], (recordingDraft[i].ratio || [0.5, 0.5])[1])));
    ops.appendChild(mk("↑", "上移", () => call("move_recorded_step", i, -1).then(loadRecording)));
    ops.appendChild(mk("↓", "下移", () => call("move_recorded_step", i, 1).then(loadRecording)));
    ops.appendChild(mk("×", "删除", () => call("delete_recorded_step", i).then(loadRecording)));
    row.appendChild(ops);

    box.appendChild(row);
  });
}

async function persistDraft() {
  await call("save_recording_draft", recordingDraft);
}

function releaseEditing() {
  const box = $("#activityList");
  if (box) delete box.dataset.editing;
}
