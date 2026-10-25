"""设置向导入口：双击 `设置向导.vbs`（或直接双击本文件）打开。"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if __name__ == "__main__":
    from gui.setup_app import main

    raise SystemExit(main())
