"""实时画面流：优先复用工作台已连接设备抓帧，本地 HTTP 输出 MJPEG。

帧源顺序：已连接的 u2/adb 设备 -> scrcpy(adb-scr-py) -> 简洁占位图。
前端只看一个 /stream 地址，状态文字由状态栏显示，不画进画面（避免中文乱码）。
"""

import asyncio
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mobileauto.paths import APP_DIR

STREAM_PORT = 8765
FRAME_INTERVAL = 0.08  # 识别模式：约 12fps
# 纯净模式（关闭按钮识别）：最高画质 + 最低延迟
CLEAR_INTERVAL = 0.02   # 约 50fps
CLEAR_JPEG_QUALITY = 98
DETECT_JPEG_QUALITY = 88


def _blank_frame():
    """纯色占位帧，不写文字，避免 cv2 中文乱码。"""
    try:
        import cv2
        import numpy as np

        img = np.zeros((720, 405, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        return buf.tobytes() if ok else None
    except Exception:
        return None


class FrameSource:
    """后台抓帧 + 本地 MJPEG 服务。"""

    def __init__(self, port=STREAM_PORT):
        self.port = port
        self.latest = _blank_frame()
        self.status = "等待设备"
        self.lock = threading.Lock()
        self._running = False
        self._server = None
        self._conn = None  # 工作台已连接的 inspector conn
        self.clear_mode = False  # True=关闭识别的纯净高清低延迟模式
        self.frame_interval = FRAME_INTERVAL
        self.jpeg_quality = DETECT_JPEG_QUALITY
        self.no_device = False  # 显式断开后为 True，保持黑屏不再抓帧
        self.ever_connected = False

    def start(self):
        with self.lock:
            if self._running:
                return
            self._running = True
        os.environ["PATH"] = APP_DIR + os.pathsep + os.environ.get("PATH", "")
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._http_loop, daemon=True).start()

    def stop(self):
        self._running = False
        if self._server:
            self._server.shutdown()

    def attach_conn(self, conn):
        """由工作台注入已连接设备；断开时立即清成黑屏并停止抓帧。"""
        with self.lock:
            self._conn = conn
            if conn is None:
                # 只有曾经连上过才算「主动断开」，首次启动仍允许 scrcpy 兜底
                if self.ever_connected:
                    self.no_device = True
                    self.latest = _blank_frame()
                    self.status = "设备已断开"
            else:
                self.ever_connected = True
                self.no_device = False
                self.status = f"画面采集中（{conn.get('mode', 'device')}）"

    def set_quality(self, clear=True):
        """切换画质模式。clear=True：关闭识别，最高清最低延迟。"""
        with self.lock:
            self.clear_mode = bool(clear)
            self.frame_interval = CLEAR_INTERVAL if self.clear_mode else FRAME_INTERVAL
            self.jpeg_quality = CLEAR_JPEG_QUALITY if self.clear_mode else DETECT_JPEG_QUALITY
            self.status = "高清画面（低延迟）" if self.clear_mode else "画面采集中"
        return self.clear_mode

    def set_status(self, text):
        with self.lock:
            self.status = text

    def get_status(self):
        with self.lock:
            return {
                "status": self.status,
                "port": self.port,
                "has_frame": self.latest is not None,
                "url": f"http://127.0.0.1:{self.port}/stream",
            }

    def _capture_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._capture())
        finally:
            loop.close()

    async def _capture(self):
        while self._running:
            # 断开后保持黑屏，不再尝试任何抓帧
            with self.lock:
                conn = self._conn
                no_device = self.no_device
            if no_device and conn is None:
                await asyncio.sleep(0.5)
                continue

            # 1) 优先复用工作台已连接的设备
            if conn is not None:
                ok = await self._grab_conn(conn)
                if ok:
                    continue
                self.attach_conn(None)
                continue

            # 2) 退化到 scrcpy 帧流（若可用）
            ok = await self._grab_scrcpy()
            if ok:
                continue

            self.set_status("等待设备连接")
            await asyncio.sleep(1.5)

    async def _grab_conn(self, conn):
        """从已连接设备抓一帧。成功返回 True，失败返回 False。"""
        try:
            from mobileauto import inspector

            if conn.get("mode") == "u2":
                img = await asyncio.to_thread(conn["client"].screenshot, format="opencv")
            else:
                import cv2
                import numpy as np

                result = await asyncio.to_thread(
                    lambda: __import__("subprocess").run(
                        [conn["client"], "exec-out", "screencap", "-p"], capture_output=True
                    )
                )
                if result.returncode != 0 or not result.stdout:
                    return False
                img = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)

            if img is None:
                return False
            import cv2

            with self.lock:
                quality = self.jpeg_quality
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                with self.lock:
                    self.latest = buf.tobytes()
                    interval = self.frame_interval
                await asyncio.sleep(interval)
            return True
        except Exception:
            return False

    async def _grab_scrcpy(self):
        """scrcpy 帧流兜底。"""
        try:
            from adb_scr import AndroidDevice, adb_devices
        except Exception:
            return False
        try:
            devices = await adb_devices()
        except Exception:
            return False
        if not devices:
            return False
        serial = devices[0]
        conn_type = "tcp" if ":" in serial else "usb"
        try:
            device = AndroidDevice(serial, conn_type)
            self.set_status(f"画面采集中（scrcpy {serial}）")
            while self._running:
                with self.lock:
                    if self._conn is not None:
                        return True
                with self.lock:
                    quality = self.jpeg_quality
                    interval = self.frame_interval
                jpg = await device.get_screenshot_jpg(quality=quality)
                if jpg:
                    with self.lock:
                        self.latest = jpg
                await asyncio.sleep(interval)
            return True
        except Exception:
            return False

    def _http_loop(self):
        source = self

        class StreamHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/stream":
                    self._mjpeg()
                elif path == "/frame":
                    self._single()
                else:
                    self.send_response(404)
                    self.end_headers()

            def _mjpeg(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while source._running:
                        with source.lock:
                            jpg = source.latest
                        if not jpg:
                            time.sleep(0.1)
                            continue
                        head = (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(jpg)).encode("ascii") + b"\r\n\r\n"
                        )
                        self.wfile.write(head + jpg + b"\r\n")
                        self.wfile.flush()
                        with source.lock:
                            interval = source.frame_interval
                        time.sleep(interval)
                except Exception:
                    pass

            def _single(self):
                with source.lock:
                    jpg = source.latest
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if jpg:
                    try:
                        self.wfile.write(jpg)
                    except Exception:
                        pass

            def log_message(self, format, *args):
                pass

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), StreamHandler)
            self._server.daemon_threads = True
            self._server.serve_forever()
        except Exception as e:
            self.set_status(f"画面服务启动失败: {e}")


_stream = None
_clear_mode = False


def start_stream(port=STREAM_PORT):
    global _stream
    if _stream is None:
        _stream = FrameSource(port)
        if _clear_mode:
            _stream.set_quality(True)
        _stream.start()
    return _stream


def attach_conn(conn):
    """工作台连接/断开设备时调用，让画面流复用同一连接。"""
    if _stream is not None:
        _stream.attach_conn(conn)


def set_status(text):
    if _stream is not None:
        _stream.set_status(text)


def set_quality(clear=True):
    """切换画面质量模式；流未启动时记录状态，启动后生效。"""
    global _clear_mode
    _clear_mode = bool(clear)
    if _stream is not None:
        return _stream.set_quality(clear)
    return _clear_mode


def get_stream_status():
    return _stream.get_status() if _stream else {"status": "未启动", "url": ""}
