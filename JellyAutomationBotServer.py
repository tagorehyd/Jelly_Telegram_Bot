# Telegram Jellyfin Bot Server (Python, REST-based)
# ------------------------------------------------
# Telegram Bot via REST (no python-telegram-bot SDK)
# Features:
# - User registration & password reset with admin approval
# - Stores full user objects in users.json
# - Jellyfin integration via REST API

import json
import logging
import random
import string
import time
import requests
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

# ---------------- CONFIG ----------------
BOT_TOKEN = "8309298410:AAEmQ41zgSFaC6DyURTECNntaf1QhPeaAro"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
JELLYFIN_URL = "http://192.168.29.29:8082"
JELLYFIN_API_KEY = "a20951185b104a6aa5db7cbdf843bbb8"
ADMIN_FILE = "admins.json"
USERS_FILE = "users.json"
PENDING_FILE = "pending.json"

logging.basicConfig(level=logging.INFO)

# ---------------- UTILITIES ----------------

def load_json(path, default):
    if not Path(path).exists():
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def generate_password(length=12):
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def jellyfin_headers():
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"}


def jellyfin_user_exists(username):
    r = requests.get(f"{JELLYFIN_URL}/Users", headers=jellyfin_headers())
    return any(u["Name"].lower() == username.lower() for u in r.json())


def jellyfin_create_user(username, password):
    requests.post(
        f"{JELLYFIN_URL}/Users/New",
        headers=jellyfin_headers(),
        json={"Name": username, "Password": password},
    )


def jellyfin_reset_password(username, password):
    users_list = requests.get(f"{JELLYFIN_URL}/Users", headers=jellyfin_headers()).json()
    for u in users_list:
        if u["Name"].lower() == username.lower():
            requests.post(
                f"{JELLYFIN_URL}/Users/{u['Id']}/Password",
                headers=jellyfin_headers(),
                json={"Id": u["Id"], "ResetPassword": True, "Password": password},
            )
            return

# ---------------- DATA ----------------
admins = load_json(ADMIN_FILE, {"admins": []})["admins"]
users = load_json(USERS_FILE, {})
pending = load_json(PENDING_FILE, {})

# ---------------- BASE USER SETUP ----------------
base_user_id = "1815866145"
if base_user_id not in users:
    users[base_user_id] = {
        "name": "Default Admin",
        "username": "default_admin",
        "telegram_id": base_user_id,
        "created_at": int(time.time()),
    }
    save_json(USERS_FILE, users)

if base_user_id not in map(str, admins):
    admins.append(int(base_user_id))
    save_json(ADMIN_FILE, {"admins": admins})

# ---------------- TELEGRAM HANDLER ----------------

broadcast_mode = {}  # admin_id -> True/False
target_broadcast = {}  # admin_id -> telegram_id or None

class TelegramWebhook(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length"))
        update = json.loads(self.rfile.read(length))

        message = update.get("message") or update.get("callback_query", {}).get("message")
        if not message:
            self.send_response(200)
            self.end_headers()
            return

        chat_id = message["chat"]["id"]
        user = update.get("message", {}).get("from") or update.get("callback_query", {}).get("from")
        tg_id = str(user["id"])
        name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()

        text = update.get("message", {}).get("text")

        # ---------------- ADMIN BROADCAST ----------------
        if tg_id in map(str, admins):
            if text == "/post":
                broadcast_mode[tg_id] = True
                target_broadcast[tg_id] = None
                send_message(chat_id, "📢 Global broadcast mode ON. Send messages/media. Use /stop-post to stop.")
                self.send_response(200)
                self.end_headers()
                return
            elif text == "/post-user":
                keyboard = []
                row = []
                for uid, u in users.items():
                    row.append({"text": u["username"], "callback_data": f"post_target:{uid}"})
                    if len(row) == 2:
                        keyboard.append(row)
                        row = []
                if row:
                    keyboard.append(row)
                send_message(chat_id, "🎯 Select a user to broadcast to:", {"inline_keyboard": keyboard})
                self.send_response(200)
                self.end_headers()
                return
            elif text == "/stop-post":
                broadcast_mode[tg_id] = False
                target_broadcast[tg_id] = None
                send_message(chat_id, "🛑 Broadcast mode stopped.")
                self.send_response(200)
                self.end_headers()
                return
            elif broadcast_mode.get(tg_id):
                targets = []
                if target_broadcast.get(tg_id):
                    targets = [target_broadcast[tg_id]]
                else:
                    targets = [uid for uid in users if uid not in map(str, admins)]

                for uid in targets:
                    if "text" in update.get("message", {}):
                        send_message(uid, update["message"]["text"])
                    elif "photo" in update.get("message", {}):
                        file_id = update["message"]["photo"][-1]["file_id"]
                        requests.post(f"{TELEGRAM_API}/sendPhoto", json={"chat_id": uid, "photo": file_id})
                    elif "video" in update.get("message", {}):
                        file_id = update["message"]["video"]["file_id"]
                        requests.post(f"{TELEGRAM_API}/sendVideo", json={"chat_id": uid, "video": file_id})
                self.send_response(200)
                self.end_headers()
                return

        # ---------------- USER FLOWS ----------------

        if text == "/start":
            if tg_id in users:
                send_message(chat_id, "You are registered. Send /reset to reset password.")
            else:
                pending[tg_id] = {"state": "WAIT_USERNAME", "name": name, "telegram_id": tg_id}
                save_json(PENDING_FILE, pending)
                send_message(chat_id, "Welcome! Send desired Jellyfin username.")
        elif text == "/reset" and tg_id in users:
            for admin in admins:
                send_message(
                    admin,
                    f"Password reset request for {users[tg_id]['username']}",
                    {
                        "inline_keyboard": [[
                            {"text": "Approve", "callback_data": f"reset_ok:{tg_id}"},
                            {"text": "Reject", "callback_data": f"reset_no:{tg_id}"},
                        ]]
                    },
                )
            send_message(chat_id, "Reset request sent to admin.")
        elif tg_id in pending and pending[tg_id]["state"] == "WAIT_USERNAME":
            username = text.strip()
            if jellyfin_user_exists(username):
                send_message(chat_id, "Username already exists. Try another.")
            else:
                pending[tg_id].update({"username": username, "state": "WAIT_APPROVAL"})
                save_json(PENDING_FILE, pending)
                for admin in admins:
                    send_message(
                        admin,
                        f"""New user approval:
Name: {name}
Username: {username}
TG ID: {tg_id}""",
                        {
                            "inline_keyboard": [[
                                {"text": "Approve", "callback_data": f"approve:{tg_id}"},
                                {"text": "Reject", "callback_data": f"reject:{tg_id}"},
                            ]]
                        },
                    )
                send_message(chat_id, "Approval request sent to admin.")
        elif update.get("callback_query"):
            data = update["callback_query"]["data"]

            if data.startswith("post_target:"):
                if tg_id not in map(str, admins):
                    send_message(chat_id, "Unauthorized")
                else:
                    target_id = data.split(":")[1]
                    broadcast_mode[tg_id] = True
                    target_broadcast[tg_id] = target_id
                    send_message(chat_id, f"""🎯 Targeted broadcast ON for user: {users[target_id]['username']}
Send messages/media. Use /stop-post to stop.""")
                self.send_response(200)
                self.end_headers()
                return

            action, uid = data.split(":")

            if tg_id not in map(str, admins):
                send_message(chat_id, "Unauthorized")
                self.send_response(200)
                self.end_headers()
                return

            if action == "approve":
                p = pending.pop(uid)
                password = generate_password()
                jellyfin_create_user(p["username"], password)
                users[uid] = {
                    "name": p["name"],
                    "username": p["username"],
                    "telegram_id": uid,
                    "created_at": int(time.time()),
                }
                save_json(USERS_FILE, users)
                save_json(PENDING_FILE, pending)
                send_message(uid, f"""Approved!
Username: {p['username']}
Password: {password}""")
            elif action == "reject":
                pending.pop(uid, None)
                save_json(PENDING_FILE, pending)
                send_message(uid, "Your request was rejected.")
            elif action == "reset_ok":
                password = generate_password()
                jellyfin_reset_password(users[uid]["username"], password)
                send_message(uid, f"Password reset approved. New password: {password}")
            elif action == "reset_no":
                send_message(uid, "Password reset rejected.")

        self.send_response(200)
        self.end_headers()

# ---------------- FUTURE STORAGE NOTE ----------------
# users.json / pending.json are intentionally used for now.
# Drop-in SQLite migration path:
# - Create users, pending, admins tables
# - Replace load_json/save_json with DAO layer
# - Zero Telegram/Jellyfin logic change needed

# ---------------- DOCKER ----------------
# Example Dockerfile:
# FROM python:3.12-slim
# WORKDIR /app
# COPY . .
# RUN pip install requests
# EXPOSE 8080
# CMD ["python", "bot.py"]

# ---------------- SERVER ----------------

def run():
    server = HTTPServer(("0.0.0.0", 8080), TelegramWebhook)
    logging.info("Telegram webhook server started on port 8080")
    server.serve_forever()


if __name__ == "__main__":
    run()
