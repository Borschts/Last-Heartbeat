import argparse
import asyncio
import getpass
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from .config import Config, ProcessLock
from .store import Store


async def login(config):
    from .telegram import Telegram

    telegram = Telegram(config)
    try:
        await telegram.client.start(
            phone=lambda: input("Phone number: "),
            password=lambda: getpass.getpass("Telegram 2FA: "),
        )
        me = await telegram.client.get_me()
        if (config.owner_id and me.id != config.owner_id) or me.bot:
            raise ValueError(f"This account is {me.id}. Check OWNER_ID before continuing.")
        await telegram.client.get_dialogs(limit=100)
        print(f"OWNER_ID={me.id}")
        print("Save OWNER_ID in .env, then start the service and open the control bot.")
    finally:
        await telegram.client.disconnect()


async def serve(store, config):
    from .runtime import Runtime

    runtime = Runtime(store, config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.stop.set)
        except NotImplementedError:
            pass
    await runtime.run()


def main():
    parser = argparse.ArgumentParser(prog="last-heartbeat")
    parser.add_argument(
        "command",
        choices=("run", "login", "recover", "status", "healthcheck"),
        nargs="?",
        default="run",
    )
    args = parser.parse_args()
    os.umask(0o077)
    data = Path(os.getenv("DATA_DIR", "data")).resolve()
    if args.command == "healthcheck":
        try:
            db = sqlite3.connect((data / "heartbeat.db").as_uri() + "?mode=ro", uri=True)
            import json

            row = db.execute("SELECT value FROM settings WHERE key='health'").fetchone()
            db.close()
            return 0 if row and 0 <= time.time() - json.loads(row[0]) < 90 else 1
        except (sqlite3.Error, OSError):
            return 1
    if args.command == "status":
        import json

        try:
            db = sqlite3.connect((data / "heartbeat.db").as_uri() + "?mode=ro", uri=True)
            row = db.execute("SELECT value FROM settings WHERE key='state'").fetchone()
            print(json.dumps(json.loads(row[0]) if row else {"mode": "paused"}, indent=2))
            db.close()
            return 0
        except sqlite3.Error:
            print("No database found.", file=sys.stderr)
            return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    try:
        with ProcessLock(data):
            if args.command == "login":
                asyncio.run(login(Config.load(require_owner=False)))
                return 0
            store = Store(data / "heartbeat.db")
            try:
                if args.command == "recover":
                    code = getpass.getpass("Recovery code: ")
                    if not store.recover("local", code, time.time()):
                        print("Invalid recovery code or too many attempts.", file=sys.stderr)
                        return 1
                    print("Takeover stopped. Set a new recovery code before arming again.")
                else:
                    asyncio.run(serve(store, Config.load()))
            finally:
                store.close()
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        # Telegram exceptions may contain message bodies or credentials.
        print(f"Startup failed: {type(error).__name__}", file=sys.stderr)
        if isinstance(error, (ValueError, RuntimeError)):
            print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
