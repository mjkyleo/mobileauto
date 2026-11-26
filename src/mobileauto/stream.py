"""实时画面：优先 scrcpy v5 H264 + WebSocket，失败回退快速截图/MJPEG。

scrcpy 服务端在手机上用 VirtualDisplay + MediaCodec 硬编 H264，PC 端只做包转发，
浏览器用 WebCodecs 硬解并只渲染最新帧；相比逐帧截图/JPEG，延迟和流畅度都更好。
"""

import asyncio
import base64
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mobileauto.paths import APP_DIR
from mobileauto.scrcpy_stream import WS_PORT, ScrcpyStream

STREAM_PORT = 8765
FRAME_INTERVAL = 0.04
CLEAR_INTERVAL = 0.02
CLEAR_JPEG_QUALITY = 98
DETECT_JPEG_QUALITY = 88
SCRCPY_RETRY_SECONDS = 6.0


def _blank_frame():
    try:
        import cv2
        import numpy as np

        img = np.zeros((720, 405, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        return buf.tobytes() if ok else None
    except Exception:
        return None


class FrameSource:
    """后台抓帧 + 本地 MJPEG/WebSocket 服务。"""

    def __init__(self, port=STREAM_PORT):
        self.port = port
        self.latest = _blank_frame()
        self.status = "等待设备"
        self.last_error = ""
        self.lock = threading.RLock()
        self.frame_cond = threading.Condition(self.lock)
        self.frame_seq = 0
        self._running = False
        self._server = None
        self._loop = None
        self._conn = None
        self.clear_mode = False
        self.frame_interval = FRAME_INTERVAL
        self.jpeg_quality = DETECT_JPEG_QUALITY
        self.no_device = False
        self.ever_connected = False
        self._scrcpy = None
        self._scrcpy_retry_at = 0.0
        self._scrcpy_connect_task = None
        self._scrcpy_disabled = False

    def start(self):
        with self.lock:
            if self._running:
                return
            self._running = True
        os.environ["PATH"] = APP_DIR + os.pathsep + os.environ.get("PATH", "")
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._http_loop, daemon=True).start()

    def stop(self):
        self.request_close_scrcpy()
        self._running = False
        if self._server:
            self._server.shutdown()

    def _note(self, text):
        self.last_error = str(text)[:300]

    def attach_conn(self, conn):
        """连接设备时注入 conn；断开时立即黑屏并关闭 H264 会话。"""
        with self.frame_cond:
            self._conn = conn
            if conn is None:
                if self.ever_connected:
                    self.no_device = True
                    self.latest = _blank_frame()
                    self.frame_seq += 1
                    self.frame_cond.notify_all()
                    self.status = "设备已断开"
            else:
                self.ever_connected = True
                self.no_device = False
                self.status = "画面采集中（正在启动 H264）"
        if conn is None:
            task = self._scrcpy_connect_task
            if task is not None:
                task.cancel()
                self._scrcpy_connect_task = None
            self.request_close_scrcpy()

    def set_h264_enabled(self, enabled=True):
        """浏览器不支持 WebCodecs 时关掉 H264，恢复截图链路。"""
        if not enabled:
            self._disable_scrcpy("浏览器不支持 WebCodecs，改用快速截图")
        return not self._scrcpy_disabled

    def set_quality(self, clear=True):
        with self.lock:
            self.clear_mode = bool(clear)
            self.frame_interval = CLEAR_INTERVAL if self.clear_mode else FRAME_INTERVAL
            self.jpeg_quality = CLEAR_JPEG_QUALITY if self.clear_mode else DETECT_JPEG_QUALITY
            mode = "兼容抓帧" if self._scrcpy_disabled else "H264"
            self.status = ("高清画面（" if self.clear_mode else "画面采集中（") + mode + "）"
        return self.clear_mode

    def set_status(self, text):
        with self.lock:
            self.status = text

    def get_status(self):
        with self.lock:
            h264 = bool(self._scrcpy and self._scrcpy.running and self._scrcpy.has_frame)
            h264_fps = self._scrcpy.sent_fps() if h264 else 0
            return {
                "status": self.status,
                "port": self.port,
                "has_frame": self.latest is not None or h264,
                "url": f"http://127.0.0.1:{self.port}/stream",
                "ws_url": f"ws://127.0.0.1:{WS_PORT}",
                "h264": h264,
                "h264_fps": h264_fps,
                "last_error": self.last_error,
            }

    # ---------- 控制通道 ----------
    def has_control(self):
        return bool(self._scrcpy and self._scrcpy.has_control())

    def get_control_size(self):
        return self._scrcpy.get_screen_size() if self._scrcpy else None

    def tap(self, x, y, timeout=2.0):
        if not self.has_control():
            return False
        return bool(self._scrcpy.tap(x, y))

    def swipe(self, x1, y1, x2, y2, timeout=4.0):
        if not self.has_control():
            return False
        return bool(self._scrcpy.swipe(x1, y1, x2, y2))

    # ---------- 抓帧循环 ----------
    def _capture_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._capture())
        finally:
            self._loop = None
            loop.close()

    async def _capture(self):
        while self._running:
            with self.lock:
                conn = self._conn
                no_device = self.no_device
            if no_device and conn is None:
                await asyncio.sleep(0.5)
                continue

            # H264 帧由 ScrcpyStream 直接推给浏览器，不占用截图循环
            if self._scrcpy and self._scrcpy.running and self._scrcpy.has_frame:
                with self.lock:
                    interval = self.frame_interval
                await asyncio.sleep(interval)
                continue

            self._maybe_start_scrcpy(conn)
            got_frame = False
            if conn is not None:
                got_frame = await self._grab_conn(conn)
            if not got_frame:
                if self._scrcpy or self._scrcpy_connect_task is not None:
                    await asyncio.sleep(0.1)
                    continue
                self.attach_conn(None)
                continue

            with self.lock:
                interval = self.frame_interval
            await asyncio.sleep(interval if got_frame else (0.02 if conn else 0.8))

    def _maybe_start_scrcpy(self, conn):
        if self._scrcpy_disabled or self.no_device or self._scrcpy_connect_task is not None:
            return
        if self._scrcpy and self._scrcpy.running:
            return
        now = time.monotonic()
        if now < self._scrcpy_retry_at:
            return
        self._scrcpy_retry_at = now + SCRCPY_RETRY_SECONDS
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        self._scrcpy_connect_task = loop.create_task(self._start_scrcpy(conn))

    async def _start_scrcpy(self, conn):
        stream = None
        try:
            from mobileauto import inspector

            adb = inspector.find_adb()
            serials = inspector.list_adb_devices(adb, refresh=True) if adb else []
            serial = self._pick_serial(conn, serials)
            if not serial:
                self._disable_scrcpy("未找到可用于 H264 的设备序列号")
                return
            stream = ScrcpyStream(adb, serial, log=self._note)
            ok = await stream.start()
            if ok and self._running and not self.no_device:
                self._scrcpy = stream
                self.set_status("高清画面（scrcpy H264）" if self.clear_mode else "画面采集中（scrcpy H264）")
                return
            await stream.stop()
            self._disable_scrcpy(self.last_error or "H264 会话启动失败")
        except Exception as e:
            self._note(e)
            self._disable_scrcpy(str(e))
            if stream is not None:
                try:
                    await stream.stop()
                except Exception:
                    pass
        finally:
            self._scrcpy_connect_task = None

    @staticmethod
    def _pick_serial(conn, serials):
        candidates = []
        label = str((conn or {}).get("label") or "").strip()
        if label and label not in ("自动检测", "自动"):
            if label.startswith("http://"):
                label = label.split("://", 1)[1].split("/", 1)[0]
            candidates.append(label)
            if label.endswith(":7912"):
                candidates.append(label.rsplit(":", 1)[0] + ":5555")
        if len(serials) == 1:
            candidates.append(serials[0])
        else:
            candidates.extend(serials)
        for item in candidates:
            if item in serials:
                return item
        if candidates and not serials:
            return candidates[0]
        return None

    def _disable_scrcpy(self, reason=""):
        if self._scrcpy_disabled:
            return
        self._scrcpy_disabled = True
        self._scrcpy_retry_at = float("inf")
        self.last_error = str(reason)[:300]
        self.set_status("画面采集中（H264 不兼容，快速截图）")
        self.request_close_scrcpy()

    async def _grab_conn(self, conn):
        """兼容抓帧：u2 直接取设备 JPEG，adb 用 screencap 后编码。"""
        try:
            with self.lock:
                quality = self.jpeg_quality
            if conn.get("mode") == "u2":
                try:
                    raw = await asyncio.to_thread(
                        conn["client"].jsonrpc.takeScreenshot, 1, quality
                    )
                    if raw:
                        jpg = base64.b64decode(raw)
                        if jpg:
                            self._set_frame(jpg)
                            return True
                except Exception:
                    pass
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

            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                self._set_frame(buf.tobytes())
            return bool(ok)
        except Exception:
            return False

    def _set_frame(self, jpg):
        with self.frame_cond:
            self.latest = jpg
            self.frame_seq += 1
            self.frame_cond.notify_all()

    async def _close_scrcpy(self):
        stream = self._scrcpy
        self._scrcpy = None
        if stream is not None:
            try:
                await stream.stop()
            except Exception:
                pass

    def request_close_scrcpy(self):
        loop = self._loop
        if loop is None or loop.is_closed() or self._scrcpy is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_scrcpy(), loop)
        except Exception:
            pass

    # ---------- 本地 MJPEG（兼容回退） ----------
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
                last_seq = -1
                try:
                    while source._running:
                        with source.frame_cond:
                            source.frame_cond.wait_for(
                                lambda: source.frame_seq != last_seq or not source._running,
                                timeout=1.0,
                            )
                            if not source._running:
                                break
                            last_seq = source.frame_seq
                            jpg = source.latest
                        if not jpg:
                            continue
                        head = (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(jpg)).encode("ascii") + b"\r\n\r\n"
                        )
                        self.wfile.write(head + jpg + b"\r\n")
                        self.wfile.flush()
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
    if _stream is not None:
        _stream.attach_conn(conn)


def set_status(text):
    if _stream is not None:
        _stream.set_status(text)


def set_h264_enabled(enabled=True):
    return bool(_stream and _stream.set_h264_enabled(enabled))


def set_quality(clear=True):
    global _clear_mode
    _clear_mode = bool(clear)
    if _stream is not None:
        return _stream.set_quality(clear)
    return _clear_mode


def get_stream_status():
    return _stream.get_status() if _stream else {"status": "未启动", "url": "", "h264": False}


def has_control():
    return bool(_stream and _stream.has_control())


def get_control_size():
    return _stream.get_control_size() if _stream else None


def tap(x, y):
    return bool(_stream and _stream.tap(x, y))


def swipe(x1, y1, x2, y2):
    return bool(_stream and _stream.swipe(x1, y1, x2, y2))
