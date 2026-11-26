import * as path from "path";
import { spawn } from "child_process";
import type { UiRow } from "../shared/types";
import { AdbClient, U2Client, findAdb, parseUiXml } from "./adb";
import { ScrcpySession } from "./scrcpy";

export type DeviceMode = "u2" | "adb" | "";

export class DeviceController {
  mode: DeviceMode = "";
  label = "";
  address = "";
  rows: UiRow[] = [];
  screen: [number, number] = [1080, 1920];
  adb: AdbClient | null = null;
  u2: U2Client | null = null;
  scrcpy: ScrcpySession | null = null;
  autoConnect = true;
  private root: string;
  private log: (s: string) => void;

  constructor(root: string, log: (s: string) => void) {
    this.root = root;
    this.log = log;
    const adbPath = findAdb(root);
    if (adbPath) this.adb = new AdbClient(adbPath);
  }

  get connected(): boolean {
    return this.mode === "u2" || this.mode === "adb";
  }

  get status(): string {
    if (!this.connected) return "设备未连接";
    const h264 = this.scrcpy?.running && this.scrcpy.hasFrame;
    return `已连接（${this.mode}）${h264 ? " · H264" : ""}`;
  }

  async connect(mode: string = "auto", address = ""): Promise<boolean> {
    address = String(address || "").trim();
    const addr = address || (await this.lastAddress());
    if (addr) {
      const looksU2 = addr.startsWith("http") || /^\d+\.\d+\.\d+\.\d+:7912$/.test(addr);
      if ((mode === "auto" || mode === "u2") && looksU2) {
        const u2 = new U2Client(addr);
        try {
          await u2.jsonrpc("deviceInfo", []);
          this.u2 = u2;
          this.mode = "u2";
          this.label = addr;
          this.address = addr;
          this.screen = await u2.screenSize();
          await this.writeLastAddress(addr);
          await this.refresh();
          await this.startScrcpy();
          return true;
        } catch (e) {
          this.log(`u2 连接失败：${(e as Error).message}`);
        }
      }
      if (this.adb && (mode === "auto" || mode === "adb")) {
        const serial = addr.replace(":7912", ":5555");
        const test = await this.adb.run(["-s", serial, "get-state"], 5000);
        if (test.code === 0 && test.stdout.includes("device")) {
          this.adb = new AdbClient(this.adb.binary, serial);
          this.mode = "adb";
          this.label = serial;
          this.address = addr;
          this.screen = await this.adb.screenSize();
          await this.writeLastAddress(addr);
          await this.refresh();
          await this.startScrcpy();
          return true;
        }
      }
    }
    if (mode !== "adb") {
      if (await this.portOpen("127.0.0.1", 7912)) {
        return this.connect("u2", "127.0.0.1:7912");
      }
    }
    if (this.adb && mode !== "u2") {
      const devices = await this.adb.listDevices();
      if (devices.length) return this.connect("adb", devices[0]);
    }
    if (mode === "u2") {
      try {
        const u2 = new U2Client("127.0.0.1:7912");
        await u2.jsonrpc("deviceInfo", []);
        return this.connect("u2", "127.0.0.1:7912");
      } catch (e) {
        this.log(`u2 自动检测失败：${(e as Error).message}`);
      }
    }
    this.log("未发现可连接设备");
    return false;
  }

  private async portOpen(host: string, port: number): Promise<boolean> {
    const { createConnection } = await import("net");
    return new Promise((resolve) => {
      const socket = createConnection({ host, port });
      const done = (value: boolean) => {
        socket.destroy();
        resolve(value);
      };
      socket.setTimeout(300);
      socket.once("connect", () => done(true));
      socket.once("timeout", () => done(false));
      socket.once("error", () => done(false));
    });
  }

  private lastAddressFile(): string {
    return path.join(this.root, "runtime", "last_device_address.txt");
  }

  private async lastAddress(): Promise<string> {
    try {
      const fs = await import("fs");
      return fs.readFileSync(this.lastAddressFile(), "utf8").trim();
    } catch {
      return "";
    }
  }

  private async writeLastAddress(address: string): Promise<void> {
    const fs = await import("fs");
    fs.writeFileSync(this.lastAddressFile(), address, "utf8");
  }

  rememberAddress(address: string): void {
    void this.writeLastAddress(address);
  }

  setAutoConnect(enabled: boolean): void {
    this.autoConnect = !!enabled;
  }

  async refresh(): Promise<UiRow[]> {
    if (!this.connected) return [];
    try {
      const xml = this.u2 ? await this.u2.dumpHierarchy() : await this.adb!.dumpHierarchy();
      this.rows = parseUiXml(xml, this.screen);
      if (this.u2) this.screen = await this.u2.screenSize();
      else if (this.adb) this.screen = await this.adb.screenSize();
    } catch (e) {
      this.log(`读取控件树失败：${(e as Error).message}`);
    }
    return this.rows;
  }

  async screenshot(quality = 88): Promise<Buffer | null> {
    if (this.u2) return this.u2.screenshot(quality);
    if (!this.adb) return null;
    const { execFileSync } = await import("child_process");
    try {
      return execFileSync(this.adb.binary,
        this.adb.serial ? ["-s", this.adb.serial, "exec-out", "screencap", "-p"] : ["exec-out", "screencap", "-p"],
        { maxBuffer: 40 * 1024 * 1024 });
    } catch {
      return null;
    }
  }

  async click(x: number, y: number, preferScrcpy = true): Promise<boolean> {
    if (preferScrcpy && this.scrcpy?.hasControl()) {
      if (this.scrcpy.tap(x, y)) return true;
    }
    if (this.u2) {
      try {
        await this.u2.jsonrpc("click", [Math.round(x), Math.round(y)]);
        return true;
      } catch { /* fallthrough */ }
    }
    return this.adb ? this.adb.tap(x, y) : false;
  }

  async clickRow(index: number): Promise<boolean> {
    const row = this.rows.find((r) => r.index === index);
    if (!row) return false;
    return this.click(row.cx, row.cy);
  }

  async clickRatio(rx: number, ry: number, record = false): Promise<boolean> {
    const x = Math.round(rx * this.screen[0]);
    const y = Math.round(ry * this.screen[1]);
    const ok = await this.click(x, y);
    if (record && ok) {
      const store = (global as any).__store;
      store?.appendActivity?.("recorded", { action: "click_ratio", ratio: [rx, ry] }, `${x},${y}`);
    }
    return ok;
  }

  async scroll(direction: "up" | "down" = "down"): Promise<boolean> {
    const [w, h] = this.screen;
    const cx = Math.round(w / 2);
    const y1 = Math.round(direction === "up" ? h * 0.75 : h * 0.3);
    const y2 = Math.round(direction === "up" ? h * 0.3 : h * 0.75);
    if (this.scrcpy?.hasControl() && this.scrcpy.swipe(cx, y1, cx, y2)) return true;
    if (this.adb) return this.adb.swipe(cx, y1, cx, y2);
    return false;
  }

  async press(kind: "back" | "home" | "recents"): Promise<boolean> {
    if (this.scrcpy?.hasControl() && this.scrcpy.keyevent(kind === "back" ? 4 : kind === "home" ? 3 : 187)) return true;
    const code = kind === "back" ? 4 : kind === "home" ? 3 : 187;
    if (this.adb) return this.adb.keyevent(code);
    if (this.u2) {
      const method = kind === "back" ? "back" : kind === "home" ? "home" : "recent";
      try {
        await this.u2.jsonrpc(method, []);
        return true;
      } catch { /* ignore */ }
    }
    return false;
  }

  async startScrcpy(): Promise<boolean> {
    if (!this.connected || !this.adb) return false;
    if (!this.adb.serial) {
      const devices = await this.adb.listDevices();
      if (devices.length) this.adb = new AdbClient(this.adb.binary, devices[0]);
    }
    if (!this.adb.serial) return false;
    const asset = path.join(this.root, "next", "assets", "scrcpy-server-5.0.1");
    const fs = await import("fs");
    const fallback = path.join(this.root, "assets", "scrcpy-server-5.0.1");
    const serverAsset = fs.existsSync(asset) ? asset : fallback;
    this.scrcpy = new ScrcpySession(this.adb, serverAsset, this.log);
    const ok = await this.scrcpy.start();
    if (!ok) this.scrcpy = null;
    return ok;
  }

  async disconnect(): Promise<void> {
    if (this.scrcpy) await this.scrcpy.stop();
    this.scrcpy = null;
    this.mode = "";
    this.u2 = null;
    this.rows = [];
    this.label = "";
  }

  diagnose(): Record<string, unknown> {
    return {
      adb: this.adb?.binary || "",
      mode: this.mode,
      label: this.label,
      rows: this.rows.length,
      screen: this.screen,
      h264: !!this.scrcpy?.running,
      hints: this.connected ? [] : ["请连接手机并开启 USB 调试，或填写手机 IP:7912"],
    };
  }
}
