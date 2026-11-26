"""实时画面流：优先使用 scrcpy(H264) 解码帧，失败回退设备截图，本地 MJPEG 推送。

scrcpy 服务端在手机上编码视频，客户端只解码最新帧；相比反复执行 screencap
或 u2.screenshot，帧率更稳定、延迟更低。控制事件走同一条 scrcpy 通道，点击
无需每次拉起新的 adb 进程。
"""

import asyncio
import base64
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mobileauto.paths import APP_DIR

STREAM_PORT = 8765
FRAME_INTERVAL = 0.04  # 按钮识别模式：约 25fps
CLEAR_INTERVAL = 0.02  # 纯净模式：约 50fps（scrcpy 只在画面变化时产生新帧）
CLEAR_JPEG_QUALITY = 98
DETECT_JPEG_QUALITY = 88
SCRCPY_RETRY_SECONDS = 3.0
_scrcpy_audio_patched = False
_scrcpy_fps_set = False


def _patch_scrcpy_no_audio():
    """让 adb-scr-py 以 audio=false 启动服务，避免 Android 17 音频捕获报错。"""
    global _scrcpy_audio_patched
    if _scrcpy_audio_patched:
        return
    try:
        from adb_scr.device import android_device as device_module

        async def _no_audio_api_level(serial, timeout=5.0):
            return 29

        device_module.adb_android_api_level = _no_audio_api_level
        _scrcpy_audio_patched = True
    except Exception:
        pass


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
        self.lock = threading.RLock()
        self.frame_cond = threading.Condition(self.lock)
        self.frame_seq = 0
        self._running = False
        self._server = None
        self._loop = None
        self._conn = None  # 工作台已连接的 inspector conn
        self.clear_mode = False  # True=关闭识别的纯净高清低延迟模式
        self.frame_interval = FRAME_INTERVAL
        self.jpeg_quality = DETECT_JPEG_QUALITY
        self.no_device = False  # 显式断开后为 True，保持黑屏不再抓帧
        self.ever_connected = False
        self._scrcpy_device = None
        self._scrcpy_serial = ""
        self._scrcpy_retry_at = 0.0
        self._scrcpy_connect_task = None
        self._scrcpy_disabled = False  # 设备不兼容时彻底停用，避免反复启动服务

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

    def attach_conn(self, conn):
        """由工作台注入已连接设备；断开时立即清成黑屏并停止抓帧。"""
        with self.frame_cond:
            self._conn = conn
            if conn is None:
                # 只有曾经连上过才算「主动断开」，首次启动仍允许 scrcpy 兜底
                if self.ever_connected:
                    self.no_device = True
                    self.latest = _blank_frame()
                    self.frame_seq += 1
                    self.frame_cond.notify_all()
                    self.status = "设备已断开"
            else:
                self.ever_connected = True
                self.no_device = False
                self.status = (
                    "画面采集中（兼容抓帧）" if self._scrcpy_disabled
                    else "画面采集中（正在启动 H264 视频）"
                )
        if conn is None:
            task = self._scrcpy_connect_task
            if task is not None:
                task.cancel()
                self._scrcpy_connect_task = None
            self.request_close_scrcpy()

    def set_quality(self, clear=True):
        """切换画质模式。clear=True：关闭识别，最高清最低延迟。"""
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
            return {
                "status": self.status,
                "port": self.port,
                "has_frame": self.latest is not None,
                "url": f"http://127.0.0.1:{self.port}/stream",
            }

    # ---------- 控制通道（scrcpy 输入注入） ----------
    def has_control(self):
        dev = self._scrcpy_device
        return bool(dev is not None and dev.is_connected)

    def get_control_size(self):
        dev = self._scrcpy_device
        if dev is None or not dev.is_connected:
            return None
        try:
            return dev.get_screen_size()
        except Exception:
            return None

    def tap(self, x, y, timeout=2.0):
        """通过 scrcpy 控制通道点击；未建通道时返回 False 交给兼容实现。"""
        dev = self._scrcpy_device
        if dev is None or not dev.is_connected:
            return False
        return self._run_control(dev.click, int(x), int(y), timeout=timeout)

    def swipe(self, x1, y1, x2, y2, timeout=4.0):
        """通过 scrcpy 控制通道滑动，带插帧，比 input swipe 更顺滑。"""
        dev = self._scrcpy_device
        if dev is None or not dev.is_connected:
            return False
        return self._run_control(
            dev.swipe,
            int(x1), int(y1), int(x2), int(y2),
            timeout=timeout,
        )

    def _run_control(self, func, *args, timeout=2.0):
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(func(*args), loop)
            future.result(timeout=timeout)
            return True
        except Exception:
            return False

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

            got_frame = False

            # 1) scrcpy 连接放到后台任务，连接期间不阻塞截图，避免卡顿
            dev = self._scrcpy_device
            if not self._scrcpy_disabled:
                if dev is not None and dev.is_connected:
                    got_frame = await self._grab_scrcpy_frame()
                    dev = self._scrcpy_device
                    # Android 17 上内置 scrcpy-server 会直接 Aborted；一旦会话断掉
                    # 就永久停用 H264，只走兼容截图，不再反复启动服务。
                    if not got_frame and (dev is None or not dev.is_connected):
                        self._disable_scrcpy()
                else:
                    self._maybe_start_scrcpy(conn)

            # 2) 兼容抓帧：scrcpy 未就绪、不可用时立即顶上
            if not got_frame and conn is not None:
                got_frame = await self._grab_conn(conn)
                if not got_frame:
                    self.attach_conn(None)
                    continue

            with self.lock:
                interval = self.frame_interval
            await asyncio.sleep(interval if got_frame else (0.02 if conn else 0.8))

    def _maybe_start_scrcpy(self, conn):
        """在抓帧后台任务里异步尝试 H264，不占用当前帧的采集时间。"""
        if self._scrcpy_disabled or self.no_device or self._scrcpy_connect_task is not None:
            return
        now = time.monotonic()
        if now < self._scrcpy_retry_at:
            return
        self._scrcpy_retry_at = now + SCRCPY_RETRY_SECONDS
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        self._scrcpy_connect_task = loop.create_task(self._ensure_scrcpy(conn))

    def _disable_scrcpy(self):
        """标记 H264 不兼容，停止后续重连，永久回到快速兼容抓帧。"""
        if self._scrcpy_disabled:
            return
        self._scrcpy_disabled = True
        self._scrcpy_retry_at = float("inf")
        self.set_status("画面采集中（H264 不兼容，兼容抓帧）")
        self.request_close_scrcpy()

    async def _ensure_scrcpy(self, conn):
        """后台建立一条 scrcpy(H264) 会话；失败即永久停用，不阻塞抓帧。"""
        global _scrcpy_fps_set
        try:
            if self.no_device or self._scrcpy_disabled:
                return False
            dev = self._scrcpy_device
            if dev is not None and dev.is_connected:
                return True
            from adb_scr import (
                AndroidDevice, ConnectionOptions, adb_devices, init_lib, set_screen_record_fps,
            )
            from mobileauto import inspector

            _patch_scrcpy_no_audio()
            adb = inspector.find_adb()
            if not adb:
                self._disable_scrcpy()
                return False
            # 当前内置 scrcpy-server 在 Android 16/17 的 DisplaySizeMonitor 上会
            # 抛 AbstractMethodError 并直接 Aborted，提前识别并跳过，避免反复启动。
            import subprocess
            api = await asyncio.to_thread(
                lambda: subprocess.run(
                    [adb, "shell", "getprop", "ro.build.version.sdk"],
                    capture_output=True,
                    timeout=5,
                )
            )
            sdk = int("".join(c for c in api.stdout.decode("ascii", "ignore") if c.isdigit()) or 0)
            if sdk >= 36:
                self._disable_scrcpy()
                return False
            await init_lib(adb_path=adb)
            if not _scrcpy_fps_set:
                set_screen_record_fps(60)
                _scrcpy_fps_set = True
            serials = list(await adb_devices() or [])
            serial = self._pick_serial(conn, serials)
            if not serial:
                self._disable_scrcpy()
                return False
            # 关闭主动 adb 探活，避免并发抓 UI 层级时误判掉线并反复重建会话
            options = ConnectionOptions(probe_interval=None, io_timeout=10.0)
            device = AndroidDevice(
                serial, "tcp" if ":" in serial else "usb", options=options
            )
            if not await device.connect():
                self._disable_scrcpy()
                return False
            self._scrcpy_device = device
            self._scrcpy_serial = serial
            self.set_status(
                "高清画面（scrcpy H264）" if self.clear_mode else "画面采集中（scrcpy H264）"
            )
            return True
        except Exception:
            self._disable_scrcpy()
            return False
        finally:
            self._scrcpy_connect_task = None

    @staticmethod
    def _pick_serial(conn, serials):
        """优先复用工作台当前设备；u2 的 7912 端口映射回 adb 的 5555。"""
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

    async def _grab_scrcpy_frame(self):
        dev = self._scrcpy_device
        if dev is None or not dev.is_connected:
            return False
        try:
            with self.lock:
                quality = self.jpeg_quality
            jpg = await dev.get_screenshot_jpg(quality=quality)
            if not jpg:
                return False
            self._set_frame(jpg)
            return True
        except Exception:
            return False

    async def _grab_conn(self, conn):
        """兼容抓帧：u2 直接取设备 JPEG，adb 用 screencap 后二次编码。"""
        try:
            with self.lock:
                quality = self.jpeg_quality
            if conn.get("mode") == "u2":
                # u2 内部本来就是先拿 JPEG 再解码，这里直接用原始 JPEG，
                # 省掉 PIL/cv2 解码再编码，帧率和延迟都会明显改善。
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
        dev = self._scrcpy_device
        self._scrcpy_device = None
        self._scrcpy_serial = ""
        if dev is None:
            return
        try:
            await dev.disconnect()
        except Exception:
            pass

    def request_close_scrcpy(self):
        """从任意线程请求关闭 scrcpy 会话（抓帧循环在同一事件循环上执行）。"""
        loop = self._loop
        if loop is None or loop.is_closed() or self._scrcpy_device is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._close_scrcpy(), loop)
        except Exception:
            pass

    # ---------- 本地 HTTP 推流 ----------
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
                        # 新帧到达立即推送；没有新帧时定时重发，维持浏览器连接
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


def has_control():
    return bool(_stream and _stream.has_control())


def get_control_size():
    return _stream.get_control_size() if _stream else None


def tap(x, y):
    return bool(_stream and _stream.tap(x, y))


def swipe(x1, y1, x2, y2):
    return bool(_stream and _stream.swipe(x1, y1, x2, y2))
