# Last Heartbeat

[繁體中文](README.zh-TW.md)

Everyone deserves a dignified farewell.

## About

Last Heartbeat is a self-hosted Telegram userbot for sending saved messages and helping trusted contacts hand over groups when the account owner stops responding.

Check in through a Telegram button or command to reset a 72-hour timer. If no check-in arrives before the timer expires, the userbot takes over the account and carries out the arrangements you have configured.

## Features

- **Saved messages:** Set different messages for each recipient. Send them on takeover, at monthly intervals, or when that person first messages you after takeover.
- **Group handover:** Allow listed friends and family to use commands to hand selected groups over to other members.
- **Account takeover:** Sign out other devices, then keep checking for and revoking new logins.
- **Recovery:** Stop a takeover by sending a recovery code from another account.

Configure these features through the Telegram control bot's menus.

## Requirements

- An always-on host with persistent storage and access to Telegram.
- Docker Engine and Docker Compose.
- A Telegram account, an API ID and API hash from [my.telegram.org](https://my.telegram.org), and a bot token from [BotFather](https://t.me/BotFather).

## FAQ

### How do I cancel a takeover if I just forgot to check in?

Reminders are sent before takeover. If it has already started, you or a trusted contact can send the saved recovery code to the userbot from another account. The control bot also accepts recovery codes.

Recovery stops further messages, group handovers and device sign-outs. Messages already sent and handovers already completed are not undone automatically.

### What happens if the 72-hour deadline passes while the host is offline?

Once connected again, the userbot receives the messages it missed and checks for a Heartbeat. If there is none, it sends another reminder and waits 24 hours before taking over.

### Which groups can my trusted contacts hand over?

Only groups selected by the account owner. Listed contacts can choose a member of the group to take over.

[Transferring ownership](https://core.telegram.org/method/channels.editCreator) requires the account's Telegram two-step verification password, supplied through a local file during setup. If no password is provided, or Telegram refuses the transfer, the userbot grants ALL the available administrator rights with anonymity turned off.

### Can it sign out every other device immediately?

It will try when takeover begins, then keep checking for new logins. Telegram requires the userbot to have been logged in for at least 24 hours before it can [sign out other devices](https://core.telegram.org/method/auth.resetAuthorizations).

### Where are my account details and messages stored?

Data stays on the host where you run the userbot and is not encrypted automatically. Keep the data directory and backups private. The `.session` files can be used to log in to your Telegram account.

## License

[MIT](LICENSE)
