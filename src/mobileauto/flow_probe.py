"""流程探测：逐步执行并记录页面状态转换，用于搞清楚真实业务流程。

只做两件事：
1. 每一步把 截图 + OCR 文字 + 当前包名 记到 runtime/flow_probe/
2. 按明确的状态做动作（首页点福利、福利页点领时长、奖励弹窗点领取、完成后返回）

每一步都留档，跑完看 state_log.jsonl 就能还原整个流程和特殊情况。
"""

import json
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

OUT_DIR = os.path.join(RUNTIME_DIR, "flow_probe")
os.makedirs(OUT_DIR, exist_ok=True)
LOG_FILE = os.path.join(OUT_DIR, "state_log.jsonl")

APP_PACKAGE = "com.luna.music"
POLL = 0.6
MAX_STEPS = int(os.environ.get("FLOW_PROBE_STEPS", "180"))

# 状态判定关键词（按优先级）
HOME_MARKERS = ["听歌总时长", "继续领", "免费听"]
WELFARE_MARKERS = ["领时长", "福利中心", "免费听时长", "再看1次", "已解锁"]
REWARD_MARKERS = ["领取奖励", "立即领取", "再看2个视频", "再看1个视频", "提前得", "坚持退出"]
COUNTDOWN_MARKERS = ["秒后可领取", "秒可领取", "后可领取奖励"]
SUCCESS_MARKERS = ["领取成功", "恭喜获得奖励", "优惠券已放入"]
AD_MARKERS = ["广告", "反馈", "立即下载", "进入直播", "立即使用"]

device = None
engine = None
last_state = ""
last_action = ""
step = 0


def log_event(event):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(
        f"[{event['ts']}] {event['state']:<12} | {event.get('action') or '-':<28} | {event.get('brief', '')}",
        flush=True,
    )


def ocr_frame(img):
    global engine
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
    import cv2

    h, w = img.shape[:2]
    scale = 1.0
    work = img
    if w > 960:
        scale = 960.0 / w
        work = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    result, _ = engine(work)
    items = []
    for row in result or []:
        if not row or len(row) < 3:
            continue
        box, text, score = row[0], row[1], float(row[2])
        pts = [(float(p[0]) / scale, float(p[1]) / scale) for p in box]
        items.append(
            {
                "text": str(text),
                "score": round(score, 3),
                "cx": int(sum(p[0] for p in pts) / len(pts)),
                "cy": int(sum(p[1] for p in pts) / len(pts)),
            }
        )
    return items


def classify(items):
    texts = [it["text"] for it in items]
    joined = " ".join(texts)

    def has(keys):
        return any(any(k in t for k in keys) for t in texts)

    # 奖励弹窗优先级最高（有绿色按钮或"再看"字样）
    if has(["领取奖励", "立即领取", "再看2个视频", "再看1个视频", "提前得", "坚持退出"]):
        return "REWARD_POPUP"
    if has(COUNTDOWN_MARKERS):
        return "COUNTDOWN"
    if has(WELFARE_MARKERS):
        return "WELFARE"
    if has(HOME_MARKERS) and not has(["广告"]):
        return "HOME"
    if has(SUCCESS_MARKERS):
        return "SUCCESS_BANNER"
    if has(AD_MARKERS):
        return "AD_CONTENT"
    return "UNKNOWN"


def find_click(items, keywords, exclude=None):
    for it in items:
        if any(k in it["text"] for k in keywords):
            if exclude and any(e in it["text"] for e in exclude):
                continue
            return it
    return None


def tap(item):
    device.click(int(item["cx"]), int(item["cy"]))


def brief(items):
    picked = [it for it in items if it["score"] >= 0.6 and any("\u4e00" <= c <= "\u9fff" for c in it["text"])]
    return " | ".join(f"{it['text']}@{it['cx']},{it['cy']}" for it in picked[:14])


def main():
    global device, last_state, last_action, step

    import uiautomator2 as u2
    import cv2

    print("连接设备...", flush=True)
    device = u2.connect()
    print("已连接设备", flush=True)

    print(f"启动 {APP_PACKAGE}...", flush=True)
    device.app_start(APP_PACKAGE)
    time.sleep(4)

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")

    for step in range(1, MAX_STEPS + 1):
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            img = device.screenshot(format="opencv")
            items = ocr_frame(img)
        except Exception as e:
            log_event({"ts": ts, "step": step, "state": "ERROR", "brief": str(e)})
            time.sleep(2)
            continue

        state = classify(items)
        package = ""
        try:
            package = device.app_current().get("package", "")
        except Exception:
            pass

        # 状态变化或每 10 步存一次截图
        save_shot = state != last_state or step % 10 == 0
        shot_file = ""
        if save_shot:
            shot_file = os.path.join(OUT_DIR, f"{step:03d}_{state}.png")
            cv2.imwrite(shot_file, img)

        action = ""
        if package != APP_PACKAGE:
            action = f"拉回App(当前{package})"
            device.app_start(APP_PACKAGE)
            time.sleep(2)
        elif state == "HOME":
            hit = find_click(items, ["福利"], exclude=["听抖音"])
            if hit:
                action = f"点福利@{hit['cx']},{hit['cy']}"
                tap(hit)
                time.sleep(1.2)
        elif state == "WELFARE":
            hit = find_click(items, ["领时长", "再看1次"])
            if hit:
                action = f"点领时长@{hit['cx']},{hit['cy']}"
                tap(hit)
                time.sleep(1.5)
        elif state == "REWARD_POPUP":
            hit = find_click(items, ["领取奖励", "立即领取"], exclude=["秒后"])
            if hit:
                action = f"点领取奖励@{hit['cx']},{hit['cy']}"
                tap(hit)
                time.sleep(1.5)
            else:
                hit = find_click(items, ["坚持退出"])
                if hit:
                    action = f"点坚持退出@{hit['cx']},{hit['cy']}"
                    tap(hit)
                    time.sleep(1.0)
        elif state == "SUCCESS_BANNER":
            hit = find_click(items, ["×", "x", "关闭"])
            if hit:
                action = f"点关闭@{hit['cx']},{hit['cy']}"
                tap(hit)
                time.sleep(1.0)
        elif state == "AD_CONTENT":
            # 广告播放中，只等待；记录一次状态变化即可
            pass

        event = {
            "ts": ts,
            "step": step,
            "state": state,
            "package": package,
            "action": action,
            "brief": brief(items),
            "ocr": items,
            "shot": os.path.basename(shot_file) if shot_file else "",
        }
        log_event(event)

        if state != last_state:
            print(f"  >>> 状态切换: {last_state or '(启动)'} -> {state}", flush=True)
            last_state = state

        time.sleep(POLL)

    print(f"探测完成，记录：{LOG_FILE}", flush=True)


if __name__ == "__main__":
    main()
