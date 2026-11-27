# -*- coding: utf-8 -*-
"""双击这个文件（或 启动.bat）即可运行。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wxpopup.app import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
