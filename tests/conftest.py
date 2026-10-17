from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from last_heartbeat.config import Config
from last_heartbeat.control import Control
from last_heartbeat.engine import DAY, Engine
from last_heartbeat.store import Store


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "heartbeat.db")
    now = SimpleNamespace(value=1_800_000_000.0)
    config = Config(123, "hash", "123:token", 10, tmp_path, "Asia/Hong_Kong")
    telegram = SimpleNamespace(
        session_age=AsyncMock(return_value=2 * DAY),
        revoke_sessions=AsyncMock(return_value="done"),
        send=AsyncMock(return_value=100),
        check_handover=AsyncMock(return_value="Group → Friend"),
        handover=AsyncMock(return_value="admin"),
        reconcile_handover=AsyncMock(return_value=None),
        person=AsyncMock(return_value=(20, "Friend")),
        group=AsyncMock(return_value=(-100123, "Group")),
    )
    engine = Engine(store, telegram, config, lambda: now.value)
    engine.connected()
    code = store.set_recovery("a-long-recovery-code-for-tests")
    with store.db:
        store.put("recovery_saved", True)
        store.db.execute("INSERT INTO people VALUES (20,'Friend','friend')")
        store.db.execute("INSERT INTO groups VALUES (-100123,'Group')")
    app = SimpleNamespace(
        store=store,
        now=now,
        config=config,
        telegram=telegram,
        engine=engine,
        control=Control(engine),
        code=code,
    )
    yield app
    app.store.close()


async def activate(app):
    await app.engine.arm()
    app.now.value += 3 * DAY
    await app.engine.tick()
