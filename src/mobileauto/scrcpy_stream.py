"""scrcpy v5 服务端直连：H264 裸包 -> WebSocket -> WebCodecs。

保留工作台原有的 UI 层级识别、录制与任务执行；这里只替换视频与控制链路。
协议参考 scrcpy doc/develop.md，服务端二进制来自官方 release v5.0.1。
"""

import asyncio
import os
import random
import re
import struct
import time
from collections import deque

import websockets

SERVER_VERSION = "5.0.1"
SERVER_ASSET = os.path.join(os.path.dirname(__file__), "assets", f"scrcpy-server-{SERVER_VERSION}")
DEVICE_SERVER = f"/data/local/tmp/scrcpy-server-{SERVER_VERSION}.jar"
WS_PORT = 8766
FORWARD_PORT = 27183 + random.randint(0, 500)

_START_CODE = re.compile(b"\x00\x00\x00?\x01")


def _split_annexb(data):
    parts = [part for part in _START_CODE.split(data) if part.strip(b"\x00")]
    return [part.rstrip(b"\x00") for part in parts if part]


def _to_avcc(data):
    nals = _split_annexb(data)
    return b"".join(len(nal).to_bytes(4, "big") + nal for nal in nals)


class ScrcpyStream:
    """管理 scrcpy-server、adb forward、视频包解析和 WebSocket 广播。"""

    def __init__(self, adb, serial, ws_port=WS_PORT, log=None):
        self.adb = adb
        self.serial = serial
        self.ws_port = ws_port
        self.log = log or (lambda text: None)
        self.scid = random.randint(0, 2**31 - 1)
        self.socket_name = f"scrcpy_{self.scid:08x}"
        self.running = False
        self.has_frame = False
        self.width = 0
        self.height = 0
        self.description = b""
        self.clients = set()
        self._send_tasks = {}
        self._frame_times = deque()
        self._loop = None
        self._proc = None
        self._ws_server = None
        self._video_reader = None
        self._video_writer = None
        self._control_writer = None
        self._video_task = None
        self._drain_task = None

    async def _adb(self, *args, timeout=15):
        try:
            proc = await asyncio.create_subprocess_exec(
                self.adb, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode == 0, (err or out).decode("utf-8", "replace").strip()
        except Exception as e:
            return False, str(e)

    async def start(self):
        if self.running:
            return True
        self._loop = asyncio.get_running_loop()
        if not os.path.isfile(SERVER_ASSET):
            self.log(f"缺少 scrcpy-server：{SERVER_ASSET}")
            return False

        ok, msg = await self._adb("push", SERVER_ASSET, DEVICE_SERVER)
        if not ok:
            self.log(f"推送 scrcpy-server 失败：{msg}")
            return False
        await self._adb("forward", "--remove", f"tcp:{FORWARD_PORT}", timeout=5)
        ok, msg = await self._adb(
            "forward", f"tcp:{FORWARD_PORT}", f"localabstract:{self.socket_name}"
        )
        if not ok:
            self.log(f"adb forward 失败：{msg}")
            return False

        try:
            self._ws_server = await websockets.serve(
                self._ws_client, "127.0.0.1", self.ws_port, max_size=None
            )
        except Exception as e:
            self.log(f"WebSocket 服务启动失败：{e}")
            return False

        cmd = [
            self.adb, "-s", self.serial, "shell",
            f"CLASSPATH={DEVICE_SERVER}", "app_process", "/",
            "com.genymobile.scrcpy.Server", SERVER_VERSION,
            f"scid={self.scid:08x}",
            "tunnel_forward=true",
            "video=true", "video_codec=h264",
            "audio=false", "control=true",
            "send_device_meta=false", "send_frame_meta=true",
            "send_dummy_byte=false", "send_stream_meta=false",
            "max_fps=60", "video_bit_rate=8000000", "max_size=1920",
            "log_level=warn",
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except Exception as e:
            self.log(f"启动 scrcpy-server 失败：{e}")
            await self.stop()
            return False
        self._drain_task = asyncio.create_task(self._drain_server())

        try:
            self._video_reader, self._video_writer = await self._open_forward()
            self._control_reader, self._control_writer = await self._open_forward()
        except Exception as e:
            self.log(f"连接 scrcpy 通道失败：{e}")
            await self.stop()
            return False

        self.running = True
        self._video_task = asyncio.create_task(self._read_video())
        self.log(f"H264 视频链路已建立：{self.serial}（WebSocket :{self.ws_port}）")
        return True

    async def _open_forward(self, timeout=12.0):
        deadline = asyncio.get_running_loop().time() + timeout
        last = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await asyncio.open_connection("127.0.0.1", FORWARD_PORT)
            except Exception as e:
                last = e
                await asyncio.sleep(0.2)
        raise last or RuntimeError("adb forward 连接超时")

    async def _drain_server(self):
        if not self._proc or not self._proc.stdout:
            return
        try:
            async for line in self._proc.stdout:
                text = line.decode("utf-8", "replace").strip()
                if text and ("ERROR" in text or "WARN" in text):
                    self.log(f"scrcpy: {text[:240]}")
        except Exception:
            pass

    async def _ws_client(self, ws):
        self.clients.add(ws)
        try:
            if self.description:
                await ws.send(b"\x00" + self.description)
            await ws.wait_closed()
        finally:
            self.clients.discard(ws)

    def _broadcast(self, data):
        """只保留每个客户端一个待发送帧，旧帧直接丢弃，避免背压拖慢抓帧。"""
        for ws in list(self.clients):
            task = self._send_tasks.get(ws)
            if task is not None and not task.done():
                continue
            self._send_tasks[ws] = asyncio.create_task(self._safe_send(ws, data))

    async def _safe_send(self, ws, data):
        try:
            await asyncio.wait_for(ws.send(data), timeout=0.1)
        except Exception:
            self.clients.discard(ws)
        finally:
            self._send_tasks.pop(ws, None)

    def sent_fps(self):
        now = time.monotonic()
        while self._frame_times and now - self._frame_times[0] > 2.0:
            self._frame_times.popleft()
        if not self._frame_times:
            return 0
        span = now - self._frame_times[0]
        return round(len(self._frame_times) / max(span, 0.1), 1)

    async def _read_video(self):
        buf = b""
        try:
            while self.running and self._video_reader:
                chunk = await self._video_reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 12:
                    first = int.from_bytes(buf[:8], "big")
                    if first >> 63:
                        self.width = int.from_bytes(buf[4:8], "big") & 0x7FFFFFFF
                        self.height = int.from_bytes(buf[8:12], "big")
                        buf = buf[12:]
                        continue
                    config = bool((first >> 62) & 1)
                    key = bool((first >> 61) & 1)
                    size = int.from_bytes(buf[8:12], "big")
                    if len(buf) < 12 + size:
                        break
                    payload = buf[12:12 + size]
                    buf = buf[12 + size:]
                    if config:
                        self._update_description(payload)
                    elif payload:
                        self.has_frame = True
                        self._frame_times.append(time.monotonic())
                        self._broadcast(b"\x01" + (b"\x01" if key else b"\x00") + _to_avcc(payload))
        except Exception as e:
            self.log(f"H264 视频读取结束：{e}")
        finally:
            self.has_frame = False

    def _update_description(self, payload):
        sps_pps = []
        for nal in _split_annexb(payload):
            if nal and nal[0] & 0x1F in (7, 8):
                sps_pps.append(len(nal).to_bytes(4, "big") + nal)
        description = b"".join(sps_pps)
        if description and description != self.description:
            self.description = description
            self._broadcast(b"\x00" + description)

    def has_control(self):
        return bool(self.running and self._control_writer is not None)

    def get_screen_size(self):
        if self.width and self.height:
            return self.width, self.height
        return None

    def _control(self, data):
        if not self.has_control() or self._loop is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(self._send_control(data), self._loop)
            future.result(timeout=2.0)
            return True
        except Exception:
            return False

    async def _send_control(self, data):
        if not self._control_writer:
            return False
        self._control_writer.write(data)
        await self._control_writer.drain()
        return True

    def tap(self, x, y):
        return self._control(self._touch(0, x, y)) and self._control(self._touch(1, x, y))

    def keyevent(self, keycode, action=0):
        data = struct.pack(">BBIII", 0, action, keycode, 0, 0)
        return self._control(data)

    def _touch(self, action, x, y):
        w, h = self.get_screen_size() or (1080, 1920)
        return struct.pack(
            ">BBQiiHHHII", 2, action, 1, int(x), int(y), w, h, 0xFFFF, 1, 1
        )

    def swipe(self, x1, y1, x2, y2):
        async def _run():
            for action, x, y, delay in (
                (0, x1, y1, 0.01), (2, x1, y1, 0.01),
                (2, (x1 + x2) // 2, (y1 + y2) // 2, 0.05),
                (2, x2, y2, 0.05), (1, x2, y2, 0),
            ):
                if not await self._send_control(self._touch(action, x, y)):
                    return False
                if delay:
                    await asyncio.sleep(delay)
            return True
        if not self.has_control() or self._loop is None:
            return False
        try:
            return asyncio.run_coroutine_threadsafe(_run(), self._loop).result(timeout=3.0)
        except Exception:
            return False

    async def stop(self):
        self.running = False
        for task in (self._video_task, self._drain_task):
            if task:
                task.cancel()
        for writer in (self._video_writer, self._control_writer):
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        await self._adb("forward", "--remove", f"tcp:{FORWARD_PORT}", timeout=5)
        self._video_writer = None
        self._control_writer = None
        self._proc = None
        self.has_frame = False
