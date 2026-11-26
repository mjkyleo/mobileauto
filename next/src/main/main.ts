import { app, BrowserWindow } from "electron";
import * as path from "path";
import { Logger } from "./logger";
import { DeviceController } from "./device";
import { Store } from "./store";
import { TaskEngine } from "./engine";
import { ApiHost } from "./ipc";

function projectRoot(): string {
  const appPath = app.getAppPath();
  return path.basename(appPath).toLowerCase() === "next" ? path.dirname(appPath) : appPath;
}

let windowRef: BrowserWindow | null = null;

app.whenReady().then(() => {
  const root = projectRoot();
  const logger = new Logger(root);
  logger.log(`工作台启动，目录：${root}`);
  const device = new DeviceController(root, (text) => logger.log(text));
  const store = new Store(root);
  const engine = new TaskEngine(device, store, root, (text) => logger.log(text));
  new ApiHost(root, logger, device, store, engine).register();

  windowRef = new BrowserWindow({
    title: "手机自动化工作台",
    width: 1500,
    height: 980,
    minWidth: 1180,
    minHeight: 780,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  windowRef.loadFile(path.join(__dirname, "..", "renderer", "index.html"));
  windowRef.on("closed", () => {
    windowRef = null;
    engine.stop();
    void device.disconnect();
  });

  setInterval(async () => {
    if (!device.connected && device.autoConnect && !engine.running) {
      await device.connect("auto");
    }
    void store;
  }, 4000);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
