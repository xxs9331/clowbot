"""pytest 共享配置：把项目根加进 sys.path，让 `import handlers.* / utils.*` 在
   `pytest tests/` 与 `pytest tests/test_xxx.py` 两种调用下都解析得到。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
