"""新任务模板：复制本文件，改 4 处即可创建一个新自动化任务。

创建步骤：
  1. 复制本文件为 my_task.py
  2. 改 CONFIG（名称、App 包名、日志名）
  3. 改 STATES：按页面写的状态判定规则（关键词 + 优先级）
  4. 改 handle()：每个状态要做什么动作
  5. 用 flow_probe.py 先探测目标 App 的每页 OCR，再写关键词

运行：python -m mobileauto.my_task
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mobileauto.flow_engine import FlowEngine

# ① 任务配置
CONFIG = {
    "name": "我的新任务",
    "app_package": "com.example.app",   # 改成目标 App 包名
    "log_file": None,                   # None = runtime/flow_live.log，也可指定
    "poll": 0.25,                       # 轮询间隔（秒），要快就调小
    "max_rounds": 0,                    # 0=无限；>0 到达轮数停止
}

# ② 状态判定规则（按优先级从高到低；三元组 = 状态名, 命中词, 排除词）
#    原则：结束态 > 前景弹窗 > 倒计时(排除) > 主页面 > 未知
STATES = [
    ("DONE", ["今日已完成", "明日再来", "已领完"], None),
    ("REWARD", ["领取奖励", "立即领取"], ["秒后", "秒可"]),
    ("COUNTDOWN", ["秒后可领取", "秒可领取", "后可领取奖励"], None),
    ("MAIN", ["首页", "任务中心", "去领取"], None),
    ("HOME", ["首页", "我的"], ["任务中心"]),
]

# ③ 结束态提取信息（可选）：结束时输出的关键文字
FINISH_KEYWORD = "明日再来"

# 可点按钮关键词（用于 brief 日志，只打命中的第一个）
BRIEF_KEYS = ["明日再来", "领取奖励", "秒后可领取", "去领取", "首页"]


def classify(items):
    """按 STATES 顺序判定优先级；第一个命中的状态胜出。"""
    for state, keys, exclude in STATES:
        if FlowEngine.has(items, keys, exclude):
            return state
    return "UNKNOWN"


def brief(items):
    for it in items:
        for key in BRIEF_KEYS:
            if key in it["text"]:
                return [key]
    return []


def handle(engine, state, items):
    """每个状态做什么。engine 提供 tap/tap_item/tap_ratio/verify_action/press_back。"""
    if state == "DONE":
        # 提取结束信息后标记结束
        hit = FlowEngine.has(items, [FINISH_KEYWORD])
        engine.finish("任务完成", hit["text"] if hit else "已达到结束条件")
        return

    if state == "MAIN":
        hit = FlowEngine.has(items, ["去领取"])
        if hit and engine.tap_item(hit, "去领取"):
            engine.verify_action("MAIN", "去领取")
        return

    if state == "REWARD":
        hit = FlowEngine.has(items, ["领取奖励", "立即领取"], ["秒后"])
        if hit and engine.tap_item(hit, "领取奖励"):
            engine.round_no += 1
            engine.log_round(engine.round_no)
            engine.verify_action("REWARD", "领取奖励")
        return

    if state == "COUNTDOWN":
        # 倒计时只等待，绝不点击；这里可加静音等一次性动作
        return

    # HOME / UNKNOWN：什么都不做，等页面变化
    # 若怀疑卡住，可在此加 engine.press_back()（注意加去重）


def main():
    engine = FlowEngine(
        name=CONFIG["name"],
        app_package=CONFIG["app_package"],
        log_file=CONFIG["log_file"],
        poll=CONFIG["poll"],
        max_rounds=CONFIG["max_rounds"],
        classify_fn=classify,
        handle_fn=handle,
        brief_fn=brief,
    )
    # 容易卡住、需要返回键兜底的状态
    engine.stuck_states = {"REWARD", "COUNTDOWN"}
    engine.stuck_limit = 12
    engine.run()


if __name__ == "__main__":
    main()
