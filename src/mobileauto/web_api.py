"""Web 工作台桥接层：给前端 JS 提供 Python 自动化能力。"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque

from mobileauto.paths import APP_DIR, RUNTIME_DIR, ensure_runtime_dirs

ensure_runtime_dirs()

# 逻辑上只有一个自动化任务，底层实现由连接方式和运行环境自动决定
TASK_MODULE = "mobileauto.app_task"
TASK_LABEL = "自动化任务"


class WorkbenchApi:
    def __init__(self):
        self._webview = None
        self._window = None
        self._lock = threading.RLock()
        self.conn = None
        self.rows = []
        self.screen_size = None
        self.device_status = "未连接"
        self.device_label = ""
        self.scanning = False
        self.refreshing = False
        self.detection_enabled = True
        self._ui_busy = False
        self._ui_tick = 0
        self.proc = None
        self.proc_label = ""
        self.task_key = ""
        self.task_name = ""
        self.logs = deque(maxlen=800)
        self.auto_connect = True
        self.auto_attempts = 0
        self._auto_delay = 2
        self.last_address = ""
        self._ui_loop_started = False
        from mobileauto.paths import RUNTIME_DIR

        self._addr_cache = os.path.join(RUNTIME_DIR, "last_device_address.txt")
        if os.path.isfile(self._addr_cache):
            try:
                with open(self._addr_cache, "r", encoding="utf-8") as f:
                    self.last_address = f.read().strip()
            except Exception:
                pass
        from mobileauto import rules, tasks

        name = rules.ensure_default_rule()
        task_key = tasks.ensure_default_task()
        self.log("Web 工作台已启动")
        self.log(f"默认规则已就绪：{name}")
        self.log(f"默认任务已就绪：{task_key}")
        if self.last_address:
            self.log(f"已记住上次设备地址：{self.last_address}")
        self._spawn(self._auto_connect_loop)
        self._spawn(self._ui_cache_loop)

    # ---------- 基础状态 ----------
    def bind_window(self, webview_module, window):
        """由 web_gui 注入 pywebview 模块与窗口，用于原生对话框。"""
        self._webview = webview_module
        self._window = window
        return True

    def log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {text}")

    def get_state(self):
        with self._lock:
            return {
                "device_status": self.device_status,
                "device_label": self.device_label,
                "scanning": self.scanning,
                "refreshing": self.refreshing,
                "rows": self.rows,
                "screen": self.screen_size,
                "task_running": self.proc is not None and self.proc.poll() is None,
                "task_label": self.proc_label,
                "logs": list(self.logs),
                "app_dir": APP_DIR,
                "auto_connect": self.auto_connect,
                "auto_attempts": self.auto_attempts,
                "last_address": self.last_address,
                "task_key": self.task_key,
                "task_name": self.task_name,
            }

    def clear_log(self):
        self.logs.clear()
        self.log("日志已清空")
        return True

    def _spawn(self, func, *args):
        threading.Thread(target=func, args=args, daemon=True).start()

    def _effective_screen_size(self):
        """优先用缓存分辨率，其次用 scrcpy 会话分辨率。"""
        with self._lock:
            size = self.screen_size
        if size:
            return size
        from mobileauto import stream

        return stream.get_control_size() or [1440, 3200]

    # ---------- 设备连接 ----------
    def set_auto_connect(self, enabled):
        with self._lock:
            self.auto_connect = bool(enabled)
        self.log(f"自动连接已{'开启' if enabled else '关闭'}")
        return True

    def _auto_connect_loop(self):
        """后台常驻：未连接且开启时，用递增退避自动重连；连接成功后重置退避。"""
        while True:
            with self._lock:
                delay = self._auto_delay
            time.sleep(delay)
            if not self.auto_connect:
                self._auto_delay = 2
                continue
            with self._lock:
                if self.conn is not None or self.scanning:
                    self._auto_delay = 2
                    continue
            self.auto_attempts += 1
            address = self.last_address
            self.log(f"自动连接第 {self.auto_attempts} 次" + (f"：{address}" if address else ""))
            self.connect("auto", address)
            self._auto_delay = min(delay * 1.5, 12)

    def remember_address(self, address):
        address = (address or "").strip()
        with self._lock:
            self.last_address = address
        try:
            with open(self._addr_cache, "w", encoding="utf-8") as f:
                f.write(address)
        except Exception:
            pass

    def diagnose_device(self):
        from mobileauto import inspector

        return inspector.diagnose()

    def connect(self, mode="auto", address=""):
        with self._lock:
            if self.scanning:
                self.log("设备扫描已在进行中")
                return False
            self.scanning = True
            self.device_status = "正在扫描设备..."
        self.log(f"开始扫描设备：{mode} {address or ''}".rstrip())
        self._spawn(self._connect_worker, mode, address)
        return True

    def disconnect(self):
        with self._lock:
            self.conn = None
            self.rows = []
            self.screen_size = None
            self.device_status = "未连接"
            self.device_label = ""
        from mobileauto import stream

        stream.attach_conn(None)
        self.log("已断开设备")
        return True

    def _connect_worker(self, mode, address=""):
        try:
            from mobileauto import inspector

            conn = inspector.connect_device(mode, address)
            size = inspector.screen_size(conn)
            with self._lock:
                self.conn = conn
                self.rows = []
                self.screen_size = size
                self.device_status = f"已连接（{conn['mode']}）"
                self.device_label = conn.get("label", "")
                self.scanning = False
            from mobileauto import stream

            # 先接上画面和控制通道，UI 层级放到后台读，设备识别更快返回
            stream.attach_conn(conn)
            with self._lock:
                self.refreshing = True
            self._spawn(self._refresh_worker)
            self.auto_attempts = 0
            self._auto_delay = 2
            if conn.get("label") and conn["label"] not in ("自动检测", "自动"):
                self.remember_address(conn["label"])
            self.log(f"设备连接成功：{conn['mode']} {conn.get('label', '')}")
            self.log("正在后台读取 UI 按钮…")
        except Exception as e:
            with self._lock:
                self.scanning = False
                self.device_status = "连接失败"
                self.device_label = ""
            from mobileauto import stream

            stream.attach_conn(None)
            self.log(f"设备扫描失败：{e}")

    def _ui_cache_loop(self):
        """后台刷新 UI 层级；关闭按钮识别时完全不抓控件树，避免占用设备带宽。"""
        while True:
            time.sleep(2.5)
            with self._lock:
                if not self.detection_enabled or self.conn is None:
                    continue
                if self._ui_busy or self.refreshing:
                    continue
                self._ui_busy = True
                conn = self.conn
            try:
                from mobileauto import inspector

                _xml, rows = inspector.snapshot(conn)
                with self._lock:
                    self.rows = rows
                    self._ui_tick += 1
                    need_size = self._ui_tick % 8 == 1
                if need_size:
                    from mobileauto import stream

                    size = inspector.screen_size(conn) or stream.get_control_size()
                    if size:
                        with self._lock:
                            self.screen_size = size
            except Exception:
                pass
            finally:
                with self._lock:
                    self._ui_busy = False

    def set_detection(self, enabled=True):
        """开关按钮识别；关闭后停止控件树采集，把带宽全部留给视频。"""
        on = bool(enabled)
        with self._lock:
            self.detection_enabled = on
            if not on:
                self.rows = []
            connected = self.conn is not None
            busy = self.refreshing
        if on and connected and not busy:
            with self._lock:
                self.refreshing = True
            self._spawn(self._refresh_worker)
        self.log("按钮识别已开启" if on else "按钮识别已关闭，画面优先")
        return True

    def refresh(self):
        """手动触发一次立即刷新（后台线程执行，前端不等待）。"""
        with self._lock:
            if self.conn is None:
                self.log("尚未连接设备，先执行扫描")
                return self.connect("auto")
            if self.refreshing:
                return False
            self.refreshing = True
        self._spawn(self._refresh_worker)
        return True

    def _refresh_worker(self):
        try:
            from mobileauto import inspector, stream

            with self._lock:
                conn = self.conn
            _xml, rows = inspector.snapshot(conn)
            size = inspector.screen_size(conn) or stream.get_control_size()
            with self._lock:
                self.rows = rows
                if size:
                    self.screen_size = size
        except Exception as e:
            self.log(f"刷新元素失败：{e}")
        finally:
            with self._lock:
                self.refreshing = False

    def click_element(self, index):
        row = next((r for r in self.rows if r.get("index") == int(index)), None)
        if not row:
            self.log(f"找不到元素：{index}")
            return False
        return self.click_xy(row["cx"], row["cy"])

    def click_xy(self, x, y, record=True):
        if self.conn is None:
            self.log("设备未连接，无法点击")
            return False
        self.log(f"点击坐标：({int(x)}, {int(y)})")
        self._spawn(self._click_worker, int(x), int(y), bool(record))
        return True

    def click_ratio(self, rx, ry):
        """按比例坐标试点击（用于录制时人工定位），不写入录制草稿。"""
        w, h = self._effective_screen_size()[:2]
        x, y = int(float(rx) * w), int(float(ry) * h)
        self.log(f"试点击比例坐标：({rx}, {ry}) -> ({x}, {y})")
        return self.click_xy(x, y, record=False)

    def _click_worker(self, x, y, record=True):
        try:
            from mobileauto import inspector, stream

            # scrcpy 控制通道复用已建立连接，点击几乎没有进程启动开销
            if not stream.tap(x, y):
                inspector.click_at(self.conn, x, y)
            from mobileauto import activity

            activity.log_action("click", f"坐标点击 ({x},{y})", f"{x},{y}")
            if record:
                w, h = self._effective_screen_size()[:2]
                activity.record_step(
                    "click", f"点击 ({x},{y})", f"{x},{y}",
                    {
                        "x": int(x), "y": int(y),
                        "ratio": [round(float(x) / w, 4), round(float(y) / h, 4)],
                    },
                )
        except Exception as e:
            self.log(f"点击失败：{e}")

    # ---------- 行为追踪 / 行为录制 ----------
    def list_actions(self, limit=200):
        from mobileauto import activity

        return activity.list_actions(limit)

    def clear_actions(self):
        from mobileauto import activity

        ok = activity.clear_actions()
        self.log("行为记录已清空")
        return ok

    def start_recording(self):
        from mobileauto import activity

        activity.set_recording(True)
        self.log("行为录制已开启：之后的点击、采集、返回会记录成任务步骤")
        return True

    def stop_recording(self):
        from mobileauto import activity

        activity.set_recording(False)
        self.log("行为录制已停止")
        return False

    def get_recording_state(self):
        from mobileauto import activity

        return {
            "recording": activity.is_recording(),
            "steps": activity.list_recorded(200),
        }

    def clear_recorded(self):
        """清空录制草稿与录制日志，保留任务执行日志。"""
        from mobileauto import activity

        kept = [item for item in activity.list_actions(500) if not item.get("recorded")]
        activity.clear_actions()
        activity.clear_draft()
        for item in kept:
            activity.log_action(item.get("kind", ""), item.get("detail", ""), item.get("target", ""))
        self.log("录制步骤已清空")
        return True

    def add_manual_click(self, x, y, label=""):
        """手动加入一个点击步骤（含选点/框选定位），无需先开启录制。"""
        from mobileauto import activity

        w, h = self._effective_screen_size()[:2]
        rx, ry = round(float(x) / w, 4), round(float(y) / h, 4)
        name = label or f"点击 ({int(x)},{int(y)})"
        step = activity.append_manual_step({
            "kind": "click",
            "detail": name,
            "target": f"{int(x)},{int(y)}",
            "action": "click_ratio",
            "label": name,
            "ratio": [rx, ry],
            "x": int(x),
            "y": int(y),
        })
        self.log(f"已加入手动点击步骤：{name}")
        return step

    def save_recording_draft(self, steps):
        """整体保存前端编辑后的录制步骤。"""
        from mobileauto import activity

        ok = activity.save_draft(steps)
        self.log(f"录制步骤已保存（{len(steps or [])} 步）")
        return ok

    def delete_recorded_step(self, index):
        from mobileauto import activity

        ok = activity.delete_recorded(index)
        if ok:
            self.log(f"已删除录制步骤 #{int(index) + 1}")
        return ok

    def move_recorded_step(self, index, offset):
        from mobileauto import activity

        ok = activity.move_recorded(index, offset)
        if ok:
            self.log(f"已调整录制步骤 #{int(index) + 1}")
        return ok

    def analyze_recording(self):
        """分析录制草稿：统计动作分布与可折叠的循环片段。"""
        from mobileauto import activity

        steps = activity.list_recorded(200)
        loops = activity.detect_loops(steps)
        actions = {}
        for step in steps:
            key = step.get("action") or step.get("kind") or "wait"
            actions[key] = actions.get(key, 0) + 1
        return {
            "steps": len(steps),
            "actions": actions,
            "loops": loops,
            "loop_hint": "检测到重复动作，可点「折叠循环」生成循环步骤" if loops else "",
        }

    def fold_recording_loops(self):
        """把录制中重复的动作序列折叠为循环步骤。"""
        from mobileauto import activity

        result = activity.fold_recording_loops()
        self.log(f"录制循环折叠：{'已完成' if result['changed'] else '无重复序列'}")
        return result

    def recording_to_task(self, name="", app=""):
        """把录制的步骤转换成配置驱动任务草稿，保留编辑后的动作与坐标。"""
        from mobileauto import activity, tasks

        steps = activity.list_recorded(200)
        if not steps:
            self.log("没有录制步骤，无法生成任务")
            return {"ok": False, "errors": ["没有录制步骤"]}

        states = []
        last_texts = []  # 采集步骤的文字作为后续点击的触发条件，保证按页面顺序执行
        max_loops = 1
        for i, step in enumerate(steps):
            if step.get("action") == "loop":
                body = ((step.get("loop") or {}).get("body") or [])
                repeat = int((step.get("loop") or {}).get("repeat") or 2)
                max_loops = max(max_loops, repeat)
                for j, sub in enumerate(body):
                    sub_action = sub.get("action") or "wait"
                    ratio = sub.get("ratio") or []
                    states.append({
                        "name": f"STEP_{i + 1}_{j + 1}",
                        "match": list(sub.get("texts") or [])[:3] or [sub.get("detail") or f"循环步骤{j + 1}"],
                        "action": sub_action,
                        "ratio": ratio,
                        "label": sub.get("detail") or f"循环步骤{j + 1}",
                    })
                continue
            action = step.get("action") or "wait"
            raw_name = step.get("label") or step.get("detail") or f"步骤{i + 1}"
            state_name = f"STEP_{i + 1}"
            texts = [t for t in (step.get("texts") or []) if t]
            if texts:
                last_texts = texts

            if action == "click_ratio":
                ratio = step.get("ratio")
                if not ratio:
                    x, y = step.get("x"), step.get("y")
                    if x is None or y is None:
                        continue
                    ratio = [round(float(x) / 1440, 4), round(float(y) / 3200, 4)]
                states.append({
                    "name": state_name,
                    "match": last_texts[:3] or [raw_name],
                    "action": "click_ratio",
                    "ratio": [round(float(ratio[0]), 4), round(float(ratio[1]), 4)],
                    "label": raw_name,
                })
            elif action == "click_match":
                texts = [t for t in (step.get("texts") or []) if t]
                match = texts[:3] or [raw_name]
                states.append({"name": state_name, "match": match, "action": "click_match", "label": raw_name})
            elif action == "back":
                states.append({"name": state_name, "match": [raw_name or "返回键"], "action": "back", "label": raw_name})
            elif action == "scroll":
                states.append({
                    "name": state_name,
                    "match": [raw_name or "滚动"],
                    "action": "scroll",
                    "direction": step.get("direction") or "down",
                    "label": raw_name,
                })
            elif action == "wait":
                texts = [t for t in (step.get("texts") or []) if t]
                states.append({
                    "name": state_name,
                    "match": texts[:3] or [raw_name],
                    "action": "wait",
                    "label": raw_name,
                })
            elif action in ("finish", "mute_once", "close_ad"):
                cfg = {"name": state_name, "match": [raw_name], "action": action, "label": raw_name}
                if step.get("ratio"):
                    cfg["ratio"] = step.get("ratio")
                if action == "finish":
                    cfg["match"] = [raw_name] + list(step.get("texts") or [])
                states.append(cfg)

        if not states:
            self.log("录制步骤无法转换为任务状态")
            return {"ok": False, "errors": ["录制步骤无法转换"]}

        task = {
            "key": "",
            "name": name or "录制生成的任务",
            "description": f"由行为录制生成，共 {len(states)} 个步骤，请按需修改状态与动作",
            "app": app or "com.luna.music",
            "rule": "",
            "engine": "configured",
            "enabled": False,
            "max_loops": max_loops,
            "capabilities": dict(tasks.DEFAULT_TASK.get("capabilities") or {}),
            "states": states,
            "stuck_states": [],
            "finish_info_keywords": [],
        }
        try:
            tasks.save_task(task)
            self.log(f"已生成任务草稿：{task['name']}（{len(states)} 步）")
            return {"ok": True, "task": task}
        except Exception as e:
            self.log(f"生成任务失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def press_back(self):
        if self.conn is None:
            return False
        self.log("发送返回键")
        self._spawn(self._back_worker)
        return True

    def _back_worker(self):
        try:
            from mobileauto import inspector

            inspector.press_back(self.conn)
            from mobileauto import activity

            activity.log_action("back", "返回键")
            activity.record_step("back", "返回键", "back")
        except Exception as e:
            self.log(f"返回失败：{e}")

    def scroll_page(self, direction="down", record=True):
        """按屏幕比例上下滚动：向上=从下往上划，向下=从上往下划。"""
        if self.conn is None:
            return False
        w, h = self._effective_screen_size()[:2]
        direction = "up" if str(direction).lower() == "up" else "down"
        cx = int(w / 2)
        if direction == "up":
            y1, y2 = int(h * 0.75), int(h * 0.3)
        else:
            y1, y2 = int(h * 0.3), int(h * 0.75)
        self.log(f"页面滚动：{direction}")
        self._spawn(self._scroll_worker, cx, y1, cx, y2, bool(record), direction)
        return True

    def _scroll_worker(self, x1, y1, x2, y2, record=True, direction="down"):
        try:
            from mobileauto import inspector, activity, stream

            # scrcpy 滑动自带插帧，比 input swipe 更顺滑
            if not stream.swipe(x1, y1, x2, y2):
                inspector.swipe(self.conn, x1, y1, x2, y2)
            activity.log_action("scroll", f"页面滚动 {direction}")
            if record:
                activity.record_step(
                    "scroll", f"滚动 {direction}", direction,
                    {"action": "scroll", "direction": direction},
                )
        except Exception as e:
            self.log(f"滚动失败：{e}")

    def press_home(self):
        if self.conn is None:
            return False
        self.log("发送主页键")
        self._spawn(self._home_worker)
        return True

    def _home_worker(self):
        try:
            from mobileauto import inspector

            inspector.press_home(self.conn)
            from mobileauto import activity

            activity.log_action("home", "主页键")
            activity.record_step("home", "主页键", "home")
        except Exception as e:
            self.log(f"主页键失败：{e}")

    def press_recents(self):
        if self.conn is None:
            return False
        self.log("发送最近任务键")
        self._spawn(self._recents_worker)
        return True

    def _recents_worker(self):
        try:
            from mobileauto import inspector

            inspector.press_recents(self.conn)
            from mobileauto import activity

            activity.log_action("recents", "最近任务键")
            activity.record_step("recents", "最近任务键", "recents")
        except Exception as e:
            self.log(f"最近任务键失败：{e}")

    # ---------- 任务进程 ----------
    def start_task(self, key=None):
        """按指定任务启动；未指定时用当前选中的任务。"""
        from mobileauto import tasks

        with self._lock:
            task_key = key or self.task_key
        if not task_key:
            enabled = [t for t in tasks.list_tasks() if t["enabled"]]
            task_key = enabled[0]["key"] if enabled else (tasks.list_tasks() or [{}])[0].get("key")
        if not task_key:
            self.log("没有可执行的任务")
            return False
        try:
            task = tasks.load_task(task_key)
        except Exception as e:
            self.log(f"读取任务失败：{e}")
            return False
        with self._lock:
            self.task_key = task_key
            self.task_name = task.get("name") or task_key
        env_extra = {
            "MOBILEAUTO_TASK_KEY": task_key,
            "MOBILEAUTO_PACKAGE": task.get("app") or "com.luna.music",
            "MOBILEAUTO_MAX_LOOPS": str(task.get("max_loops") or 3),
        }
        return self._start_process(
            [sys.executable, "-m", TASK_MODULE],
            self.task_name,
            env_extra=env_extra,
        )

    def capture_page(self):
        """单帧采集并返回结果，供前端直接展示截图与 OCR 文字。"""
        from mobileauto import capture

        with self._lock:
            conn = self.conn
        if conn is None:
            return {"ok": False, "error": "设备未连接"}
        self.log("开始单帧采集（截图 + OCR + 控件树）")
        result = capture.capture_once(conn)
        if result.get("ok"):
            self.log(f"采集完成：识别 {result.get('ocr_count')} 段文字，{result.get('clickable_count')} 个按钮")
            self.log(f"采集目录：{result.get('dir')}")
            from mobileauto import activity

            texts = [it.get("text", "") for it in (result.get("ocr") or []) if it.get("text")]
            activity.record_step(
                "capture",
                f"采集页面：{' / '.join(texts[:6])}",
                "capture",
                {"texts": texts[:20]},
            )
        else:
            self.log(f"采集失败：{result.get('error')}")
        return result

    def start_collect(self, options=None):
        """兼容旧调用：同步采集当前页面。"""
        return self.capture_page()

    def _start_process(self, cmd, label, env_extra=None):
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                self.log("已有任务运行中")
                return False
            self.proc_label = label
        env = os.environ.copy()
        src_dir = os.path.join(APP_DIR, "src")
        env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
        for key, value in (env_extra or {}).items():
            env[key] = str(value)
        kwargs = dict(
            cwd=APP_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            self.log(f"启动任务失败：{e}")
            return False
        with self._lock:
            self.proc = proc
        self.log(f"已启动：{label}")
        self._spawn(self._reader_worker, proc, label)
        return True

    def _reader_worker(self, proc, label):
        try:
            for line in proc.stdout:
                self.log(f"{label} | {line.rstrip()}")
        except Exception as e:
            self.log(f"读取任务日志失败：{e}")
        code = proc.poll()
        with self._lock:
            if self.proc is proc:
                self.proc = None
                self.proc_label = ""
        self.log(f"任务结束：{label}，返回码 {code}")

    def stop_task(self):
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return False
        self.log("正在停止任务...")
        try:
            proc.terminate()
            return True
        except Exception as e:
            self.log(f"停止任务失败：{e}")
            return False

    # ---------- 记录文件 ----------
    def list_records(self):
        files = []
        for root, _, names in os.walk(RUNTIME_DIR):
            for name in names:
                if os.path.splitext(name)[1].lower() not in (".log", ".txt", ".xml", ".json", ".png"):
                    continue
                path = os.path.join(root, name)
                files.append(
                    {
                        "path": os.path.relpath(path, APP_DIR).replace("\\", "/"),
                        "name": name,
                        "size": os.path.getsize(path),
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
                    }
                )
        files.sort(key=lambda item: item["mtime"], reverse=True)
        return files[:800]

    def read_record(self, rel_path):
        try:
            path = os.path.abspath(os.path.join(APP_DIR, rel_path))
            runtime = os.path.abspath(RUNTIME_DIR)
            if not path.startswith(runtime):
                return "路径不在 runtime 目录内"
            if os.path.splitext(path)[1].lower() == ".png":
                return f"图片文件：{path}\n\n当前版本仅预览文本内容。"
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:300000]
        except Exception as e:
            return f"读取失败：{e}"

    # ---------- 实时画面流 ----------
    def get_stream_status(self):
        from mobileauto import stream

        return stream.get_stream_status()

    def set_stream_quality(self, clear=True):
        """关闭按钮识别时切到高清低延迟模式；开启识别时回到普通帧率。"""
        from mobileauto import stream

        state = stream.set_quality(clear)
        self.log("画面模式：高清低延迟" if state else "画面模式：按钮识别")
        return {"ok": True, "clear": state}

    # ---------- 任务列表 ----------
    def list_tasks(self):
        from mobileauto import tasks

        return tasks.list_tasks()

    def load_task(self, key):
        from mobileauto import tasks

        try:
            return tasks.load_task(key)
        except Exception as e:
            self.log(f"读取任务失败：{e}")
            return None

    def save_task(self, data):
        from mobileauto import tasks

        try:
            path = tasks.save_task(data)
            self.log(f"任务已保存：{os.path.basename(path)}")
            return {"ok": True, "path": os.path.basename(path)}
        except Exception as e:
            self.log(f"保存任务失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def delete_task(self, key):
        from mobileauto import tasks

        ok = tasks.delete_task(key)
        self.log(f"{'已删除任务' if ok else '删除任务失败'}：{key}")
        return ok

    # ---------- 可流通流程格式（FlowSpec） ----------
    def export_task_flow(self, key):
        """导出任务为 FlowSpec v1 标准 JSON，可跨工具流通/版本管理。"""
        from mobileauto import flow_schema, tasks

        try:
            spec = flow_schema.export_task(tasks.load_task(key))
            self.log(f"已导出流程格式：{key}")
            return {"ok": True, "spec": spec, "mermaid": flow_schema.to_mermaid(spec),
                    "graph": flow_schema.to_graph(spec)}
        except Exception as e:
            self.log(f"导出流程失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def save_flow_as(self, key, default_name=""):
        """弹出系统「另存为」窗口，把 FlowSpec v1 写到用户选择的位置。"""
        from mobileauto import flow_schema, tasks

        if not self._webview or not self._window:
            return {"ok": False, "errors": ["原生保存对话框不可用（请通过启动工作台.bat 运行）"]}
        try:
            spec = flow_schema.export_task(tasks.load_task(key))
            name = default_name or f"{key}.flow.json"
            dialog_type = getattr(
                getattr(self._webview, "FileDialog", None), "SAVE",
                getattr(self._webview, "SAVE_DIALOG", None),
            )
            result = self._window.create_file_dialog(
                dialog_type,
                directory=os.path.join(APP_DIR, "tasks"),
                save_filename=name,
                file_types=("JSON 文件 (*.json)", "所有文件 (*.*)"),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            path = result if isinstance(result, str) else result[0]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(spec, f, ensure_ascii=False, indent=2)
            self.log(f"流程已另存为：{path}")
            return {"ok": True, "path": path}
        except Exception as e:
            self.log(f"另存流程失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def import_task_flow(self, spec):
        """导入 FlowSpec v1 文件，转为工作台任务并保存（默认停用，避免误执行）。"""
        from mobileauto import flow_schema, tasks

        errors = flow_schema.validate(spec)
        if errors:
            return {"ok": False, "errors": errors}
        try:
            task = flow_schema.import_task(spec)
            path = tasks.save_task(task)
            self.log(f"已导入流程任务：{task['name']}（{os.path.basename(path)}）")
            return {"ok": True, "task": task, "mermaid": flow_schema.to_mermaid_task(task),
                    "graph": flow_schema.to_graph(flow_schema.export_task(task))}
        except Exception as e:
            self.log(f"导入流程失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def task_mermaid(self, key):
        """生成任务的 Mermaid 流程图源码，可直接贴到支持 Mermaid 的编辑器渲染。"""
        from mobileauto import flow_schema, tasks

        try:
            task = tasks.load_task(key)
            spec = flow_schema.export_task(task)
            return {
                "ok": True,
                "mermaid": flow_schema.to_mermaid_task(task),
                "graph": flow_schema.to_graph(spec),
                "analysis": flow_schema.analyze(task),
            }
        except Exception as e:
            self.log(f"生成流程图失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def set_task_enabled(self, key, enabled):
        from mobileauto import tasks

        try:
            tasks.set_enabled(key, enabled)
            self.log(f"任务「{key}」已{'启用' if enabled else '停用'}")
            return True
        except Exception as e:
            self.log(f"设置任务状态失败：{e}")
            return False

    def select_task(self, key):
        from mobileauto import tasks

        try:
            task = tasks.load_task(key)
            with self._lock:
                self.task_key = task["key"]
                self.task_name = task.get("name") or key
            self.log(f"已选择任务：{self.task_name}")
            return True
        except Exception as e:
            self.log(f"选择任务失败：{e}")
            return False

    def get_task_progress(self, limit=120):
        """返回最近一次任务执行的步骤过程，供设备工作台实时显示。"""
        from mobileauto import activity

        items = activity.list_actions(limit)
        with self._lock:
            session = self.task_key
        if session:
            same = [item for item in items if item.get("session") == session]
            if same:
                items = same
        return items[-limit:]

    # ---------- 阶段2：规则包管理 ----------
    def list_rules(self):
        from mobileauto import rules

        return rules.list_rules()

    def ensure_default_rule(self):
        from mobileauto import rules

        name = rules.ensure_default_rule()
        self.log(f"默认规则已就绪：{name}")
        return name

    def load_rule(self, name):
        from mobileauto import rules

        try:
            return rules.load_rule(name)
        except Exception as e:
            self.log(f"读取规则失败：{e}")
            return None

    def save_rule(self, name, data):
        from mobileauto import rules

        try:
            errors = rules.validate_rule(data)
            if errors:
                self.log("规则校验失败：" + "; ".join(errors))
                return {"ok": False, "errors": errors}
            path = rules.save_rule(name, data)
            self.log(f"规则已保存：{os.path.basename(path)}")
            return {"ok": True, "path": os.path.basename(path)}
        except Exception as e:
            self.log(f"保存规则失败：{e}")
            return {"ok": False, "errors": [str(e)]}

    def delete_rule(self, name):
        from mobileauto import rules

        ok = rules.delete_rule(name)
        self.log(f"{'已删除规则' if ok else '删除规则失败'}：{name}")
        return ok

    def push_rule(self, name, method="adb"):
        from mobileauto import rules
        from mobileauto.inspector import find_adb

        path = rules.rule_path(name)
        if not os.path.isfile(path):
            self.log(f"规则不存在，无法下发：{name}")
            return False
        if method != "adb":
            self.log(f"暂不支持的下发方式：{method}")
            return False
        adb = find_adb()
        if not adb:
            self.log("找不到 adb，无法下发规则")
            return False
        try:
            remote_dir = "/sdcard/mobileauto/rules"
            subprocess.run(
                [adb, "shell", "mkdir", "-p", remote_dir],
                capture_output=True,
                text=True,
                timeout=20,
            )
            dst = remote_dir + "/active.json"
            result = subprocess.run(
                [adb, "push", path, dst], capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                self.log(f"规则下发失败：{result.stderr.strip()}")
                return False
            self.log(f"规则已下发到手机：{name}")
            return True
        except Exception as e:
            self.log(f"规则下发失败：{e}")
            return False

    # ---------- 阶段2：回传分析 ----------
    def list_reports(self):
        from mobileauto import analytics

        return analytics.list_reports()

    def pull_reports(self):
        from mobileauto import analytics

        try:
            names = analytics.pull_reports()
            self.log(f"从手机拉回 {len(names)} 个执行报告")
            return names
        except Exception as e:
            self.log(f"拉取报告失败：{e}")
            return []

    def analyze_report(self, name):
        from mobileauto import analytics

        try:
            return analytics.analyze_report(name)
        except Exception as e:
            self.log(f"分析报告失败：{e}")
            return None
