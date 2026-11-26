"""自动化任务（唯一逻辑任务）。

底层自动选择：已连接 u2 -> adb，不向用户暴露版本差异。
启动后自动打开 App，用 OCR 识别页面文字，按规则流程执行：
跳过开屏 -> 进入福利页 -> 点领时长 -> 等广告 -> 领奖励 -> 关弹窗 -> 循环。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")

from mobileauto.paths import ensure_runtime_dirs

ensure_runtime_dirs()

TASK_KEY = os.environ.get("MOBILEAUTO_TASK_KEY", "")
APP_PACKAGE = os.environ.get("MOBILEAUTO_PACKAGE", "com.luna.music")
AUTO_LAUNCH = True
MAX_LOOPS = int(os.environ.get("MOBILEAUTO_MAX_LOOPS", "3"))


def _load_task():
    if not TASK_KEY:
        return None
    try:
        from mobileauto import tasks

        return tasks.load_task(TASK_KEY)
    except Exception as e:
        print(f"· 读取任务配置失败：{e}")
        return None


def _pick_driver():
    """返回统一驱动：{'mode': 'u2'|'adb', ...}。"""
    from mobileauto import inspector

    try:
        conn = inspector.connect_device("auto")
        print(f"· 使用连接方式：{conn['mode']}")
        return conn
    except Exception as e:
        print(f"· 自动连接失败：{e}")
    return None


def _act(kind, detail, target=""):
    try:
        from mobileauto import activity

        activity.log_action(kind, detail, target)
    except Exception:
        pass


def _open_app(conn):
    if not AUTO_LAUNCH:
        return
    print(f"· 启动 App：{APP_PACKAGE}")
    try:
        if conn.get("mode") == "u2":
            conn["client"].app_start(APP_PACKAGE)
        else:
            import subprocess

            subprocess.run(
                [conn["client"], "shell", "monkey", "-p", APP_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"],
                capture_output=True,
            )
        _act("launch", f"启动 {APP_PACKAGE}", APP_PACKAGE)
    except Exception as e:
        print(f"· 启动 App 失败（可能已在前台）：{e}")
        _act("launch", f"启动失败: {e}", APP_PACKAGE)
    time.sleep(1.5)


def _load_rules(task):
    """规则是任务特有的：优先用任务内嵌的 flow/规则对象，兼容旧的规则名引用。"""
    task = task or {}
    # 1. 任务内嵌了 flow 步骤 → 直接当作规则用
    if isinstance(task.get("flow"), dict):
        return {"name": task.get("name") or "任务内嵌规则", "flow": task["flow"]}
    # 2. 任务的 rule 字段直接是规则对象
    if isinstance(task.get("rule"), dict):
        return task["rule"]
    # 3. 兼容旧版：rule 是规则名字符串
    try:
        from mobileauto import rules

        name = task.get("rule") or rules.DEFAULT_RULE["name"]
        return rules.load_rule(name)
    except Exception as e:
        print(f"· 读取规则失败：{e}")
        return None


def main():
    task = _load_task()
    global APP_PACKAGE
    if task:
        APP_PACKAGE = task.get("app") or APP_PACKAGE
    session = TASK_KEY or os.environ.get("MOBILEAUTO_SESSION", "")
    try:
        from mobileauto import activity

        activity.set_session(session)
    except Exception:
        pass

    engine_mode = (task or {}).get("engine") or ""
    print("=" * 60)
    print(f"任务启动：{(task or {}).get('name') or '自动化任务'}")
    print(f"  App: {APP_PACKAGE}")
    print(f"  执行引擎: {engine_mode or '步骤流程（旧版，兼容）'}")
    print("=" * 60)
    _act("task", (task or {}).get("name") or "自动化任务", "开始执行")

    # 配置驱动的状态机任务（新场景：打卡、领奖等，纯 JSON 定义）
    if engine_mode == "configured":
        from mobileauto import configured_flow

        # 允许 bat/命令行用 MOBILEAUTO_MAX_LOOPS 覆盖任务 JSON 里的轮数
        if os.environ.get("MOBILEAUTO_MAX_LOOPS"):
            task = dict(task or {})
            task["max_loops"] = int(os.environ["MOBILEAUTO_MAX_LOOPS"])
        try:
            configured_flow.run_configured(task)
        except Exception as e:
            print(f"· 配置任务执行失败：{e}")
            _act("error", f"配置任务执行失败: {e}", "configured")
        _act("task", (task or {}).get("name") or "自动化任务", "结束")
        return

    conn = _pick_driver()
    if conn is None:
        print("❌ 没有可用设备连接，任务退出")
        _act("error", "没有可用设备连接", "任务退出")
        return

    _open_app(conn)

    rule = _load_rules(task)
    flow = (rule or {}).get("flow") or {}
    steps = flow.get("steps") or []
    if steps:
        from mobileauto import flow as flow_runner

        print(f"· 已加载规则：{rule.get('name')}，{len(steps)} 个步骤")
        loops = int(os.environ.get("MOBILEAUTO_MAX_LOOPS") or (task or {}).get("max_loops") or flow.get("max_loops") or MAX_LOOPS)
        flow_runner.run(
            conn,
            steps,
            loop_from=int(flow.get("loop_from") or 0),
            max_loops=loops,
        )
    else:
        print("⚠ 规则没有流程步骤，仅完成启动与开屏处理")
        from mobileauto import flow as flow_runner

        flow_runner.run(
            conn,
            [
                {
                    "key": "跳过开屏广告",
                    "find": ["福利中心", "免费听时长", "领时长", "听歌总时长", "发现"],
                    "click_text": ["跳过广告", "跳过开屏", "跳过", "关闭广告"],
                    "click_ratio": [0.88, 0.06],
                    "timeout_ms": 8000,
                    "interval_ms": 200,
                    "max_clicks": 8,
                    "ocr_width": 640,
                }
            ],
        )

    _act("task", "任务流程结束", APP_PACKAGE)
    print("· 任务流程结束")


if __name__ == "__main__":
    main()
