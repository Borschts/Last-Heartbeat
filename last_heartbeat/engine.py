import asyncio
import calendar
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


DAY = 86400
WINDOW = 3 * DAY


class Retry(Exception):
    def __init__(self, seconds):
        self.seconds = max(1, seconds)


class Rejected(Exception):
    pass


def add_months(date, months):
    year, month = divmod(date.year * 12 + date.month - 1 + months, 12)
    return date.replace(
        year=year, month=month + 1, day=min(date.day, calendar.monthrange(year, month + 1)[1])
    )


def scheduled_at(anchor, message, occurrence, zone):
    date = datetime.fromtimestamp(anchor, ZoneInfo(zone))
    if message["unit"] == "months":
        return add_months(date, message["delay"] + message["interval"] * occurrence).timestamp()
    start = date + timedelta(days=message["delay"])
    return add_months(start, message["interval"] * occurrence).timestamp()


class Engine:
    def __init__(self, store, telegram, config, clock=time.time):
        self.store, self.telegram, self.config, self.clock = store, telegram, config, clock
        self.ready = False
        self.session_check_at = 0
        self.session_retry_at = 0
        self.session_lock = asyncio.Lock()
        self.worker_lock = asyncio.Lock()

    def active(self, epoch=None):
        state = self.store.state
        return state["mode"] == "active" and (epoch is None or state["epoch"] == epoch)

    def allowed(self, epoch):
        return self.ready and self.active(epoch)

    async def arm(self):
        if not self.ready:
            raise ValueError("正在連接 Telegram，請稍後再試。")
        if self.store.state["mode"] != "paused":
            raise ValueError("已經啟用。")
        if not self.store.get("recovery"):
            raise ValueError("請先設定並保管解除碼。")
        state, key = self.store.state, self.store.get("recovery")
        if await self.telegram.session_age(self.clock()) < DAY:
            raise ValueError("userbot 登入滿 24 小時後才可啟用。")
        if not self.ready or self.store.state != state or self.store.get("recovery") != key:
            raise ValueError("設定已變更，請重新啟用。")
        now = self.clock()
        with self.store.db:
            self.store.put(
                "state",
                {
                    "mode": "armed",
                    "epoch": secrets.token_hex(8),
                    "heartbeat": now,
                    "deadline": now + WINDOW,
                    "activated": 0,
                    "healthy": now,
                },
            )

    def heartbeat(self, when=None):
        state = self.store.state
        if state["mode"] != "armed":
            raise ValueError("尚未啟用，或已進入接管。接管後請使用解除碼。")
        when = min(when or self.clock(), self.clock())
        if when <= state["heartbeat"]:
            return
        with self.store.db:
            state.update(heartbeat=when, deadline=when + WINDOW)
            self.store.put("state", state)

    def connected(self):
        now, state = self.clock(), self.store.state
        with self.store.db:
            if (
                state["mode"] == "armed"
                and state["deadline"] <= now
                and state["healthy"] < state["deadline"]
            ):
                state["deadline"] = now + DAY
                self.store.notice(
                    f"grace:{state['epoch']}:{now}",
                    "連線已恢復。未收到 Heartbeat，請在 24 小時內確認。",
                    now,
                )
            state["healthy"] = now
            self.store.put("state", state)
        self.ready = True

    def activate(self, now):
        state = self.store.state
        state.update(mode="active", activated=now)
        with self.store.db:
            self.store.put("state", state)
            for message in self.store.rows("SELECT * FROM messages WHERE trigger!='first_dm'"):
                count = message["count"] if message["trigger"] == "schedule" else 1
                for n in range(count):
                    due = (
                        scheduled_at(now, message, n, self.config.timezone)
                        if message["trigger"] == "schedule"
                        else now
                    )
                    self.store.queue(
                        f"{state['epoch']}:{message['id']}:{n}",
                        state["epoch"],
                        message,
                        message["recipient"],
                        due,
                    )
            self.store.notice(
                "active:" + state["epoch"],
                "72 小時內未收到 Heartbeat，已開始接管。持有解除碼的人可私訊解除。",
                now,
            )
        self.session_check_at = 0

    def incoming(self, peer, date):
        state = self.store.state
        if not self.active() or date < state["activated"]:
            return
        with self.store.db:
            if not self.store.db.execute(
                "INSERT OR IGNORE INTO seen VALUES (?,?)", (state["epoch"], peer)
            ).rowcount:
                return
            messages = self.store.rows(
                "SELECT * FROM messages WHERE trigger='first_dm' AND recipient=?", (peer,)
            )
            if not messages:
                messages = self.store.rows(
                    "SELECT * FROM messages WHERE trigger='first_dm' AND recipient=0"
                )
            for message in messages:
                self.store.queue(
                    f"{state['epoch']}:{message['id']}:dm:{peer}",
                    state["epoch"],
                    message,
                    peer,
                    self.clock(),
                )

    def authorized_handover(self, actor, group):
        return (
            self.active()
            and self.store.trusted(actor)
            and bool(self.store.db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone())
        )

    async def prepare_handover(self, actor, group, target):
        if not self.authorized_handover(actor, group):
            raise ValueError("此帳號或群組未獲授權，或尚未接管。")
        epoch = self.store.state["epoch"]
        details = await self.telegram.check_handover(group, target)
        if not self.active(epoch) or not self.authorized_handover(actor, group):
            raise ValueError("接管狀態已變更。")
        token = secrets.token_hex(12)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO handovers(token,actor,group_id,target,epoch,expires) VALUES (?,?,?,?,?,?)",
                (token, actor, group, target, epoch, self.clock() + 300),
            )
        return token, details

    def confirm_handover(self, actor, token):
        rows = self.store.rows("SELECT * FROM handovers WHERE token=?", (token,))
        if not rows:
            raise ValueError("確認已失效。")
        op = rows[0]
        if (
            op["actor"] != actor
            or op["expires"] < self.clock()
            or op["state"] != "prepared"
            or not self.active(op["epoch"])
            or not self.authorized_handover(actor, op["group_id"])
        ):
            raise ValueError("確認已失效，請重新發送交接指令。")
        with self.store.db:
            self.store.db.execute("UPDATE handovers SET state='queued' WHERE token=?", (token,))

    async def tick(self):
        if not self.ready or self.worker_lock.locked():
            return
        async with self.worker_lock:
            now, state = self.clock(), self.store.state
            if state["mode"] == "armed":
                with self.store.db:
                    elapsed = [
                        h
                        for h in (24, 48, 60, 71)
                        if state["heartbeat"] + h * 3600 <= now < state["deadline"]
                    ]
                    if elapsed:
                        self.store.notice(
                            f"reminder:{state['epoch']}:{state['heartbeat']}:{max(elapsed)}",
                            "請確認 Heartbeat，避免自動接管。",
                            now,
                        )
                    state["healthy"] = now
                    self.store.put("state", state)
                if now >= state["deadline"]:
                    self.activate(now)
            if not self.active():
                return
            epoch = self.store.state["epoch"]
            await self.check_sessions()
            await self.deliver(epoch)
            await self.handover(epoch)

    async def check_sessions(self):
        now, epoch = self.clock(), self.store.state["epoch"]
        if (
            not self.allowed(epoch)
            or self.session_lock.locked()
            or now < max(self.session_check_at, self.session_retry_at)
        ):
            return
        async with self.session_lock:
            self.session_check_at = now + 30
            try:
                result = await self.telegram.revoke_sessions(lambda: self.allowed(epoch))
                with self.store.db:
                    self.store.put("sessions", {"at": now, "result": result})
            except Retry as error:
                self.session_retry_at = now + error.seconds
            except Exception as error:
                with self.store.db:
                    self.store.put("sessions", {"at": now, "result": type(error).__name__})

    async def deliver(self, epoch):
        now = self.clock()
        with self.store.db:
            self.store.db.execute(
                """UPDATE deliveries SET state='skipped' WHERE state='pending' AND due<=?
                AND epoch=? AND EXISTS (SELECT 1 FROM deliveries newer WHERE newer.epoch=deliveries.epoch
                AND newer.message_id=deliveries.message_id AND newer.peer=deliveries.peer
                AND newer.due>deliveries.due AND newer.due<=? AND newer.state='pending')""",
                (now, epoch, now),
            )
        for row in self.store.rows(
            "SELECT * FROM deliveries WHERE epoch=? AND state='pending' AND due<=? AND retry_at<=? ORDER BY due LIMIT 5",
            (epoch, now, now),
        ):
            if not self.allowed(epoch):
                return
            with self.store.db:
                self.store.db.execute(
                    "UPDATE deliveries SET state='sending',attempt_at=? WHERE key=?",
                    (now, row["key"]),
                )
            try:
                message_id = await self.telegram.send(row, lambda: self.allowed(epoch))
                status = (
                    "sent"
                    if message_id is not None
                    else ("pending" if self.active(epoch) else "cancelled")
                )
                error = None
            except Retry as error:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE deliveries SET state=?,retry_at=? WHERE key=?",
                        (
                            "pending" if self.active(epoch) else "cancelled",
                            now + error.seconds,
                            row["key"],
                        ),
                    )
                return
            except Rejected as failure:
                status, error, message_id = "failed", str(failure), None
            except Exception as failure:
                status, error, message_id = "uncertain", type(failure).__name__, None
            with self.store.db:
                self.store.db.execute(
                    "UPDATE deliveries SET state=?,error=?,remote_id=? WHERE key=?",
                    (status, error, message_id, row["key"]),
                )
                if error:
                    self.store.notice(
                        "delivery:" + row["key"],
                        f"訊息 #{row['message_id']}：{status}（{error}）。請查看發送紀錄。",
                        now,
                    )

    async def handover(self, epoch):
        for op in self.store.rows(
            "SELECT * FROM handovers WHERE epoch=? AND state='uncertain' AND retry_at<=? LIMIT 1",
            (epoch, self.clock()),
        ):
            if not self.allowed(epoch):
                return
            with self.store.db:
                self.store.db.execute(
                    "UPDATE handovers SET retry_at=? WHERE token=?",
                    (self.clock() + 300, op["token"]),
                )
            try:
                result = await self.telegram.reconcile_handover(op)
            except Exception:
                result = None
            if result:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE handovers SET state='done',result=? WHERE token=?",
                        (result, op["token"]),
                    )
                    self.store.notice(
                        "reconciled:" + op["token"],
                        f"已確認群組 {op['group_id']} 的交接完成（{result}）。",
                        self.clock(),
                    )
        rows = self.store.rows(
            "SELECT * FROM handovers WHERE epoch=? AND state='queued' AND retry_at<=? LIMIT 1",
            (epoch, self.clock()),
        )
        if not rows or not self.allowed(epoch):
            return
        op = rows[0]
        if not self.authorized_handover(op["actor"], op["group_id"]):
            with self.store.db:
                self.store.db.execute(
                    "UPDATE handovers SET state='cancelled' WHERE token=?", (op["token"],)
                )
            return
        with self.store.db:
            self.store.db.execute(
                "UPDATE handovers SET state='running' WHERE token=?", (op["token"],)
            )
        try:
            result = await self.telegram.handover(
                op["group_id"],
                op["target"],
                lambda: (
                    self.allowed(epoch) and self.authorized_handover(op["actor"], op["group_id"])
                ),
            )
            status = (
                "done"
                if result != "cancelled"
                else ("queued" if self.active(epoch) else "cancelled")
            )
        except Retry as error:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE handovers SET state=?,retry_at=? WHERE token=?",
                    (
                        "queued" if self.active(epoch) else "cancelled",
                        self.clock() + error.seconds,
                        op["token"],
                    ),
                )
            return
        except (Rejected, ValueError) as error:
            status, result = "failed", str(error)
        except Exception as error:
            status, result = "uncertain", type(error).__name__
        with self.store.db:
            self.store.db.execute(
                "UPDATE handovers SET state=?,result=? WHERE token=?", (status, result, op["token"])
            )
            self.store.notice(
                "handover:" + op["token"],
                f"群組 {op['group_id']} → {op['target']}：{status}（{result}）",
                self.clock(),
            )
