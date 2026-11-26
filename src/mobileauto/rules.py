"""规则包：把 L0-L4 定位策略写成声明式 JSON，PC 端编辑后下发到手机执行。"""

import json
import os
import re
import time
from datetime import datetime

from mobileauto.paths import RULES_DIR, ensure_runtime_dirs

ensure_runtime_dirs()

# 与当前 runner 顶部配置一致，作为新规则的基线
DEFAULT_RULE = {
    "version": 1,
    "name": "默认策略",
    "app": "com.luna.music",
    "updated": "",
    "targets": [
        {
            "key": "领取奖励",
            "priority": 1,
            "match": {"type": "text", "values": ["领取奖励"], "fuzzy": True},
            "action": "click",
            "roi": {"x": [0.25, 0.75], "y": [0.35, 0.70]},
            "close": False,
        },
        {
            "key": "继续观看",
            "priority": 2,
            "match": {"type": "text", "values": ["继续观看"], "fuzzy": True},
            "action": "click",
            "roi": {"x": [0.25, 0.75], "y": [0.35, 0.70]},
            "close": False,
        },
        {
            "key": "继续互动",
            "priority": 3,
            "match": {"type": "text", "values": ["继续互动"], "fuzzy": True},
            "action": "click",
            "roi": {"x": [0.25, 0.75], "y": [0.35, 0.70]},
            "close": False,
        },
        {
            "key": "领取成功",
            "priority": 4,
            "match": {"type": "text", "values": ["领取成功"], "fuzzy": True},
            "action": "close",
            "roi": {"x": [0.0, 1.0], "y": [0.01, 0.97]},
            "close": True,
            "close_ratio": [0.915, 0.07],
        },
    ],
    "flow": {
        "loop_from": 2,
        "max_loops": 3,
        "steps": [
            {
                "key": "跳过开屏广告",
                "find": ["福利中心", "免费听时长", "领时长", "听歌总时长", "做任务赚金币"],
                "click_text": ["跳过广告", "跳过开屏", "跳过", "关闭广告"],
                "click_ratio": [0.88, 0.06],
                "timeout_ms": 8000,
                "interval_ms": 200,
                "max_clicks": 8,
                "ocr_width": 640,
            },
            {
                "key": "进入福利页",
                "find": ["福利中心", "领时长", "免费听时长", "做任务赚金币"],
                "click_text": ["福利"],
                "click_ratio": [0.70, 0.935],
                "timeout_ms": 4000,
                "interval_ms": 250,
                "max_clicks": 1,
                "ocr_width": 640,
            },
            {
                "key": "点击领时长",
                "find": ["秒后可领取奖励", "领取成功", "立即下载", "再看2个视频", "恭喜获得奖励"],
                "click_text": ["领时长", "再看1次提前解锁下一天"],
                "click_ratio": [0.84, 0.395],
                "timeout_ms": 5000,
                "interval_ms": 200,
                "max_clicks": 1,
                "ocr_width": 640,
            },
            {
                "key": "等待广告结束",
                "find": ["领取奖励", "再看2个视频", "坚持退出", "恭喜获得奖励", "优惠券已放入"],
                "stay_text": ["秒后可领取奖励", "领取成功"],
                "timeout_ms": 60000,
                "interval_ms": 400,
                "ocr_width": 640,
            },
            {
                "key": "点击领取奖励",
                "find": ["恭喜获得奖励", "优惠券已放入", "立即使用"],
                "click_text": ["领取奖励", "立即领取"],
                "click_ratio": [0.5, 0.554],
                "timeout_ms": 8000,
                "interval_ms": 250,
                "max_clicks": 3,
                "ocr_width": 640,
            },
            {
                "key": "关闭奖励弹窗",
                "find": ["领时长", "做任务赚金币", "听歌赚金币", "免费听时长"],
                "action": "back",
                "timeout_ms": 5000,
                "interval_ms": 300,
                "max_clicks": 2,
                "ocr_width": 640,
            },
        ],
    },
    "fallback": {
        "template": {"enabled": True, "threshold": 0.85, "dir": "templates"},
        "ocr": {"enabled": True, "score": 0.5, "max_width": 720},
        "ratio": {
            "enabled": True,
            "points": {
                "领取奖励": [0.5, 0.52],
                "继续观看": [0.5, 0.52],
                "继续互动": [0.5, 0.52],
            },
        },
    },
    "guards": {
        "countdown_wait": True,
        "home_stop": True,
        "dedupe_ms": 2500,
        "click_cooldown_ms": 2000,
        "miss_back_limit": 5,
    },
}


def _safe_name(name):
    """把规则名转成安全文件名。"""
    name = str(name or "").strip()
    name = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", name)
    return name[:80] or "rule"


def rule_path(name):
    return os.path.join(RULES_DIR, _safe_name(name) + ".json")


def list_rules():
    """列出所有规则包，按修改时间倒序。"""
    items = []
    for name in os.listdir(RULES_DIR):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(RULES_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            title = data.get("name") or os.path.splitext(name)[0]
        except Exception:
            title = os.path.splitext(name)[0]
        items.append(
            {
                "file": name,
                "name": title,
                "size": os.path.getsize(path),
                "mtime": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))
                ),
            }
        )
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


def has_default():
    return os.path.isfile(rule_path(DEFAULT_RULE["name"]))


def ensure_default_rule():
    """首次进入规则页时落一份默认规则；旧规则缺 flow 时自动补齐。"""
    path = rule_path(DEFAULT_RULE["name"])
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and not data.get("flow"):
                data["flow"] = DEFAULT_RULE["flow"]
                save_rule(DEFAULT_RULE["name"], data)
        except Exception:
            pass
        return DEFAULT_RULE["name"]
    save_rule(DEFAULT_RULE["name"], DEFAULT_RULE)
    return DEFAULT_RULE["name"]


def load_rule(name):
    path = rule_path(name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"规则不存在：{name}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_rule(name, data):
    """保存规则包。name 为空时用数据里的 name。"""
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("规则内容必须是 JSON 对象")
    title = str(name or data.get("name") or "rule").strip()
    data["name"] = title
    data["version"] = int(data.get("version") or 1)
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = rule_path(title)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def delete_rule(name):
    path = rule_path(name)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def validate_rule(data):
    """简单校验：结构完整、目标非空、坐标在 0~1。返回错误列表。"""
    errors = []
    if not isinstance(data, dict):
        return ["规则根节点必须是对象"]
    if not data.get("name"):
        errors.append("缺少规则名称")
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        errors.append("targets 不能为空")
        return errors
    for i, target in enumerate(targets):
        if not isinstance(target, dict) or not target.get("key"):
            errors.append(f"第 {i + 1} 个目标缺少 key")
            continue
        roi = target.get("roi")
        if isinstance(roi, dict):
            for axis in ("x", "y"):
                value = roi.get(axis)
                if isinstance(value, list) and len(value) == 2:
                    try:
                        if not (0 <= float(value[0]) <= 1 and 0 <= float(value[1]) <= 1):
                            errors.append(f"{target['key']} 的 {axis} 超出 0~1")
                    except (TypeError, ValueError):
                        errors.append(f"{target['key']} 的 {axis} 不是数字")
    for key, point in (data.get("fallback", {}).get("ratio", {}).get("points") or {}).items():
        if not (isinstance(point, list) and len(point) == 2):
            errors.append(f"{key} 的比例坐标格式错误")
    return errors
