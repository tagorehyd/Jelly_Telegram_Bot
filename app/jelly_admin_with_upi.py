import json
import logging
import secrets
import string
import time
import requests
import signal
import sys
import re
from pathlib import Path
from threading import Lock, Thread
from datetime import datetime, timedelta

from bot.config_loader import CONFIG_FILE, SECRETS_FILE, load_config
from bot.http_client import HTTP_SESSION, HTTP_TIMEOUT, POLL_LONG_TIMEOUT, POLL_TIMEOUT
from bot.logging_setup import setup_logging
from bot.jellyfin_api import (
    fetch_users,
    create_user,
    get_user_id,
    delete_user,
    get_top_items,
    get_user_played_runtime,
    reset_password,
    set_user_enabled,
    username_available,
)
from bot.telegram_api import (
    send_message as send_message_api,
    send_photo as send_photo_api,
    send_video as send_video_api,
    delete_message as delete_message_api,
    edit_message_reply_markup as edit_message_reply_markup_api,
)

# -------------------------------------------------
# CONFIG LOADING + BOOTSTRAP
# -------------------------------------------------

config, secrets_config, STRINGS = load_config()

# Validate config is not using placeholder values
if secrets_config["bot_token"] == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
    print(f"❌ Error: Please configure your bot_token in {SECRETS_FILE}")
    sys.exit(1)

if secrets_config["jellyfin_api_key"] == "YOUR_JELLYFIN_API_KEY_HERE":
    print(f"❌ Error: Please configure your Jellyfin api_key in {SECRETS_FILE}")
    sys.exit(1)

# -------------------------------------------------
# GLOBAL VARIABLES
# -------------------------------------------------

TELEGRAM_API = f"https://api.telegram.org/bot{secrets_config['bot_token']}"
JELLYFIN_URL = config["jellyfin"]["url"]
JELLYFIN_API_KEY = secrets_config["jellyfin_api_key"]
UPI_ID = config["payment"]["upi_id"]
UPI_NAME = config["payment"]["upi_name"]

ADMINS_FILE = Path(config["storage"]["admins"])
USERS_FILE = Path(config["storage"]["users"])
PENDING_FILE = Path(config["storage"]["pending"])
SUBSCRIPTIONS_FILE = Path(config["storage"]["subscriptions"])
PAYMENT_REQUESTS_FILE = Path(config["storage"]["payment_requests"])
TELEGRAM_MAPPING_FILE = Path(config["storage"]["telegram_mapping"])

ROLE_ADMIN = "admin"
ROLE_PRIVILEGED = "privileged"
ROLE_REGULAR = "regular"

# Constants
SECONDS_PER_DAY = 86400

shutdown_flag = False
approval_lock = Lock()
broadcast_mode = {}
target_broadcast = {}
awaiting_username = {}  # Track users who need to provide username: {tg_id: {name, requested_at}}
username_to_uid = {}  # Fast username lookup: {username.lower(): jellyfin_user_id}
admin_request_messages = {}
admin_user_actions = {}

# -------------------------------------------------
# API WRAPPERS
# -------------------------------------------------

def jellyfin_create_user(username, password):
    return create_user(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, username, password)


def jellyfin_get_user_id(username):
    return get_user_id(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, username)


def jellyfin_enable_user(username):
    return set_user_enabled(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, username, True)


def jellyfin_disable_user(username):
    return set_user_enabled(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, username, False)


def jellyfin_reset_password(username, new_password):
    return reset_password(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, username, new_password)


def check_username_availability(username):
    return username_available(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, username)


def jellyfin_delete_user(user_id, username):
    return delete_user(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT, user_id, username)


def get_watch_stats(user_id=None):
    top_series = get_top_items(
        JELLYFIN_URL,
        JELLYFIN_API_KEY,
        HTTP_SESSION,
        HTTP_TIMEOUT,
        "Series",
        user_id=user_id,
    )
    top_movies = get_top_items(
        JELLYFIN_URL,
        JELLYFIN_API_KEY,
        HTTP_SESSION,
        HTTP_TIMEOUT,
        "Movie",
        user_id=user_id,
    )
    runtime_users = []
    if user_id is None:
        for uid, user in users.items():
            total_ticks = get_user_played_runtime(
                JELLYFIN_URL,
                JELLYFIN_API_KEY,
                HTTP_SESSION,
                HTTP_TIMEOUT,
                uid,
            )
            if total_ticks:
                runtime_users.append((user.get("username", uid), total_ticks))

        runtime_users.sort(key=lambda item: item[1], reverse=True)
        runtime_users = runtime_users[:10]

    def format_top(title, items):
        lines = [title]
        if not items:
            lines.append("  - No data")
            return "\n".join(lines)
        for idx, (name, count) in enumerate(items, start=1):
            lines.append(f"  {idx}. {name} ({count})")
        return "\n".join(lines)

    parts = [
        format_top("Top 10 Series:", top_series),
        format_top("Top 10 Movies:", top_movies),
    ]

    if runtime_users:
        runtime_lines = ["Top 10 Runtime Users (hours):"]
        for idx, (name, ticks) in enumerate(runtime_users, start=1):
            hours = round((ticks / 10_000_000) / 3600, 2)
            runtime_lines.append(f"  {idx}. {name} ({hours}h)")
        parts.append("\n".join(runtime_lines))

    return "\n\n".join(parts)


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    return send_message_api(HTTP_SESSION, HTTP_TIMEOUT, TELEGRAM_API, chat_id, text, reply_markup, parse_mode)


def send_photo(chat_id, photo, caption=None, reply_markup=None):
    return send_photo_api(HTTP_SESSION, HTTP_TIMEOUT, TELEGRAM_API, chat_id, photo, caption, reply_markup)


def send_video(chat_id, video, caption=None, reply_markup=None):
    return send_video_api(HTTP_SESSION, HTTP_TIMEOUT, TELEGRAM_API, chat_id, video, caption, reply_markup)


def delete_message(chat_id, message_id):
    return delete_message_api(HTTP_SESSION, HTTP_TIMEOUT, TELEGRAM_API, chat_id, message_id)


def edit_message_reply_markup(chat_id, message_id, reply_markup):
    return edit_message_reply_markup_api(HTTP_SESSION, HTTP_TIMEOUT, TELEGRAM_API, chat_id, message_id, reply_markup)

def set_admin_user_action(tg_id, action, user_id, source_message_id=None):
    admin_user_actions[tg_id] = {
        "action": action,
        "user_id": user_id,
        "source_message_id": source_message_id,
    }


def record_admin_request(request_key, admin_id, message_id):
    if not request_key or not message_id:
        return
    admin_request_messages.setdefault(request_key, {})[admin_id] = message_id


def revoke_admin_request(request_key):
    messages = admin_request_messages.pop(request_key, {})
    for admin_id, message_id in messages.items():
        delete_message(admin_id, message_id)


def notify_admins(request_key, text, reply_markup=None, parse_mode=None):
    for admin_id in admins:
        message_id = send_message(admin_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        record_admin_request(request_key, admin_id, message_id)


def notify_admins_notice(text, parse_mode=None):
    for admin_id in admins:
        send_message(admin_id, text, parse_mode=parse_mode)


def notify_admins_notice_except(exclude_id, text, parse_mode=None):
    for admin_id in admins:
        if str(admin_id) == str(exclude_id):
            continue
        send_message(admin_id, text, parse_mode=parse_mode)


def update_admin_request_buttons(request_key, text):
    messages = admin_request_messages.get(request_key, {})
    if not messages:
        return
    markup = json.dumps({"inline_keyboard": [[{"text": text, "callback_data": "noop"}]]})
    for admin_id, message_id in messages.items():
        edit_message_reply_markup(admin_id, message_id, markup)


def clear_admin_user_action(tg_id):
    admin_user_actions.pop(tg_id, None)

# -------------------------------------------------
# JELLYFIN USER BOOTSTRAP (SECOND RUN)
# -------------------------------------------------

def bootstrap_users_from_server():
    users_file = Path(config["storage"]["users"])

    # Check if users.json already has data
    if users_file.exists() and users_file.stat().st_size > 2:
        return

    print("🔄 Second run detected - Loading users from Jellyfin server...")

    try:
        jelly_users = fetch_users(JELLYFIN_URL, JELLYFIN_API_KEY, HTTP_SESSION, HTTP_TIMEOUT)
    except Exception as e:
        print(f"❌ Failed to connect to Jellyfin server: {e}")
        print(f"⚠️ Make sure your Jellyfin URL is correct in {CONFIG_FILE} and API key is correct in {SECRETS_FILE}")
        sys.exit(1)

    users = {}
    admin_count = 0

    for u in jelly_users:
        # Check if user has "Allow this user to manage the server" permission
        # This is stored in Policy.IsAdministrator
        is_jellyfin_admin = u.get("Policy", {}).get("IsAdministrator", False)
        
        if is_jellyfin_admin:
            # Mark as admin role
            users[str(u["Id"])] = {
                "jellyfin_id": u["Id"],
                "username": u["Name"],
                "role": ROLE_ADMIN,
                "is_admin": True,
                "telegram_id": None,  # Will be filled manually
                "created_at": int(time.time())
            }
            admin_count += 1
        else:
            # All other users are privileged
            users[str(u["Id"])] = {
                "jellyfin_id": u["Id"],
                "username": u["Name"],
                "role": ROLE_PRIVILEGED,
                "is_admin": False,
                "telegram_id": None,  # Will be filled manually or via /linkme
                "created_at": int(time.time())
            }

    save_json(users_file, users)

    print(f"✅ Loaded {len(users)} users from Jellyfin")
    print(f"✅ {admin_count} admin(s) detected (users with 'Allow this user to manage the server')")
    print(f"✅ {len(users) - admin_count} regular users marked as privileged")
    print(f"✅ All users have telegram_id field set to null")
    print()
    print("📝 NEXT STEPS:")
    print(f"1. Open {users_file}")
    print(f"2. Find the admin user(s) (marked with 'is_admin': true)")
    print("3. Add admin's Telegram ID to 'telegram_id' field")
    print("   Example: \"telegram_id\": 123456789")
    print("4. Get Telegram ID from @userinfobot")
    print("5. You can have multiple admins if multiple users have server management permission")
    print("6. Save the file and restart the bot")
    print("7. On next run, admins will be automatically synced from users.json")
    print()
    print("ℹ️  Note: admins.json is auto-generated and synced on each startup")
    print("   You only need to edit users.json to add telegram_id to admin users")
    print()
    sys.exit(0)


# -------------------------------------------------
# UTILITY FUNCTIONS
# -------------------------------------------------


def save_json(filepath, data):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

def save_subscriptions():
    """Helper function to save subscriptions"""
    save_json(SUBSCRIPTIONS_FILE, subscriptions)

def load_json(filepath):
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.warning(f"Corrupted JSON file: {filepath}, starting fresh")
        return {}

def normalize_user_data():
    """Normalize all user data on startup - handles legacy data format issues"""
    changes_made = False
    
    for user_id, user in users.items():
        # 1. Normalize telegram_id to int or None (handles string telegram_ids from manual edits)
        tg_id = user.get("telegram_id")
        if tg_id is not None and not isinstance(tg_id, int):
            try:
                normalized_id = int(tg_id) if tg_id else None
                users[user_id]["telegram_id"] = normalized_id
                changes_made = True
                logging.info(f"Normalized telegram_id for user {user_id}: '{tg_id}' (type: {type(tg_id).__name__}) → {normalized_id}")
            except (ValueError, TypeError):
                logging.error(f"Invalid telegram_id for user {user_id}: {tg_id}, setting to None")
                users[user_id]["telegram_id"] = None
                changes_made = True
        
        # 2. Ensure role field exists
        if "role" not in user:
            users[user_id]["role"] = ROLE_REGULAR
            changes_made = True
            logging.warning(f"Added default role for user {user_id}: {user.get('username', 'unknown')}")
        
        # 3. Fix role/is_admin inconsistencies
        is_admin = user.get("is_admin", False)
        role = user.get("role", ROLE_REGULAR)
        
        if is_admin and role != ROLE_ADMIN:
            users[user_id]["role"] = ROLE_ADMIN
            changes_made = True
            logging.warning(f"Fixed role for admin user {user_id} ({user.get('username', 'unknown')}): {role} → {ROLE_ADMIN}")
        
        if not is_admin and role == ROLE_ADMIN:
            users[user_id]["is_admin"] = True
            changes_made = True
            logging.warning(f"Fixed is_admin flag for user {user_id} ({user.get('username', 'unknown')})")
        
        # 4. Ensure jellyfin_id field matches key
        if user.get("jellyfin_id") != user_id:
            users[user_id]["jellyfin_id"] = user_id
            changes_made = True
            logging.warning(f"Fixed jellyfin_id field for user {user_id}")
        
        # 5. Ensure created_at exists
        if "created_at" not in user:
            users[user_id]["created_at"] = int(time.time())
            changes_made = True
            logging.warning(f"Added created_at for user {user_id}")
    
    if changes_made:
        save_json(USERS_FILE, users)
        logging.info("✅ User data normalized and saved")
    else:
        logging.info("✓ User data is already normalized")
    
    return changes_made

def cleanup_stale_data():
    """Clean up old pending requests and payment requests"""
    current_time = time.time()
    pending_timeout = 86400 * 7  # 7 days for pending requests
    payment_timeout = 86400 * 28  # 28 days for completed payments
    username_timeout = 3600  # 1 hour for username selection
    
    total_cleaned = 0
    
    # Clean awaiting_username (in-memory only)
    to_remove = [tg_id for tg_id, data in awaiting_username.items() 
                 if current_time - data.get("requested_at", 0) > username_timeout]
    for tg_id in to_remove:
        awaiting_username.pop(tg_id, None)
    if to_remove:
        total_cleaned += len(to_remove)
        logging.info(f"Cleaned {len(to_remove)} stale username selection requests")
    
    # Clean pending requests
    to_remove = [uid for uid, data in pending.items()
                 if current_time - data.get("requested_at", 0) > pending_timeout]
    for uid in to_remove:
        pending.pop(uid, None)
    if to_remove:
        save_json(PENDING_FILE, pending)
        total_cleaned += len(to_remove)
        logging.info(f"Cleaned {len(to_remove)} stale pending requests")
    
    # Clean payment requests
    to_remove = []
    for req_id, req in payment_requests.items():
        if req.get("status") == "pending" and current_time - req.get("created_at", 0) > pending_timeout:
            to_remove.append(req_id)
        elif req.get("status") in ["approved", "rejected"]:
            completion_time = req.get("approved_at", req.get("rejected_at", req.get("created_at", 0)))
            if current_time - completion_time > payment_timeout:
                to_remove.append(req_id)
    
    for req_id in to_remove:
        payment_requests.pop(req_id, None)
    if to_remove:
        save_json(PAYMENT_REQUESTS_FILE, payment_requests)
        total_cleaned += len(to_remove)
        logging.info(f"Cleaned {len(to_remove)} old payment requests")
    
    # Clean orphaned subscriptions (user doesn't exist)
    to_remove = [uid for uid in subscriptions.keys() if uid not in users]
    for uid in to_remove:
        subscriptions.pop(uid, None)
    if to_remove:
        save_subscriptions()
        total_cleaned += len(to_remove)
        logging.info(f"Cleaned {len(to_remove)} orphaned subscriptions")
    
    if total_cleaned > 0:
        logging.info(f"✅ Total cleanup: {total_cleaned} items removed")
    
    return total_cleaned

def cleanup_loop():
    """Background thread to periodically clean stale data"""
    while not shutdown_flag:
        try:
            time.sleep(3600)  # Wait 1 hour
            if not shutdown_flag:
                cleanup_stale_data()
        except Exception as e:
            logging.error(f"Cleanup loop error: {e}")
            time.sleep(60)

def validate_jellyfin_operation(operation_name, success, critical=False):
    """Validate Jellyfin operation success"""
    if not success:
        level = logging.ERROR if critical else logging.WARNING
        logging.log(level, f"Jellyfin operation failed: {operation_name}")
    return success

def safe_file_save(filepath, data, description="data"):
    """Save JSON with comprehensive error handling"""
    try:
        save_json(filepath, data)
        return True
    except Exception as e:
        logging.critical(f"❌ CRITICAL: Failed to save {description} to {filepath}: {e}")
        logging.critical(f"Data may be lost! Manual backup recommended.")
        return False

def generate_password(length=8):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def update_telegram_mapping(telegram_id, jellyfin_user_id):
    """Update telegram_to_userid mapping and persist to file"""
    if telegram_id:
        telegram_to_userid[str(telegram_id)] = jellyfin_user_id
        # Persist to file for durability
        safe_file_save(TELEGRAM_MAPPING_FILE, telegram_to_userid, "telegram mapping")

def remove_telegram_mapping(telegram_id):
    """Remove telegram_id from mapping and persist to file"""
    if telegram_id:
        telegram_to_userid.pop(str(telegram_id), None)
        # Persist to file for durability
        safe_file_save(TELEGRAM_MAPPING_FILE, telegram_to_userid, "telegram mapping")

def update_username_mapping(username, jellyfin_user_id):
    """Update username_to_uid mapping"""
    if username:
        username_to_uid[username.lower()] = jellyfin_user_id

def remove_username_mapping(username):
    """Remove username from mapping"""
    if username:
        username_to_uid.pop(username.lower(), None)

def get_user_by_username(username):
    """Get user data by username - returns (user_id, user_data) or (None, None)"""
    user_id = username_to_uid.get(username.lower())
    if user_id and user_id in users:
        return user_id, users[user_id]
    return None, None

def signal_handler(sig, frame):
    global shutdown_flag
    logging.info("🛑 Shutdown signal received, stopping gracefully...")
    shutdown_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# -------------------------------------------------
# DATA INITIALIZATION
# -------------------------------------------------

setup_logging()

# Bootstrap users from Jellyfin on second run
bootstrap_users_from_server()

users = load_json(USERS_FILE)
pending = load_json(PENDING_FILE)
subscriptions = load_json(SUBSCRIPTIONS_FILE)
payment_requests = load_json(PAYMENT_REQUESTS_FILE)

# Normalize user data to handle legacy formats and inconsistencies
logging.info("=" * 80)
logging.info("NORMALIZING USER DATA...")
logging.info("=" * 80)
normalize_user_data()

# Clean up stale data
logging.info("=" * 80)
logging.info("CLEANING STALE DATA...")
logging.info("=" * 80)
cleanup_stale_data()

# Sync admins from users.json
# admins.json is AUTO-GENERATED - it's a lookup table for fast admin checks
# The source of truth is users.json - any user with telegram_id and is_admin=true becomes an admin
# admins.json structure: {telegram_id: {user_id, username, added_at}}
admins = {}

# Load telegram_to_userid mapping from file (persistent across restarts)
# This provides O(1) lookup for telegram_id -> jellyfin_user_id
telegram_to_userid = load_json(TELEGRAM_MAPPING_FILE)  # Structure: {telegram_id: jellyfin_user_id}
username_to_uid = {}  # Mapping username.lower() -> jellyfin_user_id for fast lookups (in-memory only)

# Rebuild mappings from users.json (ensures consistency)
logging.info("Building user lookup mappings...")
telegram_mapping_rebuilt = False

# First, remove stale mappings (telegram IDs that no longer exist in users)
valid_telegram_ids = set()
for user_id, user_data in users.items():
    if user_data.get("telegram_id"):
        valid_telegram_ids.add(str(user_data["telegram_id"]))

# Remove stale entries from telegram_to_userid
stale_mappings = []
for telegram_id in list(telegram_to_userid.keys()):
    if telegram_id not in valid_telegram_ids:
        stale_mappings.append(telegram_id)
        telegram_to_userid.pop(telegram_id)
        telegram_mapping_rebuilt = True

if stale_mappings:
    logging.info(f"Removed {len(stale_mappings)} stale telegram mapping(s): {stale_mappings}")

# Now rebuild/verify mappings
for user_id, user_data in users.items():
    # Build/verify telegram_id lookup
    if user_data.get("telegram_id"):
        telegram_id_str = str(user_data["telegram_id"])
        # Check if mapping exists and is correct
        if telegram_to_userid.get(telegram_id_str) != user_id:
            telegram_to_userid[telegram_id_str] = user_id
            telegram_mapping_rebuilt = True
            logging.info(f"Rebuilding mapping: Telegram {telegram_id_str} -> Jellyfin {user_id}")
    
    # Build username lookup  
    if user_data.get("username"):
        username_to_uid[user_data["username"].lower()] = user_id
    
    # Build admins lookup
    if user_data.get("is_admin") and user_data.get("telegram_id"):
        telegram_id = str(user_data["telegram_id"])
        admins[telegram_id] = {
            "user_id": user_id,
            "username": user_data.get("username"),
            "added_at": user_data.get("created_at", int(time.time()))
        }
        # Ensure role is set to admin
        users[user_id]["role"] = ROLE_ADMIN

# Save synced admins, updated users, and telegram mapping
save_json(ADMINS_FILE, admins)
save_json(USERS_FILE, users)

# Save telegram mapping if it was rebuilt
if telegram_mapping_rebuilt:
    save_json(TELEGRAM_MAPPING_FILE, telegram_to_userid)
    logging.info(f"✅ Telegram mapping rebuilt and saved ({len(telegram_to_userid)} entries)")
else:
    logging.info(f"✅ Telegram mapping loaded ({len(telegram_to_userid)} entries)")

logging.info(f"✅ Username mapping built ({len(username_to_uid)} entries)")
logging.info(f"✅ Admin lookup built ({len(admins)} admin(s))")

# Validate: Must have at least 1 admin with telegram_id
if len(admins) == 0:
    print()
    print("╔══════════════════════════════════════════════════════════════════════════╗")
    print("║                     ⚠️  ADMIN CONFIGURATION REQUIRED                     ║")
    print("╚══════════════════════════════════════════════════════════════════════════╝")
    print()
    print("❌ No admin with Telegram ID found!")
    print()
    print("The bot requires at least ONE admin user with a Telegram ID to function.")
    print()
    print("📝 TO FIX THIS:")
    print(f"1. Open {USERS_FILE}")
    print("2. Find a user with 'is_admin': true")
    print("3. Add their Telegram ID:")
    print('   "telegram_id": 123456789')
    print()
    print("4. Get your Telegram ID from @userinfobot")
    print("5. Save the file and restart the bot")
    print()
    
    # Show which users are admins but missing telegram_id
    admin_users_without_telegram = []
    for user_id, user_data in users.items():
        if user_data.get("is_admin") and not user_data.get("telegram_id"):
            admin_users_without_telegram.append(user_data.get("username"))
    
    if admin_users_without_telegram:
        print(f"ℹ️  Admin users found (need telegram_id): {', '.join(admin_users_without_telegram)}")
        print()
    
    print("╔══════════════════════════════════════════════════════════════════════════╗")
    print("║            Bot cannot start without at least one admin setup            ║")
    print("╚══════════════════════════════════════════════════════════════════════════╝")
    print()
    sys.exit(1)

logging.info(f"Loaded {len(users)} users, {len(admins)} active admin(s) with Telegram ID")

# -------------------------------------------------
# JELLYFIN API FUNCTIONS
# -------------------------------------------------


def get_user_by_telegram_id(tg_id):
    """Get user data by telegram ID - returns (user_id, user_data) or (None, None)"""
    user_id = telegram_to_userid.get(str(tg_id))
    if user_id and user_id in users:
        return user_id, users[user_id]
    return None, None

# -------------------------------------------------
# TELEGRAM API FUNCTIONS
# -------------------------------------------------

def generate_upi_qr(amount, plan_name):
    """Generate UPI payment link"""
    upi_string = f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}&am={amount}&cu=INR&tn=Jellyfin {plan_name}"
    return upi_string

# -------------------------------------------------
# SUBSCRIPTION MANAGEMENT
# -------------------------------------------------

def check_subscription_status(user_id):
    """Check if user has an active subscription or privileged access"""
    # Check if user is admin or privileged (they always have access)
    if user_id in users:
        role = users[user_id].get("role", ROLE_REGULAR)
        if role in [ROLE_ADMIN, ROLE_PRIVILEGED]:
            return True, None  # Permanent access, no expiry
    
    # Check subscription for regular users
    if user_id not in subscriptions:
        return False, None
    
    sub = subscriptions[user_id]
    if sub["expires_at"] > time.time():
        return True, sub["expires_at"]
    return False, None


def enforce_regular_user_access(user_id, reason="subscription_check"):
    """Disable regular users who don't have an active subscription."""
    user = users.get(user_id)
    if not user:
        return False

    role = user.get("role", ROLE_REGULAR)
    if role != ROLE_REGULAR:
        return False

    has_access, _ = check_subscription_status(user_id)
    if has_access:
        return False

    username = user.get("username")
    if not username:
        return False

    success = jellyfin_disable_user(username)
    if validate_jellyfin_operation(f"disable user {username}", success, critical=True):
        logging.info(f"Disabled regular user {user_id} ({username}) without active subscription ({reason})")
        return True

    return False

def activate_subscription(user_id, duration_days):
    """Activate or extend subscription for a user - each day is exactly 24 hours (86400 seconds)"""
    # Validate inputs
    if duration_days <= 0:
        logging.error(f"Invalid duration_days: {duration_days}")
        raise ValueError("Duration must be positive")
    
    if user_id not in users:
        logging.error(f"Attempting to create subscription for non-existent user: {user_id}")
        raise ValueError(f"User {user_id} doesn't exist")
    
    # Check if user needs subscription (admins/privileged don't)
    role = users[user_id].get("role", ROLE_REGULAR)
    if role in [ROLE_ADMIN, ROLE_PRIVILEGED]:
        logging.warning(f"Attempting to create subscription for {role} user {user_id} - ignoring")
        return None  # Don't create subscription for permanent access users
    
    current_time = time.time()
    seconds_to_add = duration_days * SECONDS_PER_DAY  # Exactly 24 hours per day
    
    if user_id in subscriptions and subscriptions[user_id]["expires_at"] > current_time:
        # Extend existing subscription
        new_expiry = subscriptions[user_id]["expires_at"] + seconds_to_add
    else:
        # New subscription
        new_expiry = current_time + seconds_to_add
    
    subscriptions[user_id] = {
        "activated_at": current_time,
        "expires_at": new_expiry,
        "duration_days": duration_days
    }
    
    if not safe_file_save(SUBSCRIPTIONS_FILE, subscriptions, "subscriptions"):
        logging.error("Failed to save subscription - changes may be lost!")
    
    # Enable user in Jellyfin
    if user_id in users:
        username = users[user_id]["username"]
        success = jellyfin_enable_user(username)
        validate_jellyfin_operation(f"enable user {username}", success, critical=True)
    
    logging.info(f"Subscription activated for user {user_id}: {duration_days} day(s) = {seconds_to_add} seconds")
    
    return new_expiry

def subscription_monitor_loop():
    """Background thread to monitor and disable expired subscriptions"""
    while not shutdown_flag:
        try:
            current_time = time.time()
            expired_users = []
            
            for user_id, sub in list(subscriptions.items()):
                if sub["expires_at"] <= current_time:
                    expired_users.append(user_id)
            
            for user_id in expired_users:
                if user_id in users:
                    username = users[user_id]["username"]
                    
                    # Disable user in Jellyfin with validation
                    success = jellyfin_disable_user(username)
                    if not validate_jellyfin_operation(f"disable user {username}", success, critical=True):
                        # If disable failed, don't remove subscription - retry next cycle
                        logging.error(f"Failed to disable {username} - keeping subscription for retry")
                        continue
                    
                    # Notify user using their telegram_id
                    telegram_id = users[user_id].get("telegram_id")
                    if telegram_id:
                        try:
                            send_message(
                                telegram_id,
                                "⏰ Your Jellyfin subscription has expired.\n\n"
                                "Your account has been disabled. Use /subscribe to renew your access!"
                            )
                        except Exception as e:
                            logging.warning(f"Failed to notify user {user_id} about expiry: {e}")
                    
                    logging.info(f"Subscription expired for user {user_id} ({username})")
                else:
                    logging.warning(f"Orphaned subscription for non-existent user {user_id}")
                
                # Remove expired subscription only if disable succeeded
                subscriptions.pop(user_id, None)

            for user_id in list(users.keys()):
                enforce_regular_user_access(user_id, reason="monitor_loop")
            
            if expired_users:
                if not safe_file_save(SUBSCRIPTIONS_FILE, subscriptions, "subscriptions"):
                    logging.error("Failed to save subscriptions after expiry processing")
            
            # Check every 5 minutes
            time.sleep(300)
            
        except Exception as e:
            logging.error(f"Error in subscription monitor: {e}", exc_info=True)
            time.sleep(60)

# -------------------------------------------------
# COMMAND HANDLERS
# -------------------------------------------------

def handle_start(chat_id, tg_id, first_name):
    """Handle /start command"""
    is_admin = str(tg_id) in admins
    
    user_id, user = get_user_by_telegram_id(tg_id)
    if user:
        role = user.get("role", ROLE_REGULAR)
        username = user.get("username", "N/A")
        
        # Role-based greeting
        if is_admin:
            send_message(chat_id,
                f"👑 Welcome back, Admin {first_name}!\n\n"
                f"👤 Username: {username}\n\n"
                "🔧 Admin Commands:\n"
                "/pending - View pending registrations\n"
                "/payments - View pending payment screenshots\n"
                "/users - List all users\n"
                "/stats - View system statistics\n"
                "/broadcast - Send message to all users\n"
                "/message <username> - Send message to specific user\n"
                "/downgrade <username> <role> - Downgrade user role\n\n"
                "🔐 Personal:\n"
                "/resetpw - Reset your password"
            )
            return
        elif role == ROLE_PRIVILEGED:
            send_message(chat_id,
                f"⭐ Welcome back, {first_name}!\n\n"
                f"👤 Username: {username}\n"
                f"🎯 Status: Privileged User (No subscription required)\n\n"
                "Commands:\n"
                "/resetpw - Reset password\n"
                "/unlinkme - Unlink Telegram account\n"
                "/upgrade - Request role upgrade"
            )
            return
        else:
            # Regular user
            active, expires_at = check_subscription_status(user_id)
            
            if active and expires_at:  # Check expires_at is not None
                expiry_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
                send_message(chat_id,
                    f"👋 Welcome back, {first_name}!\n\n"
                    f"👤 Username: {username}\n"
                    f"✅ Subscription: Active\n"
                    f"⏰ Expires: {expiry_date}\n\n"
                    "Commands:\n"
                    "/subscribe - Renew subscription\n"
                    "/status - Check subscription\n"
                    "/resetpw - Reset password\n"
                    "/unlinkme - Unlink Telegram account\n"
                    "/upgrade - Request role upgrade"
                )
            elif active and not expires_at:
                # Edge case: user has permanent access but role not set correctly
                send_message(chat_id,
                    f"👋 Welcome back, {first_name}!\n\n"
                    f"👤 Username: {username}\n"
                    f"✅ Status: Active (Permanent Access)\n\n"
                    "Commands:\n"
                    "/status - Check status\n"
                    "/resetpw - Reset password\n"
                    "/unlinkme - Unlink Telegram account\n"
                    "/upgrade - Request role upgrade"
                )
            else:
                send_message(chat_id,
                    f"👋 Welcome back, {first_name}!\n\n"
                    f"👤 Username: {username}\n"
                    f"⚠️ Subscription: Expired\n\n"
                    "Use /subscribe to renew your access!\n\n"
                    "Other commands:\n"
                    "/resetpw - Reset password\n"
                    "/unlinkme - Unlink Telegram account\n"
                    "/upgrade - Request role upgrade"
                )
            return
    else:
        # New user - check if any Jellyfin users exist without telegram_id
        unlinked_users = {uid: u for uid, u in users.items() if not u.get("telegram_id")}
        
        if unlinked_users:
            send_message(chat_id,
                f"👋 Welcome, {first_name}!\n\n"
                "Choose an option:\n\n"
                "1️⃣ Link to existing Jellyfin account:\n"
                "/linkme <username>\n\n"
                "2️⃣ Create new account:\n"
                "/register\n\n"
                "💡 If you already have a Jellyfin account, use /linkme to connect it to your Telegram."
            )
        else:
            send_message(chat_id,
                f"👋 Welcome, {first_name}!\n\n"
                "To get started, please register:\n"
                "/register"
            )

def handle_register(chat_id, tg_id, username, first_name):
    """Handle /register command - prompt user to choose username"""
    user_id, user = get_user_by_telegram_id(tg_id)
    
    if user:
        send_message(chat_id, "✅ You are already registered!\n\nUse /subscribe to activate your subscription.")
        return
    
    if str(tg_id) in pending:
        send_message(chat_id, "⏳ Your registration is already pending approval.\n\nPlease wait for an admin to review your request.")
        return
    
    # Start username selection process
    awaiting_username[tg_id] = {
        "name": first_name,
        "requested_at": int(time.time())
    }
    
    send_message(chat_id,
        f"📝 Registration - Step 1: Choose Username\n\n"
        f"Please reply with your desired Jellyfin username.\n\n"
        f"⚠️ Username requirements:\n"
        f"• Must be unique\n"
        f"• Only letters, numbers, underscore, dash\n"
        f"• No spaces\n\n"
        f"Example: john_doe or user123\n\n"
        f"Send /cancel to cancel registration."
    )
    logging.info(f"User {tg_id} started registration process")

def validate_username(username):
    """Validate username format"""
    # Only allow letters, numbers, underscore, dash
    if not re.match(r'^[a-zA-Z0-9_-]+$', username):
        return False, "Username can only contain letters, numbers, underscore (_), and dash (-)"
    
    if len(username) < 3:
        return False, "Username must be at least 3 characters long"
    
    if len(username) > 20:
        return False, "Username must be less than 20 characters"
    
    return True, ""

def handle_subscribe(chat_id, tg_id):
    """Handle /subscribe command"""
    user_id, user = get_user_by_telegram_id(tg_id)
    
    if not user:
        send_message(chat_id, "❌ You need to register first!\n\nUse /register to get started.")
        return
    
    role = user.get("role", ROLE_REGULAR)
    
    # Admins and privileged users don't need subscriptions
    if role == ROLE_ADMIN:
        send_message(chat_id, "👑 Admins have unlimited access. No subscription needed!")
        return
    elif role == ROLE_PRIVILEGED:
        send_message(chat_id, "⭐ You have privileged access. No subscription needed!")
        return
    
    # Check if there are any subscription plans configured
    if not config.get("subscription_plans"):
        send_message(chat_id, "❌ No subscription plans are currently available.\n\nPlease contact an administrator.")
        logging.error("CRITICAL: No subscription plans configured in config.json!")
        return
    
    # Show subscription plans for regular users
    plans_text = "💳 Choose your subscription plan:\n\n"
    keyboard = []
    
    for plan_id, plan in config["subscription_plans"].items():
        plans_text += f"📅 {plan['name']}: ₹{plan['price']}\n"
        keyboard.append([{"text": f"{plan['name']} - ₹{plan['price']}", "callback_data": f"plan:{plan_id}"}])
    
    send_message(
        chat_id,
        plans_text,
        reply_markup=json.dumps({"inline_keyboard": keyboard})
    )

def handle_status(chat_id, tg_id):
    """Handle /status command"""
    user_id, user = get_user_by_telegram_id(tg_id)
    
    if not user:
        send_message(chat_id, "❌ You are not registered.\n\nUse /register to get started.")
        return
    
    role = user.get("role", ROLE_REGULAR)
    username = user.get("username", "N/A")
    
    # Admins and privileged users don't need to check subscription status
    if role == ROLE_ADMIN:
        send_message(chat_id, "👑 Admin Status\n\nYou have unlimited access. No subscription needed!")
        return
    elif role == ROLE_PRIVILEGED:
        send_message(chat_id, "⭐ Privileged User Status\n\nYou have permanent access. No subscription needed!")
        return
    
    # Regular users check subscription
    active, expires_at = check_subscription_status(user_id)
    
    if active and expires_at:  # Check expires_at is not None
        expiry_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
        days_left = int((expires_at - time.time()) / SECONDS_PER_DAY)
        
        send_message(chat_id,
            f"📊 Your Subscription Status\n\n"
            f"Username: `{user['username']}`\n"
            f"Status: ✅ Active\n"
            f"Expires: {expiry_date}\n"
            f"Days left: {days_left}\n\n"
            f"Use /subscribe to renew!",
            parse_mode="Markdown"
        )
    elif active and not expires_at:
        # Edge case: permanent access but role not set correctly
        send_message(chat_id,
            f"📊 Your Subscription Status\n\n"
            f"Username: `{user['username']}`\n"
            f"Status: ✅ Active (Permanent Access)\n\n"
            f"No expiration date.",
            parse_mode="Markdown"
        )
    else:
        send_message(chat_id,
            f"📊 Your Subscription Status\n\n"
            f"Username: `{user['username']}`\n"
            f"Status: ❌ Expired\n\n"
            f"Use /subscribe to activate!",
            parse_mode="Markdown"
        )

def handle_resetpw(chat_id, tg_id):
    """Handle /resetpw command"""
    user_id, user = get_user_by_telegram_id(tg_id)
    
    if not user:
        send_message(chat_id, "❌ You are not registered.")
        return
    
    send_message(chat_id, "🔐 Password reset request submitted.\n\nPlease wait for admin approval.")
    
    request_key = f"reset:{tg_id}"
    notify_admins(
        request_key,
        f"🔐 Password reset request:\n\n"
        f"Username: {user['username']}\n"
        f"Telegram ID: {tg_id}",
        reply_markup=json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"reset_ok:{tg_id}"},
                {"text": "❌ Reject", "callback_data": f"reset_no:{tg_id}"}
            ]]
        })
    )

def handle_pending(chat_id, tg_id):
    """Handle /pending command (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    if not pending:
        send_message(chat_id, "✅ No pending requests.")
        return
    
    for uid, p in pending.items():
        request_type = p.get("type", "register")
        request_key = f"{request_type}:{uid}"
        
        if request_type == "link":
            # Link request
            message_id = send_message(
                chat_id,
                f"🔗 Link Request:\n\n"
                f"👤 Name: {p['name']}\n"
                f"🎬 Jellyfin Username: {p['username']}\n"
                f"🆔 Telegram ID: {uid}",
                reply_markup=json.dumps({
                    "inline_keyboard": [[
                        {"text": "✅ Approve Link", "callback_data": f"link_approve:{uid}"},
                        {"text": "❌ Reject Link", "callback_data": f"link_reject:{uid}"}
                    ]]
                })
            )
            record_admin_request(request_key, chat_id, message_id)
        elif request_type == "unlink":
            # Unlink request
            message_id = send_message(
                chat_id,
                f"🔓 Unlink Request:\n\n"
                f"👤 Username: {p['username']}\n"
                f"🆔 Telegram ID: {uid}",
                reply_markup=json.dumps({
                    "inline_keyboard": [[
                        {"text": "✅ Approve Unlink", "callback_data": f"unlink_approve:{uid}"},
                        {"text": "❌ Reject Unlink", "callback_data": f"unlink_reject:{uid}"}
                    ]]
                })
            )
            record_admin_request(request_key, chat_id, message_id)
        else:
            # Registration request
            message_id = send_message(
                chat_id,
                f"📨 Registration Request:\n\n"
                f"Name: {p['name']}\n"
                f"Username: {p['username']}\n"
                f"Telegram ID: {uid}",
                reply_markup=json.dumps({
                    "inline_keyboard": [[
                        {"text": "✅ Approve", "callback_data": f"approve:{uid}"},
                        {"text": "❌ Reject", "callback_data": f"reject:{uid}"}
                    ]]
                })
            )
            record_admin_request(request_key, chat_id, message_id)


def handle_users(chat_id, tg_id):
    """Handle /users command (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return

    if not users:
        send_message(chat_id, "ℹ️ No registered users yet.")
        return

    admin_users = []
    privileged_users = []
    regular_users = []

    for uid, user in users.items():
        role = user.get("role", ROLE_REGULAR)
        if role == ROLE_ADMIN:
            admin_users.append((uid, user))
        elif role == ROLE_PRIVILEGED:
            privileged_users.append((uid, user))
        else:
            regular_users.append((uid, user))

    for group in (admin_users, privileged_users, regular_users):
        group.sort(key=lambda item: item[1].get("username", "").lower())

    header = "👥 Registered Users\n\n"
    header += f"📊 Total: {len(users)} users\n"
    header += f"👑 Admins: {len(admin_users)}\n"
    header += f"⭐ Privileged: {len(privileged_users)}\n"
    header += f"👤 Regular: {len(regular_users)}\n\n"
    header += "Tap a user button below to manage subscriptions, roles, and links."

    keyboard = []

    for uid, user in admin_users:
        keyboard.append([{"text": f"👑 {user.get('username', uid)}", "callback_data": f"user:{uid}"}])

    for uid, user in privileged_users:
        keyboard.append([{"text": f"⭐ {user.get('username', uid)}", "callback_data": f"user:{uid}"}])

    for uid, user in regular_users:
        active, expires_at = check_subscription_status(uid)
        status = "✅"
        if not active:
            status = "❌"
        elif expires_at:
            status = "⏰"
        keyboard.append([{"text": f"👤 {user.get('username', uid)} {status}", "callback_data": f"user:{uid}"}])

    send_message(chat_id, header, reply_markup=json.dumps({"inline_keyboard": keyboard}))

def handle_broadcast(chat_id, tg_id):
    """Handle /broadcast command (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    broadcast_mode[tg_id] = True
    target_broadcast.pop(tg_id, None)
    send_message(chat_id, "📢 Broadcast mode activated.\n\nSend your message (text, photo, or video) to broadcast to all users.\n\nSend /cancel to exit.")

def handle_message_user(chat_id, tg_id, target_username):
    """Handle /message command (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    # Use fast username lookup
    target_uid, target_user = get_user_by_username(target_username)
    
    if not target_uid:
        send_message(chat_id, f"❌ User '{target_username}' not found.")
        return
    
    broadcast_mode[tg_id] = True
    target_broadcast[tg_id] = target_uid
    send_message(chat_id, f"📨 Message mode activated for user: {target_username}\n\nSend your message.\n\nSend /cancel to exit.")

def handle_stats(chat_id, tg_id):
    """Handle /stats command (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    stats_text = get_watch_stats()
    send_message(chat_id, f"📊 Overall Watch Stats\n\n{stats_text}")

def handle_payments(chat_id, tg_id):
    """Handle /payments command (admin only) - show pending payment requests"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    pending_payments = {req_id: req for req_id, req in payment_requests.items() if req["status"] == "pending"}
    
    if not pending_payments:
        send_message(chat_id, "✅ No pending payment requests.")
        return
    
    send_message(chat_id, f"💳 Pending Payment Requests: {len(pending_payments)}\n\nUsers should send payment screenshots for verification.")
    
    for req_id, pay_req in pending_payments.items():
        user_id = pay_req["user_id"]
        plan_id = pay_req["plan_id"]
        
        if user_id in users and plan_id in config["subscription_plans"]:
            user = users[user_id]
            plan = config["subscription_plans"][plan_id]
            send_message(
                chat_id,
                f"📋 Request ID: {req_id}\n"
                f"👤 User: {user['username']}\n"
                f"📅 Plan: {plan['name']}\n"
                f"💰 Amount: ₹{plan['price']}\n"
                f"⏱️ Created: {datetime.fromtimestamp(pay_req['created_at']).strftime('%Y-%m-%d %H:%M')}"
            )

def handle_subinfo(chat_id, tg_id, username):
    """Handle /subinfo command - view user's subscription details (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    # Use fast username lookup
    target_uid, user = get_user_by_username(username)
    
    if not target_uid:
        send_message(chat_id, f"❌ User '{username}' not found.")
        return
    
    role = user.get("role", ROLE_REGULAR)
    
    msg = f"📊 Subscription Info: {username}\n\n"
    msg += f"👤 Username: {username}\n"
    msg += f"🎯 Role: {role.title()}\n"
    
    if role == ROLE_ADMIN:
        msg += f"👑 Status: Admin (Unlimited Access)\n"
    elif role == ROLE_PRIVILEGED:
        msg += f"⭐ Status: Privileged (Permanent Access)\n"
    else:
        active, expires_at = check_subscription_status(target_uid)
        
        if active and expires_at:
            expiry_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
            days_left = int((expires_at - time.time()) / SECONDS_PER_DAY)
            msg += f"✅ Status: Active\n"
            msg += f"⏰ Expires: {expiry_date}\n"
            msg += f"📅 Days Left: {days_left}\n"
        elif active and not expires_at:
            msg += f"✅ Status: Active (Permanent Access)\n"
        else:
            msg += f"❌ Status: Expired\n"
    
    msg += f"\n💡 Management Commands:\n"
    msg += f"/subextend {username} <days> - Extend subscription\n"
    msg += f"/subend {username} - End subscription immediately"
    
    send_message(chat_id, msg)

def handle_subextend(chat_id, tg_id, args):
    """Handle /subextend command - extend user's subscription (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    if len(args) < 2:
        send_message(chat_id, "❌ Usage: /subextend <username> <days>\n\nExample: /subextend john_doe 30")
        return
    
    username = args[0]
    try:
        days = int(args[1])
        if days <= 0:
            raise ValueError
    except ValueError:
        send_message(chat_id, "❌ Days must be a positive number.")
        return
    
    # Use fast username lookup
    target_uid, user = get_user_by_username(username)
    
    if not target_uid:
        send_message(chat_id, f"❌ User '{username}' not found.")
        return
    
    role = user.get("role", ROLE_REGULAR)
    
    if role in [ROLE_ADMIN, ROLE_PRIVILEGED]:
        send_message(chat_id, f"❌ Cannot extend subscription for {role} users. They have permanent access.")
        return
    
    # Extend subscription
    current_time = time.time()
    seconds_to_add = days * SECONDS_PER_DAY
    
    if target_uid in subscriptions and subscriptions[target_uid]["expires_at"] > current_time:
        # Extend existing subscription
        new_expiry = subscriptions[target_uid]["expires_at"] + seconds_to_add
        old_expiry = datetime.fromtimestamp(subscriptions[target_uid]["expires_at"]).strftime("%Y-%m-%d %H:%M")
    else:
        # New subscription from now
        new_expiry = current_time + seconds_to_add
        old_expiry = "N/A (Expired/None)"
    
    subscriptions[target_uid] = {
        "activated_at": subscriptions.get(target_uid, {}).get("activated_at", current_time),
        "expires_at": new_expiry,
        "duration_days": days  # Use consistent field name
    }
    save_subscriptions()
    
    new_expiry_str = datetime.fromtimestamp(new_expiry).strftime("%Y-%m-%d %H:%M")
    
    send_message(
        chat_id,
        f"✅ Subscription extended!\n\n"
        f"👤 User: {username}\n"
        f"📅 Days added: {days}\n"
        f"⏰ Old expiry: {old_expiry}\n"
        f"⏰ New expiry: {new_expiry_str}"
    )
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"➕ Subscription extended by {approver_label}\n\n"
        f"User: `{username}`\n"
        f"Days: {days}\n"
        f"New expiry: {new_expiry_str}",
        parse_mode="Markdown"
    )
    
    # Notify user
    telegram_id = user.get("telegram_id")
    if telegram_id:
        send_message(
            telegram_id,
            f"🎉 Your subscription has been extended!\n\n"
            f"📅 Duration: {days} days\n"
            f"⏰ New expiry: {new_expiry_str}\n\n"
            f"Extended by admin."
        )
    
    logging.info(f"Admin {tg_id} extended subscription for {username} by {days} days")

def handle_subend(chat_id, tg_id, username):
    """Handle /subend command - end user's subscription immediately (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    # Use fast username lookup
    target_uid, user = get_user_by_username(username)
    
    if not target_uid:
        send_message(chat_id, f"❌ User '{username}' not found.")
        return
    
    role = user.get("role", ROLE_REGULAR)
    
    if role in [ROLE_ADMIN, ROLE_PRIVILEGED]:
        send_message(chat_id, f"❌ Cannot end subscription for {role} users. They have permanent access.")
        return
    
    if target_uid not in subscriptions:
        send_message(chat_id, f"ℹ️ User '{username}' has no active subscription.")
        return
    
    # End subscription by setting expiry to now
    subscriptions[target_uid]["expires_at"] = time.time() - 1
    save_subscriptions()
    
    send_message(
        chat_id,
        f"✅ Subscription ended!\n\n"
        f"👤 User: {username}\n"
        f"⏰ Ended at: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"⛔ Subscription ended by {approver_label}\n\n"
        f"User: `{username}`",
        parse_mode="Markdown"
    )
    
    # Notify user
    telegram_id = user.get("telegram_id")
    if telegram_id:
        send_message(
            telegram_id,
            f"⚠️ Your subscription has been ended by an admin.\n\n"
            f"Use /subscribe to renew your access."
        )
    
    logging.info(f"Admin {tg_id} ended subscription for {username}")

def handle_linkme(chat_id, tg_id, username_to_link, first_name):
    """Handle /linkme command - user requests to link their telegram to existing Jellyfin account"""
    user_id, user = get_user_by_telegram_id(tg_id)
    if user_id:
        send_message(chat_id, "❌ Your Telegram account is already linked!\n\nUse /unlinkme to unlink first.")
        return
    
    # Check if user exists in pending
    if str(tg_id) in pending:
        send_message(chat_id, "⏳ You already have a pending request. Please wait for admin approval.")
        return
    
    # Use fast username lookup
    target_user_id, target_user = get_user_by_username(username_to_link)
    
    if not target_user_id:
        send_message(chat_id, f"❌ Jellyfin user '{username_to_link}' not found.\n\nPlease check the username and try again.")
        return
    
    # Check if user is already linked
    if target_user.get("telegram_id"):
        send_message(chat_id, f"❌ User '{username_to_link}' is already linked to another Telegram account.\n\nPlease contact an admin.")
        return
    
    # Add to pending with link request
    pending[str(tg_id)] = {
        "name": first_name,
        "username": username_to_link,
        "requested_at": int(time.time()),
        "type": "link",
        "jellyfin_user_id": target_user_id
    }
    save_json(PENDING_FILE, pending)
    
    send_message(chat_id,
        f"🔗 Link request submitted!\n\n"
        f"Username: `{username_to_link}`\n"
        f"Your Telegram ID: `{tg_id}`\n\n"
        f"⏳ Please wait for an admin to approve your link request.",
        parse_mode="Markdown"
    )
    
    request_key = f"link:{tg_id}"
    notify_admins(
        request_key,
        f"🔗 Account Link Request:\n\n"
        f"👤 Telegram Name: {first_name}\n"
        f"🆔 Telegram ID: {tg_id}\n"
        f"🎬 Jellyfin Username: {username_to_link}\n\n"
        f"⚠️ User wants to link their Telegram to existing Jellyfin account.",
        reply_markup=json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Approve Link", "callback_data": f"link_approve:{tg_id}"},
                {"text": "❌ Reject Link", "callback_data": f"link_reject:{tg_id}"}
            ]]
        })
    )
    
    logging.info(f"Link request from Telegram {tg_id} to Jellyfin user {username_to_link}")

def handle_unlinkme(chat_id, tg_id):
    """Handle /unlinkme command - user requests to unlink their telegram"""
    user_id, user = get_user_by_telegram_id(tg_id)
    if not user_id:
        send_message(chat_id, "❌ Your Telegram account is not linked to any Jellyfin account.")
        return
    
    # Check if user has pending unlink request
    if str(tg_id) in pending and pending[str(tg_id)].get("type") == "unlink":
        send_message(chat_id, "⏳ You already have a pending unlink request. Please wait for admin approval.")
        return
    
    # Add to pending with unlink request
    pending[str(tg_id)] = {
        "name": user.get("name", "User"),
        "username": user["username"],
        "requested_at": int(time.time()),
        "type": "unlink"
    }
    save_json(PENDING_FILE, pending)
    
    send_message(chat_id,
        f"🔓 Unlink request submitted!\n\n"
        f"Username: `{user['username']}`\n\n"
        f"⏳ Please wait for an admin to approve your unlink request.",
        parse_mode="Markdown"
    )
    
    request_key = f"unlink:{tg_id}"
    notify_admins(
        request_key,
        f"🔓 Account Unlink Request:\n\n"
        f"👤 User: {user['username']}\n"
        f"🆔 Telegram ID: {tg_id}\n\n"
        f"⚠️ User wants to unlink their Telegram account.",
        reply_markup=json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Approve Unlink", "callback_data": f"unlink_approve:{tg_id}"},
                {"text": "❌ Reject Unlink", "callback_data": f"unlink_reject:{tg_id}"}
            ]]
        })
    )
    
    logging.info(f"Unlink request from user {tg_id} ({user['username']})")


def handle_upgrade(chat_id, tg_id):
    """Handle /upgrade command - request role upgrade"""
    user_id, user = get_user_by_telegram_id(tg_id)
    if not user:
        send_message(chat_id, "❌ You are not registered.")
        return

    role = user.get("role", ROLE_REGULAR)
    if role == ROLE_ADMIN:
        send_message(chat_id, "👑 You are already an admin.")
        return

    if str(tg_id) in pending and pending[str(tg_id)].get("type") == "role_upgrade":
        send_message(chat_id, "⏳ You already have a pending upgrade request. Please wait for admin approval.")
        return

    target_role = ROLE_PRIVILEGED if role == ROLE_REGULAR else ROLE_ADMIN
    pending[str(tg_id)] = {
        "name": user.get("name", "User"),
        "username": user.get("username"),
        "requested_at": int(time.time()),
        "type": "role_upgrade",
        "current_role": role,
        "target_role": target_role
    }
    save_json(PENDING_FILE, pending)

    send_message(
        chat_id,
        f"⬆️ Upgrade request submitted!\n\n"
        f"Current role: {role}\n"
        f"Requested role: {target_role}\n\n"
        f"⏳ Please wait for an admin to approve your request."
    )

    request_key = f"role_upgrade:{tg_id}"
    notify_admins(
        request_key,
        f"⬆️ Role Upgrade Request:\n\n"
        f"👤 User: {user.get('username')}\n"
        f"🆔 Telegram ID: {tg_id}\n"
        f"🔼 From: {role} → {target_role}",
        reply_markup=json.dumps({
            "inline_keyboard": [[
                {"text": "✅ Approve Upgrade", "callback_data": f"role_upgrade:{tg_id}"},
                {"text": "❌ Reject Upgrade", "callback_data": f"role_upgrade_reject:{tg_id}"}
            ]]
        })
    )

def handle_admin_link(chat_id, tg_id, args):
    """Handle /link command (admin only) - admin links user to telegram"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    if len(args) < 2:
        send_message(chat_id, "❌ Usage: /link <username> <telegram_id>")
        return
    
    username_to_link = args[0]
    telegram_id_to_link = args[1]
    
    # Validate telegram_id is a number
    try:
        telegram_id_int = int(telegram_id_to_link)
    except ValueError:
        send_message(chat_id, "❌ Telegram ID must be a number.")
        return
    
    # Use fast username lookup
    target_user_id, target_user = get_user_by_username(username_to_link)
    
    if not target_user_id:
        send_message(chat_id, f"❌ Jellyfin user '{username_to_link}' not found.")
        return
    
    # Check if user is already linked
    if target_user.get("telegram_id"):
        send_message(chat_id, f"❌ User '{username_to_link}' is already linked to Telegram ID: {target_user['telegram_id']}\n\nUse /unlink first.")
        return
    
    # Check if telegram ID is already used
    existing_user_id, existing_user = get_user_by_telegram_id(telegram_id_int)
    if existing_user_id:
        send_message(chat_id, f"❌ Telegram ID {telegram_id_to_link} is already linked to user '{existing_user['username']}'")
        return
    
    # Link the account
    users[target_user_id]["telegram_id"] = telegram_id_int
    update_telegram_mapping(telegram_id_int, target_user_id)
    save_json(USERS_FILE, users)
    
    send_message(chat_id, f"✅ Successfully linked!\n\n👤 User: {username_to_link}\n🆔 Telegram ID: {telegram_id_to_link}", parse_mode="Markdown")
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"🔗 Link updated by {approver_label}\n\n"
        f"User: `{username_to_link}`\n"
        f"Telegram ID: {telegram_id_to_link}",
        parse_mode="Markdown"
    )
    
    # Notify the user
    try:
        send_message(
            telegram_id_int,
            f"✅ Your Telegram account has been linked!\n\n"
            f"🎬 Jellyfin Username: `{username_to_link}`\n\n"
            f"You can now use all bot features. Use /start to see available commands.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Could not notify user {telegram_id_int}: {e}")
    
    logging.info(f"Admin {tg_id} linked user {username_to_link} to Telegram {telegram_id_to_link}")

def handle_admin_unlink(chat_id, tg_id, args):
    """Handle /unlink command (admin only) - admin unlinks user from telegram"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    if len(args) < 1:
        send_message(chat_id, "❌ Usage: /unlink <username>")
        return
    
    username_to_unlink = args[0]
    
    # Use fast username lookup
    target_user_id, target_user = get_user_by_username(username_to_unlink)
    
    if not target_user_id:
        send_message(chat_id, f"❌ User '{username_to_unlink}' not found.")
        return
    
    # Check if user has telegram_id
    old_telegram_id = target_user.get("telegram_id")
    if not old_telegram_id:
        send_message(chat_id, f"❌ User '{username_to_unlink}' is not linked to any Telegram account.")
        return
    
    # Unlink the account
    users[target_user_id].pop("telegram_id", None)
    remove_telegram_mapping(old_telegram_id)
    save_json(USERS_FILE, users)
    
    send_message(chat_id, f"✅ Successfully unlinked!\n\n👤 User: {username_to_unlink}\n🆔 Previous Telegram ID: {old_telegram_id}", parse_mode="Markdown")
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"🔓 Unlink updated by {approver_label}\n\n"
        f"User: `{username_to_unlink}`\n"
        f"Telegram ID: {old_telegram_id}",
        parse_mode="Markdown"
    )
    
    # Notify the user
    try:
        send_message(
            old_telegram_id,
            f"⚠️ Your Telegram account has been unlinked from Jellyfin user '{username_to_unlink}'.\n\n"
            f"Contact an admin if you believe this was done in error."
        )
    except Exception as e:
        logging.warning(f"Could not notify user {old_telegram_id}: {e}")
    
    logging.info(f"Admin {tg_id} unlinked user {username_to_unlink} from Telegram {old_telegram_id}")


def handle_admin_downgrade(chat_id, tg_id, args):
    """Handle /downgrade command (admin only) - admin downgrades user role"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return

    if len(args) < 2:
        send_message(chat_id, "❌ Usage: /downgrade <username> <role>\n\nRoles: regular, privileged")
        return

    username = args[0]
    target_role = args[1].lower()
    if target_role not in [ROLE_REGULAR, ROLE_PRIVILEGED]:
        send_message(chat_id, "❌ Invalid role. Use: regular or privileged.")
        return

    target_uid, user = get_user_by_username(username)
    if not target_uid:
        send_message(chat_id, f"❌ User '{username}' not found.")
        return

    if user.get("role") == target_role:
        send_message(chat_id, f"ℹ️ User '{username}' is already {target_role}.")
        return

    users[target_uid]["role"] = target_role
    users[target_uid]["is_admin"] = target_role == ROLE_ADMIN
    save_json(USERS_FILE, users)

    if target_role == ROLE_REGULAR:
        enforce_regular_user_access(target_uid, reason="admin_downgrade")

    send_message(chat_id, f"✅ User `{username}` downgraded to {target_role}.", parse_mode="Markdown")
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"⬇️ Role downgraded by {approver_label}\n\n"
        f"User: `{username}`\n"
        f"Role: {target_role}",
        parse_mode="Markdown"
    )

    telegram_id = user.get("telegram_id")
    if telegram_id:
        send_message(
            telegram_id,
            f"⚠️ Your role has been changed by an admin.\n\n"
            f"New role: {target_role}"
        )


def handle_admin_upgrade(user_id, user, tg_id):
    current_role = user.get("role", ROLE_REGULAR)
    if current_role == ROLE_ADMIN:
        return False, "User is already an admin."

    target_role = ROLE_PRIVILEGED if current_role == ROLE_REGULAR else ROLE_ADMIN
    users[user_id]["role"] = target_role
    users[user_id]["is_admin"] = target_role == ROLE_ADMIN
    save_json(USERS_FILE, users)
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"⬆️ Role upgraded by {approver_label}\n\n"
        f"User: `{user.get('username')}`\n"
        f"Role: {target_role}",
        parse_mode="Markdown"
    )
    return True, target_role


def handle_admin_delete(user_id, user, tg_id):
    username = user.get("username", user_id)
    if not jellyfin_delete_user(user_id, username):
        return False

    users.pop(user_id, None)
    subscriptions.pop(user_id, None)
    remove_username_mapping(username)

    telegram_id = user.get("telegram_id")
    if telegram_id:
        remove_telegram_mapping(telegram_id)
        admins.pop(str(telegram_id), None)

    save_json(USERS_FILE, users)
    save_json(SUBSCRIPTIONS_FILE, subscriptions)
    save_json(ADMINS_FILE, admins)
    save_json(TELEGRAM_MAPPING_FILE, telegram_to_userid)
    approver_label = admins.get(str(tg_id), {}).get("username", str(tg_id))
    notify_admins_notice_except(
        tg_id,
        f"🗑️ User deleted by {approver_label}\n\n"
        f"User: `{username}`",
        parse_mode="Markdown"
    )
    return True



# -------------------------------------------------
# UPDATE HANDLER
# -------------------------------------------------

def handle_update(update):
    """Process incoming Telegram updates"""
    activity_logger = logging.getLogger('user_activity')
    
    try:
        # Log the raw update for debugging
        logging.debug(f"Received update: {json.dumps(update, indent=2)}")
        
        # Handle callback queries (button presses)
        if "callback_query" in update:
            callback = update["callback_query"]
            chat_id = callback["message"]["chat"]["id"]
            tg_id = callback["from"]["id"]
            username = callback["from"].get("username", "N/A")
            first_name = callback["from"].get("first_name", "User")
            data = callback["data"]
            is_admin = str(tg_id) in admins
            
            # Log callback query
            activity_logger.info(f"CALLBACK | User: {first_name} (@{username}) | TG_ID: {tg_id} | Data: {data}")
            logging.debug(f"Callback from user {tg_id} (@{username}): {data}")
            
            # Plan selection
            if data.startswith("plan:"):
                plan_id = data.split(":")[1]
                if plan_id not in config["subscription_plans"]:
                    send_message(chat_id, "❌ Invalid plan selected.")
                    return
                
                # Get user data
                user_id, user = get_user_by_telegram_id(tg_id)
                if not user_id:
                    send_message(chat_id, "❌ User not found. Please register first.")
                    return
                
                plan = config["subscription_plans"][plan_id]
                
                # Generate UPI payment link
                upi_link = generate_upi_qr(plan["price"], plan["name"])
                
                # Create payment request with both telegram_id and user_id (jellyfin_id)
                request_id = f"{tg_id}_{int(time.time())}"
                payment_requests[request_id] = {
                    "user_id": user_id,  # Jellyfin user ID
                    "telegram_id": str(tg_id),  # Telegram ID for lookup
                    "plan_id": plan_id,
                    "amount": plan["price"],
                    "created_at": int(time.time()),
                    "status": "pending"
                }
                save_json(PAYMENT_REQUESTS_FILE, payment_requests)
                
                send_message(
                    chat_id,
                    f"💳 Payment Details\n\n"
                    f"📋 Plan: {plan['name']}\n"
                    f"💰 Amount: ₹{plan['price']}\n"
                    f"⏱️ Duration: {plan['duration_days']} day(s) (24 hours per day)\n\n"
                    f"📱 Pay using UPI:\n{upi_link}\n\n"
                    f"✅ After completing payment:\n"
                    f"1. Take a screenshot of the payment confirmation\n"
                    f"2. Send the screenshot here\n"
                    f"3. Admin will verify and activate your subscription\n\n"
                    f"Request ID: `{request_id}`",
                    parse_mode="Markdown"
                )
                
                return

            if data.startswith("user:"):
                if not is_admin:
                    send_message(chat_id, "❌ Admin access required.")
                    return

                user_id = data.split(":", 1)[1]
                user = users.get(user_id)
                if not user:
                    send_message(chat_id, "❌ User not found.")
                    return
                delete_message(chat_id, callback["message"]["message_id"])

                keyboard = [
                    [{"text": "ℹ️ Sub Info", "callback_data": f"user_action:{user_id}:subinfo"}],
                    [{"text": "➕ Extend Sub", "callback_data": f"user_action:{user_id}:subextend"}],
                    [{"text": "⛔ End Sub", "callback_data": f"user_action:{user_id}:subend"}],
                    [{"text": "📊 Stats", "callback_data": f"user_action:{user_id}:stats"}],
                    [{"text": "⬆️ Upgrade", "callback_data": f"user_action:{user_id}:upgrade"}],
                    [{"text": "⬇️ Downgrade", "callback_data": f"user_action:{user_id}:downgrade"}],
                    [{"text": "🔗 Link TG", "callback_data": f"user_action:{user_id}:link"}],
                    [{"text": "🔓 Unlink TG", "callback_data": f"user_action:{user_id}:unlink"}],
                    [{"text": "🗑️ Delete User", "callback_data": f"user_action:{user_id}:delete"}],
                ]

                send_message(
                    chat_id,
                    f"Manage user: {user.get('username')}\nRole: {user.get('role', ROLE_REGULAR)}",
                    reply_markup=json.dumps({"inline_keyboard": keyboard})
                )
                return

            if data.startswith("user_action:"):
                if not is_admin:
                    send_message(chat_id, "❌ Admin access required.")
                    return

                _, user_id, action = data.split(":", 2)
                user = users.get(user_id)
                if not user:
                    send_message(chat_id, "❌ User not found.")
                    return

                delete_message(chat_id, callback["message"]["message_id"])

                username_value = user.get("username", user_id)

                if action == "subinfo":
                    delete_message(chat_id, callback["message"]["message_id"])
                    handle_subinfo(chat_id, tg_id, username_value)
                    return

                if action == "subend":
                    delete_message(chat_id, callback["message"]["message_id"])
                    handle_subend(chat_id, tg_id, username_value)
                    return

                if action == "stats":
                    delete_message(chat_id, callback["message"]["message_id"])
                    stats_text = get_watch_stats(user_id)
                    send_message(chat_id, f"📊 Watch Stats for {username_value}\n\n{stats_text}")
                    return

                if action == "upgrade":
                    success, result = handle_admin_upgrade(user_id, user, tg_id)
                    if success:
                        delete_message(chat_id, callback["message"]["message_id"])
                        send_message(chat_id, f"✅ User `{username_value}` upgraded to {result}.", parse_mode="Markdown")
                        if user.get("telegram_id"):
                            send_message(user["telegram_id"], f"✅ Your role has been upgraded to {result}.")
                    else:
                        send_message(chat_id, f"ℹ️ {result}")
                    return

                if action == "downgrade":
                    set_admin_user_action(tg_id, "downgrade", user_id, callback["message"]["message_id"])
                    send_message(chat_id, "Enter the new role: regular or privileged")
                    return

                if action == "subextend":
                    set_admin_user_action(tg_id, "subextend", user_id, callback["message"]["message_id"])
                    send_message(chat_id, "Enter number of days to extend the subscription:")
                    return

                if action == "link":
                    set_admin_user_action(tg_id, "link", user_id, callback["message"]["message_id"])
                    send_message(chat_id, "Send the Telegram ID to link to this user:")
                    return

                if action == "unlink":
                    handle_admin_unlink(chat_id, tg_id, [username_value])
                    return

                if action == "delete":
                    delete_message(chat_id, callback["message"]["message_id"])
                    if handle_admin_delete(user_id, user, tg_id):
                        send_message(chat_id, f"✅ User `{username_value}` deleted.", parse_mode="Markdown")
                    else:
                        send_message(chat_id, f"❌ Failed to delete `{username_value}`.", parse_mode="Markdown")
                    return
            
            # Admin approval/rejection actions
            if is_admin:
                parts = data.split(":")
                action = parts[0]
                uid = parts[1] if len(parts) > 1 else None
                
                if action == "approve":
                    request_key = f"register:{uid}"
                    with approval_lock:
                        p = pending.get(uid)
                        if not p:
                            send_message(chat_id, "⚠️ Request already processed or expired.")
                            return
                        
                        # Check if user already exists by telegram_id
                        # Handle uid as string (it comes from callback data)
                        try:
                            uid_int = int(uid)
                        except (ValueError, TypeError):
                            send_message(chat_id, f"❌ Invalid telegram ID format: {uid}")
                            return
                        
                        existing_user_id, existing_user = get_user_by_telegram_id(uid_int)
                        if existing_user_id:
                            pending.pop(uid, None)
                            safe_file_save(PENDING_FILE, pending, "pending requests")
                            send_message(chat_id, "⚠️ User already approved by another admin.")
                            return
                        
                        # Create Jellyfin account (disabled by default for regular users)
                        password = generate_password()
                        if not jellyfin_create_user(p["username"], password):
                            send_message(chat_id, f"❌ Failed to create Jellyfin account for {p['username']}.\n\nPlease check Jellyfin server logs.")
                            return
                        
                        # Get the Jellyfin user ID (CRITICAL: we need this!)
                        jellyfin_id = jellyfin_get_user_id(p["username"])
                        if not jellyfin_id:
                            send_message(chat_id, f"❌ Failed to retrieve Jellyfin user ID for {p['username']}.\n\nUser may have been created but cannot be managed by bot. Please check manually.")
                            # Try to clean up by deleting the user, but this might fail too
                            logging.error(f"CRITICAL: User {p['username']} created in Jellyfin but ID retrieval failed!")
                            return
                        
                        # Disable the user immediately (will be enabled on subscription)
                        if not jellyfin_disable_user(p["username"]):
                            send_message(chat_id, f"⚠️ User {p['username']} created but failed to disable.\n\nUser may have access without subscription!")
                            logging.error(f"Failed to disable newly created user {p['username']}")
                            # Continue anyway - user exists and we have the ID
                        
                        # Save user with CORRECT structure (jellyfin_id as key!)
                        users[jellyfin_id] = {
                            "jellyfin_id": jellyfin_id,
                            "username": p["username"],
                            "telegram_id": uid_int,  # Store as int
                            "created_at": int(time.time()),
                            "is_admin": False,
                            "role": ROLE_REGULAR
                        }
                        
                        # Update mappings
                        update_telegram_mapping(uid_int, jellyfin_id)
                        update_username_mapping(p["username"], jellyfin_id)
                        
                        if not safe_file_save(USERS_FILE, users, "users"):
                            send_message(chat_id, f"❌ CRITICAL: User created in Jellyfin but failed to save to database!\n\nUser: {p['username']}\nJellyfin ID: {jellyfin_id}\n\nManual intervention required!")
                            logging.critical(f"Failed to save user {jellyfin_id} ({p['username']}) to database after Jellyfin creation!")
                            return
                        
                        # Remove from pending
                        pending.pop(uid, None)
                        safe_file_save(PENDING_FILE, pending, "pending requests")
                        
                        approver_label = f"{first_name} (@{username})" if username != "N/A" else first_name
                        update_admin_request_buttons(request_key, "✅ Approved")

                        # Notify user
                        try:
                            send_message(uid_int,
                                f"✅ Registration approved!\n\n"
                                f"🎉 Your Jellyfin account has been created:\n\n"
                                f"Username: `{p['username']}`\n"
                                f"Password: `{password}`\n\n"
                                f"⚠️ Your account is currently *disabled*.\n\n"
                                f"Please subscribe using /subscribe to activate your access!",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logging.error(f"Failed to notify user {uid_int} about approval: {e}")
                        
                        # Notify admin
                        send_message(chat_id, f"✅ User `{p['username']}` approved and created successfully.\n\n"
                                    f"Jellyfin ID: {jellyfin_id}\n"
                                    f"Account is disabled until subscription.", parse_mode="Markdown")
                        notify_admins_notice_except(
                            tg_id,
                            f"✅ Registration approved by {approver_label}\n\n"
                            f"Username: `{p['username']}`\n"
                            f"Telegram ID: {uid}",
                            parse_mode="Markdown"
                        )
                        logging.info(f"User {uid} ({p['username']}) approved by admin {tg_id}, jellyfin_id: {jellyfin_id}")
                
                elif action == "reject":
                    request_key = f"register:{uid}"
                    p = pending.pop(uid, None)
                    if p:
                        update_admin_request_buttons(request_key, "❌ Rejected")
                        save_json(PENDING_FILE, pending)
                        send_message(uid, "❌ Your registration request was declined.\n\nPlease contact an administrator if you believe this was a mistake.")
                        send_message(chat_id, f"✅ Registration request for `{p.get('username', 'user')}` rejected.", parse_mode="Markdown")
                        logging.info(f"User {uid} registration rejected by admin {tg_id}")
                    else:
                        send_message(chat_id, "⚠️ Request already processed.")
                
                elif action == "reset_ok":
                    # uid here is telegram_id from callback, need to get jellyfin user_id
                    request_key = f"reset:{uid}"
                    user_id, user = get_user_by_telegram_id(uid)
                    if not user:
                        send_message(chat_id, "❌ User not found in system.")
                        return
                    
                    password = generate_password()
                    if jellyfin_reset_password(user["username"], password):
                        approver_label = f"{first_name} (@{username})" if username != "N/A" else first_name
                        update_admin_request_buttons(request_key, "✅ Approved")
                        send_message(uid, f"✅ Password reset approved!\n\n🔐 Your new Jellyfin password:\n\n`{password}`\n\nPlease save this securely.", parse_mode="Markdown")
                        send_message(chat_id, f"✅ Password reset for `{user['username']}` completed.", parse_mode="Markdown")
                        notify_admins_notice_except(
                            tg_id,
                            f"✅ Password reset approved by {approver_label}\n\n"
                            f"Username: `{user['username']}`",
                            parse_mode="Markdown"
                        )
                        logging.info(f"Password reset for user {user_id} (telegram {uid}) approved by admin {tg_id}")
                    else:
                        send_message(chat_id, f"❌ Failed to reset password for `{user['username']}`.", parse_mode="Markdown")
                        send_message(uid, "❌ Password reset failed. Please contact an administrator.")
                
                elif action == "reset_no":
                    # uid here is telegram_id from callback, need to get jellyfin user_id
                    request_key = f"reset:{uid}"
                    user_id, user = get_user_by_telegram_id(uid)
                    if user:
                        update_admin_request_buttons(request_key, "❌ Rejected")
                        send_message(uid, "❌ Your password reset request was declined.\n\nPlease contact an administrator if you need assistance.")
                        send_message(chat_id, f"✅ Password reset request for `{user['username']}` rejected.", parse_mode="Markdown")
                        logging.info(f"Password reset for user {user_id} (telegram {uid}) rejected by admin {tg_id}")
                    else:
                        send_message(chat_id, "⚠️ User not found.")
                
                elif action == "pay_approve":
                    # Format: pay_approve:request_id
                    request_id = uid
                    request_key = f"pay:{request_id}"
                    
                    if request_id not in payment_requests:
                        send_message(chat_id, "⚠️ Payment request not found or already processed.")
                        return
                    
                    pay_req = payment_requests[request_id]
                    user_id = pay_req["user_id"]  # Jellyfin user ID
                    telegram_id = pay_req.get("telegram_id", user_id)  # Get telegram_id, fallback to user_id for old requests
                    plan_id = pay_req["plan_id"]
                    
                    if user_id not in users:
                        send_message(chat_id, "❌ User not found in system.")
                        return
                    
                    if plan_id not in config["subscription_plans"]:
                        send_message(chat_id, "❌ Invalid subscription plan.")
                        return
                    
                    plan = config["subscription_plans"][plan_id]
                    
                    # Activate subscription (duration in 24-hour days)
                    expiry_timestamp = activate_subscription(user_id, plan["duration_days"])
                    expiry_date = datetime.fromtimestamp(expiry_timestamp).strftime("%Y-%m-%d %H:%M:%S")
                    
                    # Mark payment as approved
                    payment_requests[request_id]["status"] = "approved"
                    payment_requests[request_id]["approved_by"] = str(tg_id)
                    payment_requests[request_id]["approved_at"] = int(time.time())
                    save_json(PAYMENT_REQUESTS_FILE, payment_requests)
                    approver_label = f"{first_name} (@{username})" if username != "N/A" else first_name
                    update_admin_request_buttons(request_key, "✅ Approved")
                    
                    # Notify user (send to telegram_id)
                    send_message(
                        int(telegram_id),
                        f"✅ Payment verified and approved!\n\n"
                        f"🎉 Your subscription has been activated:\n\n"
                        f"📋 Plan: {plan['name']}\n"
                        f"⏱️ Duration: {plan['duration_days']} day(s)\n"
                        f"📅 Expires: {expiry_date}\n\n"
                        f"Your Jellyfin account is now enabled. Enjoy!",
                        parse_mode="Markdown"
                    )
                    
                    # Notify admin
                    send_message(
                        chat_id,
                        f"✅ Payment approved for `{users[user_id]['username']}`\n\n"
                        f"Plan: {plan['name']}\n"
                        f"Duration: {plan['duration_days']} day(s)\n"
                        f"Expires: {expiry_date}",
                        parse_mode="Markdown"
                    )
                    notify_admins_notice_except(
                        tg_id,
                        f"✅ Payment approved by {approver_label}\n\n"
                        f"Username: `{users[user_id]['username']}`\n"
                        f"Plan: {plan['name']}\n"
                        f"Expires: {expiry_date}",
                        parse_mode="Markdown"
                    )
                    
                    logging.info(f"Payment {request_id} approved by admin {tg_id} for user {user_id} (telegram_id: {telegram_id})")
                
                elif action == "pay_reject":
                    # Format: pay_reject:request_id
                    request_id = uid
                    request_key = f"pay:{request_id}"
                    
                    if request_id not in payment_requests:
                        send_message(chat_id, "⚠️ Payment request not found or already processed.")
                        return
                    
                    pay_req = payment_requests[request_id]
                    user_id = pay_req["user_id"]  # Jellyfin user ID
                    telegram_id = pay_req.get("telegram_id", user_id)  # Get telegram_id, fallback for old requests
                    
                    # Mark payment as rejected
                    payment_requests[request_id]["status"] = "rejected"
                    payment_requests[request_id]["rejected_by"] = str(tg_id)
                    payment_requests[request_id]["rejected_at"] = int(time.time())
                    save_json(PAYMENT_REQUESTS_FILE, payment_requests)
                    update_admin_request_buttons(request_key, "❌ Rejected")
                    
                    # Notify user (send to telegram_id)
                    if user_id in users:
                        send_message(
                            int(telegram_id),
                            "❌ Your payment screenshot was not verified.\n\n"
                            "Possible reasons:\n"
                            "• Payment incomplete or unclear screenshot\n"
                            "• Wrong amount paid\n"
                            "• Payment details mismatch\n\n"
                            "Please contact an administrator for assistance or try again with /subscribe"
                        )
                    
                    # Notify admin
                    send_message(
                        chat_id,
                        f"✅ Payment rejected for user {user_id}",
                        parse_mode="Markdown"
                    )
                    
                    logging.info(f"Payment {request_id} rejected by admin {tg_id}")
                
                elif action == "link_approve":
                    request_key = f"link:{uid}"
                    # Approve link request
                    if uid not in pending:
                        send_message(chat_id, "⚠️ Link request not found or already processed.")
                        return
                    
                    p = pending[uid]
                    if p.get("type") != "link":
                        send_message(chat_id, "⚠️ This is not a link request.")
                        return
                    
                    jellyfin_user_id = p.get("jellyfin_user_id")
                    
                    # Check if user still exists and is unlinked
                    if jellyfin_user_id not in users:
                        send_message(chat_id, "❌ Jellyfin user no longer exists.")
                        pending.pop(uid, None)
                        safe_file_save(PENDING_FILE, pending, "pending requests")
                        return
                    
                    if users[jellyfin_user_id].get("telegram_id"):
                        send_message(chat_id, "❌ User is already linked to another Telegram account.")
                        pending.pop(uid, None)
                        safe_file_save(PENDING_FILE, pending, "pending requests")
                        return
                    
                    # Convert uid to int (it's a string from callback data)
                    try:
                        telegram_id_int = int(uid)
                    except (ValueError, TypeError):
                        send_message(chat_id, f"❌ Invalid Telegram ID format: {uid}")
                        pending.pop(uid, None)
                        safe_file_save(PENDING_FILE, pending, "pending requests")
                        return
                    
                    # Link the account
                    users[jellyfin_user_id]["telegram_id"] = telegram_id_int
                    if "name" in p:  # Optional field
                        users[jellyfin_user_id]["name"] = p["name"]
                    
                    # Update mappings
                    update_telegram_mapping(telegram_id_int, jellyfin_user_id)
                    
                    if not safe_file_save(USERS_FILE, users, "users"):
                        logging.critical(f"Failed to save user link: Telegram {telegram_id_int} → Jellyfin {jellyfin_user_id}")
                    
                    # Remove from pending
                    pending.pop(uid, None)
                    safe_file_save(PENDING_FILE, pending, "pending requests")
                    
                    approver_label = f"{first_name} (@{username})" if username != "N/A" else first_name
                    update_admin_request_buttons(request_key, "✅ Approved")

                    # Notify user
                    try:
                        send_message(
                            telegram_id_int,
                            f"✅ Link request approved!\n\n"
                            f"🎬 Your Telegram is now linked to Jellyfin user: `{users[jellyfin_user_id]['username']}`\n\n"
                            f"You can now use all bot features. Use /start to see available commands.",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logging.error(f"Failed to notify user {telegram_id_int} about link approval: {e}")
                    
                    # Notify admin
                    send_message(
                        chat_id,
                        f"✅ Link approved!\n\n"
                        f"🆔 Telegram ID: {uid}\n"
                        f"👤 Jellyfin User: {users[jellyfin_user_id]['username']}",
                        parse_mode="Markdown"
                    )
                    notify_admins_notice_except(
                        tg_id,
                        f"✅ Link approved by {approver_label}\n\n"
                        f"Telegram ID: {uid}\n"
                        f"Jellyfin User: `{users[jellyfin_user_id]['username']}`",
                        parse_mode="Markdown"
                    )
                    
                    logging.info(f"Link approved by admin {tg_id}: Telegram {uid} → Jellyfin {users[jellyfin_user_id]['username']}")
                
                elif action == "link_reject":
                    request_key = f"link:{uid}"
                    # Reject link request
                    p = pending.pop(uid, None)
                    if p:
                        update_admin_request_buttons(request_key, "❌ Rejected")
                        save_json(PENDING_FILE, pending)
                        send_message(uid, "❌ Your link request was declined.\n\nPlease contact an administrator if you believe this was a mistake.")
                        send_message(chat_id, f"✅ Link request rejected for Telegram ID {uid}.")
                        logging.info(f"Link request rejected by admin {tg_id} for Telegram {uid}")
                    else:
                        send_message(chat_id, "⚠️ Link request not found or already processed.")
                
                elif action == "unlink_approve":
                    request_key = f"unlink:{uid}"
                    # Approve unlink request
                    user_id, user = get_user_by_telegram_id(uid)
                    if not user_id:
                        send_message(chat_id, "❌ User not found in system.")
                        pending.pop(uid, None)
                        save_json(PENDING_FILE, pending)
                        return
                    
                    username = user["username"]
                    
                    # Unlink the account
                    users[user_id].pop("telegram_id", None)
                    remove_telegram_mapping(int(uid))
                    save_json(USERS_FILE, users)
                    
                    # Remove from pending
                    pending.pop(uid, None)
                    save_json(PENDING_FILE, pending)
                    approver_label = f"{first_name} (@{username})" if username != "N/A" else first_name
                    update_admin_request_buttons(request_key, "✅ Approved")
                    
                    # Notify user
                    send_message(
                        int(uid),
                        f"✅ Unlink request approved!\n\n"
                        f"Your Telegram account has been unlinked from Jellyfin user: `{username}`\n\n"
                        f"You can link to a different account using /linkme <username> or create a new account with /register",
                        parse_mode="Markdown"
                    )
                    
                    # Notify admin
                    send_message(
                        chat_id,
                        f"✅ Unlink approved!\n\n"
                        f"👤 User: {username}\n"
                        f"🆔 Telegram ID: {uid}",
                        parse_mode="Markdown"
                    )
                    notify_admins_notice_except(
                        tg_id,
                        f"✅ Unlink approved by {approver_label}\n\n"
                        f"User: `{username}`\n"
                        f"Telegram ID: {uid}",
                        parse_mode="Markdown"
                    )
                    
                    logging.info(f"Unlink approved by admin {tg_id} for user {username}")
                
                elif action == "unlink_reject":
                    request_key = f"unlink:{uid}"
                    # Reject unlink request
                    if uid in pending:
                        username = pending[uid].get("username", "User")
                        pending.pop(uid, None)
                        save_json(PENDING_FILE, pending)
                        update_admin_request_buttons(request_key, "❌ Rejected")
                        send_message(uid, "❌ Your unlink request was declined.\n\nPlease contact an administrator if you need assistance.")
                        send_message(chat_id, f"✅ Unlink request rejected for user {username}.")
                        logging.info(f"Unlink request rejected by admin {tg_id} for user {username}")
                    else:
                        send_message(chat_id, "⚠️ Unlink request not found or already processed.")
                
                elif action == "role_upgrade":
                    request_key = f"role_upgrade:{uid}"
                    p = pending.get(uid)
                    if not p or p.get("type") != "role_upgrade":
                        send_message(chat_id, "⚠️ Upgrade request not found or already processed.")
                        return

                    user_id, user = get_user_by_telegram_id(uid)
                    if not user:
                        send_message(chat_id, "❌ User not found in system.")
                        return

                    target_role = p.get("target_role", ROLE_PRIVILEGED)
                    users[user_id]["role"] = target_role
                    users[user_id]["is_admin"] = target_role == ROLE_ADMIN
                    save_json(USERS_FILE, users)

                    pending.pop(uid, None)
                    save_json(PENDING_FILE, pending)
                    update_admin_request_buttons(request_key, "✅ Approved")

                    approver_label = f"{first_name} (@{username})" if username != "N/A" else first_name
                    send_message(
                        int(uid),
                        f"✅ Your role upgrade has been approved!\n\nNew role: {target_role}"
                    )
                    notify_admins_notice_except(
                        tg_id,
                        f"✅ Role upgrade approved by {approver_label}\n\n"
                        f"User: `{user.get('username')}`\n"
                        f"Role: {target_role}",
                        parse_mode="Markdown"
                    )
                
                elif action == "role_upgrade_reject":
                    request_key = f"role_upgrade:{uid}"
                    if uid in pending and pending[uid].get("type") == "role_upgrade":
                        pending.pop(uid, None)
                        save_json(PENDING_FILE, pending)
                        update_admin_request_buttons(request_key, "❌ Rejected")
                        send_message(uid, "❌ Your role upgrade request was declined.\n\nPlease contact an administrator if you need assistance.")
                        send_message(chat_id, "✅ Role upgrade request rejected.")
                    else:
                        send_message(chat_id, "⚠️ Upgrade request not found or already processed.")


        
        # Handle messages
        elif "message" in update:
            message = update["message"]
            chat_id = message["chat"]["id"]
            tg_id = message["from"]["id"]
            first_name = message["from"].get("first_name", "User")
            username = message["from"].get("username", "N/A")
            
            # Log all incoming messages
            message_text = message.get("text", "")
            message_type = "text"
            if "photo" in message:
                message_type = "photo"
            elif "video" in message:
                message_type = "video"
            elif "document" in message:
                message_type = "document"
            
            activity_logger.info(f"MESSAGE | User: {first_name} (@{username}) | TG_ID: {tg_id} | Type: {message_type} | Content: {message_text[:100]}")
            logging.debug(f"Message from {tg_id} (@{username}): Type={message_type}, Text={message_text}")
            
            is_admin = str(tg_id) in admins
            logging.debug(f"User {tg_id} admin status: {is_admin}")
            
            # Handle username input during registration
            if tg_id in awaiting_username and "text" in message:
                text = message["text"].strip()
                
                logging.debug(f"User {tg_id} is in registration flow, provided username: {text}")
                activity_logger.info(f"REGISTRATION_INPUT | User: {first_name} (@{username}) | TG_ID: {tg_id} | Username: {text}")
                
                # Allow cancel
                if text.lower() == "/cancel":
                    awaiting_username.pop(tg_id, None)
                    send_message(chat_id, "✅ Registration cancelled.")
                    logging.info(f"User {tg_id} cancelled registration")
                    return
                
                # Validate username format
                is_valid, error_msg = validate_username(text)
                if not is_valid:
                    logging.debug(f"Invalid username from {tg_id}: {error_msg}")
                    send_message(chat_id, f"❌ Invalid username: {error_msg}\n\nPlease try again or send /cancel to cancel.")
                    return
                
                # Check availability
                if not check_username_availability(text):
                    logging.debug(f"Username '{text}' already taken, requested by {tg_id}")
                    send_message(chat_id, 
                        f"❌ Username '{text}' is already taken.\n\n"
                        f"Please choose a different username or send /cancel to cancel.")
                    return
                
                # Username is valid and available - add to pending
                user_data = awaiting_username.pop(tg_id)
                pending[str(tg_id)] = {
                    "name": user_data["name"],
                    "username": text,
                    "requested_at": int(time.time())
                }
                save_json(PENDING_FILE, pending)
                
                send_message(chat_id,
                    f"✅ Registration request submitted!\n\n"
                    f"Username: `{text}`\n\n"
                    f"⏳ Please wait for an admin to approve your request.",
                    parse_mode="Markdown"
                )
                
                # Notify admins
                request_key = f"register:{tg_id}"
                notify_admins(
                    request_key,
                    f"📨 New registration request:\n\n"
                    f"Name: {user_data['name']}\n"
                    f"Username: {text}\n"
                    f"Telegram ID: {tg_id}",
                    reply_markup=json.dumps({
                        "inline_keyboard": [[
                            {"text": "✅ Approve", "callback_data": f"approve:{tg_id}"},
                            {"text": "❌ Reject", "callback_data": f"reject:{tg_id}"}
                        ]]
                    })
                )
                
                logging.info(f"Registration request from {tg_id} with username '{text}'")
                return
            
            # Handle payment screenshots (photos from non-admin users)
            if ("photo" in message or "video" in message) and not is_admin and str(tg_id) in telegram_to_userid:
                user_id = telegram_to_userid[str(tg_id)]
                user_info = users.get(user_id)
                
                if not user_info:
                    logging.error(f"User info not found for telegram_id {tg_id}, user_id {user_id}")
                    return
                
                username = user_info.get('username', 'Unknown')
                
                # Check if user has pending payment request (search by telegram_id)
                pending_payment = None
                for req_id, pay_req in payment_requests.items():
                    if pay_req.get("telegram_id") == str(tg_id) and pay_req["status"] == "pending":
                        pending_payment = req_id
                        break
                
                # Handle photo
                if "photo" in message:
                    if pending_payment:
                        # This is a PAYMENT SCREENSHOT - high priority
                        plan_id = payment_requests[pending_payment]["plan_id"]
                        plan = config["subscription_plans"][plan_id]
                        
                        # Forward screenshot to all admins with error tracking
                        successful_sends = 0
                        failed_sends = 0
                        request_key = f"pay:{pending_payment}"
                        for admin_id in admins:
                            message_id = send_photo(
                                admin_id,
                                message["photo"][-1]["file_id"],
                                caption=f"🚨 💳 PAYMENT SCREENSHOT 💳 🚨\n\n"
                                        f"👤 User: {username}\n"
                                        f"📋 Plan: {plan['name']}\n"
                                        f"💰 Amount: ₹{plan['price']}\n"
                                        f"⏱️ Duration: {plan['duration_days']} day(s)\n"
                                        f"🆔 Request ID: {pending_payment}\n"
                                        f"🔔 Telegram ID: {tg_id}",
                                reply_markup=json.dumps({
                                    "inline_keyboard": [[
                                        {"text": "✅ Approve Payment", "callback_data": f"pay_approve:{pending_payment}"},
                                        {"text": "❌ Reject Payment", "callback_data": f"pay_reject:{pending_payment}"}
                                    ]]
                                })
                            )
                            if message_id:
                                successful_sends += 1
                            else:
                                failed_sends += 1
                            record_admin_request(request_key, admin_id, message_id)
                        
                        logging.info(f"PAYMENT SCREENSHOT from user {tg_id} for request {pending_payment}: sent to {successful_sends}/{len(admins)} admins")
                        
                        if successful_sends > 0:
                            send_message(
                                chat_id,
                                "✅ Payment screenshot received!\n\n"
                                "⏳ Your payment is being verified by our admin team.\n"
                                "You will be notified once it's approved."
                            )
                        else:
                            send_message(
                                chat_id,
                                "⚠️ Screenshot received but there was an issue forwarding to admins.\n\n"
                                "Please contact support or try again later."
                            )
                            logging.error(f"Failed to send payment screenshot to any admin for request {pending_payment}")
                    else:
                        # Regular photo from user - forward to admins
                        caption = message.get("caption", "")
                        for admin_id in admins:
                            send_photo(
                                admin_id,
                                message["photo"][-1]["file_id"],
                                caption=f"📸 Photo from user\n\n"
                                        f"👤 User: {username}\n"
                                        f"🆔 Telegram ID: {tg_id}\n"
                                        f"📝 Caption: {caption if caption else '(no caption)'}"
                            )
                        logging.info(f"Photo from user {tg_id} forwarded to admins")
                    
                    return
                
                # Handle video
                if "video" in message:
                    caption = message.get("caption", "")
                    
                    if pending_payment:
                        # Video related to payment - mark as important
                        plan_id = payment_requests[pending_payment]["plan_id"]
                        plan = config["subscription_plans"][plan_id]
                        request_key = f"pay:{pending_payment}"
                        for admin_id in admins:
                            message_id = send_video(
                                admin_id,
                                message["video"]["file_id"],
                                caption=f"🚨 💳 PAYMENT VIDEO 💳 🚨\n\n"
                                        f"👤 User: {username}\n"
                                        f"📋 Plan: {plan['name']}\n"
                                        f"💰 Amount: ₹{plan['price']}\n"
                                        f"🆔 Request ID: {pending_payment}\n"
                                        f"🔔 Telegram ID: {tg_id}\n"
                                        f"📝 Caption: {caption if caption else '(no caption)'}",
                                reply_markup=json.dumps({
                                    "inline_keyboard": [[
                                        {"text": "✅ Approve Payment", "callback_data": f"pay_approve:{pending_payment}"},
                                        {"text": "❌ Reject Payment", "callback_data": f"pay_reject:{pending_payment}"}
                                    ]]
                                })
                            )
                            record_admin_request(request_key, admin_id, message_id)
                        logging.info(f"PAYMENT VIDEO from user {tg_id} for request {pending_payment} forwarded to admins")
                        send_message(
                            chat_id,
                            "✅ Payment video received!\n\n"
                            "⏳ Your payment is being verified by our admin team."
                        )
                    else:
                        # Regular video from user
                        for admin_id in admins:
                            send_video(
                                admin_id,
                                message["video"]["file_id"],
                                caption=f"🎥 Video from user\n\n"
                                        f"👤 User: {username}\n"
                                        f"🆔 Telegram ID: {tg_id}\n"
                                        f"📝 Caption: {caption if caption else '(no caption)'}"
                            )
                        logging.info(f"Video from user {tg_id} forwarded to admins")
                    
                    return
                
                # If neither photo nor video but we got here, there's an issue
                return
            
            if is_admin and tg_id in admin_user_actions and "text" in message and not message["text"].startswith("/"):
                action_payload = admin_user_actions.get(tg_id, {})
                action = action_payload.get("action")
                target_user_id = action_payload.get("user_id")
                source_message_id = action_payload.get("source_message_id")
                target_user = users.get(target_user_id)
                if not target_user:
                    clear_admin_user_action(tg_id)
                    send_message(chat_id, "❌ User not found.")
                    return

                text_value = message["text"].strip()
                target_username = target_user.get("username", target_user_id)

                if action == "subextend":
                    try:
                        days = int(text_value)
                        if days <= 0:
                            raise ValueError
                    except ValueError:
                        send_message(chat_id, "❌ Days must be a positive number.")
                        return
                    clear_admin_user_action(tg_id)
                    if source_message_id:
                        delete_message(chat_id, source_message_id)
                    handle_subextend(chat_id, tg_id, [target_username, str(days)])
                    return

                if action == "downgrade":
                    role_value = text_value.lower()
                    if role_value not in [ROLE_REGULAR, ROLE_PRIVILEGED]:
                        send_message(chat_id, "❌ Invalid role. Use: regular or privileged.")
                        return
                    clear_admin_user_action(tg_id)
                    if source_message_id:
                        delete_message(chat_id, source_message_id)
                    handle_admin_downgrade(chat_id, tg_id, [target_username, role_value])
                    return

                if action == "link":
                    try:
                        telegram_id_value = int(text_value)
                    except ValueError:
                        send_message(chat_id, "❌ Telegram ID must be a number.")
                        return
                    clear_admin_user_action(tg_id)
                    if source_message_id:
                        delete_message(chat_id, source_message_id)
                    handle_admin_link(chat_id, tg_id, [target_username, str(telegram_id_value)])
                    return

            if is_admin and "text" in message and message["text"].startswith("/") and message["text"].endswith("_info"):
                command = message["text"][1:]
                username_value = command[:-5]
                if not username_value:
                    return
                user_id, user = get_user_by_username(username_value)
                if not user_id:
                    send_message(chat_id, f"❌ User '{username_value}' not found.")
                    return
                delete_message(chat_id, message.get("message_id"))
                admin_user_actions.pop(tg_id, None)
                # Reuse user action menu
                keyboard = [
                    [{"text": "ℹ️ Sub Info", "callback_data": f"user_action:{user_id}:subinfo"}],
                    [{"text": "➕ Extend Sub", "callback_data": f"user_action:{user_id}:subextend"}],
                    [{"text": "⛔ End Sub", "callback_data": f"user_action:{user_id}:subend"}],
                    [{"text": "📊 Stats", "callback_data": f"user_action:{user_id}:stats"}],
                    [{"text": "⬆️ Upgrade", "callback_data": f"user_action:{user_id}:upgrade"}],
                    [{"text": "⬇️ Downgrade", "callback_data": f"user_action:{user_id}:downgrade"}],
                    [{"text": "🔗 Link TG", "callback_data": f"user_action:{user_id}:link"}],
                    [{"text": "🔓 Unlink TG", "callback_data": f"user_action:{user_id}:unlink"}],
                    [{"text": "🗑️ Delete User", "callback_data": f"user_action:{user_id}:delete"}],
                ]
                send_message(
                    chat_id,
                    f"Manage user: {user.get('username')}\nRole: {user.get('role', ROLE_REGULAR)}",
                    reply_markup=json.dumps({"inline_keyboard": keyboard})
                )
                return

            # Handle commands
            if "text" in message and message["text"].startswith("/"):
                text = message["text"]
                cmd = text.split()[0].lower()
                
                # Log command execution
                activity_logger.info(f"COMMAND | User: {first_name} (@{username}) | TG_ID: {tg_id} | Command: {cmd} | Full: {text}")
                logging.debug(f"Processing command from {tg_id}: {text}")
                
                if cmd == "/cancel":
                    broadcast_mode.pop(tg_id, None)
                    target_broadcast.pop(tg_id, None)
                    awaiting_username.pop(tg_id, None)
                    clear_admin_user_action(tg_id)
                    send_message(chat_id, "✅ Cancelled.")
                    logging.info(f"User {tg_id} cancelled current operation")
                    return
                
                if cmd == "/start":
                    logging.debug(f"Executing /start for user {tg_id}")
                    handle_start(chat_id, tg_id, first_name)
                elif cmd == "/register":
                    logging.debug(f"Executing /register for user {tg_id}")
                    handle_register(chat_id, tg_id, username, first_name)
                elif cmd == "/subscribe":
                    logging.debug(f"Executing /subscribe for user {tg_id}")
                    handle_subscribe(chat_id, tg_id)
                elif cmd == "/status":
                    logging.debug(f"Executing /status for user {tg_id}")
                    handle_status(chat_id, tg_id)
                elif cmd == "/resetpw":
                    logging.debug(f"Executing /resetpw for user {tg_id}")
                    handle_resetpw(chat_id, tg_id)
                elif cmd == "/pending":
                    logging.debug(f"Executing /pending for admin {tg_id}")
                    handle_pending(chat_id, tg_id)
                elif cmd == "/users":
                    logging.debug(f"Executing /users for admin {tg_id}")
                    handle_users(chat_id, tg_id)
                elif cmd == "/broadcast":
                    logging.debug(f"Executing /broadcast for admin {tg_id}")
                    handle_broadcast(chat_id, tg_id)
                elif cmd == "/message":
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        send_message(chat_id, "❌ Usage: /message <username>")
                        logging.debug(f"Invalid /message usage from {tg_id}")
                    else:
                        logging.debug(f"Executing /message for admin {tg_id} to {parts[1]}")
                        handle_message_user(chat_id, tg_id, parts[1])
                elif cmd == "/stats":
                    logging.debug(f"Executing /stats for admin {tg_id}")
                    handle_stats(chat_id, tg_id)
                elif cmd == "/payments":
                    logging.debug(f"Executing /payments for admin {tg_id}")
                    handle_payments(chat_id, tg_id)
                elif cmd == "/subinfo":
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        send_message(chat_id, "❌ Usage: /subinfo <username>")
                        logging.debug(f"Invalid /subinfo usage from {tg_id}")
                    else:
                        logging.debug(f"Executing /subinfo for admin {tg_id} on user {parts[1]}")
                        handle_subinfo(chat_id, tg_id, parts[1])
                elif cmd == "/subextend":
                    parts = text.split()
                    if len(parts) < 3:
                        send_message(chat_id, "❌ Usage: /subextend <username> <days>")
                        logging.debug(f"Invalid /subextend usage from {tg_id}")
                    else:
                        logging.debug(f"Executing /subextend for admin {tg_id}: {parts[1]} +{parts[2]} days")
                        handle_subextend(chat_id, tg_id, parts[1:])
                elif cmd == "/subend":
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        send_message(chat_id, "❌ Usage: /subend <username>")
                        logging.debug(f"Invalid /subend usage from {tg_id}")
                    else:
                        logging.debug(f"Executing /subend for admin {tg_id} on user {parts[1]}")
                        handle_subend(chat_id, tg_id, parts[1])
                elif cmd == "/linkme":
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        send_message(chat_id, "❌ Usage: /linkme <username>")
                        logging.debug(f"Invalid /linkme usage from {tg_id}")
                    else:
                        logging.debug(f"Executing /linkme for user {tg_id} with username {parts[1]}")
                        handle_linkme(chat_id, tg_id, parts[1], first_name)
                elif cmd == "/unlinkme":
                    logging.debug(f"Executing /unlinkme for user {tg_id}")
                    handle_unlinkme(chat_id, tg_id)
                elif cmd == "/upgrade":
                    logging.debug(f"Executing /upgrade for user {tg_id}")
                    handle_upgrade(chat_id, tg_id)
                elif cmd == "/link":
                    parts = text.split()
                    logging.debug(f"Executing /link for admin {tg_id} with args: {parts[1:]}")
                    handle_admin_link(chat_id, tg_id, parts[1:])
                elif cmd == "/unlink":
                    parts = text.split()
                    logging.debug(f"Executing /unlink for admin {tg_id} with args: {parts[1:]}")
                    handle_admin_unlink(chat_id, tg_id, parts[1:])
                elif cmd == "/downgrade":
                    parts = text.split()
                    logging.debug(f"Executing /downgrade for admin {tg_id} with args: {parts[1:]}")
                    handle_admin_downgrade(chat_id, tg_id, parts[1:])
                else:
                    # Unknown command
                    logging.warning(f"Unknown command from user {tg_id} (@{username}): {cmd}")
                    activity_logger.info(f"UNKNOWN_COMMAND | User: {first_name} (@{username}) | TG_ID: {tg_id} | Command: {cmd}")
                    send_message(chat_id, "❌ Unknown command. Use /start to see available commands.")
                
                return
            
            # Handle non-command text messages (log them as potential mistakes)
            if "text" in message and not message["text"].startswith("/"):
                non_command_text = message["text"]
                logging.debug(f"Non-command text from user {tg_id}: {non_command_text[:100]}")
                activity_logger.info(f"NON_COMMAND_TEXT | User: {first_name} (@{username}) | TG_ID: {tg_id} | Text: {non_command_text[:100]}")
            
            # Handle broadcast messages
            if is_admin and broadcast_mode.get(tg_id) and message:
                # Determine targets
                if target_broadcast.get(tg_id):
                    # Targeted broadcast
                    targets = [target_broadcast[tg_id]]
                    if targets[0] not in users:
                        send_message(chat_id, "❌ Target user no longer exists.")
                        broadcast_mode.pop(tg_id, None)
                        target_broadcast.pop(tg_id, None)
                        return
                else:
                    # Global broadcast (exclude admins)
                    targets = [uid for uid, u in users.items() if u.get("role") != ROLE_ADMIN and u.get("telegram_id")]
                
                if not targets:
                    send_message(chat_id, "ℹ️ No users to broadcast to.")
                    broadcast_mode.pop(tg_id, None)
                    target_broadcast.pop(tg_id, None)
                    return
                
                # Send broadcast
                success_count = 0
                for uid in targets:
                    try:
                        # Get telegram_id from user data
                        target_telegram_id = users[uid].get("telegram_id")
                        if not target_telegram_id:
                            continue
                        
                        if "text" in message and message["text"]:
                            send_message(target_telegram_id, message["text"])
                            success_count += 1
                        elif "photo" in message and message["photo"]:
                            send_photo(target_telegram_id, message["photo"][-1]["file_id"])
                            success_count += 1
                        elif "video" in message and message.get("video", {}).get("file_id"):
                            send_video(target_telegram_id, message["video"]["file_id"])
                            success_count += 1
                    except Exception as e:
                        logging.warning(f"Failed to broadcast to {uid}: {e}")
                
                # Confirm to admin
                if target_broadcast.get(tg_id):
                    send_message(chat_id, f"✅ Message sent to `{users[targets[0]]['username']}`", parse_mode="Markdown")
                else:
                    send_message(chat_id, f"✅ Broadcast sent to {success_count}/{len(targets)} users")
                
                broadcast_mode.pop(tg_id, None)
                target_broadcast.pop(tg_id, None)
                return
    
    except Exception as e:
        # Get user info if available
        user_info = "Unknown"
        try:
            if "callback_query" in update:
                tg_id = update["callback_query"]["from"]["id"]
                username = update["callback_query"]["from"].get("username", "N/A")
                user_info = f"TG_ID: {tg_id} (@{username})"
            elif "message" in update:
                tg_id = update["message"]["from"]["id"]
                username = update["message"]["from"].get("username", "N/A")
                user_info = f"TG_ID: {tg_id} (@{username})"
        except:
            pass
        
        logging.error(f"Update handling failed for {user_info}: {e}", exc_info=True)
        logging.error(f"Update data: {json.dumps(update, indent=2)}")
        
        # Log to activity logger as well
        activity_logger = logging.getLogger('user_activity')
        activity_logger.error(f"ERROR | {user_info} | Exception: {str(e)}")


# -------------------------------------------------
# POLLING LOOP
# -------------------------------------------------

def run():
    """Main polling loop with graceful shutdown"""
    offset = None
    logging.info("🚀 Bot started (long-polling mode)")
    logging.info(f"📡 Telegram API: {TELEGRAM_API}")
    logging.info(f"🎬 Jellyfin URL: {JELLYFIN_URL}")
    logging.info(f"💳 UPI ID: {UPI_ID}")
    
    # Start subscription monitor thread
    monitor_thread = Thread(target=subscription_monitor_loop, daemon=True, name="SubscriptionMonitor")
    monitor_thread.start()
    logging.info("✓ Subscription monitor thread started")
    
    # Start cleanup thread
    cleanup_thread = Thread(target=cleanup_loop, daemon=True, name="DataCleanup")
    cleanup_thread.start()
    logging.info("✓ Data cleanup thread started")
    
    while not shutdown_flag:
        try:
            r = HTTP_SESSION.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"timeout": POLL_LONG_TIMEOUT, "offset": offset},
                timeout=POLL_TIMEOUT
            )
            
            if r.status_code != 200:
                logging.error(f"Telegram API error: {r.status_code} - {r.text}")
                time.sleep(5)
                continue
            
            result = r.json()
            if not result.get("ok"):
                logging.error(f"Telegram API returned error: {result}")
                time.sleep(5)
                continue
            
            for update in result.get("result", []):
                if shutdown_flag:
                    break
                handle_update(update)
                offset = update.get("update_id", 0) + 1
        
        except requests.exceptions.Timeout:
            logging.debug("Polling timeout (expected, continuing...)")
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error during polling: {e}")
            time.sleep(5)
        except Exception as e:
            logging.error(f"Polling error: {e}", exc_info=True)
            time.sleep(5)
    
    logging.info("👋 Bot shutdown complete")

# -------------------------------------------------
# ENTRY POINT
# -------------------------------------------------

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logging.info("🛑 Interrupted by user")
        shutdown_flag = True
        sys.exit(0)
    except Exception as e:
        logging.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
