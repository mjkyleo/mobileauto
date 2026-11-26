"""统一入口：pywebview 手机自动化工作台（GUI / 打包共用）。"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mobileauto.web_gui import main

if __name__ == "__main__":
    main()
