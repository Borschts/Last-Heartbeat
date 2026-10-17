# 使用說明

[English](usage.md)

## 日常使用

控制 bot 的「Heartbeat」按鈕和 `/heartbeat` 都可以確認。每次確認後重新計算 72 小時，期間會在第 24、48、60、71 小時提醒。

只有本人的確認有效。聊天或上線不會重設計時。

要修改安排，先按「暫停」。完成後再按「啟用」，重新開始計時。

## 預留訊息

按「預留訊息」→「新增」，依次填入收件人、內容和發送方式。最後會顯示預覽，按「儲存」才生效。

- **接管時**：開始接管便發送。
- **定時發送**：設定接管後幾天或幾個月發送，也可以每隔幾個月重複，並指定總次數。
- **首次私訊時**：接管後，對方第一次私訊時回覆一次。

收件人可以填 `@username` 或 Telegram ID。輸入 `*` 可設定其他人的首次私訊回覆。有專屬訊息的人只會收到自己的訊息。

月份按 `.env` 的 `TIMEZONE` 計算。若當月沒有相同日期，會用該月最後一天。例如 1 月 31 日設定每月發送，之後會在 2 月底及 3 月 31 日發送。

## 群組交接

本人先在「聯絡人」把有權交接的人設為「密友」或「親友」，再於「交接群組」加入群組。

接管後，這些人可以私訊 userbot 或控制 bot：

```text
/groups
/handover -1001234567890 123456789
```

`/groups` 列出獲授權的群組。`/handover` 後面依次是群組 ID 和接任者 ID。接任者必須已在群內。

控制 bot 會顯示群組、接任者和預計操作，按「確認交接」執行。使用 userbot 時，按回覆提示發送 `/confirm 確認碼`。確認在五分鐘後失效。完成後用 `/handover_status` 查看結果。

如要轉移群主，把 Telegram 兩步驗證密碼存入 `secrets/telegram-2fa`，在 `.env` 設定：

```dotenv
TELEGRAM_2FA_FILE=/run/secrets/telegram-2fa
```

這個檔案須讓容器內的 UID `10001` 讀取。修改後執行 `docker compose up -d --force-recreate`。沒有提供密碼時，會授予本人能授出的管理員權限，關閉匿名。

## 解除接管

從另一個 Telegram 帳號，把解除碼私訊給 userbot 或控制 bot，也可以使用：

```text
/recover 你的解除碼
```

任何持有解除碼的人都可以解除。解除後停止後續操作，這個碼也隨即失效。本人重新登入後，需要設定新碼，再決定何時啟用。

如果 Telegram 無法連線，可以在主機執行：

```sh
docker compose stop heartbeat
docker compose run --rm heartbeat recover
docker compose up -d
```

按提示輸入解除碼。

## 查看運作情況

```sh
docker compose ps
docker compose logs --tail=100 heartbeat
docker compose exec heartbeat last-heartbeat status
```

控制 bot 的「紀錄」也會列出訊息及交接結果：

| 紀錄 | 意思 |
| --- | --- |
| `pending` / `queued` | 等待發送或執行 |
| `sent` / `done` | 已完成 |
| `cancelled` | 已取消 |
| `skipped` | 已略過較早的重複訊息 |
| `failed` | Telegram 拒絕了操作，旁邊會列出原因 |
| `uncertain` | 沒有收到明確結果，請查看實際對話或群組權限 |

結果不明的群組交接會定期重新查詢權限，不會直接重做。

## 備份

停止服務後再備份，將資料、`.env` 和 `secrets/` 放在一起保管。不要同時啟動原機和備份副本。

```sh
mkdir -p backups
docker compose stop heartbeat
docker compose run --rm -T --no-deps --entrypoint tar heartbeat -C /data -cf - . > backups/data.tar
docker compose up -d
```

還原到新主機時，先放回 `.env` 和 `secrets/`，再執行：

```sh
docker compose build
docker compose run --rm -T --no-deps --entrypoint tar heartbeat -C /data -xf - < backups/data.tar
docker compose up -d
```

備份與還原指令適用於 Linux shell。還原後請先查看「狀態」和「紀錄」，確認保留的設定及接管狀態。
