"""The test harness.

━━━ Two conventions ━━━

1. **No network.** What is tested here is the arithmetic and semantics of the processing layer,
   not whether an upstream endpoint is alive. Reachability is a runtime matter, and putting it in
   unit tests only makes them go red with the market. (What each source actually does, and what was
   measured when, lives in that module's own docstring.)

2. **Never touch the user's real database.** `modules/db.py` defaults to `~/.floorzero/history.db`,
   and one test run would dirty months of accrued open-interest history — history that **cannot be backfilled**.
   So every test that stores anything goes through `tmp_db`, pointed at a throwaway directory,
   and **proves the isolation works before letting anything through** (see below).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# So `from modules import ...` works directly (tests run from anywhere in the repo)
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point SQLite at a throwaway directory, and **prove that actually took effect first**.

    ⚠️ `db.py` computes `DB_PATH` from the environment **at import time**, so setting the variable
    is not enough — the modules have to be reloaded with it, or the tests write into the user's real
    database regardless. The trap is quiet: every test passes, having polluted someone's data on the way.

    ⚠️ Worse, **reloading alone is not enough either**. Should `db.py` one day misspell the variable
    or go back to a hardcoded `~/.floorzero`, this fixture would wave it through without a word,
    and the first `record()` would pollute real history — which cannot be backfilled.
    So it is **fail-closed**: before letting anything through it asserts that `DB_PATH` really does
    land inside the temporary directory, and aborts on the spot rather than running anyway.
    """
    monkeypatch.setenv("FZ_DATA_DIR", str(tmp_path))
    db = _reload_db_stack()

    # ⭐ The isolation proves itself. This one line is why the fixture exists.
    resolved = Path(db.DB_PATH).resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), (
        f"Isolation failed: DB_PATH is at {resolved}, not inside the temporary directory {tmp_path}.\n"
        f"Aborting before anything is written — the history in the user's ~/.floorzero cannot be backfilled.\n"
        f"Most likely db.py no longer honours FZ_DATA_DIR.")

    yield tmp_path
    monkeypatch.delenv("FZ_DATA_DIR", raising=False)
    _reload_db_stack()


def _reload_db_stack():
    """Reload `db` **and every already-imported module that depends on it**.

    ⚠️ No hardcoded list: add a `*_store` later and a hand-written list misses it,
    while the missed module still points at the real database — "all tests green, data dirtied" all over again.
    This decides by whether a module holds a reference named `db`, so it discovers them itself.
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
    """Supply a placeholder contact.

    At runtime an unset `FZ_CONTACT` fails fast, which is deliberate;
    the tests need not act that out in every case, and a dedicated case covers it.
    """
    monkeypatch.setenv("FZ_CONTACT", "Test Runner test@example.invalid")
