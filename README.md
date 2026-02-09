# Jellyfin Telegram Bot

A Telegram bot that manages Jellyfin users, subscriptions, and admin workflows using the Telegram HTTP API (requests) and the Jellyfin REST API.

## What's Included
- `jelly_admin_with_upi.py` – main bot script
- `DOCUMENTATION.md` – setup, commands, and technical reference
- `config/strings.json` – user-facing setup strings

## Quick Start
1. Read **DOCUMENTATION.md** for setup guidance.
2. Run the bot once to generate `config/config.json` and `config/secrets.json`.
3. Update those config files with your values.
4. Run the bot again to start service.

## Documentation
See **DOCUMENTATION.md** for full setup, commands, data files, and logging details.

## Docker Compose
1. Build and start:
   ```bash
   docker compose up --build
   ```
2. Edit `config/config.json` and `config/secrets.json` on the host.
3. Restart the container:
   ```bash
   docker compose restart
   ```
