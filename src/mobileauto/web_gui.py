"""pywebview 工作台入口：Python 后端 + HTML/CSS/JS 前端。"""

import importlib
import os
import sys

from mobileauto.paths import WEB_INDEX
from mobileauto.stream import start_stream
from mobileauto.web_api import WorkbenchApi


def main():
    if not os.path.isfile(WEB_INDEX):
        raise FileNotFoundError(f"找不到 Web 页面：{WEB_INDEX}")

    webview = importlib.import_module("webview")
    start_stream()
    api = WorkbenchApi()

    window = webview.create_window(
        "手机自动化工作台",
        url=WEB_INDEX,
        js_api=api,
        width=1500,
        height=980,
        min_size=(1180, 780),
        resizable=True,
    )
    api.bind_window(webview, window)  # 供「另存为」等原生对话框使用
    webview.start()


if __name__ == "__main__":
    main()
