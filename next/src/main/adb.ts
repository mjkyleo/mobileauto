import { spawn, execFileSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import type { UiRow } from "../shared/types";

export interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

export function findAdb(root: string): string {
  const candidates = [
    process.env.ADB_PATH,
    path.join(root, "adb.exe"),
    path.join(root, "adb"),
    path.join(root, "platform-tools", "adb.exe"),
    path.join(root, "platform-tools", "adb"),
  ].filter(Boolean) as string[];
  for (const item of candidates) {
    if (fs.existsSync(item)) return item;
  }
  try {
    const out = execFileSync(process.platform === "win32" ? "where" : "which", ["adb"], {
      encoding: "utf8",
    }).split(/\r?\n/)[0];
    if (out && fs.existsSync(out)) return out.trim();
  } catch {
    /* ignore */
  }
  return "";
}

export function runCommand(file: string, args: string[], timeoutMs = 8000): Promise<CommandResult> {
  return new Promise((resolve) => {
    const child = spawn(file, args, { windowsHide: true });
    let stdout = "";
    let stderr = "";
    let done = false;
    const finish = (code: number) => {
      if (done) return;
      done = true;
      resolve({ code, stdout, stderr });
    };
    child.stdout?.on("data", (chunk) => (stdout += chunk.toString("utf8")));
    child.stderr?.on("data", (chunk) => (stderr += chunk.toString("utf8")));
    child.on("error", () => finish(-1));
    child.on("close", (code) => finish(code ?? -1));
    const timer = setTimeout(() => {
      child.kill();
      finish(-2);
    }, timeoutMs);
    child.on("close", () => clearTimeout(timer));
  });
}

export class AdbClient {
  constructor(public readonly binary: string, public readonly serial = "") {}

  private args(parts: string[]): string[] {
    return this.serial ? ["-s", this.serial, ...parts] : parts;
  }

  async run(parts: string[], timeoutMs = 8000): Promise<CommandResult> {
    if (!this.binary) return { code: -1, stdout: "", stderr: "adb not found" };
    return runCommand(this.binary, this.args(parts), timeoutMs);
  }

  async listDevices(): Promise<string[]> {
    const res = await this.run(["devices"], 5000);
    return res.stdout
      .split(/\r?\n/)
      .slice(1)
      .map((line) => line.split(/\s+/))
      .filter((parts) => parts.length >= 2 && parts[1] === "device")
      .map((parts) => parts[0]);
  }

  async screenSize(): Promise<[number, number]> {
    const res = await this.run(["shell", "wm", "size"], 6000);
    const match = res.stdout.match(/(\d+)x(\d+)/);
    if (match) return [Number(match[1]), Number(match[2])];
    return [1080, 1920];
  }

  async tap(x: number, y: number): Promise<boolean> {
    const res = await this.run(["shell", "input", "tap", String(Math.round(x)), String(Math.round(y))]);
    return res.code === 0;
  }

  async swipe(x1: number, y1: number, x2: number, y2: number): Promise<boolean> {
    const res = await this.run([
      "shell", "input", "swipe",
      String(Math.round(x1)), String(Math.round(y1)),
      String(Math.round(x2)), String(Math.round(y2)), "300",
    ]);
    return res.code === 0;
  }

  async keyevent(code: number): Promise<boolean> {
    return (await this.run(["shell", "input", "keyevent", String(code)])).code === 0;
  }

  async dumpHierarchy(): Promise<string> {
    const remote = "/sdcard/window.xml";
    await this.run(["shell", "uiautomator", "dump", remote], 30000);
    const res = await this.run(["shell", "cat", remote], 30000);
    return res.stdout;
  }

  async push(local: string, remote: string): Promise<boolean> {
    return (await this.run(["push", local, remote], 30000)).code === 0;
  }

  async forwardRemove(local: string): Promise<void> {
    await this.run(["forward", "--remove", local], 5000);
  }

  async forward(local: string, remote: string): Promise<boolean> {
    await this.forwardRemove(local);
    return (await this.run(["forward", local, remote], 8000)).code === 0;
  }
}

export function parseUiXml(xml: string, screen: [number, number]): UiRow[] {
  const rows: UiRow[] = [];
  const nodes = xml.match(/<node\b[^>]*>/g) || [];
  for (let i = 0; i < nodes.length; i++) {
    const tag = nodes[i];
    const attr = (name: string) => {
      const m = tag.match(new RegExp(`${name}="([^"]*)"`));
      return m ? m[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&") : "";
    };
    if (attr("visible") === "false") continue;
    const bounds = attr("bounds").match(/\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]/);
    if (!bounds) continue;
    const x1 = Number(bounds[1]);
    const y1 = Number(bounds[2]);
    const x2 = Number(bounds[3]);
    const y2 = Number(bounds[4]);
    const w = x2 - x1;
    const h = y2 - y1;
    if (w <= 0 || h <= 0) continue;
    if (w > screen[0] * 0.95 && h > screen[1] * 0.9) continue;
    rows.push({
      index: i,
      text: attr("text"),
      desc: attr("content-desc"),
      resourceId: attr("resource-id"),
      className: attr("class"),
      package: attr("package"),
      clickable: attr("clickable") === "true",
      enabled: attr("enabled") !== "false",
      visible: true,
      x1, y1, x2, y2,
      cx: Math.round((x1 + x2) / 2),
      cy: Math.round((y1 + y2) / 2),
      w, h,
    });
  }
  return rows;
}

export class U2Client {
  constructor(public readonly address: string) {}

  private get base(): string {
    return this.address.startsWith("http") ? this.address.replace(/\/$/, "") : `http://${this.address}`;
  }

  async jsonrpc(method: string, params: unknown[] = []): Promise<any> {
    const res = await fetch(`${this.base}/jsonrpc/0`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", method, params, id: Date.now() }),
      signal: AbortSignal.timeout(15000),
    });
    const data: any = await res.json();
    if (data.error) throw new Error(JSON.stringify(data.error));
    return data.result;
  }

  async screenshot(quality = 88): Promise<Buffer | null> {
    const raw = await this.jsonrpc("takeScreenshot", [1, quality]);
    if (!raw) return null;
    return Buffer.from(raw, "base64");
  }

  async dumpHierarchy(): Promise<string> {
    return String(await this.jsonrpc("dumpHierarchy", []) || "");
  }

  async screenSize(): Promise<[number, number]> {
    try {
      const value = await this.jsonrpc("getDisplaySize", []);
      if (Array.isArray(value) && value.length >= 2) return [Number(value[0]), Number(value[1])];
    } catch {
      /* ignore */
    }
    return [1080, 1920];
  }
}
