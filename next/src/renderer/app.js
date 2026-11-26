import { H264View } from "./webcodecs.js";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const call = (name, ...args) => window.bridge.invoke(name, ...args);

let state = null;
let stream = null;
let recording = false;
let selectButtonMode = false;
let selectedIndex = -1;
let currentTask = null;
let tasks = [];
let records = [];
let lastOverlaySignature = "";
let flowScale = 1;
let lastFrameAt = 0;
const video = new H264View($("#videoCanvas"), $("#liveFrame"), (text) => {
  $("#videoStatus").textContent = text;
});

function formatTime(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleTimeString();
}

async function poll() {
  state = await call("getState");
  stream = await call("getStreamStatus");
  renderState();
  renderOverlay();
  $("#miniLog").textContent = (state.logs || []).slice(-18).join("\n");
  if ($("#terminalClock")) $("#terminalClock").textContent = new Date().toLocaleTimeString();
  if ($("#terminalLoad")) $("#terminalLoad").textContent = `load: ${Number(stream?.h264Fps || 0).toFixed(2)}`;
  if (stream.h264 && typeof VideoDecoder !== "undefined") {
    video.start(stream.ws_url || stream.wsUrl);
  } else {
    video.stop();
    if (state.connected && Date.now() - lastFrameAt > 800) {
      lastFrameAt = Date.now();
      const url = await call("grabFrame", stream.clear ? 98 : 88);
      video.showFallback(url);
    }
  }
  if (state.taskRunning || $("#page-device").classList.contains("active")) {
    $("#executionList").innerHTML = (state.taskProgress || []).slice().reverse().map((item) =>
      `<div class="execution-row"><span class="point-dot"></span><span>${escapeHtml(item.text)}</span><time>${formatTime(item.ts)}</time></div>`
    ).join("");
  }
}

function renderState() {
  $("#deviceStatus").textContent = state.status || "设备未连接";
  $("#deviceDot").classList.toggle("on", state.connected);
  $("#deviceLabel").textContent = state.label || "未选择设备";
  $("#autoConnect").checked = !!state.autoConnect;
  $("#showElements").checked = state.detection !== false;
  $("#clearMode").checked = !!state.clear;
  if (document.activeElement !== $("#addressInput") && state.label) $("#addressInput").value = state.label;
  $("#runTaskBtn").disabled = !state.connected || state.taskRunning;
  $("#stopTaskBtn").disabled = !state.taskRunning;
  $("#refreshBtn").disabled = !state.connected;
  $("#disconnectBtn").disabled = !state.connected;
  const label = stream.status || "画面未启动";
  $("#streamStatus").textContent = stream.h264 ? `${label} · H264 ${stream.h264Fps || 0}fps` : label;
  $("#recordBtn").textContent = recording ? "停止录制" : "开始录制";
  $("#recordBtn").classList.toggle("primary", !recording);
  if (!recording) {
    void (async () => {
      const info = await call("getRecordingState");
      renderSteps(info.steps || []);
    })();
  }
  syncTaskSelect();
}

function filteredRows() {
  const keyword = ($("#filterInput").value || "").toLowerCase();
  const hideSystem = $("#hideSystem").checked;
  return (state?.rows || []).filter((row) => {
    if (!row.clickable && !(row.text || row.desc)) return false;
    if (hideSystem && String(row.package || "").includes("com.android.systemui")) return false;
    if (!keyword) return true;
    return [row.text, row.desc, row.resourceId, row.className]
      .some((value) => String(value || "").toLowerCase().includes(keyword));
  }).slice(0, 200);
}

function renderOverlay() {
  if (!state) return;
  const rows = filteredRows();
  const show = $("#showElements").checked;
  $("#phone").classList.toggle("hide-elements", !show);
  $("#elementCount").textContent = `${rows.length} 个按钮`;
  const signature = JSON.stringify([
    state.screen, selectedIndex, show, selectButtonMode,
    $("#filterInput").value, $("#hideSystem").checked,
    rows.map((r) => [r.index, r.x1, r.y1, r.x2, r.y2, r.text, r.desc, r.resourceId]),
  ]);
  if (signature === lastOverlaySignature) return;
  lastOverlaySignature = signature;
  const [w, h] = state.screen || [1080, 1920];
  const overlay = $("#overlay");
  overlay.innerHTML = rows.map((row) => {
    const label = row.text || row.desc || String(row.resourceId || "").split("/").pop() || "按钮";
    const cls = row.index === selectedIndex ? "selected" : "";
    return `<div class="phone-element ${cls}" data-index="${row.index}"
      style="left:${row.x1 / w * 100}%;top:${row.y1 / h * 100}%;width:${row.w / w * 100}%;height:${row.h / h * 100}%"
      title="${escapeHtml(label)}">${escapeHtml(label.slice(0, 10))}</div>`;
  }).join("");
  for (const el of $$(".phone-element")) {
    el.addEventListener("click", async (event) => {
      event.stopPropagation();
      const index = Number(el.dataset.index);
      selectedIndex = index;
      if (selectButtonMode) {
        const row = rows.find((r) => r.index === index);
        if (row) await call("addManualClick", row.cx, row.cy, row.text || row.desc || row.resourceId);
        selectButtonMode = false;
        $("#selectButtonBtn").classList.remove("active");
        renderSteps((await call("getRecordingState")).steps || []);
      } else {
        await call("clickElement", index);
      }
      lastOverlaySignature = "";
      poll();
    });
  }
}

function renderSteps(steps) {
  const kinds = ["click_ratio", "click_match", "back", "scroll", "wait", "finish", "mute_once", "close_ad"];
  $("#recordSteps").innerHTML = steps.map((step, index) => `
    <div class="step-row" data-index="${index}">
      <span>${index + 1}</span>
      <input class="step-detail" value="${escapeAttr(step.detail || step.kind)}" />
      <select class="step-kind">${kinds.map((k) => `<option ${k === step.kind ? "selected" : ""}>${k}</option>`).join("")}</select>
      <span><button class="step-up">↑</button><button class="step-down">↓</button><button class="step-del">×</button></span>
    </div>`).join("");
  for (const row of $$(".step-row")) {
    const index = Number(row.dataset.index);
    row.querySelector(".step-detail").addEventListener("change", async (e) => {
      steps[index].detail = e.target.value;
      await call("saveRecordingDraft", steps);
    });
    row.querySelector(".step-kind").addEventListener("change", async (e) => {
      steps[index].kind = e.target.value;
      await call("saveRecordingDraft", steps);
    });
    row.querySelector(".step-up").addEventListener("click", () => call("moveRecordedStep", index, -1).then(poll));
    row.querySelector(".step-down").addEventListener("click", () => call("moveRecordedStep", index, 1).then(poll));
    row.querySelector(".step-del").addEventListener("click", () => call("deleteRecordedStep", index).then(poll));
  }
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(text) { return escapeHtml(text).replace(/\n/g, " "); }

async function syncTaskSelect() {
  if (tasks.length) return;
  tasks = await call("listTasks");
  const select = $("#taskSelect");
  select.innerHTML = tasks.map((t) => `<option value="${escapeAttr(t.key)}">${escapeHtml(t.name)}${t.enabled ? "" : "（停用）"}</option>`).join("");
  if (state?.selectedTask) select.value = state.selectedTask;
}

function showPage(name) {
  $$(".nav").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  const index = ["device", "tasks", "analysis", "records"].indexOf(name);
  $$(".tmux-win").forEach((win, i) => win.classList.toggle("active", i === index));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
  if (name === "tasks") loadTasks();
  if (name === "analysis") loadAnalysis();
  if (name === "records") loadRecords();
}

async function loadTasks() {
  tasks = await call("listTasks");
  $("#taskList").innerHTML = tasks.map((t) => `
    <div class="task-item" data-key="${escapeAttr(t.key)}">
      <h3>${escapeHtml(t.name)} <input type="checkbox" class="task-enabled" data-key="${escapeAttr(t.key)}" ${t.enabled ? "checked" : ""} /></h3>
      <small>${escapeHtml(t.app || "未指定 App")} · ${t.flow.length} 步 · ${escapeHtml(t.updated || "")}</small>
    </div>`).join("");
  for (const item of $$(".task-item")) {
    item.addEventListener("click", async (e) => {
      if (e.target.classList.contains("task-enabled")) return;
      currentTask = await call("loadTask", item.dataset.key);
      $("#taskJson").value = JSON.stringify(currentTask, null, 2);
      await call("selectTask", currentTask.key);
      renderCapabilities();
      await renderFlow();
    });
  }
  for (const box of $$(".task-enabled")) {
    box.addEventListener("change", async () => {
      await call("setTaskEnabled", box.dataset.key, box.checked);
      await loadTasks();
    });
  }
  if (!currentTask && tasks.length) {
    currentTask = await call("loadTask", tasks[0].key);
    $("#taskJson").value = JSON.stringify(currentTask, null, 2);
    renderCapabilities();
    await renderFlow();
  }
}

const CAPABILITIES = ["ocr", "action_verify", "anti_stuck", "dedupe", "structured_log", "end_detect", "auto_start", "entitlement"];
function renderCapabilities() {
  const caps = currentTask?.capabilities || {};
  $("#capabilities").innerHTML = CAPABILITIES.map((name) =>
    `<label class="check"><input type="checkbox" data-cap="${name}" ${caps[name] ? "checked" : ""} /> ${name}</label>`).join("");
  for (const box of $$("[data-cap]")) {
    box.addEventListener("change", () => {
      currentTask.capabilities[box.dataset.cap] = box.checked;
      $("#taskJson").value = JSON.stringify(currentTask, null, 2);
    });
  }
}

async function renderFlow() {
  if (!currentTask) return;
  const info = await call("taskMermaid", currentTask.key);
  $("#mermaidSource").textContent = info.mermaid || info.mermaid || "";
  const stats = info.stats || {};
  $("#flowContent").innerHTML = `<div class="flow-node">${escapeHtml(currentTask.name)}</div>` +
    (currentTask.flow || []).map((s) => `<div class="flow-node">${escapeHtml(String(s.id || s.action || "步骤"))} · ${escapeHtml(String(s.action || ""))}</div>`).join("") +
    `<div class="flow-node">结束 · 结束词 ${stats.finish || 0}</div>`;
  applyFlowView();
}

function applyFlowView() {
  $("#flowContent").style.transform = `scale(${flowScale})`;
  $("#flowZoomLabel").textContent = `${Math.round(flowScale * 100)}%`;
}

async function loadAnalysis() {
  const data = await call("analyzeReport");
  const cards = [
    ["总事件", data.total], ["失败", data.failed], ["成功率", `${data.successRate}%`],
    ["动作数", Object.keys(data.actionStats || {}).length],
  ];
  $("#analysisStats").innerHTML = cards.map(([k, v]) => `<div class="stat-card"><span>${k}</span><strong>${v}</strong></div>`).join("");
  $("#analysisEvents").innerHTML = (data.events || []).slice().reverse().map((e) =>
    `<div class="event-row">${formatTime(e.ts)} · ${escapeHtml(e.kind)} · ${escapeHtml(e.detail)}</div>`).join("");
  $("#analysisDist").textContent = JSON.stringify({
    result: data.resultStats, source: data.sourceStats, action: data.actionStats,
  }, null, 2);
}

async function loadRecords() {
  records = await call("listRecords");
  renderRecords();
}

function renderRecords() {
  const filter = $("#recordFilter").value;
  const list = records.filter((r) => filter === "all" || r.kind === filter);
  $("#recordList").innerHTML = list.map((r) =>
    `<div class="record-item" data-path="${escapeAttr(r.path)}">${escapeHtml(r.name)}<br><small>${escapeHtml(r.path)} · ${Math.round(r.size / 1024)}KB</small></div>`).join("");
  for (const item of $$(".record-item")) {
    item.addEventListener("click", async () => {
      $("#recordPreview").textContent = await call("readRecord", item.dataset.path);
    });
  }
}

function bindEvents() {
  $$(".nav").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
  $("#scanBtn").addEventListener("click", async () => {
    const ok = await call("connect", "auto", $("#addressInput").value.trim());
    if (!ok) alert("连接失败，请检查设备或点击“诊断”。");
    tasks = [];
    poll();
  });
  $("#refreshBtn").addEventListener("click", () => call("refresh").then(poll));
  $("#diagnoseBtn").addEventListener("click", async () => {
    const info = await call("diagnose");
    $("#diagnoseBox").textContent = JSON.stringify(info, null, 2);
    $("#diagnoseBox").classList.remove("hidden");
  });
  $("#disconnectBtn").addEventListener("click", () => call("disconnect").then(poll));
  $("#autoConnect").addEventListener("change", (e) => call("setAutoConnect", e.target.checked));
  $("#addressInput").addEventListener("change", (e) => call("rememberAddress", e.target.value.trim()));

  $("#scrollUpBtn").addEventListener("click", () => call("scrollPage", "up"));
  $("#scrollDownBtn").addEventListener("click", () => call("scrollPage", "down"));
  $("#backBtn").addEventListener("click", () => call("pressBack"));
  $("#homeBtn").addEventListener("click", () => call("pressHome"));
  $("#recentsBtn").addEventListener("click", () => call("pressRecents"));
  $("#showElements").addEventListener("change", () => { lastOverlaySignature = ""; poll(); });
  $("#clearMode").addEventListener("change", (e) => call("setStreamQuality", e.target.checked));
  $("#filterInput").addEventListener("input", () => { lastOverlaySignature = ""; renderOverlay(); });
  $("#hideSystem").addEventListener("change", () => { lastOverlaySignature = ""; renderOverlay(); });

  const overlay = $("#overlay");
  let start = null;
  overlay.addEventListener("pointerdown", (e) => {
    if (e.target !== overlay) return;
    start = { x: e.offsetX, y: e.offsetY };
  });
  overlay.addEventListener("pointerup", async (e) => {
    if (!start) return;
    const end = { x: e.offsetX, y: e.offsetY };
    const rect = overlay.getBoundingClientRect();
    const x = ((start.x + end.x) / 2) / rect.width;
    const y = ((start.y + end.y) / 2) / rect.height;
    start = null;
    await call("clickRatio", x, y, true);
    poll();
  });

  $("#recordBtn").addEventListener("click", async () => {
    recording = !recording;
    await call(recording ? "startRecording" : "stopRecording");
    poll();
  });
  $("#selectButtonBtn").addEventListener("click", (e) => {
    selectButtonMode = !selectButtonMode;
    e.target.classList.toggle("active", selectButtonMode);
    lastOverlaySignature = "";
    renderOverlay();
  });
  $("#clearRecordBtn").addEventListener("click", () => call("clearRecorded").then(poll));
  $("#foldLoopsBtn").addEventListener("click", () => call("foldRecordingLoops").then(poll));
  $("#toTaskBtn").addEventListener("click", async () => {
    const name = prompt("任务名称", "录制任务");
    if (name) { await call("recordingToTask", name, currentTask?.app || ""); tasks = []; await loadTasks(); }
  });
  $("#clearActionsBtn").addEventListener("click", () => call("clearActions").then(poll));

  $("#captureBtn").addEventListener("click", async () => {
    $("#captureBox").textContent = "采集中…";
    const data = await call("capturePage");
    if (!data.ok) { $("#captureBox").textContent = data.message; return; }
    $("#captureBox").innerHTML = `<img src="${data.preview}" /><div>${data.time} · OCR ${data.ocr_count} 条 · 按钮 ${data.clickable_count} 个</div><div>${escapeHtml(data.dir)}</div><div>${escapeHtml((data.ocr || []).map((o) => o.text).join(" / "))}</div>`;
  });

  $("#runTaskBtn").addEventListener("click", async () => {
    await call("startTask", $("#taskSelect").value);
    showPage("device");
    poll();
  });
  $("#stopTaskBtn").addEventListener("click", () => call("stopTask").then(poll));

  $("#refreshTasksBtn").addEventListener("click", () => { currentTask = null; loadTasks(); });
  $("#newTaskBtn").addEventListener("click", async () => {
    currentTask = await call("saveTask", await call("newTask"));
    tasks = [];
    await loadTasks();
  });
  $("#saveTaskBtn").addEventListener("click", async () => {
    try { currentTask = JSON.parse($("#taskJson").value); } catch (e) { alert("JSON 格式错误"); return; }
    currentTask = await call("saveTask", currentTask);
    tasks = [];
    await poll();
  });
  $("#deleteTaskBtn").addEventListener("click", async () => {
    if (!currentTask || !confirm("确认删除任务？")) return;
    await call("deleteTask", currentTask.key);
    currentTask = null;
    tasks = [];
    await loadTasks();
  });
  $("#exportFlowBtn").addEventListener("click", async () => {
    if (!currentTask) return;
    const spec = await call("exportTaskFlow", currentTask.key);
    download(`${currentTask.key}_flow.json`, JSON.stringify(spec, null, 2));
  });
  $("#importFlowBtn").addEventListener("click", () => $("#flowFile").click());

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".tab-page").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
    if (tab.dataset.tab === "flow") renderFlow();
  }));
  $("#flowZoomIn").addEventListener("click", () => { flowScale = Math.min(2, flowScale + 0.1); applyFlowView(); });
  $("#flowZoomOut").addEventListener("click", () => { flowScale = Math.max(0.4, flowScale - 0.1); applyFlowView(); });
  $("#flowFit").addEventListener("click", () => { flowScale = 1; applyFlowView(); });

  $("#refreshAnalysisBtn").addEventListener("click", loadAnalysis);
  $("#recordFilter").addEventListener("change", renderRecords);
}

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

async function bootstrap() {
  bindEvents();
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json";
  input.id = "flowFile";
  input.classList.add("hidden");
  document.body.appendChild(input);
  input.addEventListener("change", async () => {
    const file = input.files?.[0];
    if (!file) return;
    try {
      const spec = JSON.parse(await file.text());
      const task = await call("importTaskFlow", spec);
      if (!task) alert("FlowSpec 格式不正确");
      currentTask = null;
      tasks = [];
      await loadTasks();
    } catch (e) { alert(`导入失败：${e.message}`); }
  });
  await poll();
  setInterval(poll, 900);
}

bootstrap();
