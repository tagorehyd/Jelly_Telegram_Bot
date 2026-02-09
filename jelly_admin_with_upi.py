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

# -------------------------------------------------
# CONFIG LOADING + BOOTSTRAP
# -------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = BASE_DIR / "logs"

CONFIG_FILE = CONFIG_DIR / "config.json"
SECRETS_FILE = CONFIG_DIR / "secrets.json"
STRINGS_FILE = CONFIG_DIR / "strings.json"

DEFAULT_CONFIG = {
    "jellyfin": {
        "url": "http://your-jellyfin-server:8096"
    },
    "payment": {
        "upi_id": "yourname@paytm",
        "upi_name": "Your Name"
    },
    "storage": {
        "admins": "data/admins.json",
        "users": "data/users.json",
        "pending": "data/pending.json",
        "subscriptions": "data/subscriptions.json",
        "payment_requests": "data/payment_requests.json",
        "telegram_mapping": "data/telegram_mapping.json"
    },
    "subscription_plans": {
        "1day": {"duration_days": 1, "price": 5, "name": "1 Day"},
        "1week": {"duration_days": 7, "price": 10, "name": "1 Week"},
        "1month": {"duration_days": 30, "price": 35, "name": "1 Month"}
    }
}

DEFAULT_SECRETS = {
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN_HERE",
    "jellyfin_api_key": "YOUR_JELLYFIN_API_KEY_HERE"
}

def read_json_file(filepath):
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {filepath}: {e}")

def write_json_file(filepath, data):
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

def load_strings():
    if not STRINGS_FILE.exists():
        raise RuntimeError(f"Missing required strings file: {STRINGS_FILE}")
    return read_json_file(STRINGS_FILE)

STRINGS = load_strings()

def ensure_config_files():
    """Create missing config/secrets files for first-time setup"""
    config_data = DEFAULT_CONFIG
    secrets_data = DEFAULT_SECRETS
    created_files = []

    if not CONFIG_FILE.exists():
        write_json_file(CONFIG_FILE, config_data)
        created_files.append(CONFIG_FILE)

    if not SECRETS_FILE.exists():
        write_json_file(SECRETS_FILE, secrets_data)
        created_files.append(SECRETS_FILE)

    if not created_files:
        return False

    created_list = "\n".join(f"- {path}" for path in created_files)
    print(
        STRINGS["first_run_message"].format(
            created_list=created_list,
            config_file=CONFIG_FILE
        )
    )
    return True

def load_config():
    if ensure_config_files():
        sys.exit(0)

    config = read_json_file(CONFIG_FILE)
    secrets_config = read_json_file(SECRETS_FILE)

    # Validate required keys
    required_config_keys = {
        "jellyfin": dict,
        "storage": dict
    }

    required_secret_keys = {
        "bot_token": str,
        "jellyfin_api_key": str
    }

    for key, expected_type in required_config_keys.items():
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")
        if not isinstance(config[key], expected_type):
            raise ValueError(f"Config key '{key}' must be of type {expected_type.__name__}")

    for key, expected_type in required_secret_keys.items():
        if key not in secrets_config:
            raise ValueError(f"Missing required secrets key: {key}")
        if not isinstance(secrets_config[key], expected_type):
            raise ValueError(f"Secrets key '{key}' must be of type {expected_type.__name__}")

    # Validate nested keys
    if "url" not in config["jellyfin"]:
        raise ValueError("jellyfin config must contain 'url'")

    if not all(k in config["storage"] for k in ["admins", "users", "pending"]):
        raise ValueError("storage config must contain 'admins', 'users', and 'pending'")

    # Add payment config if not present
    if "payment" not in config:
        config["payment"] = {
            "upi_id": "yourname@paytm",
            "upi_name": "Your Name"
        }

    # Add subscription plans if not present
    if "subscription_plans" not in config:
        config["subscription_plans"] = {
            "1day": {"duration_days": 1, "price": 5, "name": "1 Day"},
            "1week": {"duration_days": 7, "price": 10, "name": "1 Week"},
            "1month": {"duration_days": 30, "price": 35, "name": "1 Month"}
        }

    # Add storage paths if not present
    if "subscriptions" not in config["storage"]:
        config["storage"]["subscriptions"] = "data/subscriptions.json"
    if "payment_requests" not in config["storage"]:
        config["storage"]["payment_requests"] = "data/payment_requests.json"
    if "telegram_mapping" not in config["storage"]:
        config["storage"]["telegram_mapping"] = "data/telegram_mapping.json"

    return config, secrets_config

config, secrets_config = load_config()

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

# -------------------------------------------------
# JELLYFIN USER BOOTSTRAP (SECOND RUN)
# -------------------------------------------------

def fetch_jellyfin_users():
    resp = requests.get(
        f"{JELLYFIN_URL}/Users",
        headers={"X-Emby-Token": JELLYFIN_API_KEY},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()


def bootstrap_users_from_server():
    users_file = Path(config["storage"]["users"])

    # Check if users.json already has data
    if users_file.exists() and users_file.stat().st_size > 2:
        return

    print("🔄 Second run detected - Loading users from Jellyfin server...")

    try:
        jelly_users = fetch_jellyfin_users()
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

def setup_logging():
    """Setup comprehensive logging system with separate info and debug logs"""
    
    # Create logs directory if it doesn't exist
    LOGS_DIR.mkdir(exist_ok=True)
    
    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)-20s | Line %(lineno)-4d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Get root logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # Capture everything
    
    # Remove any existing handlers
    logger.handlers.clear()
    
    # Handler 1: Console output (INFO and above)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    logger.addHandler(console_handler)
    
    # Handler 2: General log file (INFO and above)
    info_handler = logging.FileHandler(LOGS_DIR / "bot.log", encoding='utf-8')
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(simple_formatter)
    logger.addHandler(info_handler)
    
    # Handler 3: Debug log file (ALL messages including DEBUG)
    debug_handler = logging.FileHandler(LOGS_DIR / "debug.log", encoding='utf-8')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(detailed_formatter)
    logger.addHandler(debug_handler)
    
    # Handler 4: Error log file (ERROR and CRITICAL only)
    error_handler = logging.FileHandler(LOGS_DIR / "error.log", encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    logger.addHandler(error_handler)
    
    # Handler 5: User activity log (custom logger for tracking all user interactions)
    activity_logger = logging.getLogger('user_activity')
    activity_logger.setLevel(logging.DEBUG)
    activity_logger.propagate = False  # Don't propagate to root logger
    
    activity_handler = logging.FileHandler(LOGS_DIR / "user_activity.log", encoding='utf-8')
    activity_handler.setLevel(logging.DEBUG)
    activity_formatter = logging.Formatter(
        '%(asctime)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    activity_handler.setFormatter(activity_formatter)
    activity_logger.addHandler(activity_handler)
    
    logging.info("=" * 80)
    logging.info("LOGGING SYSTEM INITIALIZED")
    logging.info("=" * 80)
    logging.info(f"Console Output: INFO level and above")
    logging.info(f"General Log: {LOGS_DIR / 'bot.log'} (INFO+)")
    logging.info(f"Debug Log: {LOGS_DIR / 'debug.log'} (ALL messages)")
    logging.info(f"Error Log: {LOGS_DIR / 'error.log'} (ERROR+)")
    logging.info(f"Activity Log: {LOGS_DIR / 'user_activity.log'} (All user interactions)")
    logging.info("=" * 80)

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

def jellyfin_create_user(username, password):
    """Create a new Jellyfin user"""
    try:
        resp = requests.post(
            f"{JELLYFIN_URL}/Users/New",
            headers={"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"},
            json={"Name": username, "Password": password},
            timeout=10
        )
        if resp.status_code == 200:
            logging.info(f"Jellyfin user '{username}' created successfully")
            return True
        else:
            logging.error(f"Failed to create Jellyfin user '{username}': {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logging.error(f"Error creating Jellyfin user '{username}': {e}")
        return False

def jellyfin_get_user_id(username):
    """Get Jellyfin user ID by username"""
    try:
        resp = requests.get(
            f"{JELLYFIN_URL}/Users",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=10
        )
        if resp.status_code == 200:
            for user in resp.json():
                if user["Name"].lower() == username.lower():
                    return user["Id"]
        return None
    except Exception as e:
        logging.error(f"Error getting Jellyfin user ID for '{username}': {e}")
        return None

def jellyfin_enable_user(username):
    """Enable a Jellyfin user"""
    user_id = jellyfin_get_user_id(username)
    if not user_id:
        logging.error(f"Cannot enable user '{username}': User not found")
        return False
    
    try:
        # Get current user policy
        resp = requests.get(
            f"{JELLYFIN_URL}/Users/{user_id}",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=10
        )
        if resp.status_code != 200:
            logging.error(f"Failed to get user policy for '{username}'")
            return False
        
        user_data = resp.json()
        policy = user_data.get("Policy", {})
        policy["IsDisabled"] = False
        
        # Update user policy
        resp = requests.post(
            f"{JELLYFIN_URL}/Users/{user_id}/Policy",
            headers={"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"},
            json=policy,
            timeout=10
        )
        
        if resp.status_code == 204 or resp.status_code == 200:
            logging.info(f"Jellyfin user '{username}' enabled successfully")
            return True
        else:
            logging.error(f"Failed to enable Jellyfin user '{username}': {resp.status_code}")
            return False
    except Exception as e:
        logging.error(f"Error enabling Jellyfin user '{username}': {e}")
        return False

def jellyfin_disable_user(username):
    """Disable a Jellyfin user"""
    user_id = jellyfin_get_user_id(username)
    if not user_id:
        logging.error(f"Cannot disable user '{username}': User not found")
        return False
    
    try:
        # Get current user policy
        resp = requests.get(
            f"{JELLYFIN_URL}/Users/{user_id}",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=10
        )
        if resp.status_code != 200:
            logging.error(f"Failed to get user policy for '{username}'")
            return False
        
        user_data = resp.json()
        policy = user_data.get("Policy", {})
        policy["IsDisabled"] = True
        
        # Update user policy
        resp = requests.post(
            f"{JELLYFIN_URL}/Users/{user_id}/Policy",
            headers={"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"},
            json=policy,
            timeout=10
        )
        
        if resp.status_code == 204 or resp.status_code == 200:
            logging.info(f"Jellyfin user '{username}' disabled successfully")
            return True
        else:
            logging.error(f"Failed to disable Jellyfin user '{username}': {resp.status_code}")
            return False
    except Exception as e:
        logging.error(f"Error disabling Jellyfin user '{username}': {e}")
        return False

def jellyfin_reset_password(username, new_password):
    """Reset password for a Jellyfin user"""
    user_id = jellyfin_get_user_id(username)
    if not user_id:
        logging.error(f"Cannot reset password for '{username}': User not found")
        return False
    
    try:
        resp = requests.post(
            f"{JELLYFIN_URL}/Users/{user_id}/Password",
            headers={"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "application/json"},
            json={"NewPw": new_password},
            timeout=10
        )
        
        if resp.status_code == 204 or resp.status_code == 200:
            logging.info(f"Password reset for Jellyfin user '{username}' successful")
            return True
        else:
            logging.error(f"Failed to reset password for '{username}': {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logging.error(f"Error resetting password for '{username}': {e}")
        return False

# -------------------------------------------------
# TELEGRAM API FUNCTIONS
# -------------------------------------------------

def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    """Send a text message"""
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Failed to send message to {chat_id}: {e}")

def send_photo(chat_id, photo, caption=None, reply_markup=None):
    """Send a photo"""
    payload = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(f"{TELEGRAM_API}/sendPhoto", json=payload, timeout=10)
        if response.status_code != 200:
            logging.error(f"Failed to send photo to {chat_id}: {response.status_code} - {response.text}")
        else:
            logging.info(f"Photo sent successfully to {chat_id}")
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Failed to send photo to {chat_id}: {e}")
        return False

def send_video(chat_id, video, caption=None, reply_markup=None):
    """Send a video"""
    payload = {"chat_id": chat_id, "video": video}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(f"{TELEGRAM_API}/sendVideo", json=payload, timeout=10)
        if response.status_code != 200:
            logging.error(f"Failed to send video to {chat_id}: {response.status_code} - {response.text}")
        else:
            logging.info(f"Video sent successfully to {chat_id}")
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Failed to send video to {chat_id}: {e}")
        return False

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
            
            for user_id, sub in subscriptions.items():
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
                "/message <username> - Send message to specific user\n\n"
                "👥 User Management:\n"
                "/link <username> <telegram_id> - Link user to Telegram ID\n"
                "/unlink <username> - Unlink user from Telegram ID\n\n"
                "💳 Subscription Management:\n"
                "/subinfo <username> - View subscription details\n"
                "/subextend <username> <days> - Extend subscription\n"
                "/subend <username> - End subscription\n\n"
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
                "/unlinkme - Unlink Telegram account"
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
                    "/unlinkme - Unlink Telegram account"
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
                    "/unlinkme - Unlink Telegram account"
                )
            else:
                send_message(chat_id,
                    f"👋 Welcome back, {first_name}!\n\n"
                    f"👤 Username: {username}\n"
                    f"⚠️ Subscription: Expired\n\n"
                    "Use /subscribe to renew your access!\n\n"
                    "Other commands:\n"
                    "/resetpw - Reset password\n"
                    "/unlinkme - Unlink Telegram account"
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

def check_username_availability(username):
    """Check if username is available in Jellyfin - returns False on any error (safe default)"""
    try:
        resp = requests.get(
            f"{JELLYFIN_URL}/Users",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
            timeout=10
        )
        if resp.status_code != 200:
            logging.error(f"Jellyfin API error while checking username: {resp.status_code} - {resp.text}")
            return False  # Safe default - assume not available on error
        
        existing_users = resp.json()
        for user in existing_users:
            if user["Name"].lower() == username.lower():
                return False  # Username taken
        return True  # Username available
    except requests.exceptions.Timeout:
        logging.error(f"Timeout checking username availability for '{username}'")
        return False  # Safe default
    except Exception as e:
        logging.error(f"Error checking username availability for '{username}': {e}")
        return False  # Safe default

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
    
    # Notify admins
    for admin_id in admins:
        send_message(
            admin_id,
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
        
        if request_type == "link":
            # Link request
            send_message(
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
        elif request_type == "unlink":
            # Unlink request
            send_message(
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
        else:
            # Registration request
            send_message(
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


def handle_users(chat_id, tg_id):
    """Handle /users command (admin only)"""
    if str(tg_id) not in admins:
        send_message(chat_id, "❌ Admin access required.")
        return
    
    if not users:
        send_message(chat_id, "ℹ️ No registered users yet.")
        return
    
    # Create paginated user list with inline buttons
    user_list = "👥 **Registered Users**\n\n"
    
    # Separate users by role
    admin_users = []
    privileged_users = []
    regular_users = []
    
    for uid, u in users.items():
        role = u.get("role", ROLE_REGULAR)
        if role == ROLE_ADMIN:
            admin_users.append((uid, u))
        elif role == ROLE_PRIVILEGED:
            privileged_users.append((uid, u))
        else:
            regular_users.append((uid, u))
    
    # Display admins
    if admin_users:
        user_list += "**👑 Admins:**\n"
        for uid, u in admin_users:
            user_list += f"👑 `{u['username']}`\n"
        user_list += "\n"
    
    # Display privileged users
    if privileged_users:
        user_list += "**⭐ Privileged Users:**\n"
        for uid, u in privileged_users:
            user_list += f"⭐ `{u['username']}`\n"
        user_list += "\n"
    
    # Display regular users with subscription status
    if regular_users:
        user_list += "**👤 Regular Users:**\n"
        for uid, u in regular_users:
            active, expires_at = check_subscription_status(uid)
            if active and expires_at:
                expiry_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
                user_list += f"👤 `{u['username']}` - ✅ Active (expires {expiry_date})\n"
            elif active and not expires_at:
                user_list += f"👤 `{u['username']}` - ✅ Active (permanent)\n"
            else:
                user_list += f"👤 `{u['username']}` - ❌ Expired\n"
    
    user_list += f"\n📊 Total: {len(users)} users\n"
    user_list += f"👑 Admins: {len(admin_users)}\n"
    user_list += f"⭐ Privileged: {len(privileged_users)}\n"
    user_list += f"👤 Regular: {len(regular_users)}\n\n"
    user_list += "💡 To manage a user's subscription, use:\n"
    user_list += "`/subinfo <username>` - View subscription details\n"
    user_list += "`/subextend <username> <days>` - Extend subscription\n"
    user_list += "`/subend <username>` - End subscription"
    
    send_message(chat_id, user_list, parse_mode="Markdown")

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
    
    total_users = len(users)
    linked_users = sum(1 for u in users.values() if u.get("telegram_id"))
    unlinked_users = total_users - linked_users
    active_subs = sum(1 for uid in users if check_subscription_status(uid)[0])
    
    pending_registrations = sum(1 for p in pending.values() if p.get("type", "register") == "register")
    pending_links = sum(1 for p in pending.values() if p.get("type") == "link")
    pending_unlinks = sum(1 for p in pending.values() if p.get("type") == "unlink")
    pending_payments = sum(1 for req in payment_requests.values() if req["status"] == "pending")
    
    send_message(chat_id,
        f"📊 System Statistics\n\n"
        f"👥 Total Jellyfin Users: {total_users}\n"
        f"🔗 Linked to Telegram: {linked_users}\n"
        f"🔓 Unlinked: {unlinked_users}\n"
        f"✅ Active Subscriptions: {active_subs}\n\n"
        f"⏳ Pending:\n"
        f"  • Registrations: {pending_registrations}\n"
        f"  • Link Requests: {pending_links}\n"
        f"  • Unlink Requests: {pending_unlinks}\n"
        f"  • Payments: {pending_payments}"
    )

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
    
    msg = f"📊 **Subscription Info: {username}**\n\n"
    msg += f"👤 Username: `{username}`\n"
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
    
    msg += f"\n💡 **Management Commands:**\n"
    msg += f"`/subextend {username} <days>` - Extend subscription\n"
    msg += f"`/subend {username}` - End subscription immediately"
    
    send_message(chat_id, msg, parse_mode="Markdown")

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
    
    # Notify admins
    for admin_id in admins:
        send_message(
            admin_id,
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
    
    # Notify admins
    for admin_id in admins:
        send_message(
            admin_id,
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
            
            # Admin approval/rejection actions
            is_admin = str(tg_id) in admins
            
            if is_admin:
                parts = data.split(":")
                action = parts[0]
                uid = parts[1] if len(parts) > 1 else None
                
                if action == "approve":
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
                        logging.info(f"User {uid} ({p['username']}) approved by admin {tg_id}, jellyfin_id: {jellyfin_id}")
                
                elif action == "reject":
                    p = pending.pop(uid, None)
                    if p:
                        save_json(PENDING_FILE, pending)
                        send_message(uid, "❌ Your registration request was declined.\n\nPlease contact an administrator if you believe this was a mistake.")
                        send_message(chat_id, f"✅ Registration request for `{p.get('username', 'user')}` rejected.", parse_mode="Markdown")
                        logging.info(f"User {uid} registration rejected by admin {tg_id}")
                    else:
                        send_message(chat_id, "⚠️ Request already processed.")
                
                elif action == "reset_ok":
                    # uid here is telegram_id from callback, need to get jellyfin user_id
                    user_id, user = get_user_by_telegram_id(uid)
                    if not user:
                        send_message(chat_id, "❌ User not found in system.")
                        return
                    
                    password = generate_password()
                    if jellyfin_reset_password(user["username"], password):
                        send_message(uid, f"✅ Password reset approved!\n\n🔐 Your new Jellyfin password:\n\n`{password}`\n\nPlease save this securely.", parse_mode="Markdown")
                        send_message(chat_id, f"✅ Password reset for `{user['username']}` completed.", parse_mode="Markdown")
                        logging.info(f"Password reset for user {user_id} (telegram {uid}) approved by admin {tg_id}")
                    else:
                        send_message(chat_id, f"❌ Failed to reset password for `{user['username']}`.", parse_mode="Markdown")
                        send_message(uid, "❌ Password reset failed. Please contact an administrator.")
                
                elif action == "reset_no":
                    # uid here is telegram_id from callback, need to get jellyfin user_id
                    user_id, user = get_user_by_telegram_id(uid)
                    if user:
                        send_message(uid, "❌ Your password reset request was declined.\n\nPlease contact an administrator if you need assistance.")
                        send_message(chat_id, f"✅ Password reset request for `{user['username']}` rejected.", parse_mode="Markdown")
                        logging.info(f"Password reset for user {user_id} (telegram {uid}) rejected by admin {tg_id}")
                    else:
                        send_message(chat_id, "⚠️ User not found.")
                
                elif action == "pay_approve":
                    # Format: pay_approve:request_id
                    request_id = uid
                    
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
                    
                    logging.info(f"Payment {request_id} approved by admin {tg_id} for user {user_id} (telegram_id: {telegram_id})")
                
                elif action == "pay_reject":
                    # Format: pay_reject:request_id
                    request_id = uid
                    
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
                    
                    logging.info(f"Link approved by admin {tg_id}: Telegram {uid} → Jellyfin {users[jellyfin_user_id]['username']}")
                
                elif action == "link_reject":
                    # Reject link request
                    p = pending.pop(uid, None)
                    if p:
                        save_json(PENDING_FILE, pending)
                        send_message(uid, "❌ Your link request was declined.\n\nPlease contact an administrator if you believe this was a mistake.")
                        send_message(chat_id, f"✅ Link request rejected for Telegram ID {uid}.")
                        logging.info(f"Link request rejected by admin {tg_id} for Telegram {uid}")
                    else:
                        send_message(chat_id, "⚠️ Link request not found or already processed.")
                
                elif action == "unlink_approve":
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
                    
                    logging.info(f"Unlink approved by admin {tg_id} for user {username}")
                
                elif action == "unlink_reject":
                    # Reject unlink request
                    if uid in pending:
                        username = pending[uid].get("username", "User")
                        pending.pop(uid, None)
                        save_json(PENDING_FILE, pending)
                        send_message(uid, "❌ Your unlink request was declined.\n\nPlease contact an administrator if you need assistance.")
                        send_message(chat_id, f"✅ Unlink request rejected for user {username}.")
                        logging.info(f"Unlink request rejected by admin {tg_id} for user {username}")
                    else:
                        send_message(chat_id, "⚠️ Unlink request not found or already processed.")


        
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
                for admin_id in admins:
                    send_message(
                        admin_id,
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
                        for admin_id in admins:
                            success = send_photo(
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
                            if success:
                                successful_sends += 1
                            else:
                                failed_sends += 1
                        
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
                        
                        for admin_id in admins:
                            send_video(
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
                elif cmd == "/link":
                    parts = text.split()
                    logging.debug(f"Executing /link for admin {tg_id} with args: {parts[1:]}")
                    handle_admin_link(chat_id, tg_id, parts[1:])
                elif cmd == "/unlink":
                    parts = text.split()
                    logging.debug(f"Executing /unlink for admin {tg_id} with args: {parts[1:]}")
                    handle_admin_unlink(chat_id, tg_id, parts[1:])
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
            r = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={"timeout": 60, "offset": offset},
                timeout=70
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
