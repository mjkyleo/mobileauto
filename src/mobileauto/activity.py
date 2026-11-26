"""行为录制：开启后记录每一步操作（点击/采集/返回），并可转换为任务草稿。

录制记录带 recorded=1 标记，与任务执行日志区分；「行为录制」页只显示录制步骤。
"""

import json
import os
import threading
import time
from collections import deque
from datetime import datetime

from mobileauto.paths import RUNTIME_DIR, ensure_runtime_dirs

ensure_runtime_dirs()

ACTIVITY_FILE = os.path.join(RUNTIME_DIR, "activity.jsonl")
DRAFT_FILE = os.path.join(RUNTIME_DIR, "recording_draft.json")
LOCK = threading.Lock()
RECENT = deque(maxlen=200)


SESSION_ID = ""
RECORDING = False


def is_recording():
    return RECORDING


def set_recording(enabled):
    global RECORDING
    RECORDING = bool(enabled)
    return RECORDING


def _read_draft():
    if not os.path.isfile(DRAFT_FILE):
        return []
    try:
        with open(DRAFT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_draft(steps):
    with LOCK:
        try:
            with open(DRAFT_FILE, "w", encoding="utf-8") as f:
                json.dump(list(steps or []), f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False


def load_draft():
    """读取当前录制草稿步骤（可编辑）。"""
    return _read_draft()


def save_draft(steps):
    """整体保存录制草稿步骤（编辑后由前端写回）。"""
    return _write_draft(steps)


def clear_draft():
    with LOCK:
        try:
            if os.path.isfile(DRAFT_FILE):
                os.remove(DRAFT_FILE)
        except Exception:
            pass
    return True


def record_step(kind, detail="", target="", extra=None):
    """录制一步操作；只有录制开启时才写。kind 如 click/capture/back。

    同时写入活动日志和录制草稿，草稿供前端编辑与转任务使用。
    """
    if not RECORDING:
        return None
    data = dict(extra or {})
    data["recorded"] = 1
    event = log_action(kind, detail, target, data)

    step = {
        "id": f"{int(time.time() * 1000)}_{len(_read_draft())}",
        "kind": kind,
        "detail": detail,
        "target": target,
        "action": data.get("action") or ("click_ratio" if kind == "click" else "back" if kind == "back" else "wait"),
    }
    for key in ("x", "y", "ratio", "rx", "ry", "texts", "label", "match"):
        if key in data:
            step[key] = data[key]
    draft = _read_draft()
    draft.append(step)
    _write_draft(draft)
    return event


def list_recorded(limit=200):
    """返回可编辑的录制草稿步骤；没有草稿时兼容旧的活动日志。"""
    draft = _read_draft()
    if draft:
        return draft[-limit:]
    return [item for item in list_actions(limit)
            if item.get("recorded") and item.get("kind") != "clear"]


def append_manual_step(step):
    """手动插入一步（如框选区域点击）。"""
    draft = _read_draft()
    item = dict(step or {})
    item.setdefault("id", f"{int(time.time() * 1000)}_manual")
    draft.append(item)
    _write_draft(draft)
    return item


def delete_recorded(index):
    """按位置删除一步，index 为草稿数组下标。"""
    draft = _read_draft()
    idx = int(index)
    if idx < 0 or idx >= len(draft):
        return False
    draft.pop(idx)
    return _write_draft(draft)


def move_recorded(index, offset):
    """上移/下移一步。offset=-1 上移，1 下移。"""
    draft = _read_draft()
    idx = int(index)
    other = idx + int(offset)
    if idx < 0 or idx >= len(draft) or other < 0 or other >= len(draft):
        return False
    draft[idx], draft[other] = draft[other], draft[idx]
    return _write_draft(draft)


def _signature(step):
    """步骤签名：用于识别重复动作序列。"""
    action = step.get("action") or step.get("kind") or "wait"
    ratio = step.get("ratio") or []
    ratio_key = ",".join(f"{float(v):.2f}" for v in ratio) if ratio else ""
    return f"{action}:{ratio_key}"


def detect_loops(steps=None, min_repeat=2):
    """检测连续重复的动作序列，返回可折叠为循环的片段。

    例如 A B A B A B -> {start:0, length:2, repeat:3}，录制转任务时可生成循环。
    """
    items = list(steps if steps is not None else _read_draft())
    sigs = [_signature(s) for s in items]
    n = len(sigs)
    found = []
    used = set()
    max_block = max(2, n // min_repeat)
    for length in range(1, max_block + 1):
        for start in range(0, n - length * min_repeat + 1):
            if any(i in used for i in range(start, start + length * min_repeat)):
                continue
            block = sigs[start:start + length]
            repeat = 1
            while start + length * (repeat + 1) <= n and \
                    sigs[start + length * repeat:start + length * (repeat + 1)] == block:
                repeat += 1
            if repeat >= min_repeat:
                for i in range(start, start + length * repeat):
                    used.add(i)
                found.append({
                    "start": start,
                    "length": length,
                    "repeat": repeat,
                    "steps": [items[start + i].get("detail") or items[start + i].get("label")
                              for i in range(length)],
                })
    return found


def fold_recording_loops(min_repeat=2):
    """把重复序列折叠成一个 loop 步骤，其余步骤保留，返回新草稿。"""
    draft = _read_draft()
    loops = detect_loops(draft, min_repeat)
    if not loops:
        return {"changed": False, "steps": draft, "loops": []}

    used = set()
    for lp in loops:
        for i in range(lp["start"], lp["start"] + lp["length"] * lp["repeat"]):
            used.add(i)

    folded = []
    for lp in loops:
        folded.append({
            "id": f"loop_{lp['start']}",
            "kind": "loop",
            "action": "loop",
            "detail": f"循环 {lp['repeat']} 次：{' → '.join(str(s) for s in lp['steps'])}",
            "label": f"循环 {lp['repeat']} 次",
            "loop": {
                "from": lp["start"],
                "length": lp["length"],
                "repeat": lp["repeat"],
                "body": draft[lp["start"]:lp["start"] + lp["length"]],
            },
        })
    result = []
    inserted = set()
    for i, step in enumerate(draft):
        if i in used:
            lp = next((l for l in loops if l["start"] == i), None)
            if lp and lp["start"] not in inserted:
                result.append(next(f for f in folded if f["id"] == f"loop_{lp['start']}"))
                inserted.add(lp["start"])
            continue
        result.append(step)
    _write_draft(result)
    return {"changed": True, "steps": result, "loops": loops}


def set_session(session_id=""):
    global SESSION_ID
    SESSION_ID = str(session_id or "")
    return SESSION_ID


def log_action(kind, detail="", target="", extra=None):
    """记录一条行为。kind 如 launch/splash/click/task/step。"""
    event = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "target": str(target or ""),
        "detail": str(detail or ""),
    }
    if SESSION_ID:
        event["session"] = SESSION_ID
    if extra:
        try:
            event.update(extra)
        except Exception:
            pass
    with LOCK:
        RECENT.append(event)
        try:
            with open(ACTIVITY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return event


def list_actions(limit=200):
    """优先读内存中的最近记录，保证前端立刻可见。"""
    with LOCK:
        if not os.path.isfile(ACTIVITY_FILE):
            return list(RECENT)[-limit:]
        try:
            with open(ACTIVITY_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            disk = []
            for line in lines[-limit:]:
                try:
                    disk.append(json.loads(line))
                except Exception:
                    continue
            return disk[-limit:] if disk else list(RECENT)[-limit:]
        except Exception:
            return list(RECENT)[-limit:]


def clear_actions():
    with LOCK:
        RECENT.clear()
        try:
            if os.path.isfile(ACTIVITY_FILE):
                os.remove(ACTIVITY_FILE)
        except Exception:
            pass
    return True
