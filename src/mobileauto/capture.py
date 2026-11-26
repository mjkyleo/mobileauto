"""单帧页面采集：截图 + OCR 文字 + 控件树，结果落盘并直接返回前端展示。"""

import os
import time
from datetime import datetime

from mobileauto.paths import PAGE_DATA_DIR, ensure_runtime_dirs

ensure_runtime_dirs()

_ocr_engine = None
_ocr_tried = False


def _get_ocr():
    global _ocr_engine, _ocr_tried
    if _ocr_tried:
        return _ocr_engine
    _ocr_tried = True
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    except Exception:
        _ocr_engine = None
    return _ocr_engine


def ocr_image(img, max_width=960):
    """返回 [{'text','score','cx','cy'}, ...]。max_width 越小越快。"""
    engine = _get_ocr()
    if engine is None:
        return []
    try:
        import cv2

        h, w = img.shape[:2]
        scale = 1.0
        work = img
        if w > max_width:
            scale = float(max_width) / w
            work = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        result, _ = engine(work)
        items = []
        for row in result or []:
            if not row or len(row) < 3:
                continue
            box, text, score = row[0], row[1], float(row[2])
            pts = [(float(p[0]) / scale, float(p[1]) / scale) for p in box]
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            items.append({"text": str(text), "score": round(score, 3), "cx": int(cx), "cy": int(cy)})
        return items
    except Exception:
        return []


def _grab_frame(conn):
    """从连接抓一帧 BGR 图。"""
    if conn is None:
        return None
    if conn.get("mode") == "u2":
        return conn["client"].screenshot(format="opencv")
    import cv2
    import numpy as np
    import subprocess

    result = subprocess.run([conn["client"], "exec-out", "screencap", "-p"], capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    return cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)


def grab_ocr(conn, max_width=960, rotate=False):
    """轻量抓帧 + OCR，供任务循环高频调用，不落盘。

    rotate=True 时先顺时针旋转 90 度再识别，用于横屏广告等场景。
    返回 (items, size)；items 坐标已换算回原始屏幕方向。
    """
    import cv2

    frame = _grab_frame(conn)
    if frame is None:
        return [], (0, 0)
    h, w = frame.shape[:2]
    work = frame
    if rotate:
        work = cv2.rotate(work, cv2.ROTATE_90_CLOCKWISE)
    items = ocr_image(work, max_width=max_width)
    if rotate:
        _, rw = work.shape[:2]
        for item in items:
            item["cx"], item["cy"] = int(item["cy"]), int(rw - 1 - item["cx"])
    return items, (w, h)


def capture_once(conn):
    """采集当前页面，返回前端可直接展示的结果。"""
    import cv2

    frame = _grab_frame(conn)
    if frame is None:
        return {"ok": False, "error": "抓帧失败，请确认设备已连接"}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(PAGE_DATA_DIR, f"{stamp}_单帧采集")
    os.makedirs(out_dir, exist_ok=True)

    img_path = os.path.join(out_dir, "screen.png")
    cv2.imwrite(img_path, frame)

    items = ocr_image(frame)
    ocr_path = os.path.join(out_dir, "ocr.txt")
    with open(ocr_path, "w", encoding="utf-8") as f:
        f.write(f"采集时间: {stamp}\n识别文字数: {len(items)}\n\n")
        for item in items:
            f.write(f"{item['text']}\t{item['score']}\t({item['cx']},{item['cy']})\n")

    rows = []
    try:
        from mobileauto import inspector

        _, rows = inspector.snapshot(conn)
        ui_path = os.path.join(out_dir, "ui.xml")
        with open(ui_path, "w", encoding="utf-8") as f:
            f.write(inspector.dump_hierarchy(conn))
    except Exception:
        pass

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    preview = ""
    if ok:
        import base64

        preview = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")

    return {
        "ok": True,
        "time": stamp,
        "dir": out_dir,
        "preview": preview,
        "ocr": items,
        "clickable": [
            {
                "name": r.get("text") or r.get("desc") or (r.get("resource_id") or "").split("/")[-1] or "按钮",
                "cx": r.get("cx"),
                "cy": r.get("cy"),
            }
            for r in rows
            if str(r.get("clickable", "")).lower() == "true"
        ],
        "ocr_count": len(items),
        "clickable_count": sum(1 for r in rows if str(r.get("clickable", "")).lower() == "true"),
    }
