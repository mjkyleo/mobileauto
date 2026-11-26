import { EventEmitter } from "events";
import * as net from "net";
import * as path from "path";
import { spawn, ChildProcess } from "child_process";
import { WebSocketServer, WebSocket } from "ws";
import type { AdbClient } from "./adb";

const VERSION = "5.0.1";
const WS_PORT = 8766;
const FORWARD_PORT = 27300 + Math.floor(Math.random() * 400);

export interface ScrcpyEvents {
  frame: (key: boolean, avcc: Buffer) => void;
  config: (description: Buffer) => void;
  status: (text: string) => void;
}

function annexbToAvcc(data: Buffer): Buffer {
  const chunks: Buffer[] = [];
  for (const nal of splitAnnexb(data)) {
    const head = Buffer.alloc(4);
    head.writeUInt32BE(nal.length, 0);
    chunks.push(head, nal);
  }
  return Buffer.concat(chunks);
}

export class ScrcpySession extends EventEmitter {
  private serverProcess: ChildProcess | null = null;
  private videoSocket: net.Socket | null = null;
  private controlSocket: net.Socket | null = null;
  private wss: WebSocketServer | null = null;
  private buffer = Buffer.alloc(0);
  private description = Buffer.alloc(0);
  private frameTimes: number[] = [];
  private scid: number;
  private socketName: string;
  private localForward: string;
  running = false;
  hasFrame = false;
  width = 0;
  height = 0;

  constructor(private adb: AdbClient, private serverAsset: string, private log: (s: string) => void) {
    super();
    this.scid = Math.floor(Math.random() * 0x7fffffff);
    this.socketName = `scrcpy_${this.scid.toString(16).padStart(8, "0")}`;
    this.localForward = `tcp:${FORWARD_PORT}`;
  }

  get wsUrl(): string {
    return `ws://127.0.0.1:${WS_PORT}`;
  }

  fps(): number {
    const now = Date.now();
    this.frameTimes = this.frameTimes.filter((t) => now - t <= 2000);
    if (!this.frameTimes.length) return 0;
    return Math.round((this.frameTimes.length / Math.max((now - this.frameTimes[0]) / 1000, 0.1)) * 10) / 10;
  }

  async start(): Promise<boolean> {
    if (this.running) return true;
    if (!this.adb.binary || !this.adb.serial) {
      this.log("scrcpy 需要 adb 与设备 serial");
      return false;
    }
    const remote = `/data/local/tmp/scrcpy-server-${VERSION}.jar`;
    if (!(await this.adb.push(this.serverAsset, remote))) {
      this.log("scrcpy-server 推送失败");
      return false;
    }
    if (!(await this.adb.forward(this.localForward, `localabstract:${this.socketName}`))) {
      this.log("adb forward 失败");
      return false;
    }
    this.wss = new WebSocketServer({ host: "127.0.0.1", port: WS_PORT });
    this.wss.on("connection", (ws) => {
      if (this.description.length) this.safeSend(ws, Buffer.concat([Buffer.from([0]), this.description]));
      ws.on("error", () => undefined);
    });

    const args = [
      "-s", this.adb.serial, "shell",
      `CLASSPATH=${remote}`, "app_process", "/",
      "com.genymobile.scrcpy.Server", VERSION,
      `scid=${this.scid.toString(16).padStart(8, "0")}`,
      "tunnel_forward=true", "video=true", "video_codec=h264", "audio=false",
      "control=true", "send_device_meta=false", "send_frame_meta=true",
      "send_dummy_byte=false", "send_stream_meta=false", "max_fps=60",
      "video_bit_rate=8000000", "max_size=1920", "log_level=warn",
    ];
    this.serverProcess = spawn(this.adb.binary, args, { windowsHide: true });
    this.serverProcess.stderr?.on("data", (chunk) => {
      const text = chunk.toString("utf8").trim();
      if (text) this.log(`scrcpy: ${text.slice(0, 240)}`);
    });
    this.serverProcess.on("exit", () => {
      if (this.running) this.log("scrcpy-server 已退出");
    });

    try {
      this.videoSocket = await this.connectForward();
      this.controlSocket = await this.connectForward();
    } catch (e) {
      this.log(`scrcpy 通道连接失败：${(e as Error).message}`);
      await this.stop();
      return false;
    }
    this.videoSocket.on("data", (chunk: unknown) =>
      this.onVideoData(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk))));
    this.videoSocket.on("error", () => undefined);
    this.controlSocket.on("error", () => undefined);
    this.running = true;
    this.log(`H264 链路已建立：${this.adb.serial}`);
    return true;
  }

  private connectForward(timeoutMs = 12000): Promise<net.Socket> {
    return new Promise((resolve, reject) => {
      const started = Date.now();
      const attempt = () => {
        const socket = net.createConnection({ host: "127.0.0.1", port: FORWARD_PORT });
        const onError = (err: Error) => {
          socket.destroy();
          if (Date.now() - started > timeoutMs) reject(err);
          else setTimeout(attempt, 200);
        };
        socket.once("error", onError);
        socket.once("connect", () => {
          socket.off("error", onError);
          resolve(socket);
        });
      };
      attempt();
    });
  }

  private onVideoData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length >= 12) {
      const first = this.buffer.readBigUInt64BE(0);
      if ((first >> 63n) === 1n) {
        this.width = Number(this.buffer.readUInt32BE(4) & 0x7fffffff);
        this.height = this.buffer.readUInt32BE(8);
        this.buffer = this.buffer.subarray(12);
        continue;
      }
      const config = ((first >> 62n) & 1n) === 1n;
      const key = ((first >> 61n) & 1n) === 1n;
      const size = this.buffer.readUInt32BE(8);
      if (this.buffer.length < 12 + size) break;
      const payload = this.buffer.subarray(12, 12 + size);
      this.buffer = this.buffer.subarray(12 + size);
      if (config) {
        const nals: Buffer[] = [];
        for (const nal of splitAnnexb(payload)) {
          if ((nal[0] & 0x1f) === 7 || (nal[0] & 0x1f) === 8) {
            const head = Buffer.alloc(4);
            head.writeUInt32BE(nal.length, 0);
            nals.push(head, nal);
          }
        }
        const description = Buffer.concat(nals);
        if (description.length && !description.equals(this.description)) {
          this.description = description;
          this.broadcast(Buffer.concat([Buffer.from([0]), description]));
          this.emit("config", description);
        }
      } else if (payload.length) {
        this.hasFrame = true;
        this.frameTimes.push(Date.now());
        const avcc = annexbToAvcc(payload);
        this.broadcast(Buffer.concat([Buffer.from([1, key ? 1 : 0]), avcc]));
        this.emit("frame", key, avcc);
      }
    }
  }

  private safeSend(ws: WebSocket, data: Buffer): void {
    try {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    } catch {
      /* ignore */
    }
  }

  private broadcast(data: Buffer): void {
    if (!this.wss) return;
    for (const ws of this.wss.clients) this.safeSend(ws as WebSocket, data);
  }

  hasControl(): boolean {
    return this.running && !!this.controlSocket && !this.controlSocket.destroyed;
  }

  getScreenSize(): [number, number] | null {
    return this.width && this.height ? [this.width, this.height] : null;
  }

  private send(buffer: Buffer): boolean {
    if (!this.hasControl() || !this.controlSocket) return false;
    try {
      this.controlSocket.write(buffer);
      return true;
    } catch {
      return false;
    }
  }

  private touch(action: number, x: number, y: number): Buffer {
    const [w, h] = this.getScreenSize() || [1080, 1920];
    const msg = Buffer.alloc(32);
    msg.writeUInt8(2, 0);
    msg.writeUInt8(action, 1);
    msg.writeBigUInt64BE(1n, 2);
    msg.writeInt32BE(Math.round(x), 10);
    msg.writeInt32BE(Math.round(y), 14);
    msg.writeUInt16BE(Math.min(w, 65535), 18);
    msg.writeUInt16BE(Math.min(h, 65535), 20);
    msg.writeUInt16BE(0xffff, 22);
    msg.writeUInt32BE(1, 24);
    msg.writeUInt32BE(1, 28);
    return msg;
  }

  tap(x: number, y: number): boolean {
    return this.send(this.touch(0, x, y)) && this.send(this.touch(1, x, y));
  }

  swipe(x1: number, y1: number, x2: number, y2: number): boolean {
    const points: Array<[number, number, number]> = [
      [0, x1, y1], [2, x1, y1],
      [2, Math.round((x1 + x2) / 2), Math.round((y1 + y2) / 2)],
      [2, x2, y2], [1, x2, y2],
    ];
    for (const [action, x, y] of points) {
      if (!this.send(this.touch(action, x, y))) return false;
    }
    return true;
  }

  keyevent(code: number, action = 0): boolean {
    const msg = Buffer.alloc(14);
    msg.writeUInt8(0, 0);
    msg.writeUInt8(action, 1);
    msg.writeInt32BE(code, 2);
    msg.writeUInt32BE(0, 6);
    msg.writeUInt32BE(0, 10);
    return this.send(msg);
  }

  async stop(): Promise<void> {
    this.running = false;
    this.hasFrame = false;
    this.videoSocket?.destroy();
    this.controlSocket?.destroy();
    this.videoSocket = null;
    this.controlSocket = null;
    if (this.serverProcess) {
      try { this.serverProcess.kill(); } catch { /* ignore */ }
      this.serverProcess = null;
    }
    if (this.wss) {
      for (const client of this.wss.clients) (client as WebSocket).close();
      this.wss.close();
      this.wss = null;
    }
    await this.adb.forwardRemove(this.localForward);
  }
}

function splitAnnexb(data: Buffer): Buffer[] {
  return data.toString("binary")
    .split(/\x00\x00\x00?\x01/)
    .map((part) => Buffer.from(part, "binary"))
    .filter((part) => part.length && part.some((value) => value !== 0));
}
