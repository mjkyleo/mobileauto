"""工作台启动器：控制台实时输出，同时镜像写入 runtime/workbench.log。"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            try:
                stream.write(text)
                stream.flush()
            except Exception:
                pass

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass


def main():
    runtime = os.path.join(ROOT, "runtime")
    os.makedirs(runtime, exist_ok=True)
    log_path = os.path.join(runtime, "workbench.log")
    log = open(log_path, "a", encoding="utf-8")
    sys.stdout = Tee(sys.__stdout__, log)
    sys.stderr = Tee(sys.__stderr__, log)

    print(f"[{time.strftime('%H:%M:%S')}] 工作台启动，日志：{log_path}", flush=True)
    from mobileauto.web_gui import main as run_gui

    run_gui()


if __name__ == "__main__":
    main()
