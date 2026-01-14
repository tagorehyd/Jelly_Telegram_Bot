import json
import logging
import random
import string
import time
import requests
from pathlib import Path

# ---------------- LOAD CONFIG ----------------

def load_config():
    config_path = "config.json"
    if not Path(config_path).exists():
        raise FileNotFoundError("config.json not found. Please create it with the required settings.")
    with open(config_path, "r") as f:
        return json.load(f)

config = load_config()

BOT_TOKEN = config["bot_token"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
JELLYFIN_URL = config["jellyfin_url"]
JELLYFIN_API_KEY = config["jellyfin_api_key"]
ADMIN_FILE = config["admin_file"]
USERS_FILE = config["users_file"]
PENDING_FILE = config["pending_file"]

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

def send_photo(chat_id, file_id):
    requests.post(f"{TELEGRAM_API}/sendPhoto", json={"chat_id": chat_id, "photo": file_id})

def send_video(chat_id, file_id):
    requests.post(f"{TELEGRAM_API}/sendVideo", json={"chat_id": chat_id, "video": file_id})

# ---------------- JELLYFIN ----------------

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

broadcast_mode = {}       # admin_id -> bool
target_broadcast = {}     # admin_id -> user_id or None

# ---------------- BOOTSTRAPPING ----------------

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

# ---------------- POLLING HANDLER ----------------

def handle_update(update):
    message = update.get("message")
    callback = update.get("callback_query")

    src = message or callback
    if not src:
        return

    chat_id = src["chat"]["id"]
    user = src.get("from")
    tg_id = str(user["id"])
    name = f"{user.get('first_name','')} {user.get('last_name','')}".strip()
    text = message.get("text") if message else None

    # Handle admin and user logic as before...

# ---------------- MAIN LOOP ----------------

def main():
    offset = None
    while True:
        try:
            updates = requests.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset, "timeout": 100}).json()
            if "result" in updates:
                for update in updates["result"]:
                    handle_update(update)
                    offset = update["update_id"] + 1
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(5)  # Backoff on error

if __name__ == "__main__":
    main()
