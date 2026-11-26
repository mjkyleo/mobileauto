import { ipcMain } from "electron";
import * as fs from "fs";
import * as path from "path";
import { DeviceController } from "./device";
import { TaskEngine } from "./engine";
import { Logger } from "./logger";
import { Store, flowSpecToTask, taskToFlowSpec } from "./store";

export class ApiHost {
  constructor(private root: string, private logger: Logger, private device: DeviceController,
              private store: Store, private engine: TaskEngine) {}

  register(): void {
    const on = (name: string, fn: (...args: any[]) => any) => {
      ipcMain.handle(`api:${name}`, (_event, ...args) => Promise.resolve(fn(...args)));
    };

    on("getState", () => ({
      connected: this.device.connected,
      mode: this.device.mode,
      label: this.device.label,
      status: this.device.status,
      rows: this.device.rows,
      screen: this.device.screen,
      taskRunning: this.engine.running,
      taskProgress: this.engine.progress.map((text) => ({ ts: "", text })),
      autoConnect: this.device.autoConnect,
      detection: detectionEnabled,
      clear: clearMode,
      selectedTask: selectedTask,
      logs: this.logger.getLogs(80),
    }));

    on("grabFrame", async (quality = 88) => {
      const image = await this.device.screenshot(Number(quality));
      return image ? `data:image/png;base64,${image.toString("base64")}` : "";
    });
    on("getStreamStatus", () => ({
      status: this.device.status,
      h264: !!this.device.scrcpy?.running && !!this.device.scrcpy.hasFrame,
      h264Fps: this.device.scrcpy?.fps() || 0,
      wsUrl: this.device.scrcpy?.wsUrl || "ws://127.0.0.1:8766",
      mjpegUrl: "",
      clear: clearMode,
    }));
    on("clearLog", () => this.logger.clear());

    on("setAutoConnect", (enabled: boolean) => {
      this.device.setAutoConnect(enabled);
      return true;
    });
    on("rememberAddress", (address: string) => {
      this.device.rememberAddress(address);
      return true;
    });
    on("connect", async (mode = "auto", address = "") => {
      const ok = await this.device.connect(mode, address);
      this.logger.log(ok ? `设备连接成功：${this.device.status}` : "设备连接失败");
      return ok;
    });
    on("refresh", async () => (await this.device.refresh()).length);
    on("diagnose", () => this.device.diagnose());
    on("disconnect", async () => {
      await this.device.disconnect();
      return true;
    });

    on("scrollPage", async (direction = "down") => {
      const ok = await this.device.scroll(direction);
      this.store.appendActivity("scroll", `页面滚动 ${direction}`);
      return ok;
    });
    on("pressBack", async () => this.device.press("back"));
    on("pressHome", async () => this.device.press("home"));
    on("pressRecents", async () => this.device.press("recents"));
    on("clickXy", async (x: number, y: number, record = false) => {
      const ok = await this.device.click(Number(x), Number(y));
      if (ok) this.recordStep("click_ratio", `${x},${y}`, "", { ratio: [x / this.device.screen[0], y / this.device.screen[1]] }, record);
      return ok;
    });
    on("clickRatio", async (rx: number, ry: number) => this.device.clickRatio(Number(rx), Number(ry)));
    on("clickElement", async (index: number) => {
      const ok = await this.device.clickRow(Number(index));
      const row = this.device.rows.find((r) => r.index === Number(index));
      if (ok && row) this.recordStep("click_ratio", row.text || row.desc || row.resourceId, "", { ratio: [row.cx / this.device.screen[0], row.cy / this.device.screen[1]] });
      return ok;
    });

    on("setStreamQuality", (clear: boolean) => {
      clearMode = !!clear;
      return true;
    });
    on("setDetection", (enabled: boolean) => {
      detectionEnabled = !!enabled;
      if (!detectionEnabled) this.device.rows = [];
      return true;
    });
    on("setH264Enabled", async (enabled: boolean) => {
      if (!enabled && this.device.scrcpy) {
        await this.device.scrcpy.stop();
        this.device.scrcpy = null;
      }
      return true;
    });

    on("capturePage", async () => {
      const image = await this.device.screenshot(clearMode ? 98 : 88);
      if (!image) return { ok: false, message: "截图失败" };
      const dir = path.join(this.root, "runtime", "page_data", `${Date.now()}_单帧采集`);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, "screen.png"), image);
      const ocr = await this.engine.runOcr(image);
      fs.writeFileSync(path.join(dir, "ocr.txt"), JSON.stringify(ocr, null, 2), "utf8");
      const xml = this.device.u2 ? await this.device.u2.dumpHierarchy() : await this.device.adb?.dumpHierarchy() || "";
      fs.writeFileSync(path.join(dir, "ui.xml"), xml, "utf8");
      this.recordStep("capture", `OCR ${ocr.length} 条`, "", { texts: ocr.map((o) => o.text).slice(0, 20) });
      return {
        ok: true, time: new Date().toLocaleString(), dir,
        preview: `data:image/png;base64,${image.toString("base64")}`,
        ocr, clickable: this.device.rows.map((r) => ({ label: r.text || r.desc || r.resourceId, x: r.cx, y: r.cy })),
        ocr_count: ocr.length, clickable_count: this.device.rows.length,
      };
    });

    on("getRecordingState", () => ({ running: recording, steps: this.store.loadDraft() }));
    on("startRecording", () => { recording = true; return true; });
    on("stopRecording", () => { recording = false; return true; });
    on("clearRecorded", () => { this.store.clearDraft(); return true; });
    on("addManualClick", async (x: number, y: number, label = "") => {
      this.recordStep("click_ratio", label || `${x},${y}`, "", {
        ratio: [x / this.device.screen[0], y / this.device.screen[1]], x, y,
      });
      return true;
    });
    on("saveRecordingDraft", (steps: any[]) => { this.store.saveDraft(steps); return true; });
    on("deleteRecordedStep", (index: number) => {
      const steps = this.store.loadDraft();
      steps.splice(index, 1);
      this.store.saveDraft(steps);
      return true;
    });
    on("moveRecordedStep", (index: number, offset: number) => {
      const steps = this.store.loadDraft();
      const target = index + offset;
      if (target < 0 || target >= steps.length) return false;
      [steps[index], steps[target]] = [steps[target], steps[index]];
      this.store.saveDraft(steps);
      return true;
    });
    on("foldRecordingLoops", () => {
      const steps = this.store.loadDraft();
      this.store.saveDraft(foldLoops(steps));
      return true;
    });
    on("recordingToTask", (name: string, app = "") => {
      const steps = this.store.loadDraft();
      const task = this.store.newTask(String(name || "录制任务"));
      task.app = String(app || "");
      task.flow = steps.map((s) => {
        if (s.kind === "loop") {
          return { id: s.id, match_any: (s.body || []).flatMap((b) => b.texts || [b.detail]),
            action: (s.body || [])[0]?.kind || "click_ratio", ratio: (s.body || [])[0]?.ratio,
            max_loops: s.repeat || 1 };
        }
        return { id: s.id, match_any: s.texts || [], action: s.kind, ratio: s.ratio,
          detail: s.detail, target: s.target };
      });
      return this.store.saveTask(task);
    });
    on("listActions", (limit = 300) => this.store.listActivity(Number(limit)));
    on("clearActions", () => { this.store.clearActivity(); return true; });
    on("analyzeRecording", () => this.store.analyzeActivity());

    on("startTask", (key: string) => this.engine.start(this.store.loadTask(key) || this.store.newTask(key)));
    on("stopTask", () => { this.engine.stop(); return true; });
    on("getTaskProgress", (limit = 300) => this.engine.progress.slice(-Number(limit)).map((text) => ({ ts: "", text })));

    on("newTask", (name = "新任务") => this.store.newTask(String(name)));
    on("listTasks", () => this.store.listTasks());
    on("loadTask", (key: string) => this.store.loadTask(key));
    on("saveTask", (data: any) => this.store.saveTask(data));
    on("deleteTask", (key: string) => this.store.deleteTask(key));
    on("setTaskEnabled", (key: string, enabled: boolean) => {
      const task = this.store.loadTask(key);
      if (!task) return false;
      task.enabled = !!enabled;
      this.store.saveTask(task);
      return true;
    });
    on("selectTask", (key: string) => { selectedTask = key; return true; });
    on("taskMermaid", (key: string) => {
      const task = this.store.loadTask(key);
      if (!task) return { mermaid: "flowchart TD\n  A[任务不存在]" };
      const lines = ["flowchart TD", `  A[${escapeMermaid(task.name)}]`];
      for (let i = 0; i < task.flow.length; i++) {
        const s = task.flow[i] as any;
        const words = [].concat(s.match || s.match_any || []).slice(0, 3).join("/");
        lines.push(`  S${i}[${escapeMermaid(String(s.id || words || s.action || `步骤${i + 1}`))}]`);
        lines.push(`  ${i === 0 ? "A" : `S${i - 1}`} --> S${i}`);
      }
      lines.push(`  S${Math.max(0, task.flow.length - 1)} --> Z[结束]`);
      return {
        mermaid: lines.join("\n"),
        stats: { mode: task.engine, steps: task.flow.length,
          clicks: task.flow.filter((s: any) => String(s.action).startsWith("click")).length,
          loops: task.flow.filter((s: any) => s.action === "loop").length,
          finish: task.finish_info_keywords.length },
      };
    });
    on("saveFlowAs", (key: string, name: string) => {
      const task = this.store.loadTask(key);
      if (!task) return false;
      const file = path.join(this.root, "runtime", safeFile(name || `${key}_flow.json`));
      fs.writeFileSync(file, JSON.stringify(taskToFlowSpec(task), null, 2), "utf8");
      return true;
    });
    on("exportTaskFlow", (key: string) => {
      const task = this.store.loadTask(key);
      return task ? taskToFlowSpec(task) : null;
    });
    on("importTaskFlow", (spec: any) => {
      if (!spec || spec.$schema !== "mobileauto/flow/v1") return null;
      const flow = (spec.flow as any)?.states;
      if (!Array.isArray(flow)) return null;
      const ids = new Set<string>();
      for (const state of flow) {
        const id = String(state?.id || "");
        if (!id || ids.has(id)) return null;
        ids.add(id);
      }
      return this.store.saveTask(flowSpecToTask(spec));
    });

    on("listRecords", () => this.store.listRecords());
    on("readRecord", (p: string) => this.store.readRecord(p));
    on("listReports", () => {
      const files = ["activity.jsonl"];
      const dir = path.join(this.root, "runtime", "reports");
      if (fs.existsSync(dir)) files.push(...fs.readdirSync(dir).map((f) => `reports/${f}`));
      return files;
    });
    on("analyzeReport", () => this.store.analyzeActivity());
    on("pullReports", async () => {
      if (!this.device.adb) return false;
      const res = await this.device.adb.run(["shell", "ls", "/sdcard/mobileauto/reports"], 8000);
      const names = res.stdout.split(/\r?\n/).map((s) => s.trim()).filter((s) => s.endsWith(".jsonl"));
      const dir = path.join(this.root, "runtime", "reports");
      fs.mkdirSync(dir, { recursive: true });
      for (const name of names) {
        const out = await this.device.adb.run(["pull", `/sdcard/mobileauto/reports/${name}`, path.join(dir, `phone_${name}`)], 15000);
        void out;
      }
      return names.length;
    });

    on("listRules", () => this.store.listRules());
    on("loadRule", (name: string) => this.store.loadRule(name));
    on("saveRule", (name: string, data: any) => this.store.saveRule(name, data));
    on("deleteRule", (name: string) => this.store.deleteRule(name));
    on("pushRule", async (name: string) => {
      const file = path.join(this.store.rulesDir, safeFile(name));
      if (!this.device.adb || !fs.existsSync(file)) return false;
      return this.device.adb.push(file, "/sdcard/mobileauto/rules/active.json");
    });

    on("captureDebug", async () => (await this.device.screenshot(88))?.toString("base64") || "");
  }

  private recordStep(kind: string, detail: string, target: string, extra: Record<string, unknown>, record = true): void {
    const step: any = {
      id: `s_${Date.now()}_${Math.floor(Math.random() * 1000)}`,
      kind, detail, target, ...extra,
    };
    if (record) {
      const steps = this.store.loadDraft();
      steps.push(step);
      this.store.saveDraft(steps);
      this.store.appendActivity("recorded", detail, target, { recorded: true });
    } else {
      this.store.appendActivity(kind, detail, target);
    }
  }
}

let detectionEnabled = true;
let clearMode = false;
let recording = false;
let selectedTask = "";

function escapeMermaid(text: string): string {
  return text.replace(/[\[\](){}]/g, " ").replace(/"/g, "'").slice(0, 60);
}

function safeFile(name: string): string {
  return String(name || "").replace(/[\\/:*?"<>|]/g, "_");
}

function foldLoops(steps: any[]): any[] {
  if (steps.length < 4) return steps;
  for (let len = 3; len >= 1; len--) {
    for (let start = 0; start + len * 2 <= steps.length; start++) {
      const body = steps.slice(start, start + len);
      const sig = (s: any) => `${s.kind}:${JSON.stringify(s.ratio || [])}`;
      let repeat = 1;
      while (start + len * (repeat + 1) <= steps.length &&
             body.every((s, i) => sig(s) === sig(steps[start + len * repeat + i]))) repeat++;
      if (repeat >= 2) {
        const before = steps.slice(0, start);
        const after = steps.slice(start + len * repeat);
        return foldLoops([
          ...before,
          { id: `loop_${Date.now()}`, kind: "loop", detail: `循环 ${repeat} 次`, target: "", body, repeat },
          ...after,
        ]);
      }
    }
  }
  return steps;
}
