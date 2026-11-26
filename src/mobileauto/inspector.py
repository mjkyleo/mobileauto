"""手机页面元素检查：读取 UI 层级，提取可用于自动化的元素位置信息。"""

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

from mobileauto.paths import APP_DIR


def find_adb():
    env_adb = os.environ.get("ADB_PATH")
    if env_adb and os.path.isfile(env_adb):
        return env_adb
    which = shutil.which("adb")
    if which:
        return which
    local_adb = os.path.join(APP_DIR, "adb.exe" if os.name == "nt" else "adb")
    if os.path.isfile(local_adb):
        return local_adb
    bundled = os.path.join(
        APP_DIR, "platform-tools", "adb.exe" if os.name == "nt" else "adb"
    )
    if os.path.isfile(bundled):
        return bundled
    return None


def diagnose():
    """返回连接环境诊断，供前端功能区显示。"""
    info = {"adb": find_adb() or "", "adb_devices": [], "u2_local": False, "hints": []}
    if info["adb"]:
        try:
            result = subprocess.run(
                [info["adb"], "devices"], capture_output=True, text=True, timeout=8
            )
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    info["adb_devices"].append(parts[0])
        except Exception as e:
            info["hints"].append(f"adb devices 失败: {e}")
    else:
        info["hints"].append("未找到 adb.exe，请放到项目目录或加入 PATH")
    try:
        import socket

        s = socket.create_connection(("127.0.0.1", 7912), timeout=1)
        s.close()
        info["u2_local"] = True
    except Exception:
        info["hints"].append("本机 7912 无 u2 服务（手机端 atx-agent 未启动）")
    if not info["adb_devices"] and not info["u2_local"]:
        info["hints"].append("请连接手机并开启 USB 调试，或填入手机 IP:7912 后连接")
    return info


def connect_device(mode="auto", address=""):
    """返回 {'mode': 'u2'|'adb', 'client': ...}。address 可指定 u2 地址或 adb serial。"""
    errors = []
    address = (address or "").strip()

    if mode in ("auto", "u2"):
        try:
            import uiautomator2 as u2

            if address:
                addresses = [address]
            else:
                addresses = [
                    os.environ.get("ATX_AGENT_URL", "http://127.0.0.1:7912"),
                    "http://127.0.0.1:7912",
                    "127.0.0.1:7912",
                    "",  # u2.connect() 自动检测（原来就有，恢复它）
                ]
            for addr in addresses:
                try:
                    client = u2.connect(addr) if addr else u2.connect()
                    return {"mode": "u2", "client": client, "label": addr or "自动检测"}
                except Exception as e:
                    errors.append(f"u2 {addr or 'auto'}: {e}")
        except Exception as e:
            errors.append(f"u2 import: {e}")

    if mode == "u2":
        raise RuntimeError("u2 连接失败\n" + "\n".join(errors[-3:]))

    adb = find_adb()
    if adb:
        if address:
            result = subprocess.run(
                [adb, "-s", address, "get-state"], capture_output=True, text=True, timeout=8
            )
            if result.returncode == 0 and "device" in (result.stdout or ""):
                return {"mode": "adb", "client": adb, "label": address}
            errors.append(f"adb -s {address} 不可用: {result.stderr.strip()}")
        else:
            check = subprocess.run([adb, "devices"], capture_output=True, text=True)
            if check.returncode == 0:
                lines = [
                    line.split()[0]
                    for line in check.stdout.splitlines()[1:]
                    if len(line.split()) >= 2 and line.split()[1] == "device"
                ]
                if lines:
                    return {
                        "mode": "adb",
                        "client": adb,
                        "label": lines[0],
                    }
                errors.append("adb devices 未发现已授权设备")
    raise RuntimeError("无法连接设备\n" + "\n".join(errors[-3:]))


def dump_hierarchy(conn):
    if conn["mode"] == "u2":
        return conn["client"].dump_hierarchy()

    adb = conn["client"]
    remote = "/sdcard/window.xml"
    subprocess.run(
        [adb, "shell", "uiautomator", "dump", remote],
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = subprocess.run(
        [adb, "shell", "cat", remote],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "adb dump hierarchy 失败")
    return result.stdout


def parse_bounds(value):
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(v) for v in match.groups())
    return {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "cx": (x1 + x2) // 2,
        "cy": (y1 + y2) // 2,
        "w": x2 - x1,
        "h": y2 - y1,
    }


def parse_hierarchy(xml_text):
    rows = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"UI 层级 XML 解析失败: {e}")

    for index, node in enumerate(root.iter("node")):
        attrs = node.attrib
        bounds = parse_bounds(attrs.get("bounds", ""))
        if bounds is None:
            continue
        rows.append(
            {
                "index": index,
                "text": attrs.get("text", ""),
                "desc": attrs.get("content-desc", ""),
                "resource_id": attrs.get("resource-id", ""),
                "class": attrs.get("class", ""),
                "package": attrs.get("package", ""),
                "clickable": attrs.get("clickable", "false"),
                "enabled": attrs.get("enabled", "false"),
                "visible": attrs.get("visible-to-user", attrs.get("visible", "")),
                "bounds": attrs.get("bounds", ""),
                **bounds,
            }
        )
    return rows


def screen_size(conn):
    """返回手机屏幕像素尺寸，失败返回 None。"""
    if conn["mode"] == "u2":
        try:
            info = conn["client"].info or {}
            width = info.get("displayWidth") or info.get("displayWidthPixels")
            height = info.get("displayHeight") or info.get("displayHeightPixels")
            if width and height:
                return int(width), int(height)
        except Exception:
            pass
        return None

    adb = conn["client"]
    try:
        result = subprocess.run(
            [adb, "shell", "wm", "size"], capture_output=True, text=True, timeout=20
        )
        match = re.search(r"(\d+)x(\d+)", result.stdout or "")
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return None


def snapshot(conn):
    xml_text = dump_hierarchy(conn)
    rows = parse_hierarchy(xml_text)
    return xml_text, rows


def click_at(conn, x, y):
    if conn["mode"] == "u2":
        conn["client"].click(int(x), int(y))
    else:
        subprocess.run(
            [conn["client"], "shell", "input", "tap", str(int(x)), str(int(y))],
            capture_output=True,
            text=True,
        )


def press_back(conn):
    if conn["mode"] == "u2":
        conn["client"].press("back")
    else:
        subprocess.run(
            [conn["client"], "shell", "input", "keyevent", "4"],
            capture_output=True,
            text=True,
        )


def press_home(conn):
    if conn["mode"] == "u2":
        conn["client"].press("home")
    else:
        subprocess.run(
            [conn["client"], "shell", "input", "keyevent", "3"],
            capture_output=True,
            text=True,
        )


def press_recents(conn):
    if conn["mode"] == "u2":
        conn["client"].press("recent")
    else:
        subprocess.run(
            [conn["client"], "shell", "input", "keyevent", "187"],
            capture_output=True,
            text=True,
        )
