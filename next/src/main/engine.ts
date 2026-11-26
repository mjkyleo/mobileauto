import * as fs from "fs";
import * as path from "path";
import { spawn } from "child_process";
import type { TaskData } from "../shared/types";
import type { DeviceController } from "./device";
import type { Store } from "./store";

export interface OcrItem { text: string; score: number; x: number; y: number }

export class TaskEngine {
  private active = false;
  private process: import("child_process").ChildProcess | null = null;
  private logLines: string[] = [];

  constructor(private device: DeviceController, private store: Store, private root: string,
              private notify: (text: string) => void) {}

  get running(): boolean { return this.active; }

  get progress(): string[] { return this.logLines.slice(-300); }

  stop(): void {
    this.active = false;
    this.process?.kill();
    this.process = null;
  }

  clear(): void { this.logLines = []; }

  private emit(text: string, kind = "task"): void {
    const line = `[${new Date().toLocaleTimeString()}] ${text}`;
    this.logLines.push(line);
    if (this.logLines.length > 400) this.logLines.shift();
    this.store.appendActivity(kind, text, "", { session: "task" });
  }

  async start(task: TaskData): Promise<boolean> {
    if (this.active) return false;
    this.active = true;
    this.logLines = [];
    this.emit(`任务启动：${task.name}`, "task");
    void this.run(task);
    return true;
  }

  private async run(task: TaskData): Promise<void> {
    const maxLoops = Number(task.max_loops || 0);
    let round = 0;
    let lastState = "";
    let idle = 0;
    try {
      while (this.active && (!maxLoops || round < maxLoops)) {
        round++;
        this.emit(`—— 第 ${round} 轮 ——`, "round");
        await this.device.refresh();
        const image = await this.device.screenshot(88);
        const ocr = image ? await this.runOcr(image) : [];
        const texts = [
          ...ocr.map((o) => o.text),
          ...this.device.rows.map((r) => r.text || r.desc),
        ];
        const state = this.matchState(task, texts);
        const name = String((state as any)?.id || (state as any)?.name || "UNKNOWN");
        if (name !== lastState) {
          this.emit(`页面切换 ${lastState || "-"} → ${name}`, "state");
          lastState = name;
          idle = 0;
        } else {
          idle++;
        }
        if ((task.stuck_states || []).includes(name) && idle % Number(task.stuck_limit || 12) === 0 && idle > 0) {
          this.emit("疑似卡住，按返回键兜底", "warn");
          await this.device.press("back");
        }
        if (!state) {
          await this.sleep(Number(task.poll || 0.25));
          continue;
        }
        const done = await this.act(state, ocr, task);
        if (done === "finish") break;
        await this.sleep(Number(task.poll || 0.25));
      }
      this.emit("任务结束", "task");
    } catch (e) {
      this.emit(`任务异常：${(e as Error).message}`, "error");
    } finally {
      this.active = false;
      this.notify("任务已结束");
    }
  }

  private ruleHit(texts: string[], rule: unknown, all = false): boolean {
    if (rule == null) return false;
    if (typeof rule === "string") return texts.some((text) => text.includes(rule));
    if (Array.isArray(rule)) {
      const check = all ? Array.prototype.every : Array.prototype.some;
      return check.call(rule, (item: unknown) => this.ruleHit(texts, item));
    }
    if (typeof rule === "object") return this.objectHit(texts, rule as Record<string, unknown>);
    return false;
  }

  private objectHit(texts: string[], rule: Record<string, unknown>): boolean {
    if (rule.exclude && this.ruleHit(texts, rule.exclude)) return false;
    if (rule.match && !this.ruleHit(texts, rule.match)) return false;
    if (rule.match_any && !this.ruleHit(texts, rule.match_any)) return false;
    if (rule.match_all && !(rule.match_all as unknown[]).every((group) => this.ruleHit(texts, group))) return false;
    if (rule.all && !(rule.all as unknown[]).every((group) => this.ruleHit(texts, group))) return false;
    return true;
  }

  private matchState(task: TaskData, texts: string[]): Record<string, unknown> | null {
    let fallback: Record<string, unknown> | null = null;
    for (const state of task.flow || []) {
      const s = state as any;
      if (s.fallback) fallback = s;
      if (s.exclude && this.ruleHit(texts, s.exclude)) continue;
      if (s.match_all && !(s.match_all as unknown[]).every((group) => this.ruleHit(texts, group))) continue;
      if (s.match_any && !this.ruleHit(texts, s.match_any)) continue;
      if ((s.match || s.match_text) && !this.ruleHit(texts, s.match || s.match_text)) continue;
      return s;
    }
    return fallback;
  }

  private async act(state: Record<string, unknown>, ocr: OcrItem[], task: TaskData): Promise<string> {
    const s = state as any;
    const action = String(s.action || "wait");
    const name = String(s.name || s.id || s.label || action);
    const flag = flagFor(name, action);
    if (action === "finish") {
      this.emit(String(s.finish_title || `命中结束态 ${name}，停止任务`), "finish");
      if (s.finish_fallback) this.emit(String(s.finish_fallback), "finish");
      return "finish";
    }
    if (action === "click_match") {
      const targetValue = s.click_target ?? s.match ?? s.match_text ?? [];
      const targets = Array.isArray(targetValue) ? targetValue.map(String) : String(targetValue).split(",");
      const excludedRaw = s.click_exclude ?? s.exclude ?? [];
      const excluded = Array.isArray(excludedRaw) ? excludedRaw.map(String) : [String(excludedRaw)];
      const hit = ocr.find((o) => targets.some((target) => o.text.includes(target)) &&
        !excluded.some((word) => o.text.includes(word)));
      if (hit) {
        this.emit(`[${name}] 点击文字「${hit.text}」(${hit.x},${hit.y})`, "action");
        await this.device.click(hit.x, hit.y);
      } else if (s.retry || s.count) {
        this.emit(`[${name}] 未找到文字「${targets.join("/")}，保持等待`, "warn");
      }
    } else if (action === "click_ratio" || action === "click_ratio_once") {
      if (action === "click_ratio_once" && usedFlags.has(flag)) {
        this.emit(`[${name}] 一次性点击已执行，跳过`, "state");
      } else {
        const ratio = s.ratio || s.click_ratio || [0.5, 0.5];
        const [rx, ry] = Array.isArray(ratio) ? ratio.map(Number) : [0.5, 0.5];
        this.emit(`[${name}] 点击比例 ${rx},${ry}`, "action");
        await this.device.clickRatio(rx, ry);
        if (action === "click_ratio_once") usedFlags.add(flag);
      }
    } else if (action === "back") {
      this.emit(`[${name}] 按返回键`, "action");
      await this.device.press("back");
    } else if (action === "scroll") {
      const direction = s.direction === "up" ? "up" : "down";
      this.emit(`[${name}] 滚动 ${direction}`, "action");
      await this.device.scroll(direction);
    } else if (action === "wait") {
      await this.sleep(Number(s.seconds || 1));
    } else if (action === "mute_once") {
      if (!usedFlags.has(flag)) {
        const ratio = (s.ratio || [0.23, 0.027]) as [number, number];
        this.emit(`[${name}] 静音一次 ${ratio.join(",")}`, "action");
        await this.device.clickRatio(ratio[0], ratio[1]);
        usedFlags.add(flag);
      }
    } else if (action === "close_ad") {
      const close = ocr.find((o) => ["×", "x", "关闭", "跳过"].some((w) => o.text.includes(w)));
      if (close) {
        this.emit(`[${name}] 关闭广告：${close.text}`, "action");
        await this.device.click(close.x, close.y);
      } else if (s.ratio) {
        const [rx, ry] = (s.ratio as unknown[]).map(Number);
        this.emit(`[${name}] 按比例关闭 ${rx},${ry}`, "action");
        await this.device.clickRatio(rx, ry);
      }
      if (s.back_fallback) await this.device.press("back");
      usedFlags.clear();
    }
    for (const reset of (s.reset_flags || []) as string[]) usedFlags.delete(reset);
    await this.sleep(Number(task.tap_gap || 0.35));
    return "continue";
  }

  private sleep(seconds: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, seconds) * 1000));
  }

  async runOcr(image: Buffer): Promise<OcrItem[]> {
    const dir = path.join(this.root, "runtime", "debug_ocr");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `engine_${Date.now()}.png`);
    fs.writeFileSync(file, image);
    const script = path.join(this.root, "next", "tools", "ocr.py");
    if (!fs.existsSync(script)) return [];
    return new Promise((resolve) => {
      const child = spawn(process.env.PYTHON || "python", [script, file], { windowsHide: true });
      let out = "";
      child.stdout?.on("data", (c) => (out += c.toString("utf8")));
      child.on("close", () => {
        try {
          const value = JSON.parse(out || "[]");
          resolve(Array.isArray(value) ? value : []);
        } catch {
          resolve([]);
        }
      });
    });
  }
}

const usedFlags = new Set<string>();

function flagFor(name: string, action: string): string {
  if (name === "AD") return "_muted_AD";
  if (name === "COUNTDOWN") return "_muted_COUNTDOWN";
  if (name === "UNKNOWN") return "_muted_UNKNOWN";
  if (name === "LIVE") return "_once_LIVE";
  return `_${action}_${name}`;
}
