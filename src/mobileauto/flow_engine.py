"""通用自动化流程引擎（从汽水音乐任务提炼的可复用核心）。

核心思想（踩坑后总结）：
  1. 先探测再写状态机：不要凭猜测写点击逻辑，先用 flow_probe 把每页 OCR 记录下来。
  2. OCR 驱动：自绘 App 的 UI 层级读不到文字，一切靠屏幕 OCR。
  3. 状态判定严格按优先级：前景弹窗 > 结束态 > 倒计时 > 页面；倒计时词永不当按钮。
  4. 动作必验证：点完检查页面是否真的变了，没变就重试/换策略，绝不盲点。
  5. 防卡死：同一状态连续多次无变化 → 返回键兜底；有明确结束态就停。
  6. 分层日志：页面切换 / 动作 / 验证结果 三级，只打关键触发词，不刷整屏 OCR。

用法：继承 FlowTask，实现 classify(items) 和 handle(state, items)，
      或直接传 state_rules / handlers 配置给 FlowEngine。
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")

from mobileauto.paths import RUNTIME_DIR, ensure_runtime_dirs

ensure_runtime_dirs()


# 可选能力清单（任务 JSON 的 capabilities 块可逐项覆盖）
# ocr: 屏幕 OCR 驱动；action_verify: 点击后验证页面变化；anti_stuck: 卡住返回键兜底；
# dedupe: 点击去重；structured_log: 分层日志；end_detect: 结束态检测；
# auto_start: 启动 App；entitlement: 结束信息提取
DEFAULT_CAPABILITIES = {
    "ocr": True,
    "action_verify": True,
    "anti_stuck": True,
    "dedupe": True,
    "structured_log": True,
    "end_detect": True,
    "auto_start": True,
    "entitlement": True,
}


def normalize_capabilities(value):
    """把任意输入归一化成完整能力字典；未知键忽略。"""
    result = dict(DEFAULT_CAPABILITIES)
    if isinstance(value, dict):
        for key in DEFAULT_CAPABILITIES:
            if key in value:
                result[key] = bool(value[key])
    elif isinstance(value, (list, tuple, set)):
        # 也允许写成 ["ocr", "action_verify"] 表示只启用这些
        result = {key: False for key in DEFAULT_CAPABILITIES}
        for key in value:
            if key in result:
                result[key] = True
    return result


class FlowEngine:
    """OCR 状态机执行引擎。业务只需提供状态判定与动作处理。"""

    def __init__(
        self,
        name="自动化任务",
        app_package="",
        log_file=None,
        poll=0.25,
        max_rounds=0,
        tap_gap=0.35,
        screen_width=1440,
        screen_height=3200,
        classify_fn=None,
        handle_fn=None,
        brief_fn=None,
        device=None,
        capabilities=None,
    ):
        self.name = name
        self.app_package = app_package
        self.log_file = log_file or os.path.join(RUNTIME_DIR, "flow_live.log")
        self.poll = poll
        self.max_rounds = max_rounds
        self.tap_gap = tap_gap
        self.sw = screen_width
        self.sh = screen_height
        self.classify_fn = classify_fn
        self.handle_fn = handle_fn
        self.brief_fn = brief_fn
        self.device = device

        # 可选能力：全部默认开启；传 False 即关闭对应能力
        self.capabilities = dict(DEFAULT_CAPABILITIES)
        if capabilities:
            self.capabilities.update(capabilities)

        self.engine = None
        self.state = "INIT"
        self.round_no = 0
        self.last_tap = 0.0
        self.stats = {"ok": 0, "fail": 0}
        self.stuck_states = set()
        self.stuck_limit = 12
        self.finished = False
        self.finish_info = []

    # ---------- 日志 ----------
    def _write(self, line):
        print(line, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def log(self, msg):
        self._write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def log_box(self, title, *lines):
        if not self.capabilities.get("structured_log", True):
            self._write(title)
            return
        self._write("")
        self._write("=" * 58)
        self._write(f"  {title}")
        for line in lines:
            self._write(f"  {line}")
        self._write("=" * 58)
        self._write("")

    def log_round(self, n):
        if not self.capabilities.get("structured_log", True):
            return
        self._write("")
        self._write(f"---------- 第 {n} 轮 ----------")

    def log_state(self, old, new, keywords):
        if not self.capabilities.get("structured_log", True):
            return
        kw = ", ".join(keywords[:3]) if keywords else ""
        self._write(
            f"[{datetime.now().strftime('%H:%M:%S')}] 页面: {old} -> {new}"
            + (f"   [{kw}]" if kw else "")
        )

    def log_action(self, label, x=None, y=None):
        pos = f" @({int(x)},{int(y)})" if x is not None else ""
        self.log(f"  -> {label}{pos}")

    def log_detail(self, msg):
        self.log(f"     {msg}")

    def log_result(self, ok, label, extra=""):
        mark = "[OK]" if ok else "[FAIL]"
        self.log(f"     {mark} {label}" + (f"  {extra}" if extra else ""))

    # ---------- 基础能力 ----------
    def ocr(self, img):
        """返回 [{text, score, cx, cy}]，坐标换算回原始屏幕。"""
        if self.engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self.engine = RapidOCR()
        import cv2

        h, w = img.shape[:2]
        scale = 1.0
        work = img
        if w > 960:
            scale = 960.0 / w
            work = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        result, _ = self.engine(work)
        items = []
        for row in result or []:
            if not row or len(row) < 3:
                continue
            box, text, score = row[0], row[1], float(row[2])
            pts = [(float(p[0]) / scale, float(p[1]) / scale) for p in box]
            items.append({
                "text": str(text),
                "score": round(float(score), 3),
                "cx": int(sum(p[0] for p in pts) / len(pts)),
                "cy": int(sum(p[1] for p in pts) / len(pts)),
            })
        return items

    @staticmethod
    def has(items, keys, exclude=None):
        for it in items:
            text = it.get("text", "") or ""
            if exclude and any(e in text for e in exclude):
                continue
            if any(k in text for k in keys):
                return it
        return None

    def tap(self, x, y, label=""):
        now = time.time()
        if self.capabilities.get("dedupe", True) and now - self.last_tap < self.tap_gap:
            return False
        self.device.click(int(x), int(y))
        self.last_tap = now
        self.log_action(label, x, y)
        return True

    def tap_item(self, item, label=""):
        return self.tap(item["cx"], item["cy"], label or item.get("text", ""))

    def tap_ratio(self, rx, ry, label):
        return self.tap(self.sw * rx, self.sh * ry, label)

    def press_back(self, label="返回键"):
        self.device.press("back")
        self.log_action(label)

    def verify_action(self, before_state, label, max_wait=2.5):
        """点击后验证页面是否变化；关闭该能力时直接视为成功。"""
        if not self.capabilities.get("action_verify", True):
            return True
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(self.poll)
            img = self.device.screenshot(format="opencv")
            if img is None:
                continue
            after = self.classify(self.ocr(img))
            if after != before_state:
                self.stats["ok"] += 1
                self.log_result(True, label, f"{before_state} -> {after}")
                return True
        self.stats["fail"] += 1
        self.log_result(False, label, f"页面未变化（{before_state}）")
        return False

    def finish(self, title, *lines):
        """标记任务结束；主循环检测到后退出。受 end_detect 能力开关控制。"""
        if not self.capabilities.get("end_detect", True):
            return
        self.finished = True
        self.finish_info = [title] + list(lines)

    def extract_info(self, items, keywords):
        """提取结束态文案（如畅听权益截止）；受 entitlement 开关控制。"""
        if not self.capabilities.get("entitlement", True):
            return ""
        hit = self.has(items, keywords)
        return hit["text"] if hit else ""

    # ---------- 状态机钩子（业务可覆盖） ----------
    def classify(self, items):
        if self.classify_fn:
            return self.classify_fn(items)
        raise NotImplementedError("请提供 classify_fn 或覆盖 classify()")

    def handle(self, state_now, items):
        if self.handle_fn:
            return self.handle_fn(self, state_now, items)
        raise NotImplementedError("请提供 handle_fn 或覆盖 handle()")

    def brief(self, items):
        if self.brief_fn:
            return self.brief_fn(items)
        return []

    # ---------- 主循环 ----------
    def run(self):
        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write("")
        except Exception:
            pass

        mode = "无限循环" if self.max_rounds == 0 else f"最多 {self.max_rounds} 轮"
        self.log_box(
            f"{self.name} - 实时过程",
            f"模式：{mode}",
            f"App：{self.app_package or '未指定'}   Ctrl+C 停止",
        )

        if self.device is None:
            import uiautomator2 as u2

            self.log("连接设备...")
            self.device = u2.connect()
        if self.app_package and self.capabilities.get("auto_start", True):
            self.log(f"启动 {self.app_package}...")
            try:
                self.device.app_start(self.app_package)
            except Exception as e:
                self.log(f"启动 App 失败（可能已在前台）：{e}")
            time.sleep(3.5)

        idle = 0
        while not self.finished and (self.max_rounds == 0 or self.round_no < self.max_rounds):
            try:
                screen = self.device.screenshot(format="opencv")
                if screen is None:
                    time.sleep(0.3)
                    continue
                items = self.ocr(screen)
                new_state = self.classify(items)
                if new_state != self.state:
                    self.log_state(self.state, new_state, self.brief(items))
                    self.state = new_state
                    idle = 0
                else:
                    idle += 1

                self.handle(self.state, items)

                if self.finished:
                    break

                # 防卡死：指定状态连续无变化 -> 返回键兜底（可选）
                if (
                    self.capabilities.get("anti_stuck", True)
                    and self.state in self.stuck_states
                    and idle > 0
                    and idle % self.stuck_limit == 0
                ):
                    self.log_detail(f"{self.state} 多次未退出，返回键兜底")
                    self.press_back()

                time.sleep(self.poll)
            except KeyboardInterrupt:
                self.log("已停止")
                break
            except Exception as e:
                self.log(f"错误: {e}")
                time.sleep(1)

        if self.finish_info:
            self.log_box(*self.finish_info)
        else:
            self.log_box(
                "任务已停止",
                f"共 {self.round_no} 轮 | 动作验证：成功 {self.stats['ok']}，失败 {self.stats['fail']}",
            )
        return self.stats
