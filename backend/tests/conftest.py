"""测试底座。

━━━ 两条约定 ━━━

1. **不打网络。** 这里测的是加工层的算术与语义，不是上游接口还活着没有。
   上游是否可达属于运行时的事，写进单元测试只会让测试随行情变红。
   （数据源层的实况以及"哪年哪月实测到什么"记在 `docs/开发日志.md`。）

2. **不碰用户的真实数据库。** `modules/db.py` 默认落 `~/.vibe-flow/history.db`，
   跑一次测试就把人家攒了几个月的持仓量历史写脏了 —— 而那份历史**补不回来**。
   所以每个用到落库的测试都走 `tmp_db`，指向一次性目录，
   并且**在放行之前先自证隔离生效**（见下）。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# 让 `from modules import ...` 能直接用（测试从仓库任意位置都跑得起来）
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """把 SQLite 指到一次性目录，并**先证明这件事真的生效了**。

    ⚠️ `db.py` 在**导入时**就读环境变量算出 `DB_PATH`，所以只设环境变量
    是不够的 —— 必须连模块一起重载，否则测试照样写进用户的真实库。
    这个坑不显眼：测试会全部通过，只是顺手污染了别人的数据。

    ⚠️ 更要命的是**光重载也不够**。如果哪天 `db.py` 把变量名拼错、
    或改回写死 `~/.vibe-flow`，这个 fixture 会一声不响地放行，
    第一次 `record()` 就污染真实历史 —— 而那份历史补不回来。
    所以这里 **fail-closed**：放行前先断言 `DB_PATH` 确实落在临时目录里，
    不满足就当场终止，绝不"先跑了再说"。
    """
    monkeypatch.setenv("VF_DATA_DIR", str(tmp_path))
    db = _reload_db_stack()

    # ⭐ 隔离自证。这一句是整个 fixture 存在的理由。
    resolved = Path(db.DB_PATH).resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"隔离失效：DB_PATH 落在 {resolved}，不在临时目录 {tmp_path} 里。\n"
        f"在写任何东西之前中止 —— 用户 ~/.vibe-flow 里的历史补不回来。\n"
        f"多半是 db.py 不再认 VF_DATA_DIR 了。")

    yield tmp_path
    monkeypatch.delenv("VF_DATA_DIR", raising=False)
    _reload_db_stack()


def _reload_db_stack():
    """重载 `db` 以及**所有已导入的、依赖它的模块**。

    ⚠️ 不写死名单：以后新增一个 `*_store`，手工名单会漏掉它，
    而漏掉的那个模块仍指向真实库 —— 又是一次"测试全绿、数据被写脏"。
    这里按"模块里有没有 `db` 这个引用"来判断，让它自己发现。
    """
    from modules import db
    importlib.reload(db)
    for name, mod in list(sys.modules.items()):
        if not name.startswith("modules.") or name == "modules.db":
            continue
        if getattr(mod, "db", None) is not None:
            importlib.reload(mod)
    db._applied.clear()
    return db


@pytest.fixture(autouse=True)
def _contact(monkeypatch):
    """给个占位联系方式。

    真实运行时 `VF_CONTACT` 未配会 fail-fast（那是刻意的）；
    测试里不需要每个用例都去演一遍那件事，另有专门的用例测它。
    """
    monkeypatch.setenv("VF_CONTACT", "Test Runner test@example.invalid")
