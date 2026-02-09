# Jellyfin Telegram Bot Documentation

## Overview
This bot connects Telegram users to Jellyfin accounts, handling registrations, account linking, subscriptions, and admin workflows using Telegram’s HTTP API (requests) and Jellyfin’s REST API.

## Setup (First Run)
1. Run the bot once to generate:
   - `config/config.json`
   - `config/secrets.json`
2. Update `config/config.json` with your Jellyfin URL, storage paths, and payment settings.
3. Update `config/secrets.json` with your Telegram bot token and Jellyfin API key.
4. Create the data directory:
   ```bash
   mkdir -p data
   ```
5. Run the bot again. On the second run, it will import Jellyfin users and mark admins based on Jellyfin permissions.

## Configuration Files
- `config/config.json` – non-secret settings (Jellyfin URL, payment, storage paths, subscription plans).
- `config/secrets.json` – secrets (Telegram bot token, Jellyfin API key).
- `config/strings.json` – user-facing setup text shown on first run.

## Data Files
- `data/users.json` – source of truth for users.
- `data/admins.json` – auto-generated admin lookup by Telegram ID.
- `data/pending.json` – pending registration/link/unlink requests.
- `data/subscriptions.json` – active subscriptions and expirations.
- `data/payment_requests.json` – payment request history.
- `data/telegram_mapping.json` – fast Telegram ID → Jellyfin ID mapping.

## Commands
### User Commands
- `/start` – show the welcome menu and status.
- `/register` – request a new Jellyfin account.
- `/subscribe` – show subscription plans and start payment flow.
- `/status` – show current subscription status.
- `/resetpw` – request a password reset.
- `/linkme <username>` – link Telegram to an existing Jellyfin account.
- `/unlinkme` – request unlinking from the current Jellyfin account.
- `/cancel` – cancel the current flow.

### Admin Commands
- `/pending` – list pending requests.
- `/users` – list users and status.
- `/stats` – system summary.
- `/broadcast` – send a message to all users.
- `/message <username>` – send a direct message to a user.
- `/payments` – list pending payment requests.
- `/subinfo <username>` – view subscription details.
- `/subextend <username> <days>` – extend subscription.
- `/subend <username>` – end subscription.
- `/link <username> <telegram_id>` – force link.
- `/unlink <username>` – force unlink.

## Logging
Logs are stored under `logs/`:
- `bot.log` – operational info.
- `debug.log` – full debug output.
- `error.log` – errors only.
- `user_activity.log` – user interaction stream.

## Admin Bootstrapping
After the second run:
1. Open `data/users.json`.
2. Find users marked with `"is_admin": true`.
3. Add their `telegram_id` values.
4. Restart the bot to sync `data/admins.json`.

## Troubleshooting
- If the bot exits on first run, fill in `config/config.json` and `config/secrets.json` and restart.
- If Jellyfin operations fail, verify the Jellyfin URL and API key.
- If no admin is detected, the bot will refuse to start until an admin `telegram_id` is configured.
