"""日志初始化：命令行和双击启动器共用一份，行为保持一致。

以前 login.py 和 launcher.pyw 各写了一份 setup_logging，格式相同但
**只有 login.py 那份会轮转**——开机自启走的正是 launcher，所以日志只增不减。
"""

from __future__ import annotations

import logging
import os
import sys

from . import paths

MAX_BYTES = 1024 * 1024
FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def _rotate() -> None:
    try:
        if os.path.exists(paths.LOG_FILE) and os.path.getsize(paths.LOG_FILE) > MAX_BYTES:
            os.replace(paths.LOG_FILE, paths.LOG_FILE + ".1")
    except OSError:
        pass


def setup_logging(quiet: bool = False, level: int = logging.INFO) -> logging.Logger:
    """配好 campusnet 这个 logger：一直写文件，`quiet=False` 时再往控制台回显。"""
    paths.ensure_log_dir()
    _rotate()

    root = logging.getLogger("campusnet")
    root.setLevel(logging.DEBUG)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(paths.LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        root.addHandler(fh)
    if not quiet and not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers
    ):
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(sh)
    return root
