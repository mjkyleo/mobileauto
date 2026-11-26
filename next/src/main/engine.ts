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
    let muteUsed = false;
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
        const done = await this.act(state, ocr, task, () => (muteUsed = true), () => (muteUsed = false));
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

  private matchState(task: TaskData, texts: string[]): Record<string, unknown> | null {
    const states = task.flow || [];
    for (const state of states) {
      const s = state as any;
      if ((s.exclude || []).some?.((word: string) => texts.some((t) => t.includes(word)))) continue;
      if ((s.match_any || []).some?.((word: string) => texts.some((t) => t.includes(word)))) return s;
      const needAll = (s.match_all || []) as string[];
      const values = Array.isArray(needAll) ? needAll : [];
      if (values.length && values.every((word) => texts.some((t) => t.includes(word)))) return s;
      const match = s.match || s.match_text || [];
      const words = Array.isArray(match) ? match : String(match).split(",").filter(Boolean);
      if (words.some((word) => texts.some((t) => t.includes(String(word))))) return s;
      if (s.fallback) return s;
    }
    return null;
  }

  private async act(state: Record<string, unknown>, ocr: OcrItem[], task: TaskData,
                    setMute: () => void, resetMute: () => void): Promise<string> {
    const s = state as any;
    const action = String(s.action || "wait");
    if (action === "finish") {
      this.emit("命中结束态，停止任务", "finish");
      return "finish";
    }
    if (action === "click_match") {
      const target = String(s.click_target || s.match || s.match_text || "");
      const excluded = (s.click_exclude || []) as string[];
      const hit = ocr.find((o) => o.text.includes(target) && !excluded.some((x) => o.text.includes(x)));
      if (hit) {
        this.emit(`点击文字「${hit.text}」(${hit.x},${hit.y})`, "action");
        await this.device.click(hit.x, hit.y);
      } else {
        this.emit(`未找到文字「${target}」`, "warn");
      }
    } else if (action === "click_ratio") {
      const ratio = s.ratio || s.click_ratio || [0.5, 0.5];
      const [rx, ry] = Array.isArray(ratio) ? ratio.map(Number) : [0.5, 0.5];
      this.emit(`点击比例 ${rx},${ry}`, "action");
      await this.device.clickRatio(rx, ry);
    } else if (action === "back") {
      this.emit("按返回键", "action");
      await this.device.press("back");
    } else if (action === "scroll") {
      const direction = s.direction === "up" ? "up" : "down";
      this.emit(`滚动 ${direction}`, "action");
      await this.device.scroll(direction);
    } else if (action === "wait") {
      await this.sleep(Number(s.seconds || 1));
    } else if (action === "mute_once") {
      if (!muteState.has(task.key)) {
        this.emit("静音一次", "action");
        await this.device.clickRatio(...(s.ratio || [0.23, 0.027]) as [number, number]);
        muteState.add(task.key);
        setMute();
      }
    } else if (action === "close_ad") {
      const close = ocr.find((o) => ["×", "x", "关闭", "跳过"].some((w) => o.text.includes(w)));
      if (close) {
        this.emit(`关闭广告：${close.text}`, "action");
        await this.device.click(close.x, close.y);
      } else {
        await this.device.clickRatio(...(s.ratio || [0.95, 0.03]) as [number, number]);
        await this.device.press("back");
      }
      muteState.delete(task.key);
      resetMute();
    }
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

const muteState = new Set<string>();
