"""页面数据采集：把手机当前页面的截图、OCR 文本、UI 层级存到脚本目录，用于后续优化。"""

import argparse
import atexit
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")

from mobileauto.paths import (
    APP_DIR as SCRIPT_DIR,
    RUNTIME_DIR,
    PAGE_DATA_DIR,
    COLLECT_KEEP_MAX,
    ensure_runtime_dirs,
)
from mobileauto.cleanup import prune_dirs

ensure_runtime_dirs()
LOG_FILE = os.path.join(RUNTIME_DIR, "collect_page_data.log")


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
            except Exception:
                pass

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self):
        return any(getattr(s, "isatty", lambda: False)() for s in self.streams)


try:
    _log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    _log_fp.write(f"\n===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 采集启动 =====\n")
    sys.stdout = _Tee(sys.stdout, _log_fp)
    sys.stderr = _Tee(sys.stderr, _log_fp)
    atexit.register(_log_fp.close)
except Exception as e:
    LOG_FILE = None
    print(f"⚠️ 无法创建日志文件: {e}")


def ensure_u2():
    try:
        if importlib.util.find_spec("uiautomator2") is not None:
            return True
    except (ImportError, ValueError):
        pass
    print("缺少 uiautomator2，尝试自动安装...")
    cmd = [sys.executable, "-m", "pip", "install", "uiautomator2"]
    if subprocess.run(cmd).returncode != 0:
        print("⚠️ uiautomator2 安装失败，将只尝试 adb 截图模式。")
        return False
    importlib.invalidate_caches()
    return importlib.util.find_spec("uiautomator2") is not None


def parse_args():
    parser = argparse.ArgumentParser(description="采集手机页面数据，用于优化识别规则")
    parser.add_argument("--count", type=int, default=10, help="采集帧数，默认 10")
    parser.add_argument("--interval", type=float, default=1.0, help="采集间隔秒，默认 1.0")
    parser.add_argument("--mode", choices=["auto", "u2", "adb"], default="auto", help="连接方式")
    parser.add_argument("--prefix", default="页面采集", help="目录名前缀")
    parser.add_argument("--ocr", choices=["auto", "on", "off"], default="auto", help="是否做 OCR")

    if len(sys.argv) > 1:
        return parser.parse_args()

    def ask(prompt, default, cast=str, choices=None):
        while True:
            raw = input(f"{prompt} [{default}]: ").strip()
            if not raw:
                return default
            try:
                value = cast(raw)
            except Exception:
                print("  输入无效，请重试")
                continue
            if choices and value not in choices:
                print(f"  可选值: {', '.join(choices)}")
                continue
            return value

    print("=" * 60)
    print("页面数据采集（交互模式，直接回车使用默认值）")
    print("=" * 60)
    count = ask("采集帧数", 10, int)
    interval = ask("采集间隔（秒）", 1.0, float)
    mode = ask("连接方式 auto/u2/adb", "auto", str, ["auto", "u2", "adb"])
    prefix = ask("目录名前缀", "页面采集", str)
    ocr = ask("OCR auto/on/off", "auto", str, ["auto", "on", "off"])
    return argparse.Namespace(count=count, interval=interval, mode=mode, prefix=prefix, ocr=ocr)


def find_adb():
    env_adb = os.environ.get("ADB_PATH")
    if env_adb and os.path.isfile(env_adb):
        return env_adb
    which = shutil.which("adb")
    if which:
        return which
    local_adb = os.path.join(SCRIPT_DIR, "adb.exe" if os.name == "nt" else "adb")
    if os.path.isfile(local_adb):
        return local_adb
    return None


def connect(args, has_u2):
    if has_u2 and args.mode in ("auto", "u2"):
        u2 = importlib.import_module("uiautomator2")
        attempts = [
            os.environ.get("ATX_AGENT_URL", "http://127.0.0.1:7912"),
            "http://127.0.0.1:7912",
            "127.0.0.1:7912",
            "",
        ]
        for addr in attempts:
            try:
                device = u2.connect(addr) if addr else u2.connect()
                print(f"✅ uiautomator2 连接成功: {addr or '自动检测'}")
                return "u2", device
            except Exception:
                pass
        print("⚠️ uiautomator2 所有地址均未连接成功")

    if args.mode == "u2":
        print("❌ 指定 u2 模式但连接失败")
        sys.exit(1)

    adb = find_adb()
    if adb:
        print(f"✅ 使用 adb 模式: {adb}")
        return "adb", adb

    print("❌ 无法连接设备")
    print("   u2：确认 atx-agent 已安装且无障碍权限已开启")
    print("   adb：确认 adb.exe 在项目目录或 PATH 中")
    sys.exit(1)


def init_ocr(setting):
    if setting == "off":
        return None, "已关闭"
    module_names = ["rapidocr_onnxruntime", "PIL", "numpy"]
    missing = []
    for name in module_names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if missing:
        return None, "缺少 " + ", ".join(missing)
    try:
        RapidOCR = importlib.import_module("rapidocr_onnxruntime").RapidOCR
        return RapidOCR(), "就绪"
    except Exception as e:
        return None, f"初始化失败: {e}"


def capture_u2(device, mode, count, interval, out_dir, engine):
    frames_dir = os.path.join(out_dir, "frames")
    ocr_dir = os.path.join(out_dir, "ocr")
    ui_dir = os.path.join(out_dir, "ui")
    for d in (frames_dir, ocr_dir, ui_dir):
        os.makedirs(d, exist_ok=True)

    for i in range(1, count + 1):
        ts = datetime.now().strftime("%H%M%S")
        img_path = os.path.join(frames_dir, f"{i:03d}_{ts}.png")
        try:
            device.screenshot(img_path)
        except Exception as e:
            print(f"⚠️ 第 {i} 帧截图失败: {e}")
            time.sleep(interval)
            continue

        if engine is not None:
            txt_path = os.path.join(ocr_dir, f"{i:03d}_{ts}.txt")
            img_path_saved = img_path
            from PIL import Image
            import numpy as np

            img = Image.open(img_path_saved).convert("RGB")
            result, _ = engine(np.array(img))
            lines = []
            for row in result or []:
                if len(row) < 3:
                    continue
                box, text, score = row[0], row[1], float(row[2])
                cx = sum(float(p[0]) for p in box) / len(box)
                cy = sum(float(p[1]) for p in box) / len(box)
                lines.append(f"{text}\t{score:.3f}\t({int(cx)},{int(cy)})")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

        try:
            xml = device.dump_hierarchy()
            with open(os.path.join(ui_dir, f"{i:03d}_{ts}.xml"), "w", encoding="utf-8") as f:
                f.write(xml)
        except Exception:
            pass

        print(f"· 已采集第 {i}/{count} 帧: {img_path}")
        if i < count:
            time.sleep(interval)


def capture_adb(adb, count, interval, out_dir, engine):
    frames_dir = os.path.join(out_dir, "frames")
    ocr_dir = os.path.join(out_dir, "ocr")
    os.makedirs(frames_dir, exist_ok=True)
    os.makedirs(ocr_dir, exist_ok=True)

    for i in range(1, count + 1):
        ts = datetime.now().strftime("%H%M%S")
        img_path = os.path.join(frames_dir, f"{i:03d}_{ts}.png")
        result = subprocess.run([adb, "exec-out", "screencap", "-p"], capture_output=True)
        if result.returncode != 0 or not result.stdout:
            print(f"⚠️ 第 {i} 帧截图失败")
            time.sleep(interval)
            continue
        with open(img_path, "wb") as f:
            f.write(result.stdout)
        if engine is not None:
            txt_path = os.path.join(ocr_dir, f"{i:03d}_{ts}.txt")
            from PIL import Image
            import numpy as np

            img = Image.open(img_path).convert("RGB")
            ocr_result, _ = engine(np.array(img))
            lines = []
            for row in ocr_result or []:
                if len(row) < 3:
                    continue
                box, text, score = row[0], row[1], float(row[2])
                cx = sum(float(p[0]) for p in box) / len(box)
                cy = sum(float(p[1]) for p in box) / len(box)
                lines.append(f"{text}\t{score:.3f}\t({int(cx)},{int(cy)})")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        print(f"· 已采集第 {i}/{count} 帧: {img_path}")
        if i < count:
            time.sleep(interval)


def main():
    args = parse_args()
    has_u2 = ensure_u2()
    mode, device = connect(args, has_u2)

    engine, ocr_status = init_ocr(args.ocr)
    if args.ocr == "on" and engine is None:
        print(f"❌ 指定开启 OCR，但不可用：{ocr_status}")
        sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = "".join(c for c in args.prefix if c.isalnum() or c in "-_")
    out_dir = os.path.join(PAGE_DATA_DIR, f"{stamp}_{safe_prefix}")
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"采集时间: {stamp}\n")
        f.write(f"连接方式: {mode}\n")
        f.write(f"采集帧数: {args.count}\n")
        f.write(f"采集间隔: {args.interval}\n")
        f.write(f"OCR 状态: {ocr_status}\n")

    print("=" * 60)
    print(f"采集目录: {out_dir}")
    print(f"连接方式: {mode}  OCR: {ocr_status}")
    print("=" * 60)

    if mode == "u2":
        capture_u2(device, mode, args.count, args.interval, out_dir, engine)
    else:
        capture_adb(device, args.count, args.interval, out_dir, engine)

    print(f"✅ 采集完成，请把目录发回用于优化：{out_dir}")
    removed = prune_dirs(PAGE_DATA_DIR, COLLECT_KEEP_MAX)
    if removed:
        print(f"· 已清理 {removed} 个过期采集批次，当前最多保留 {COLLECT_KEEP_MAX} 批")


if __name__ == "__main__":
    main()
