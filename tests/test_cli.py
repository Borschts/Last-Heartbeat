import json
import os
import subprocess
import sys
import time

import pytest

from last_heartbeat.config import Config, ProcessLock


def command(app, name):
    return subprocess.run(
        [sys.executable, "-m", "last_heartbeat.cli", name],
        env={**os.environ, "DATA_DIR": str(app.config.data_dir)},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_status_and_healthcheck_read_running_database(app):
    with app.store.db:
        app.store.put("state", {**app.store.state, "mode": "armed"})
        app.store.put("health", time.time())
    with ProcessLock(app.config.data_dir):
        status = command(app, "status")
        assert status.returncode == 0
        assert json.loads(status.stdout)["mode"] == "armed"
        assert command(app, "healthcheck").returncode == 0
    with app.store.db:
        app.store.put("health", time.time() - 120)
    assert command(app, "healthcheck").returncode == 1


def test_two_processes_cannot_use_same_account_data(app):
    with ProcessLock(app.config.data_dir):
        result = command(app, "run")
    assert result.returncode == 1
    assert "Another Last Heartbeat process" in result.stderr


def test_login_can_discover_owner_but_service_requires_it(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
    monkeypatch.setenv("OWNER_ID", "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert Config.load(require_owner=False).owner_id == 0
    with pytest.raises(ValueError):
        Config.load()


def test_secrets_can_be_loaded_from_files(monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text("123:secret\n", encoding="utf-8")
    for name, value in {
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "hash",
        "OWNER_ID": "10",
        "TELEGRAM_BOT_TOKEN_FILE": str(path),
    }.items():
        monkeypatch.setenv(name, value)
    assert Config.load().bot_token == "123:secret"
