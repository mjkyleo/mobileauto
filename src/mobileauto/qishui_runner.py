"""汽水音乐领时长脚本（基于 flow_probe 实测状态机 + 4 项优化）。

完整流程：开屏 -> HOME -> 福利 -> WELFARE -> 领时长 -> 广告
    -> 倒计时(静音+秒点X) -> 奖励弹窗(快速点领取) -> 成功尾页关X -> 循环

优化点：
1. 广告开始点静音（顶部反馈左侧的喇叭图标）
2. 倒计时结束以最短时间关 ×（轮询 0.25s）
3. 领取奖励不 dump UI、不长时间等待，识别即点
4. 直播广告识别到就立刻点直播 ×，绝不进直播间卡住
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

APP_PACKAGE = os.environ.get("MOBILEAUTO_PACKAGE", "com.luna.music")
POLL = 0.25
MAX_ROUNDS = int(os.environ.get("QISHUI_MAX_ROUNDS", "0"))  # 0=无限循环；设 30 可限制一个月
DEBUG_DIR = os.path.join(RUNTIME_DIR, "debug_clicks")
os.makedirs(DEBUG_DIR, exist_ok=True)

# 实测坐标（1440×3200）
AD_CLOSE = (0.924, 0.027)       # 普通广告右上 × ≈ (1330,85)
LIVE_CLOSE = (0.938, 0.078)     # 直播页右上 × ≈ (1350,250)
MUTE_ICON = (0.23, 0.027)       # 顶部「广告」右侧喇叭图标 ≈ (330,85)

device = None
engine = None
state = "INIT"
round_no = 0
last_tap_time = 0.0
TAP_GAP = 0.35
muted_this_ad = False
live_handled = False
action_stats = {"ok": 0, "fail": 0}
done_reason = ""
entitlement_deadline = ""


LOG_FILE = os.path.join(RUNTIME_DIR, "qishui_live.log")
LINE = "=" * 58


def _write(line):
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log(msg):
    _write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def log_box(title, *lines):
    _write("")
    _write(LINE)
    _write(f"  {title}")
    for line in lines:
        _write(f"  {line}")
    _write(LINE)
    _write("")


def log_round(n):
    _write("")
    _write(f"---------- 第 {n} 轮 ----------")


def log_state(old, new, keywords):
    kw = ", ".join(keywords[:3]) if keywords else ""
    _write(f"[{datetime.now().strftime('%H:%M:%S')}] 页面: {old} -> {new}" + (f"   [{kw}]" if kw else ""))


def log_action(label, x=None, y=None):
    pos = f" @({int(x)},{int(y)})" if x is not None else ""
    log(f"  -> {label}{pos}")


def log_detail(msg):
    log(f"     {msg}")


def log_result(ok, label, extra=""):
    mark = "[OK]" if ok else "[FAIL]"
    log(f"     {mark} {label}" + (f"  {extra}" if extra else ""))


def verify_action(before_state, label, max_wait=2.5):
    """点击后验证页面是否变化；返回 True=成功，False=无变化/失败。"""
    global action_stats
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(POLL)
        img = device.screenshot(format="opencv")
        if img is None:
            continue
        items = ocr(img)
        after = classify(items)
        if after != before_state:
            action_stats["ok"] += 1
            log_result(True, label, f"{before_state} -> {after}")
            return True
    action_stats["fail"] += 1
    log_result(False, label, f"页面未变化（{before_state}）")
    return False


def ocr(img):
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
        items.append({
            "text": str(text),
            "score": round(float(score), 3),
            "cx": int(sum(p[0] for p in pts) / len(pts)),
            "cy": int(sum(p[1] for p in pts) / len(pts)),
        })
    return items


def has(items, keys, exclude=None):
    for it in items:
        t = it["text"]
        if exclude and any(e in t for e in exclude):
            continue
        if any(k in t for k in keys):
            return it
    return None


def classify(items):
    """按优先级判定状态；前景奖励弹窗永远优先于背景广告/直播。"""
    # 0. 结束检测（最高优先）：领完当月，"明日再来"灰色按钮 + "畅听权益截止"
    if has(items, ["明日再来", "畅听权益截止", "已领完"]):
        return "DONE"
    # 0.5 开屏广告：有"跳过 N"按钮（含倒计时秒数）
    if has(items, ["跳过"]) and has(items, ["开通SVIP", "汽水音乐", "点击查看更多"]):
        return "SPLASH"
    # 1. 续看弹窗（"再看XX秒得"+红色"继续观看"按钮，必须点继续观看）
    if has(items, ["继续观看", "再看20秒得", "再看15秒得", "再看10秒得"]):
        return "CONTINUE"
    # 2. 奖励弹窗（前景弹窗最优先：背景即使有"直播中"也不能误判）
    if has(items, ["领取奖励", "立即领取"], exclude=["秒后", "秒可"]) or has(
        items, ["再看1个视频", "再看2个视频", "提前得", "坚持退出"]
    ):
        return "REWARD"
    # 2. 倒计时
    if has(items, ["秒后可领取", "秒可领取", "后可领取奖励"]):
        return "COUNTDOWN"
    # 3. 真正直播间（必须有输入/关注等直播专属元素，避免"直播中"背景误判）
    if has(items, ["说点什么", "本场点赞", "更多直播"]) or (
        has(items, ["已关注"]) and has(items, ["直播"])
    ):
        return "LIVE"
    # 4. 直播落地页（背景是直播但未进直播间，有"点击进入直播间"）
    if has(items, ["点击进入直播间", "直播间观看时长计入"]) and not has(items, ["说点什么"]):
        return "LANDING"
    # 5. 领取成功标识
    if has(items, ["领取成功", "恭喜获得奖励", "优惠券已放入"]):
        return "SUCCESS"
    # 5. 福利页
    if has(items, ["领时长", "福利中心", "免费听时长", "已解锁"]):
        return "WELFARE"
    # 6. 首页
    if has(items, ["福利"], exclude=["福利中心"]) and has(items, ["我的", "音乐"]):
        return "HOME"
    # 7. 广告内容
    if has(items, ["广告", "反馈", "立即下载", "进入直播", "立即使用"]):
        return "AD"
    return "UNKNOWN"


def tap(x, y, label=""):
    global last_tap_time
    now = time.time()
    if now - last_tap_time < TAP_GAP:
        return False
    device.click(int(x), int(y))
    last_tap_time = now
    log_action(label, x, y)
    return True


def tap_item(item, label=""):
    return tap(item["cx"], item["cy"], label or item["text"])


def tap_ratio(rx, ry, label):
    return tap(1440 * rx, 3200 * ry, label)


def mute_ad():
    """广告开始时点静音：优先 OCR 找静音/喇叭文字，否则点实测位置。"""
    global muted_this_ad
    if muted_this_ad:
        return
    muted_this_ad = True
    log_detail("广告静音")
    tap_ratio(*MUTE_ICON, "静音")


def close_ad(items):
    """关广告：优先点 OCR 识别到的「× / 领取成功×」图标，否则固定位，最后返回键兜底。"""
    hit = has(items, ["×", "领取成功×", "成功×"])
    if hit:
        tap_item(hit, "关广告×(OCR)")
        return
    tap_ratio(*AD_CLOSE, "关广告×")


def close_live():
    """直播广告：立刻点直播页右上 ×（实测 1350,250），不走普通 ×。"""
    tap_ratio(*LIVE_CLOSE, "关直播×")


def brief(items):
    """只返回一个最关键触发词，避免整屏 OCR 刷屏。"""
    keys = ["明日再来", "畅听权益截止", "跳过", "继续观看", "领取奖励", "秒后可领取",
            "领取成功", "领时长", "点击进入直播间", "直播", "广告"]
    for it in items:
        for k in keys:
            if k in it["text"]:
                return [k]
    return []


def handle(state_now, items):
    global state, round_no, muted_this_ad, live_handled, done_reason, entitlement_deadline

    if state_now == "DONE":
        # 提取"畅听权益截止 12/24 24:00"
        deadline = has(items, ["畅听权益截止"])
        if deadline:
            entitlement_deadline = deadline["text"]
        done_reason = "明日再来/已领完，今日时长已刷满"
        return

    if state_now == "SPLASH":
        hit = has(items, ["跳过"])
        if hit:
            if tap_item(hit, "跳过开屏"):
                verify_action("SPLASH", "跳过开屏")
        return

    if state_now == "HOME":
        hit = has(items, ["福利"], exclude=["福利中心"])
        if hit:
            if tap_item(hit, "福利入口"):
                verify_action("HOME", "福利入口")
        return

    if state_now == "WELFARE":
        hit = has(items, ["领时长", "再看1次", "再看2次"])
        if hit:
            round_no += 1
            log_round(round_no)
            log_detail("点领时长，进入广告")
            ok = tap_item(hit, "领时长")
            if ok:
                if not verify_action("WELFARE", "领时长"):
                    log_detail("领时长点击无效（可能灰色），重试一次")
                    tap_item(hit, "领时长重试")
                    verify_action("WELFARE", "领时长重试")
            muted_this_ad = False
            live_handled = False
        return

    if state_now in ("AD", "UNKNOWN"):
        mute_ad()
        return

    if state_now == "COUNTDOWN":
        mute_ad()
        # 倒计时文字消失即秒点 ×；下一帧会进入 AD/SUCCESS/REWARD
        return

    if state_now == "CONTINUE":
        hit = has(items, ["继续观看"])
        if hit:
            tap_item(hit, "继续观看")
        return

    if state_now == "REWARD":
        hit = has(items, ["领取奖励", "立即领取"], exclude=["秒后", "秒可"])
        if hit:
            log_detail("领取奖励")
            ok = tap_item(hit, "领取奖励")  # 不 dump UI、不等待，识别即点
            if ok:
                verify_action("REWARD", "领取奖励")
        return

    if state_now == "SUCCESS":
        # "领取成功"提示条：带×用OCR点，否则返回键退出（比固定×快且可靠）
        if has(items, ["×", "领取成功×", "成功×"]):
            close_ad(items)
        else:
            log_detail("领取成功，返回键退出广告")
            device.press("back")
        return

    if state_now == "LANDING":
        close_ad(items)
        return

    if state_now == "LIVE":
        # 直播广告：马上关，绝不在直播间停留
        close_live()
        return


def main():
    global device, state

    import cv2
    import uiautomator2 as u2

    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
    log_box(
        "汽水音乐刷时长 - 实时过程",
        f"模式：{'无限循环（明日再来自动停）' if MAX_ROUNDS == 0 else f'最多 {MAX_ROUNDS} 轮'}",
        f"App：{APP_PACKAGE}   Ctrl+C 停止",
    )
    log("连接设备...")
    device = u2.connect()
    log(f"启动 {APP_PACKAGE}...")
    device.app_start(APP_PACKAGE)
    time.sleep(3.5)
    idle = 0

    while MAX_ROUNDS == 0 or round_no < MAX_ROUNDS:
        try:
            screen = device.screenshot(format="opencv")
            if screen is None:
                time.sleep(0.3)
                continue
            items = ocr(screen)
            new_state = classify(items)
            if new_state != state:
                log_state(state, new_state, brief(items))
                state = new_state
                idle = 0
            else:
                idle += 1

            # 检测到"明日再来"→ 今日已刷满，输出权益截止并结束
            if new_state == "DONE":
                handle("DONE", items)
                deadline = entitlement_deadline or (
                    (has(items, ["畅听权益截止"]) or {}).get("text", "")
                )
                log_box(
                    "今日时长已刷满，任务结束",
                    deadline,
                    f"共领取 {round_no} 次 | 动作验证：成功 {action_stats['ok']}，失败 {action_stats['fail']}",
                )
                return

            handle(state, items)

            # 关广告类状态连续尝试无效 -> 返回键兜底，绝不无限点 ×
            if state in ("SUCCESS", "LANDING", "LIVE") and idle > 0 and idle % 12 == 0:
                log_detail(f"{state} 多次未退出，返回键兜底")
                device.press("back")
                time.sleep(0.5)

            if idle == 40:
                log_detail(f"卡在 {state} 较久，继续等待...")
                idle = 0

            time.sleep(POLL)
        except KeyboardInterrupt:
            log("已停止")
            break
        except Exception as e:
            log(f"错误: {e}")
            time.sleep(1)

    log_box(
        "任务已停止",
        f"共领取 {round_no} 次 | 动作验证：成功 {action_stats['ok']}，失败 {action_stats['fail']}",
    )


if __name__ == "__main__":
    main()
