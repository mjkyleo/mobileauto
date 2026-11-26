import * as fs from "fs";
import * as path from "path";

export class Logger {
  private lines: string[] = [];
  private file: string;

  constructor(root: string) {
    const runtime = path.join(root, "runtime");
    fs.mkdirSync(runtime, { recursive: true });
    this.file = path.join(runtime, "workbench.log");
  }

  log(text: string): void {
    const ts = new Date().toISOString();
    const line = `[${ts}] ${text}`;
    this.lines.push(line);
    if (this.lines.length > 400) this.lines.shift();
    console.log(line);
    try {
      fs.appendFileSync(this.file, line + "\n", "utf8");
    } catch {
      /* ignore */
    }
  }

  getLogs(limit = 200): string[] {
    return this.lines.slice(-limit);
  }

  clear(): void {
    this.lines = [];
  }
}
