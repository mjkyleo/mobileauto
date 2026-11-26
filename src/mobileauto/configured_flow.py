"""配置驱动的状态机任务：让新任务只需写 JSON，不用写 Python 代码。

任务 JSON 示例（tasks/xxx.json）：
{
  "key": "checkin",
  "name": "每日打卡",
  "app": "com.example.app",
  "rule": "默认策略",
  "enabled": true,
  "max_loops": 1,
  "engine": "configured",            # 用本模块执行；不填则走旧 flow.run
  "capabilities": {                  # 可选能力，逐项开关（不填=全开）
    "ocr": true, "action_verify": true, "anti_stuck": true,
    "dedupe": true, "structured_log": true, "end_detect": true,
    "auto_start": true, "entitlement": true
  },
  "states": [                        # 状态判定（按优先级，第一个命中胜出）
    {"name": "DONE", "match": ["已完成", "明日再来"], "action": "finish"},
    {"name": "REWARD", "match": ["领取奖励", "立即领取"], "exclude": ["秒后"], "action": "click_match", "count": true},
    {"name": "COUNTDOWN", "match": ["秒后可领取"], "action": "wait"},
    {"name": "MAIN", "match": ["去领取"], "action": "click_match"},
    {"name": "HOME", "match": ["首页", "我的"], "action": "wait"}
  ],
  "stuck_states": ["REWARD", "COUNTDOWN"],
  "finish_info_keywords": ["明日再来", "已完成"]
}

动作类型（action）：
  click_match  点命中的文字（OCR 坐标）
  click_ratio  点固定比例坐标（配 ratio: [rx, ry]）
  back         返回键
  wait         只等待不点击
  finish       标记结束（提取 finish_info_keywords）
  mute_once    每个循环静音一次（配 ratio，默认右上角喇叭）
  close_ad     关广告（优先 OCR ×，否则 ratio）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mobileauto.flow_engine import FlowEngine, normalize_capabilities


def make_classify(states):
    """把 JSON 状态规则编译成 classify 函数。

    判定字段（按优先级顺序逐条判断，第一条命中即返回）：
      match      任一关键词命中（OR）
      match_any  多个条件任一满足；条件可以是字符串或字符串数组（数组=任一命中）
      match_all  多个条件全部满足；条件同样支持数组（数组=任一命中）
      exclude    命中文本里带这些词时忽略

    这样可以把 qishui_runner 里的 has(a) or (has(b) and has(c)) 组合逻辑写进 JSON。
    """
    rules = []
    fallback = "UNKNOWN"
    for item in states or []:
        name = item.get("name") or item.get("state")
        rule = {
            "name": name,
            "match": item.get("match") or [],
            "match_any": item.get("match_any") or [],
            "match_all": item.get("match_all") or [],
            "exclude": item.get("exclude") or [],
        }
        if item.get("fallback"):
            fallback = name or fallback
        if name and (rule["match"] or rule["match_any"] or rule["match_all"]):
            rules.append(rule)

    def _hit(texts, cond, exclude):
        if isinstance(cond, dict):
            if "all" in cond:
                return all(_hit(texts, c, exclude) for c in cond["all"])
            if "any" in cond:
                return any(_hit(texts, c, exclude) for c in cond["any"])
            if "match" in cond:
                local_exclude = cond.get("exclude") or exclude
                return _hit(texts, cond["match"], local_exclude)
            return False
        keys = cond if isinstance(cond, (list, tuple)) else [cond]
        return any(
            (not exclude or not any(e in t for e in exclude))
            and any(k in t for k in keys)
            for t in texts
        )

    def classify(items):
        texts = [it.get("text", "") or "" for it in items]
        for rule in rules:
            exclude = rule["exclude"]
            if rule["match"] and not _hit(texts, rule["match"], exclude):
                continue
            if rule["match_any"] and not any(_hit(texts, c, exclude) for c in rule["match_any"]):
                continue
            if rule["match_all"] and not all(_hit(texts, c, exclude) for c in rule["match_all"]):
                continue
            return rule["name"]
        return fallback

    return classify


def _collect_keys(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_keys(item, out)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_keys(item, out)


def make_brief(states):
    keys = []
    for item in states or []:
        _collect_keys(item.get("match"), keys)
        _collect_keys(item.get("match_any"), keys)
        _collect_keys(item.get("match_all"), keys)
    keys = list(dict.fromkeys(keys))[:12]

    def brief(items):
        for it in items:
            for key in keys:
                if key in it["text"]:
                    return [key]
        return []

    return brief


def make_handle(states, finish_keywords=None):
    """把 JSON 动作规则编译成 handle 函数。"""
    handlers = {}
    for item in states or []:
        name = item.get("name") or item.get("state")
        if name:
            handlers[name] = item
    finish_keywords = finish_keywords or []

    def handle(engine, state_now, items):
        cfg = handlers.get(state_now, {})
        action = cfg.get("action") or "wait"
        match = cfg.get("click_target") or cfg.get("match") or []
        exclude = cfg.get("click_exclude") or cfg.get("exclude")

        if action == "finish":
            title = cfg.get("finish_title") or "任务完成"
            engine.finish(
                title,
                engine.extract_info(items, finish_keywords) or cfg.get("finish_fallback") or "已达到结束条件",
            )
            return

        if action == "wait":
            return

        if action == "back":
            engine.press_back()
            engine.verify_action(state_now, "返回键")
            return

        if action == "scroll":
            direction = cfg.get("direction") or "down"
            engine.device.scroll_page(direction, record=False)
            engine.log_action(f"页面滚动 {direction}")
            return

        if action == "click_ratio":
            ratio = cfg.get("ratio") or []
            if len(ratio) == 2 and engine.tap_ratio(ratio[0], ratio[1], cfg.get("label") or state_now):
                engine.verify_action(state_now, cfg.get("label") or state_now)
            return

        if action == "close_ad":
            hit = FlowEngine.has(items, ["×", "领取成功×", "成功×"])
            if hit and engine.tap_item(hit, "关广告×"):
                engine.verify_action(state_now, "关广告×")
            elif cfg.get("ratio") and engine.tap_ratio(*cfg["ratio"], "关广告×"):
                engine.verify_action(state_now, "关广告×")
            elif cfg.get("back_fallback", True):
                engine.press_back("关广告返回键")
            return

        if action == "mute_once":
            # 用状态名做一次性标记，避免每帧重复点击；reset_on 可让下一轮重新静音
            flag = f"_muted_{state_now}"
            if getattr(engine, flag, False):
                return
            setattr(engine, flag, True)
            ratio = cfg.get("ratio") or [0.23, 0.027]
            engine.tap_ratio(ratio[0], ratio[1], "静音")
            return

        if action == "click_ratio_once":
            flag = f"_once_{state_now}"
            if getattr(engine, flag, False):
                return
            ratio = cfg.get("ratio") or []
            if len(ratio) == 2 and engine.tap_ratio(ratio[0], ratio[1], cfg.get("label") or state_now):
                setattr(engine, flag, True)
                engine.verify_action(state_now, cfg.get("label") or state_now)
            return

        if action == "click_match":
            hit = FlowEngine.has(items, match, exclude)
            if hit and engine.tap_item(hit, cfg.get("label") or state_now):
                if cfg.get("count"):
                    engine.round_no += 1
                    engine.log_round(engine.round_no)
                    for flag in cfg.get("reset_flags") or []:
                        setattr(engine, flag, False)
                ok = engine.verify_action(state_now, cfg.get("label") or state_now)
                if not ok and cfg.get("retry", True):
                    engine.log_detail("点击无效，重试一次")
                    engine.tap_item(hit, (cfg.get("label") or state_now) + "重试")
                    engine.verify_action(state_now, "重试")
            return

    return handle


def run_configured(task, device=None, log_file=None):
    """按任务 JSON 配置运行一个状态机任务，返回引擎统计。"""
    caps = normalize_capabilities(task.get("capabilities"))
    states = task.get("states") or []
    engine = FlowEngine(
        name=task.get("name") or "配置任务",
        app_package=task.get("app") or "",
        log_file=log_file,
        poll=float(task.get("poll") or 0.25),
        max_rounds=int(task.get("max_loops") or 0),
        tap_gap=float(task.get("tap_gap") or 0.35),
        capabilities=caps,
        classify_fn=make_classify(states),
        handle_fn=make_handle(states, task.get("finish_info_keywords")),
        brief_fn=make_brief(states),
        device=device,
    )
    engine.stuck_states = set(task.get("stuck_states") or [])
    engine.stuck_limit = int(task.get("stuck_limit") or 12)
    return engine.run()
