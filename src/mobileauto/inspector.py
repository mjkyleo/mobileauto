"""手机页面元素检查：读取 UI 层级，提取可用于自动化的元素位置信息。"""

import os
import re
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET

from mobileauto.paths import APP_DIR

_ADB_DEVICES_CACHE = {"at": 0.0, "value": []}


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


def _port_open(host, port, timeout=0.25):
    """快速探测服务端口，避免对不存在的地址执行慢速连接。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def list_adb_devices(adb=None, refresh=False, timeout=3):
    """带短缓存的 adb devices，减少自动连接时的重复进程启动。"""
    now = time.monotonic()
    if not refresh and now - _ADB_DEVICES_CACHE["at"] < 0.5:
        return list(_ADB_DEVICES_CACHE["value"])
    adb = adb or find_adb()
    if not adb:
        _ADB_DEVICES_CACHE.update(at=now, value=[])
        return []
    try:
        result = subprocess.run(
            [adb, "devices"], capture_output=True, text=True, timeout=timeout
        )
        devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
    except Exception:
        devices = []
    _ADB_DEVICES_CACHE.update(at=now, value=devices)
    return list(devices)


def diagnose():
    """返回连接环境诊断，供前端功能区显示。"""
    adb = find_adb()
    devices = list_adb_devices(adb, refresh=True)
    info = {"adb": adb or "", "adb_devices": devices, "u2_local": False, "hints": []}
    if adb:
        if not devices:
            info["hints"].append("adb 已找到，但未发现已授权设备")
    else:
        info["hints"].append("未找到 adb.exe，请放到项目目录或加入 PATH")
    info["u2_local"] = _port_open("127.0.0.1", 7912, timeout=0.3)
    if not info["u2_local"]:
        info["hints"].append("本机 7912 无 u2 服务（手机端 atx-agent 未启动）")
    if not devices and not info["u2_local"]:
        info["hints"].append("请连接手机并开启 USB 调试，或填入手机 IP:7912 后连接")
    return info


def _looks_like_u2_address(address):
    text = (address or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return True
    return bool(re.match(r"^\d{1,3}(?:\.\d{1,3}){3}:7912$", text))


def _adb_serial_from_address(address):
    """IP:7912 -> IP:5555；http 地址取 host。"""
    text = (address or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        text = text.split("://", 1)[1].split("/", 1)[0]
    if text.endswith(":7912"):
        return text.rsplit(":", 1)[0] + ":5555"
    return text


def _try_u2(addresses, errors):
    try:
        import uiautomator2 as u2
    except Exception as e:
        errors.append(f"u2 import: {e}")
        return None
    for addr in addresses:
        try:
            client = u2.connect() if not addr else u2.connect(addr)
            return {"mode": "u2", "client": client, "label": addr or "自动检测"}
        except Exception as e:
            errors.append(f"u2 {addr or 'auto'}: {e}")
    return None


def _try_adb(adb, address, errors):
    if not adb:
        return None
    if address:
        serial = _adb_serial_from_address(address)
        try:
            result = subprocess.run(
                [adb, "-s", serial, "get-state"], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0 and "device" in (result.stdout or ""):
                return {"mode": "adb", "client": adb, "label": serial}
            errors.append(f"adb -s {serial} 不可用: {result.stderr.strip()}")
        except Exception as e:
            errors.append(f"adb -s {serial} 失败: {e}")
        return None
    serials = list_adb_devices(adb, refresh=True)
    if serials:
        return {"mode": "adb", "client": adb, "label": serials[0]}
    errors.append("adb devices 未发现已授权设备")
    return None


def connect_device(mode="auto", address=""):
    """返回 {'mode': 'u2'|'adb', 'client': ...}。address 可指定 u2 地址或 adb serial。"""
    errors = []
    address = (address or "").strip()
    adb = find_adb()

    if address:
        if _looks_like_u2_address(address):
            conn = _try_u2([address], errors)
            if conn:
                return conn
            if mode == "u2":
                raise RuntimeError("u2 连接失败\n" + "\n".join(errors[-3:]))
            conn = _try_adb(adb, address, errors)
            if conn:
                return conn
        else:
            if mode == "adb":
                conn = _try_adb(adb, address, errors)
                if conn:
                    return conn
            else:
                conn = _try_adb(adb, address, errors)
                if conn:
                    return conn
                host = address.rsplit(":", 1)[0] if ":" in address else address
                conn = _try_u2([f"{host}:7912"], errors)
                if conn:
                    return conn
            if mode == "u2":
                conn = _try_u2([address], errors)
                if conn:
                    return conn
        raise RuntimeError("无法连接设备\n" + "\n".join(errors[-3:]))

    # 无显式地址：先看本机 atx-agent 端口，再查 adb，最后才尝试 u2 自动发现
    env_url = os.environ.get("ATX_AGENT_URL", "").strip()
    local_u2 = _port_open("127.0.0.1", 7912, timeout=0.2)
    u2_addresses = []
    if env_url:
        u2_addresses.append(env_url)
    if local_u2:
        u2_addresses.extend(["http://127.0.0.1:7912", "127.0.0.1:7912"])
    if mode == "u2" or (mode == "auto" and not local_u2):
        u2_addresses.append("")

    if mode == "u2" and u2_addresses:
        conn = _try_u2(u2_addresses, errors)
        if conn:
            return conn
    if mode == "auto":
        if local_u2:
            conn = _try_u2(u2_addresses, errors)
            if conn:
                return conn
        conn = _try_adb(adb, "", errors)
        if conn:
            return conn
        if "" not in u2_addresses:
            conn = _try_u2([""], errors)
            if conn:
                return conn
    if mode == "adb":
        conn = _try_adb(adb, "", errors)
        if conn:
            return conn
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


def swipe(conn, x1, y1, x2, y2, duration=300):
    """屏幕滑动，duration 毫秒。用于页面上下滚动。"""
    if conn["mode"] == "u2":
        conn["client"].swipe(int(x1), int(y1), int(x2), int(y2), duration=duration)
    else:
        subprocess.run(
            [conn["client"], "shell", "input", "swipe", str(int(x1)), str(int(y1)),
             str(int(x2)), str(int(y2)), str(int(duration))],
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
