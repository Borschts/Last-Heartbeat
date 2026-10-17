import hashlib
import hmac
import json
import secrets
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            raise RuntimeError("This database needs a newer Last Heartbeat version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'recipient'
            );
            CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recipient INTEGER NOT NULL, body TEXT NOT NULL,
                trigger TEXT NOT NULL, delay INTEGER NOT NULL DEFAULT 0,
                unit TEXT NOT NULL DEFAULT 'days', interval INTEGER NOT NULL DEFAULT 0,
                count INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                key TEXT PRIMARY KEY, epoch TEXT NOT NULL, message_id INTEGER,
                peer INTEGER NOT NULL, body TEXT NOT NULL, due REAL NOT NULL,
                random_id INTEGER NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'pending',
                error TEXT, remote_id INTEGER, attempt_at REAL, retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS seen (epoch TEXT, peer INTEGER, PRIMARY KEY(epoch, peer));
            CREATE TABLE IF NOT EXISTS handovers (
                token TEXT PRIMARY KEY, actor INTEGER NOT NULL, group_id INTEGER NOT NULL,
                target INTEGER NOT NULL, epoch TEXT NOT NULL, expires REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'prepared', result TEXT, retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS recovery_attempts (
                actor TEXT PRIMARY KEY, failures INTEGER NOT NULL, retry_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notices (
                key TEXT PRIMARY KEY, body TEXT NOT NULL, due REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
            );
            PRAGMA user_version=1;
        """)
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state='uncertain', error='interrupted' WHERE state='sending'"
            )
            self.db.execute(
                "UPDATE handovers SET state='uncertain', result='interrupted' WHERE state='running'"
            )
            self.db.execute("UPDATE notices SET state='uncertain' WHERE state='sending'")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    @property
    def state(self):
        return self.get(
            "state",
            {
                "mode": "paused",
                "epoch": "",
                "heartbeat": 0,
                "deadline": 0,
                "activated": 0,
                "healthy": 0,
            },
        )

    def notice(self, key, body, now):
        self.db.execute(
            "INSERT OR IGNORE INTO notices(key,body,due) VALUES (?,?,?)", (key, body, now)
        )

    def trusted(self, actor):
        return bool(
            self.db.execute(
                "SELECT 1 FROM people WHERE id=? AND role IN ('friend','family')", (actor,)
            ).fetchone()
        )

    def set_recovery(self, code=None):
        code = code or secrets.token_urlsafe(32)
        if len(code) < 20 or len(code) > 256 or any(c.isspace() for c in code):
            raise ValueError("解除碼須為 20–256 字元，不能包含空白。")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(code.encode(), salt=salt, n=16384, r=8, p=1).hex()
        with self.db:
            self.put("recovery", {"salt": salt.hex(), "hash": digest})
            self.put("recovery_saved", False)
        return code

    def recover(self, actor, code, now):
        key = self.get("recovery")
        attempt = self.db.execute(
            "SELECT * FROM recovery_attempts WHERE actor=?", (str(actor),)
        ).fetchone()
        if attempt and attempt["retry_at"] > now:
            return False
        valid = False
        if key and 20 <= len(code) <= 256:
            digest = hashlib.scrypt(
                code.encode(), salt=bytes.fromhex(key["salt"]), n=16384, r=8, p=1
            ).hex()
            valid = hmac.compare_digest(digest, key["hash"])
        with self.db:
            if not valid:
                failures = min((attempt["failures"] if attempt else 0) + 1, 10)
                self.db.execute(
                    "INSERT OR REPLACE INTO recovery_attempts VALUES (?,?,?)",
                    (str(actor), failures, now + min(300, 2**failures)),
                )
                return False
            self.pause()
            self.put("recovery", None)
            self.put("draft", None)
            self.db.execute("DELETE FROM recovery_attempts")
            self.notice(
                "recover:" + secrets.token_hex(8),
                "接管已解除。請重新設定解除碼，再由本人啟用 Heartbeat。",
                now,
            )
        return True

    def pause(self):
        state = self.state
        state["mode"] = "paused"
        state["revision"] = state.get("revision", 0) + 1
        self.put("state", state)
        self.db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending'")
        self.db.execute(
            "UPDATE handovers SET state='cancelled' WHERE state IN ('prepared','queued')"
        )

    def add_message(
        self, recipient, body, trigger, delay=0, unit="days", interval=0, count=1, ident=None
    ):
        if not body.strip() or len(body.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("訊息須為 1–4096 字元。")
        if trigger not in ("activation", "schedule", "first_dm") or unit not in ("days", "months"):
            raise ValueError("發送方式無效。")
        if not 0 <= delay <= 120 or not 0 <= interval <= 120 or not 1 <= count <= 120:
            raise ValueError("間隔及次數須在 1–120 之間，首次延遲可為 0。")
        if count > 1 and not interval:
            raise ValueError("重複發送須設定月份間隔。")
        if recipient == 0 and trigger != "first_dm":
            raise ValueError("通用回覆只適用於首次私訊。")
        if ident is None and self.db.execute("SELECT count(*) FROM messages").fetchone()[0] >= 100:
            raise ValueError("最多可儲存 100 則訊息。")
        with self.db:
            self.put("draft", None)
            if ident is not None:
                self.db.execute(
                    "UPDATE messages SET recipient=?,body=?,trigger=?,delay=?,unit=?,interval=?,count=? WHERE id=?",
                    (recipient, body, trigger, delay, unit, interval, count, ident),
                )
                return ident
            return self.db.execute(
                "INSERT INTO messages(recipient,body,trigger,delay,unit,interval,count) VALUES (?,?,?,?,?,?,?)",
                (recipient, body, trigger, delay, unit, interval, count),
            ).lastrowid

    def queue(self, key, epoch, message, peer, due):
        self.db.execute(
            """INSERT OR IGNORE INTO deliveries
            (key,epoch,message_id,peer,body,due,random_id) VALUES (?,?,?,?,?,?,?)""",
            (key, epoch, message["id"], peer, message["body"], due, secrets.randbits(63)),
        )

    def close(self):
        self.db.close()
