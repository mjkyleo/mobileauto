import * as fs from "fs";
import * as path from "path";
import type { AnalysisResult, FlowSpec, RecordedStep, TaskData } from "../shared/types";

function safeName(name: string): string {
  return String(name || "").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
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
      const data = this.readJson<TaskData>(path.join(this.tasksDir, name));
      if (data) items.push(data);
    }
    return items.sort((a, b) => String(b.updated).localeCompare(String(a.updated)));
  }

  loadTask(key: string): TaskData | null {
    const file = path.join(this.tasksDir, `${safeName(key)}.json`);
    return fs.existsSync(file) ? this.readJson<TaskData>(file) : null;
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

  listRecords(): Array<{ path: string; name: string; size: number; mtime: number; kind: string }> {
    const result: Array<{ path: string; name: string; size: number; mtime: number; kind: string }> = [];
    const walk = (dir: string) => {
      if (!fs.existsSync(dir)) return;
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          walk(full);
          continue;
        }
        if (!/\.(log|txt|xml|json|jsonl|png)$/i.test(entry.name)) continue;
        const stat = fs.statSync(full);
        const rel = path.relative(this.runtimeDir, full).replace(/\\/g, "/");
        let kind = "all";
        if (/\.log$/i.test(entry.name)) kind = "log";
        else if (/ocr|\.txt$/i.test(entry.name)) kind = "text";
        else if (/ui|\.xml$/i.test(entry.name)) kind = "ui";
        else if (/report|activity/i.test(entry.name)) kind = "report";
        result.push({ path: rel, name: entry.name, size: stat.size, mtime: stat.mtimeMs, kind });
      }
    };
    walk(this.runtimeDir);
    return result.sort((a, b) => b.mtime - a.mtime).slice(0, 800);
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
