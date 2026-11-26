"""任务管理：任务列表存成独立 JSON，支持新建、编辑、启停与选择执行。

任务与规则分离：任务描述「做什么」（App、规则、循环次数），规则描述「怎么做」（OCR 流程）。
"""

import json
import os
import re
import time
from datetime import datetime

from mobileauto.paths import APP_DIR, ensure_runtime_dirs

ensure_runtime_dirs()

TASKS_DIR = os.path.join(APP_DIR, "tasks")


def _default_flow():
    """任务的规则是任务特有的：把默认流程复制进任务，避免共享全局规则。"""
    try:
        from mobileauto.rules import DEFAULT_RULE

        return {"loop_from": DEFAULT_RULE["flow"].get("loop_from", 0),
                "max_loops": DEFAULT_RULE["flow"].get("max_loops", 3),
                "steps": DEFAULT_RULE["flow"]["steps"]}
    except Exception:
        return {"loop_from": 0, "max_loops": 3, "steps": []}


try:
    from mobileauto.qishui_task import QISHUI_TASK as DEFAULT_TASK
except Exception:
    DEFAULT_TASK = {
        "key": "qishui_time",
        "name": "汽水音乐-领时长",
        "description": "打开汽水音乐，跳过开屏广告，在福利页领时长，看广告后领取奖励并循环",
        "app": "com.luna.music",
        "flow": _default_flow(),
        "enabled": True,
        "max_loops": 3,
        "module": "mobileauto.app_task",
        "capabilities": {},
    }

# 配置驱动任务示例（新场景不用写代码，改 JSON 即可）
EXAMPLE_CONFIGURED_TASK = {
    "key": "daily_checkin",
    "name": "示例-每日打卡",
    "description": "配置驱动的状态机示例：打开App，找到打卡按钮，点击后结束（可复制修改）",
    "app": "com.example.app",
    "rule": "默认策略",
    "enabled": False,
    "max_loops": 1,
    "engine": "configured",
    "capabilities": {
        "ocr": True,
        "action_verify": True,
        "anti_stuck": False,
        "dedupe": True,
        "structured_log": True,
        "end_detect": True,
        "auto_start": True,
        "entitlement": False,
    },
    "states": [
        {"name": "DONE", "match": ["已完成", "明日再来"], "action": "finish"},
        {"name": "REWARD", "match": ["领取奖励", "立即领取"], "exclude": ["秒后"], "action": "click_match", "count": True},
        {"name": "COUNTDOWN", "match": ["秒后可领取"], "action": "wait"},
        {"name": "MAIN", "match": ["去领取", "打卡"], "action": "click_match"},
        {"name": "HOME", "match": ["首页", "我的"], "action": "wait"},
    ],
    "stuck_states": ["REWARD"],
    "finish_info_keywords": ["已完成", "明日再来"],
}


def _safe_key(key):
    key = str(key or "").strip()
    key = re.sub(r"[^\w\-]+", "_", key)
    return key[:60] or "task"


def task_path(key):
    return os.path.join(TASKS_DIR, _safe_key(key) + ".json")


def list_tasks():
    os.makedirs(TASKS_DIR, exist_ok=True)
    items = []
    for name in os.listdir(TASKS_DIR):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(TASKS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        items.append(
            {
                "key": data.get("key") or os.path.splitext(name)[0],
                "name": data.get("name") or os.path.splitext(name)[0],
                "description": data.get("description", ""),
                "app": data.get("app", ""),
                "rule": ("任务内嵌" if data.get("flow") else (data.get("rule") or "")),
                "enabled": bool(data.get("enabled", False)),
                "max_loops": int(data.get("max_loops") or 1),
                "engine": data.get("engine") or "",
                "capabilities": data.get("capabilities") or {},
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
            }
        )
    order = {DEFAULT_TASK["key"]: 0}
    items.sort(key=lambda item: (order.get(item["key"], 1), item["mtime"]), reverse=False)
    return items


def load_task(key):
    path = task_path(key)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"任务不存在：{key}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_task(data):
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("任务内容必须是 JSON 对象")
    key = _safe_key(data.get("key") or data.get("name"))
    if not key or not data.get("name"):
        raise ValueError("任务缺少 key 或 name")
    data["key"] = key
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = task_path(key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def delete_task(key):
    path = task_path(key)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def set_enabled(key, enabled):
    data = load_task(key)
    data["enabled"] = bool(enabled)
    save_task(data)
    return True


def ensure_default_task():
    """首次启动落完整汽水任务+示例；旧的简化任务自动升级到完整状态机 JSON。"""
    os.makedirs(TASKS_DIR, exist_ok=True)
    path = task_path(DEFAULT_TASK["key"])
    if not os.path.isfile(path):
        save_task(DEFAULT_TASK)
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 旧任务是 flow.steps 简化版，没有 engine/states → 升级为完整配置任务
                if data.get("engine") != "configured" and not data.get("states"):
                    upgraded = dict(DEFAULT_TASK)
                    upgraded["enabled"] = data.get("enabled", True)
                    upgraded["updated"] = data.get("updated", "")
                    save_task(upgraded)
                else:
                    changed = False
                    if not data.get("capabilities"):
                        data["capabilities"] = DEFAULT_TASK["capabilities"]
                        changed = True
                    if changed:
                        save_task(data)
        except Exception:
            pass
    example_path = task_path(EXAMPLE_CONFIGURED_TASK["key"])
    if not os.path.isfile(example_path):
        save_task(EXAMPLE_CONFIGURED_TASK)
    return DEFAULT_TASK["key"]
