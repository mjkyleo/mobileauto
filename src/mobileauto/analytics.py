"""回传分析：读取手机端执行报告（JSONL），统计各层级命中与耗时。"""

import json
import os
import subprocess
import time
from collections import Counter, defaultdict

from mobileauto.paths import REPORTS_DIR, ensure_runtime_dirs

ensure_runtime_dirs()

# 手机端报告目录；阶段2 手机执行引擎按此路径写出 JSONL
PHONE_REPORT_DIR = "/sdcard/mobileauto/reports"


def list_reports():
    items = []
    for name in os.listdir(REPORTS_DIR):
        if not name.lower().endswith((".jsonl", ".log")):
            continue
        path = os.path.join(REPORTS_DIR, name)
        items.append(
            {
                "name": name,
                "size": os.path.getsize(path),
                "mtime": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))
                ),
            }
        )
    # 任务每次执行都会写 runtime/activity.jsonl，这是回放分析的真实数据源
    from mobileauto.paths import RUNTIME_DIR

    activity_path = os.path.join(RUNTIME_DIR, "activity.jsonl")
    if os.path.isfile(activity_path) and os.path.getsize(activity_path) > 0:
        items.append({
            "name": "activity.jsonl",
            "size": os.path.getsize(activity_path),
            "mtime": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(activity_path))
            ),
        })
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


def _read_events(path):
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def _report_path(name):
    """解析报告路径：普通报告在 reports/，任务执行日志在 runtime/。"""
    base = os.path.basename(str(name))
    if base == "activity.jsonl":
        from mobileauto.paths import RUNTIME_DIR

        return os.path.join(RUNTIME_DIR, base)
    return os.path.join(REPORTS_DIR, base)


def _infer_result(event):
    """任务日志没有 result 字段，从 kind/detail 推断执行结果。"""
    if event.get("result"):
        return str(event["result"])
    detail = str(event.get("detail") or "")
    kind = str(event.get("kind") or "")
    if kind == "error" or "失败" in detail or "FAIL" in detail:
        return "error"
    if kind == "timeout" or "超时" in detail:
        return "timeout"
    if kind == "verify" or "[OK]" in detail or "成功" in detail:
        return "ok"
    if kind in ("click", "splash", "launch", "click_ratio", "click_match"):
        return "clicked"
    if kind == "task":
        return "task"
    return "info"


def analyze_report(name, limit=2000):
    """统计报告：动作/层级分布、来源分布、各层耗时、成功率、最近事件。

    同时支持两种数据源：
      - 手机端回传报告（level/source/result/cost_ms）
      - PC 任务执行日志 activity.jsonl（ts/kind/target/detail/session）
    """
    path = os.path.abspath(_report_path(name))
    allowed = (os.path.abspath(REPORTS_DIR), os.path.abspath(os.path.dirname(path)))
    if not any(path.startswith(root) for root in allowed):
        raise ValueError("报告路径不在允许目录内")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"报告不存在：{name}")

    events = _read_events(path)
    level_counts = Counter()
    source_counts = Counter()
    result_counts = Counter()
    target_counts = Counter()
    cost_by_level = defaultdict(list)

    for event in events:
        # 手机报告用 level，任务日志用 kind，统一成「层级/动作」
        level = str(event.get("level") or event.get("kind") or "-")
        source = str(event.get("source") or event.get("session") or "-")
        result = _infer_result(event)
        target = str(event.get("target") or "")
        detail = str(event.get("detail") or "")
        level_counts[level] += 1
        source_counts[source] += 1
        result_counts[result] += 1
        if target:
            target_counts[target] += 1
        try:
            cost = float(event.get("cost_ms"))
            cost_by_level[level].append(cost)
        except (TypeError, ValueError):
            pass

    avg_cost = {
        level: round(sum(values) / len(values), 2)
        for level, values in cost_by_level.items()
        if values
    }
    acted = sum(v for k, v in result_counts.items() if k in ("clicked", "closed", "ok"))
    failures = sum(v for k, v in result_counts.items() if k in ("error", "timeout", "fail"))
    total = len(events)

    return {
        "file": os.path.basename(path),
        "total": total,
        "acted": acted,
        "failures": failures,
        "success_rate": round(acted / total * 100, 1) if total else 0.0,
        "levels": dict(level_counts.most_common()),
        "sources": dict(source_counts.most_common()),
        "results": dict(result_counts.most_common()),
        "targets": dict(target_counts.most_common(20)),
        "avg_cost_ms": avg_cost,
        "recent": events[-limit:],
    }


def pull_reports(adb=None):
    """从手机拉取执行报告到 PC。返回拉回的文件列表。"""
    if not adb:
        from mobileauto.inspector import find_adb

        adb = find_adb()
    if not adb:
        raise RuntimeError("找不到 adb，无法拉取手机报告")

    check = subprocess.run([adb, "shell", "ls", PHONE_REPORT_DIR], capture_output=True, text=True)
    if check.returncode != 0:
        return []

    pulled = []
    for name in check.stdout.splitlines():
        name = name.strip()
        if not name.endswith(".jsonl"):
            continue
        remote = f"{PHONE_REPORT_DIR}/{name}"
        local = os.path.join(REPORTS_DIR, f"phone_{name}")
        result = subprocess.run([adb, "pull", remote, local], capture_output=True, text=True)
        if result.returncode == 0:
            pulled.append(os.path.basename(local))
    return pulled
