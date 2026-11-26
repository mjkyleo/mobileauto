"""规则驱动的 OCR 流程执行器。

App 页面为自绘渲染，UI 层级里文字基本为空，因此流程判定完全走 OCR：
抓一帧 -> 识别文字 -> 命中关键词就点击/等待/返回 -> 达到完成条件进入下一步。
每一步都有独立超时，不会像旧逻辑那样固定 sleep 后再盲点。
"""

import time

from mobileauto import capture, inspector

DEFAULT_INTERVAL = 0.25
DEFAULT_TIMEOUT = 5.0
CLICK_COOLDOWN = 0.5


def _log(text, emit=None):
    if emit:
        try:
            emit(text)
        except Exception:
            pass
    print(text, flush=True)


def _act(kind, detail, target="", emit=None):
    try:
        from mobileauto import activity

        activity.log_action(kind, detail, target)
    except Exception:
        pass
    _log(f"  [{kind}] {detail}", emit)


def find_item(items, keywords):
    """按关键词找第一个 OCR 命中项。"""
    for item in items:
        text = item.get("text", "") or ""
        for keyword in keywords:
            if keyword and keyword in text:
                return item
    return None


def has_text(items, keywords):
    return find_item(items, keywords) is not None


def click_ratio(conn, ratio, size):
    width, height = size
    if not width or not height or not ratio or len(ratio) != 2:
        return False
    x = int(width * float(ratio[0]))
    y = int(height * float(ratio[1]))
    try:
        inspector.click_at(conn, x, y)
        return True
    except Exception:
        return False


def run_step(conn, step, emit=None):
    """执行单步；返回 (是否命中完成条件, 实际等待秒数)。"""
    name = step.get("key") or step.get("name") or "步骤"
    find = step.get("find") or []
    stay = step.get("stay_text") or []
    click_text = step.get("click_text") or []
    click_ratio_value = step.get("click_ratio")
    action = step.get("action") or ""
    timeout = float(step.get("timeout_ms") or DEFAULT_TIMEOUT * 1000) / 1000
    interval = max(0.12, float(step.get("interval_ms") or DEFAULT_INTERVAL * 1000) / 1000)
    max_clicks = int(step.get("max_clicks") or 0)
    max_width = int(step.get("ocr_width") or 640)

    deadline = time.time() + timeout
    clicks = 0
    last_click = 0.0
    started = time.time()

    _log(f"· {name}（最长 {timeout:.1f}s）", emit)
    while time.time() < deadline:
        items, size = capture.grab_ocr(conn, max_width=max_width)

        if has_text(items, find):
            _log(f"  [完成] {name}", emit)
            _act("step", f"完成：{name}", name, emit)
            return True, time.time() - started

        if has_text(items, stay):
            time.sleep(interval)
            continue

        now = time.time()
        if now - last_click < CLICK_COOLDOWN or (max_clicks and clicks >= max_clicks):
            time.sleep(interval)
            continue

        hit = find_item(items, click_text) if click_text else None
        done = False
        detail = ""
        if hit:
            try:
                inspector.click_at(conn, int(hit["cx"]), int(hit["cy"]))
                done = True
            except Exception:
                done = False
            detail = f"点击「{hit.get('text')}」({int(hit['cx'])},{int(hit['cy'])})"
        elif click_ratio_value:
            done = click_ratio(conn, click_ratio_value, size)
            detail = f"点击比例 {click_ratio_value}"
        elif action == "back":
            try:
                inspector.press_back(conn)
                done = True
            except Exception:
                done = False
            detail = "返回键"

        if done:
            clicks += 1
            last_click = now
            _log(f"  · {detail}", emit)
            _act("click", detail, name, emit)
        time.sleep(interval)

    _log(f"  [超时] {name}（{timeout:.1f}s）", emit)
    _act("timeout", f"超时：{name}", name, emit)
    return False, time.time() - started


def run(conn, steps, emit=None, loop_from=0, max_loops=1):
    """按规则顺序执行步骤；loop_from 表示从第几步开始重复。"""
    if not steps:
        return False
    total_loops = max(1, int(max_loops))
    for loop in range(total_loops):
        if total_loops > 1:
            _log(f"===== 第 {loop + 1}/{total_loops} 轮 =====", emit)
        start = loop_from if loop else 0
        for step in steps[start:]:
            ok, _ = run_step(conn, step, emit)
            if not ok:
                return False
    return True
