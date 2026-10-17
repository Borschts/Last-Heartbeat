import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    data_dir: Path
    timezone: str = "Asia/Hong_Kong"
    password_file: Path | None = None

    @classmethod
    def load(cls, require_owner=True):
        def secret(name):
            path = os.getenv(name + "_FILE")
            return (
                Path(path).read_text(encoding="utf-8").strip() if path else os.environ[name].strip()
            )

        try:
            zone = os.getenv("TIMEZONE", "Asia/Hong_Kong")
            ZoneInfo(zone)
            config = cls(
                api_id=int(os.environ["TELEGRAM_API_ID"]),
                api_hash=secret("TELEGRAM_API_HASH"),
                bot_token=secret("TELEGRAM_BOT_TOKEN"),
                owner_id=int(os.getenv("OWNER_ID") or 0),
                data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
                timezone=zone,
                password_file=Path(os.environ["TELEGRAM_2FA_FILE"])
                if os.getenv("TELEGRAM_2FA_FILE")
                else None,
            )
            if (
                config.api_id <= 0
                or (require_owner and config.owner_id <= 0)
                or not config.bot_token
                or not config.api_hash
            ):
                raise ValueError("Invalid account settings")
            return config
        except (KeyError, ValueError) as error:
            raise ValueError(
                "Check TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN and OWNER_ID"
            ) from error


class ProcessLock:
    def __init__(self, directory):
        self.path = Path(directory) / "process.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another Last Heartbeat process is using this directory") from None
        return self

    def __exit__(self, *_):
        self.file.close()
