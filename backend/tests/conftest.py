"""测试底座。

━━━ 两条约定 ━━━

1. **不打网络。** 这里测的是加工层的算术与语义，不是上游接口还活着没有。
   上游是否可达属于运行时的事，写进单元测试只会让测试随行情变红。
   （数据源层的实况以及"哪年哪月实测到什么"记在 `docs/开发日志.md`。）

2. **不碰用户的真实数据库。** `modules/db.py` 默认落 `~/.vibe-flow/history.db`，
   跑一次测试就把人家攒了几个月的持仓量历史写脏了 —— 而那份历史**补不回来**。
   所以每个用到落库的测试都走 `tmp_db`，指向一次性目录。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# 让 `from modules import ...` 能直接用（测试从仓库任意位置都跑得起来）
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把 SQLite 指到一次性目录。

    ⚠️ `db.py` 在**导入时**就读了环境变量算出 `DB_PATH`，所以只设环境变量
    是不够的 —— 必须连模块一起重载，否则测试照样写进用户的真实库。
    这个坑不显眼：测试会全部通过，只是顺手污染了别人的数据。
    """
    monkeypatch.setenv("VF_DATA_DIR", str(tmp_path))
    from modules import db
    importlib.reload(db)
    # 依赖 db 的模块持有的是旧引用，一并重载
    for name in ("flow_store", "scanner_store", "market_store"):
        mod = sys.modules.get(f"modules.{name}")
        if mod is not None:
            importlib.reload(mod)
    # 已建过表的记录要清掉，否则新库上不会重建
    db._applied.clear()
    yield tmp_path
    monkeypatch.delenv("VF_DATA_DIR", raising=False)
    importlib.reload(db)
    db._applied.clear()


@pytest.fixture(autouse=True)
def _contact(monkeypatch):
    """给个占位联系方式。

    真实运行时 `VF_CONTACT` 未配会 fail-fast（那是刻意的）；
    测试里不需要每个用例都去演一遍那件事，另有专门的用例测它。
    """
    monkeypatch.setenv("VF_CONTACT", "Test Runner test@example.invalid")
