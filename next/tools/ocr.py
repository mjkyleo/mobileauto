"""独立 OCR 辅助进程：输入图片路径，输出 JSON 文字列表。"""

import json
import sys

from rapidocr_onnxruntime import RapidOCR


def main():
    if len(sys.argv) < 2:
        print("[]")
        return
    engine = RapidOCR()
    result, _ = engine(sys.argv[1])
    items = []
    for box, text, score in result or []:
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        items.append({
            "text": text,
            "score": float(score),
            "x": int(sum(xs) / len(xs)),
            "y": int(sum(ys) / len(ys)),
        })
    print(json.dumps(items, ensure_ascii=False))


if __name__ == "__main__":
    main()
