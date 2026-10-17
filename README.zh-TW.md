# Last Heartbeat

[English](README.md)

所有人都值得一個體面的離開。

## 簡介

Last Heartbeat 是一個自行部署的 Telegram userbot，用於在使用者長時間沒有回應時，發送預留訊息並協助親友交接群組。

使用者透過 Telegram 按鈕或指令確認 Heartbeat，每次確認後重新計算 72 小時。超過這段時間仍未確認，userbot 便會接管帳號，執行事先設定的安排。

## 功能

- **預留訊息**：為不同親友設定訊息，可在接管時發送、按月發送，或在對方於接管後第一次私訊時回覆。
- **群組交接**：加入密友或親友名單的人，可透過指令將指定群組交給其他成員管理。
- **帳號接管**：接管時登出其他裝置，之後持續檢查並登出新登入的裝置。
- **解除接管**：本人或親友可從另一帳號發送解除碼，停止接管。

以上設定均可透過 Telegram 控制 bot 的選單調整。

## 使用要求

- 一台能長期開機、保存資料並連接 Telegram 的主機。
- Docker Engine 及 Docker Compose。
- Telegram 帳號、從 [my.telegram.org](https://my.telegram.org) 取得的 API ID 和 API hash，以及由 [BotFather](https://t.me/BotFather) 建立的 bot token。

## FAQ

### 如果只是忘了確認，怎樣取消接管？

接管前會先發送提醒。如果已經開始接管，本人或親友可以從另一帳號私訊 userbot，發送事先設定的解除碼。控制 bot 也接受解除碼。

解除後會停止發送訊息、交接群組及登出裝置。已送出的訊息和已完成的交接不會自動撤回。

### 主機斷線期間超過了 72 小時，會怎樣處理？

恢復連線後，userbot 會先接收斷線期間的訊息，檢查有沒有漏收的 Heartbeat。若仍未收到若期間未收到任何Heartbeat，會補發提醒，再等 24 小時才接管。

### 親友可以接管哪些群組？

只有本人事先選好的群組。名單內的親友可以指定一位群內成員接任。

[轉移群主](https://core.telegram.org/method/channels.editCreator)需要帳號的 Telegram 兩步驗證密碼，可在部署時透過本機檔案提供。若未提供密碼，或 Telegram 不允許轉移，則改為授予所有可用的管理員權限，並關閉匿名。

### 能立即踢走所有其他裝置嗎？

接管後會嘗試登出其他裝置，再持續檢查新的登入。Telegram 要求 userbot 本身已登入至少 24 小時，才允許[登出其他裝置](https://core.telegram.org/method/auth.resetAuthorizations)。

### 帳號和訊息資料放在哪裏？

資料保存在自行部署的主機上，不會自動加密。請妥善保管資料目錄和備份，其中的 `.session` 檔案可以直接用來登入 Telegram 帳號。

## 授權

[MIT](LICENSE)
