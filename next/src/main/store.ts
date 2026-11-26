import * as fs from "fs";
import * as path from "path";
import type { AnalysisResult, FlowSpec, RecordedStep, TaskData } from "../shared/types";

function safeName(name: string): string {
  return String(name || "").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
}

function normalizeTask(data: any): TaskData {
  const task = data && typeof data === "object" ? data : {};
  // 旧版把状态机放在 states 数组，新版统一为 flow
  if (!Array.isArray(task.flow) && Array.isArray(task.states)) {
    task.flow = task.states;
    task.states = {};
  }
  if (!Array.isArray(task.flow)) task.flow = [];
  if (!task.states || Array.isArray(task.states)) task.states = {};
  if (!task.capabilities || typeof task.capabilities !== "object" || Array.isArray(task.capabilities)) {
    task.capabilities = {};
  }
  if (!Array.isArray(task.stuck_states)) task.stuck_states = [];
  if (!Array.isArray(task.finish_info_keywords)) task.finish_info_keywords = [];
  task.key = String(task.key || "");
  task.name = String(task.name || task.key || "未命名任务");
  task.engine = task.engine === "legacy" ? "legacy" : "configured";
  task.max_loops = Number(task.max_loops || 30);
  task.poll = Number(task.poll || 0.25);
  task.tap_gap = Number(task.tap_gap || 0.35);
  task.stuck_limit = Number(task.stuck_limit || 12);
  return task as TaskData;
}

export class Store {
  readonly tasksDir: string;
  readonly rulesDir: string;
  readonly runtimeDir: string;
  readonly activityFile: string;
  readonly draftFile: string;

  constructor(public readonly root: string) {
    this.tasksDir = path.join(root, "tasks");
    this.rulesDir = path.join(root, "rules");
    this.runtimeDir = path.join(root, "runtime");
    this.activityFile = path.join(this.runtimeDir, "activity.jsonl");
    this.draftFile = path.join(this.runtimeDir, "recording_draft.json");
    for (const dir of [this.tasksDir, this.rulesDir, this.runtimeDir]) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }

  private readJson<T>(file: string): T | null {
    try {
      return JSON.parse(fs.readFileSync(file, "utf8")) as T;
    } catch {
      return null;
    }
  }

  listTasks(): TaskData[] {
    const items: TaskData[] = [];
    for (const name of fs.readdirSync(this.tasksDir)) {
      if (!name.endsWith(".json")) continue;
      const data = this.readJson<any>(path.join(this.tasksDir, name));
      if (data) items.push(normalizeTask(data));
    }
    return items.sort((a, b) => String(b.updated).localeCompare(String(a.updated)));
  }

  loadTask(key: string): TaskData | null {
    const file = path.join(this.tasksDir, `${safeName(key)}.json`);
    const data = fs.existsSync(file) ? this.readJson<any>(file) : null;
    return data ? normalizeTask(data) : null;
  }

  saveTask(data: TaskData): TaskData {
    const key = safeName(data.key || data.name || `task_${Date.now()}`);
    data.key = key;
    data.updated = new Date().toISOString();
    fs.writeFileSync(path.join(this.tasksDir, `${key}.json`), JSON.stringify(data, null, 2), "utf8");
    return data;
  }

  deleteTask(key: string): boolean {
    const file = path.join(this.tasksDir, `${safeName(key)}.json`);
    if (!fs.existsSync(file)) return false;
    fs.unlinkSync(file);
    return true;
  }

  newTask(name = "新任务"): TaskData {
    const key = `task_${Date.now().toString(36)}`;
    return {
      key, name, description: "", app: "", rule: "", flow: [],
      engine: "configured", enabled: false, max_loops: 30,
      capabilities: { ocr: true, action_verify: true, anti_stuck: true, dedupe: true,
        structured_log: true, end_detect: true, auto_start: true, entitlement: true },
      states: {}, stuck_states: [], stuck_limit: 12,
      finish_info_keywords: [], poll: 0.25, tap_gap: 0.35,
      updated: new Date().toISOString(),
    };
  }

  appendActivity(kind: string, detail: unknown, target = "", extra: Record<string, unknown> = {}): void {
    const entry = {
      ts: new Date().toISOString(),
      kind,
      detail: typeof detail === "string" ? detail : JSON.stringify(detail),
      target,
      ...extra,
    };
    fs.appendFileSync(this.activityFile, JSON.stringify(entry) + "\n", "utf8");
  }

  listActivity(limit = 300): any[] {
    if (!fs.existsSync(this.activityFile)) return [];
    return fs.readFileSync(this.activityFile, "utf8")
      .split(/\r?\n/).filter(Boolean)
      .map((line) => {
        try { return JSON.parse(line); } catch { return null; }
      })
      .filter(Boolean)
      .slice(-limit);
  }

  clearActivity(): void {
    if (fs.existsSync(this.activityFile)) fs.writeFileSync(this.activityFile, "", "utf8");
  }

  loadDraft(): RecordedStep[] {
    return this.readJson<RecordedStep[]>(this.draftFile) || [];
  }

  saveDraft(steps: RecordedStep[]): void {
    fs.writeFileSync(this.draftFile, JSON.stringify(steps, null, 2), "utf8");
  }

  clearDraft(): void {
    this.saveDraft([]);
    const kept = this.listActivity(10000).filter((e) => e.kind !== "recorded");
    fs.writeFileSync(this.activityFile, kept.map((e) => JSON.stringify(e)).join("\n") + (kept.length ? "\n" : ""), "utf8");
  }

  listRecords(): Array<{ path: string; name: string; size: number; mtime: number; kind: string; group: string; label: string }> {
    const result: Array<{ path: string; name: string; size: number; mtime: number; kind: string; group: string; label: string }> = [];
    const walk = (dir: string) => {
      if (!fs.existsSync(dir)) return;
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!/\.(log|txt|xml|json|jsonl|png)$/i.test(entry.name)) continue;
        if (entry.name === "last_device_address.txt") continue;
        const stat = fs.statSync(full);
        const rel = path.relative(this.runtimeDir, full).replace(/\\/g, "/");
        let kind = "other";
        let group = "其他记录";
        let label = "临时文件";
        if (/\.log$/i.test(entry.name)) { kind = "log"; group = "运行日志"; label = "工作台/任务运行日志"; }
        else if (rel.startsWith("page_data/") || rel.startsWith("debug_phone/")) { kind = "text"; group = "单帧采集"; label = "截图 + OCR + 控件树"; }
        else if (rel.startsWith("debug_ocr/")) { kind = "text"; group = "OCR 调试"; label = "任务引擎识别的原始文字"; }
        else if (rel.startsWith("debug_ui/") || /ui|\.xml$/i.test(entry.name)) { kind = "ui"; group = "UI 层级"; label = "uiautomator 控件树"; }
        else if (rel.startsWith("reports/") || /report/i.test(entry.name)) { kind = "report"; group = "分析报告"; label = "手机回传报告"; }
        else if (entry.name === "activity.jsonl") { kind = "report"; group = "任务活动"; label = "点击/滚动/录制/任务事件"; }
        else if (entry.name === "recording_draft.json") { kind = "text"; group = "录制草稿"; label = "录制步骤编辑数据"; }
        result.push({ path: rel, name: entry.name, size: stat.size, mtime: stat.mtimeMs, kind, group, label });
      }
    };
    walk(this.runtimeDir);
    return result.sort((a, b) => b.mtime - a.mtime).slice(0, 800);
  }

  deleteRecord(relative: string): boolean {
    const full = path.resolve(this.runtimeDir, relative);
    if (!full.startsWith(path.resolve(this.runtimeDir))) return false;
    try {
      fs.unlinkSync(full);
      return true;
    } catch {
      return false;
    }
  }

  clearRecords(kind: string): number {
    const target = String(kind || "");
    const removeDir = (dir: string) => {
      if (!fs.existsSync(dir)) return 0;
      let count = 0;
      for (const entry of fs.readdirSync(dir)) {
        const full = path.join(dir, entry);
        if (fs.statSync(full).isDirectory()) count += removeDir(full);
        else { fs.unlinkSync(full); count++; }
      }
      fs.rmdirSync(dir);
      return count;
    };
    if (target === "capture") {
      return removeDir(path.join(this.runtimeDir, "page_data")) +
        removeDir(path.join(this.runtimeDir, "debug_phone")) +
        removeDir(path.join(this.runtimeDir, "debug_ocr")) +
        removeDir(path.join(this.runtimeDir, "debug_ui"));
    }
    if (target === "log") {
      return this.listRecords().filter((r) => r.kind === "log").reduce((n, r) => n + (this.deleteRecord(r.path) ? 1 : 0), 0);
    }
    if (target === "report") {
      return this.listRecords().filter((r) => r.kind === "report").reduce((n, r) => n + (this.deleteRecord(r.path) ? 1 : 0), 0);
    }
    if (target === "draft") {
      return this.listRecords().filter((r) => r.group === "录制草稿").reduce((n, r) => n + (this.deleteRecord(r.path) ? 1 : 0), 0);
    }
    if (target === "all") {
      let count = 0;
      for (const entry of fs.readdirSync(this.runtimeDir)) {
        if (entry === "last_device_address.txt") continue;
        const full = path.join(this.runtimeDir, entry);
        if (fs.statSync(full).isDirectory()) count += removeDir(full);
        else { fs.unlinkSync(full); count++; }
      }
      return count;
    }
    return 0;
  }

  readRecord(relative: string): string {
    const full = path.resolve(this.runtimeDir, relative);
    if (!full.startsWith(path.resolve(this.runtimeDir))) return "路径越界";
    try {
      return fs.readFileSync(full, "utf8").slice(0, 300000);
    } catch (e) {
      return String(e);
    }
  }

  listRules(): string[] {
    return fs.existsSync(this.rulesDir)
      ? fs.readdirSync(this.rulesDir).filter((n) => n.endsWith(".json"))
      : [];
  }

  loadRule(name: string): unknown | null {
    return this.readJson(path.join(this.rulesDir, safeName(name)));
  }

  saveRule(name: string, data: unknown): boolean {
    const text = JSON.stringify(data, null, 2);
    if (!JSON.parse(text) || Array.isArray(JSON.parse(text))) return false;
    fs.writeFileSync(path.join(this.rulesDir, `${safeName(name)}.json`), text, "utf8");
    return true;
  }

  deleteRule(name: string): boolean {
    const file = path.join(this.rulesDir, safeName(name));
    if (!fs.existsSync(file)) return false;
    fs.unlinkSync(file);
    return true;
  }

  analyzeActivity(): AnalysisResult {
    const events = this.listActivity(10000);
    const resultStats: Record<string, number> = {};
    const sourceStats: Record<string, number> = {};
    const actionStats: Record<string, number> = {};
    let failed = 0;
    for (const event of events) {
      const detail = String(event.detail || "");
      let result = "info";
      if (/fail|error|失败/i.test(detail)) { result = "error"; failed++; }
      else if (/timeout|超时/i.test(detail)) { result = "timeout"; failed++; }
      else if (/\[OK\]|成功|ok/i.test(detail)) result = "ok";
      resultStats[result] = (resultStats[result] || 0) + 1;
      const source = String(event.session || "manual");
      sourceStats[source] = (sourceStats[source] || 0) + 1;
      const kind = String(event.kind || "unknown");
      actionStats[kind] = (actionStats[kind] || 0) + 1;
    }
    return {
      total: events.length,
      executed: events.length,
      failed,
      successRate: events.length ? Math.round(((events.length - failed) / events.length) * 100) : 0,
      actionStats, resultStats, sourceStats,
      events: events.slice(-150),
    };
  }
}

export function taskToFlowSpec(task: TaskData): FlowSpec {
  return {
    $schema: "mobileauto/flow/v1",
    version: "1.0",
    meta: { key: task.key, name: task.name, description: task.description, app: task.app },
    settings: { engine: task.engine, maxLoops: task.max_loops, capabilities: task.capabilities,
      poll: task.poll, tapGap: task.tap_gap, stuckLimit: task.stuck_limit },
    locators: task.states || {},
    flow: { mode: "priority", states: task.flow || [] },
    observability: { stuckStates: task.stuck_states, finishInfoKeywords: task.finish_info_keywords },
  };
}

export function flowSpecToTask(spec: FlowSpec, key?: string): TaskData {
  const template = new_task(String((spec.meta as any)?.name || "导入任务"));
  const flowValue = spec.flow as any;
  const states = Array.isArray(flowValue?.states) ? flowValue.states : [];
  const settings = (spec.settings || {}) as any;
  const observability = (spec.observability || {}) as any;
  Object.assign(template, {
    key: key || template.key,
    name: String((spec.meta as any)?.name || template.name),
    description: String((spec.meta as any)?.description || ""),
    app: String((spec.meta as any)?.app || ""),
    enabled: false,
    flow: states,
    states: spec.locators || {},
    capabilities: settings.capabilities || template.capabilities,
    max_loops: Number(settings.maxLoops || 30),
    poll: Number(settings.poll || 0.25),
    tap_gap: Number(settings.tapGap || 0.35),
    stuck_states: observability.stuckStates || [],
    finish_info_keywords: observability.finishInfoKeywords || [],
  });
  return template;
}

function new_task(name: string): TaskData {
  const key = `task_${Date.now().toString(36)}`;
  return {
    key, name, description: "", app: "", rule: "", flow: [],
    engine: "configured", enabled: false, max_loops: 30,
    capabilities: {}, states: {}, stuck_states: [], stuck_limit: 12,
    finish_info_keywords: [], poll: 0.25, tap_gap: 0.35,
    updated: new Date().toISOString(),
  };
}
