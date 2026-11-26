"""可流通的任务流程格式（FlowSpec）与 Mermaid 可视化。

设计目标（参考 Maestro / Tasker / Node-RED 的调研结论）：
  1. 声明式：JSON 描述「看到什么 -> 做什么 -> 去哪里」，不含执行代码
  2. 人可读、机器可写：录制/编辑产出 FlowSpec，执行引擎消费 FlowSpec
  3. 动作与观察分离：locators（怎么找）与 states（找到后做什么）解耦
  4. 智能等待：when 条件轮询命中即执行，不写死 sleep
  5. 可视化：同一份 FlowSpec 直接生成 Mermaid，支持循环/分支
  6. 流通：导入导出标准格式，可跨工具、可版本管理

FlowSpec v1 结构见 export_task() 的输出示例。
"""

import json
import os
import time
from datetime import datetime

SCHEMA = "mobileauto/flow/v1"
VERSION = "1.0"

ACTION_LABELS = {
    "click_match": "点击文字",
    "click_ratio": "点击坐标",
    "back": "返回键",
    "wait": "等待",
    "finish": "结束",
    "mute_once": "静音一次",
    "close_ad": "关广告",
    "input_text": "输入文本",
    "assert_visible": "断言可见",
}

MERMAID_SHAPES = {
    "click_match": ("[", "]"),
    "click_ratio": ("[", "]"),
    "back": ("([", "])"),
    "wait": (">", "]"),
    "finish": (("{", "}")),
    "mute_once": (">", "]"),
    "close_ad": ("[", "]"),
    "input_text": (">", "]"),
    "assert_visible": (("{", "}")),
}


# ---------- 导出：现有任务 -> FlowSpec ----------
def _state_to_node(item, idx):
    action = item.get("action") or "wait"
    node = {
        "id": item.get("name") or item.get("state") or f"S{idx + 1}",
        "name": item.get("label") or item.get("name") or f"步骤{idx + 1}",
        "when": {
            "text_any": list(item.get("match") or []),
            "exclude": list(item.get("exclude") or []),
        },
        "do": [{"action": action}],
        "retry": bool(item.get("retry", True)),
    }
    if action == "click_ratio":
        ratio = item.get("ratio") or []
        node["do"][0]["target"] = {"type": "ratio", "value": ratio}
    elif action in ("click_match", "close_ad", "mute_once"):
        ratio = item.get("ratio") or []
        if ratio:
            node["do"][0]["target"] = {"type": "ratio_fallback", "value": ratio}
    if item.get("count"):
        node["count"] = True
    return node


def _locators_from_states(states):
    locators = []
    seen = set()
    for item in states or []:
        for text in (item.get("match") or []):
            if not text or text in seen:
                continue
            seen.add(text)
            locators.append({
                "id": f"loc_{len(locators) + 1}",
                "type": "text",
                "value": text,
                "strategy": ["exact", "contains", "regex"],
            })
    return locators[:24]


def export_task(task):
    """把工作台任务导出为可流通的 FlowSpec v1。"""
    states = task.get("states") or []
    flow = task.get("flow") or {}
    is_configured = bool(task.get("engine") == "configured" or states)

    spec = {
        "$schema": SCHEMA,
        "version": VERSION,
        "meta": {
            "key": task.get("key") or "",
            "name": task.get("name") or "未命名任务",
            "description": task.get("description") or "",
            "app": task.get("app") or "",
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "settings": {
            "engine": "configured" if is_configured else "steps",
            "max_loops": int(task.get("max_loops") or 0),
            "poll": float(task.get("poll") or 0.25),
            "tap_gap": float(task.get("tap_gap") or 0.35),
            "capabilities": dict(task.get("capabilities") or {}),
        },
        "locators": _locators_from_states(states) if is_configured else [],
        "flow": {},
        "observability": {
            "screenshot_on_step": True,
            "ocr_on_step": True,
            "log_level": "action",
        },
    }

    if is_configured:
        nodes = [_state_to_node(item, i) for i, item in enumerate(states)]
        spec["flow"] = {
            "start": nodes[0]["id"] if nodes else "",
            "mode": "priority",
            "states": nodes,
            "loops": [
                {
                    "id": "main_loop",
                    "from": nodes[0]["id"] if nodes else "",
                    "max": int(task.get("max_loops") or 0),
                    "exit_when": {"text_any": list(task.get("finish_info_keywords") or [])},
                }
            ] if (task.get("max_loops") or 0) != 1 and nodes else [],
            "stuck_states": list(task.get("stuck_states") or []),
        }
    else:
        steps = []
        for i, item in enumerate(flow.get("steps") or []):
            if isinstance(item, str):
                item = {"action": item}
            steps.append({
                "id": item.get("id") or f"S{i + 1}",
                "name": item.get("label") or item.get("name") or str(item.get("action") or "步骤"),
                "when": {"text_any": list(item.get("match") or [])},
                "do": [{"action": item.get("action") or "wait"}],
            })
        loop_from = int(flow.get("loop_from") or 0)
        spec["flow"] = {
            "start": steps[0]["id"] if steps else "",
            "mode": "sequence",
            "steps": steps,
            "loops": [
                {
                    "id": "main_loop",
                    "from": steps[loop_from]["id"] if 0 <= loop_from < len(steps) else "",
                    "max": int(flow.get("max_loops") or task.get("max_loops") or 0),
                }
            ] if steps else [],
        }
    return spec


# ---------- 导入：FlowSpec -> 现有任务 ----------
def _node_to_state(node, idx):
    action = (node.get("do") or [{}])[0]
    action_name = action.get("action") or "wait"
    when = node.get("when") or {}
    match = list(when.get("text_any") or [])
    state = {
        "name": node.get("id") or f"STEP_{idx + 1}",
        "match": match or [node.get("name") or f"步骤{idx + 1}"],
        "action": action_name,
    }
    if when.get("exclude"):
        state["exclude"] = list(when["exclude"])
    target = action.get("target") or {}
    if target.get("value"):
        state["ratio"] = target["value"]
    if node.get("count"):
        state["count"] = True
    if node.get("name"):
        state["label"] = node["name"]
    return state


def import_task(spec):
    """把 FlowSpec v1 转成工作台任务 JSON，兼容现有执行引擎。"""
    if not isinstance(spec, dict):
        raise ValueError("流程文件必须是 JSON 对象")
    if spec.get("$schema") != SCHEMA:
        raise ValueError(f"不支持的流程格式：{spec.get('$schema') or '未知'}（需要 {SCHEMA}）")

    meta = spec.get("meta") or {}
    settings = spec.get("settings") or {}
    flow = spec.get("flow") or {}
    name = meta.get("name") or "导入的任务"
    key = meta.get("key") or name

    task = {
        "key": key,
        "name": name,
        "description": meta.get("description") or f"由 {SCHEMA} 导入",
        "app": meta.get("app") or "",
        "enabled": False,
        "max_loops": int(settings.get("max_loops") or 1),
        "capabilities": dict(settings.get("capabilities") or {}),
        "poll": float(settings.get("poll") or 0.25),
        "tap_gap": float(settings.get("tap_gap") or 0.35),
    }

    if flow.get("mode") == "sequence":
        steps = []
        for idx, node in enumerate(flow.get("steps") or []):
            act = (node.get("do") or [{}])[0].get("action") or "wait"
            item = {"action": act}
            if node.get("name"):
                item["label"] = node["name"]
            if (node.get("when") or {}).get("text_any"):
                item["match"] = node["when"]["text_any"]
            steps.append(item)
        loop_from_id = (flow.get("loops") or [{}])[0].get("from") or ""
        loop_from = next(
            (i for i, n in enumerate(flow.get("steps") or []) if n.get("id") == loop_from_id), 0
        )
        task["flow"] = {
            "loop_from": loop_from,
            "max_loops": int(task["max_loops"]),
            "steps": steps,
        }
    else:
        states = [_node_to_state(node, i) for i, node in enumerate(flow.get("states") or [])]
        task["engine"] = "configured"
        task["states"] = states
        task["stuck_states"] = list(flow.get("stuck_states") or [])
        exit_keys = []
        for lp in flow.get("loops") or []:
            exit_keys.extend(((lp.get("exit_when") or {}).get("text_any") or []))
        task["finish_info_keywords"] = list(dict.fromkeys(exit_keys))
    return task


def validate(spec):
    """导入前的基本结构校验，返回错误列表。"""
    errors = []
    if not isinstance(spec, dict):
        return ["流程文件必须是 JSON 对象"]
    if spec.get("$schema") != SCHEMA:
        errors.append(f"格式标识不正确：{spec.get('$schema') or '缺失'}（需要 {SCHEMA}）")
    flow = spec.get("flow") or {}
    nodes = flow.get("states") if flow.get("mode") == "priority" else flow.get("steps")
    if not isinstance(nodes, list) or not nodes:
        errors.append("flow.states/flow.steps 不能为空")
    ids = [n.get("id") for n in (nodes or []) if isinstance(n, dict) and n.get("id")]
    if len(ids) != len(set(ids)):
        errors.append("步骤 id 重复")
    return errors


# ---------- 可视化：FlowSpec -> Mermaid ----------
def _id(text):
    """Mermaid 节点 id 只用安全字符，避免中文/符号导致解析失败。"""
    value = str(text or "")
    return "N_" + "".join(ch if ch.isalnum() else "_" for ch in value)[:24]


def _label(text, limit=18):
    """节点标签：去掉会破坏 Mermaid 的字符，控制长度。"""
    value = str(text or "").replace('"', "'").replace("<br/>", " ").replace("\n", " ")
    value = value.replace("{", "(").replace("}", ")").replace("[", "(").replace("]", ")")
    return value[:limit]


def to_mermaid(spec):
    """生成 Mermaid flowchart：节点=状态，标签=做了什么，箭头=流转，回边=循环。"""
    flow = spec.get("flow") or {}
    nodes = flow.get("states") if flow.get("mode") == "priority" else flow.get("steps")
    nodes = [n for n in (nodes or []) if isinstance(n, dict)]
    if not nodes:
        return "flowchart TD\n  START([开始]) --> END([结束])"

    ids = [_id(n.get("id") or f"S{i + 1}") for i, n in enumerate(nodes)]
    # 防止 id 冲突
    seen = {}
    for i, nid in enumerate(ids):
        count = seen.get(nid, 0)
        seen[nid] = count + 1
        if count:
            ids[i] = f"{nid}_{count + 1}"

    lines = [
        "%% 由 FlowSpec v1 自动生成",
        "flowchart TD",
        "  START([开始])",
    ]

    # 节点定义：显示「名称 + 关键触发词」，让人一眼看出在干什么
    for node, nid in zip(nodes, ids):
        action = ((node.get("do") or [{}])[0].get("action") or "wait")
        name = _label(node.get("name") or ACTION_LABELS.get(action, action))
        texts = ((node.get("when") or {}).get("text_any") or [])
        trigger = _label(texts[0], 12) if texts else ""
        shape = "{" if action == "finish" else "["
        close = "}" if action == "finish" else "]"
        label = f"{name}<br/>{trigger}" if trigger else name
        lines.append(f'  {nid}{shape}"{label}"{close}')

    # 主流程：开始 -> 每个状态 -> 完成
    lines.append(f"  START --> {ids[0]}")
    for i in range(len(nodes) - 1):
        lines.append(f"  {ids[i]} --> {ids[i + 1]}")
    lines.append(f"  {ids[-1]} --> END([完成])")

    # 循环回边：标出循环轮数
    for lp in flow.get("loops") or []:
        from_id = _id(lp.get("from") or "")
        if from_id in ids:
            idx = ids.index(from_id)
            target = ids[max(0, idx - 1)] if idx > 0 else ids[-1]
            lines.append(f'  {target} -->|"循环 {lp.get("max") or "∞"} 轮"| {from_id}')

    # 卡住重试自环
    stuck = set(flow.get("stuck_states") or [])
    for node, nid in zip(nodes, ids):
        if node.get("id") in stuck:
            lines.append(f'  {nid} -->|"卡住重试"| {nid}')

    # 样式区分动作类型
    lines.extend([
        "  classDef actClick fill:#dbeafe,stroke:#2563eb,color:#1e3a8a;",
        "  classDef actWait fill:#fef3c7,stroke:#d97706,color:#92400e;",
        "  classDef actFinish fill:#dcfce7,stroke:#16a34a,color:#14532d;",
    ])
    for node, nid in zip(nodes, ids):
        action = ((node.get("do") or [{}])[0].get("action") or "wait")
        if action == "finish":
            lines.append(f"  class {nid} actFinish;")
        elif "click" in action or "close" in action:
            lines.append(f"  class {nid} actClick;")
        elif action in ("wait", "mute_once"):
            lines.append(f"  class {nid} actWait;")
    return "\n".join(lines)


def to_mermaid_task(task):
    """便捷函数：直接从工作台任务生成 Mermaid。"""
    return to_mermaid(export_task(task))


def to_graph(spec):
    """生成可本地渲染的图结构：nodes + edges，供工作台画 SVG 流程图。"""
    flow = spec.get("flow") or {}
    nodes = flow.get("states") if flow.get("mode") == "priority" else flow.get("steps")
    nodes = [n for n in (nodes or []) if isinstance(n, dict)]
    if not nodes:
        return {"nodes": [], "edges": []}

    graph_nodes = [{
        "id": "START", "label": "开始", "action": "start",
        "text": "", "detail": "",
    }]
    ids = []
    for node in nodes:
        action = (node.get("do") or [{}])[0].get("action") or "wait"
        when = (node.get("when") or {}).get("text_any") or []
        target = ((node.get("do") or [{}])[0].get("target") or {}).get("value") or []
        nid = _id(node.get("id") or f"S{len(ids) + 1}")
        ids.append(nid)
        graph_nodes.append({
            "id": nid,
            "label": node.get("name") or ACTION_LABELS.get(action, action),
            "action": action,
            "text": str(when[0]) if when else "",
            "detail": "/".join(f"{v:.2f}" for v in target) if target else "",
        })

    has_finish = any(n["action"] == "finish" for n in graph_nodes)
    if not has_finish:
        graph_nodes.append({"id": "DONE", "label": "结束", "action": "finish", "text": "", "detail": ""})
        end_id = "DONE"
    else:
        end_id = next(n["id"] for n in graph_nodes if n["action"] == "finish")

    edges = [{"from": "START", "to": ids[0], "kind": "next", "label": ""}]
    for i in range(len(ids) - 1):
        edges.append({"from": ids[i], "to": ids[i + 1], "kind": "match", "label": "命中"})
    edges.append({"from": ids[-1], "to": end_id, "kind": "next", "label": ""})

    for lp in flow.get("loops") or []:
        from_id = _id(lp.get("from") or "")
        if from_id in ids:
            edges.append({
                "from": ids[-1], "to": from_id, "kind": "loop",
                "label": f"循环 {lp.get('max') or '∞'} 轮",
            })
    stuck = set(flow.get("stuck_states") or [])
    for nid in ids:
        if nid in stuck:
            edges.append({"from": nid, "to": nid, "kind": "stuck", "label": "卡住重试"})

    return {"nodes": graph_nodes, "edges": edges, "end": end_id}


def analyze(task):
    """统计任务流程：步骤数、点击数、循环、结束条件，供前端展示。"""
    spec = export_task(task)
    flow = spec.get("flow") or {}
    nodes = flow.get("states") if flow.get("mode") == "priority" else flow.get("steps")
    nodes = nodes or []
    actions = {}
    clicks = 0
    for node in nodes:
        action = ((node.get("do") or [{}])[0].get("action") or "wait")
        actions[action] = actions.get(action, 0) + 1
        if "click" in action:
            clicks += 1
    loops = flow.get("loops") or []
    return {
        "schema": SCHEMA,
        "mode": flow.get("mode") or "",
        "steps": len(nodes),
        "clicks": clicks,
        "actions": actions,
        "loops": len(loops),
        "loop_max": loops[0].get("max") if loops else 0,
        "stuck_states": list(flow.get("stuck_states") or []),
        "end_keywords": list(task.get("finish_info_keywords") or []),
    }
