# Usage

[繁體中文](usage.zh-TW.md)

## Checking in

Press **Heartbeat** in the control bot or send `/heartbeat`. Each check-in starts a new 72-hour timer. Reminders arrive after 24, 48, 60 and 71 hours.

Only the owner's check-in counts. Chatting or appearing online does not reset the timer.

Press **暫停** to pause before changing your arrangements. Press **啟用** when finished to start a fresh timer.

## Saved messages

Choose **預留訊息** → **新增**, then enter the recipient, message and delivery timing. Review the preview and press **儲存** to save.

- **接管時**: Send when takeover begins.
- **定時發送**: Send a set number of days or months after takeover. Optionally repeat at monthly intervals for a chosen number of deliveries.
- **首次私訊時**: Reply once when the recipient first messages the account after takeover.

Enter a recipient's `@username` or Telegram ID. Use `*` for a general first-message reply. People with a dedicated reply receive that instead.

Months follow `TIMEZONE` in `.env`. If a month has no matching day, the last day is used. A monthly schedule starting on January 31 continues at the end of February, then on March 31.

## Group handover

The owner adds contacts under **聯絡人**, choosing **密友** (friend) or **親友** (family), and adds groups under **交接群組**.

After takeover, those contacts can message the userbot or control bot:

```text
/groups
/handover -1001234567890 123456789
```

`/groups` lists the allowed groups. The two arguments to `/handover` are the group ID and successor's user ID. The successor must already be a member.

The control bot shows the group, successor and intended action. Press **確認交接** to confirm. With the userbot, send the `/confirm` command shown in its reply. Confirmation expires after five minutes. Use `/handover_status` to check the result.

To transfer ownership, save the Telegram two-step verification password in `secrets/telegram-2fa` and set this in `.env`:

```dotenv
TELEGRAM_2FA_FILE=/run/secrets/telegram-2fa
```

The file must be readable by UID `10001` inside the container. Run `docker compose up -d --force-recreate` after changing the setting. Without a password, the bot grants the administrator rights the owner can assign, with anonymity off.

## Recovery

From another Telegram account, send the recovery code privately to the userbot or control bot. You can also use:

```text
/recover your-recovery-code
```

Anyone holding the code can stop takeover. Recovery stops further actions and consumes the code. The owner can log back in, set a new code and decide when to arm again.

If Telegram is unavailable, recover on the host:

```sh
docker compose stop heartbeat
docker compose run --rm heartbeat recover
docker compose up -d
```

Enter the code when prompted.

## Checking the service

```sh
docker compose ps
docker compose logs --tail=100 heartbeat
docker compose exec heartbeat last-heartbeat status
```

The control bot's **紀錄** button lists message and handover results:

| Record | Meaning |
| --- | --- |
| `pending` / `queued` | Waiting to send or run |
| `sent` / `done` | Completed |
| `cancelled` | Cancelled |
| `skipped` | An older missed recurring message was skipped |
| `failed` | Telegram rejected the action, with the reason shown alongside |
| `uncertain` | No definite result was received. Check the conversation or group permissions |

Uncertain handovers are checked periodically against the group's actual permissions without repeating the change.

## Backups

Stop the service before backing up. Keep the data, `.env` and `secrets/` together. Never run the original host and its restored copy at the same time.

```sh
mkdir -p backups
docker compose stop heartbeat
docker compose run --rm -T --no-deps --entrypoint tar heartbeat -C /data -cf - . > backups/data.tar
docker compose up -d
```

To restore on a new host, put `.env` and `secrets/` back, then run:

```sh
docker compose build
docker compose run --rm -T --no-deps --entrypoint tar heartbeat -C /data -xf - < backups/data.tar
docker compose up -d
```

The backup and restore commands use a Linux shell. After restoring, check **狀態** and **紀錄** to review the saved settings and takeover state.
