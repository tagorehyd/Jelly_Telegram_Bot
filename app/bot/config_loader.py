import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
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


def ensure_config_files(strings):
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
        strings["first_run_message"].format(
            created_list=created_list,
            config_file=CONFIG_FILE
        )
    )
    return True


def load_config():
    strings = load_strings()
    if ensure_config_files(strings):
        sys.exit(0)

    config = read_json_file(CONFIG_FILE)
    secrets_config = read_json_file(SECRETS_FILE)

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

    if "url" not in config["jellyfin"]:
        raise ValueError("jellyfin config must contain 'url'")

    if not all(k in config["storage"] for k in ["admins", "users", "pending"]):
        raise ValueError("storage config must contain 'admins', 'users', and 'pending'")

    if "payment" not in config:
        config["payment"] = {
            "upi_id": "yourname@paytm",
            "upi_name": "Your Name"
        }

    if "subscription_plans" not in config:
        config["subscription_plans"] = {
            "1day": {"duration_days": 1, "price": 5, "name": "1 Day"},
            "1week": {"duration_days": 7, "price": 10, "name": "1 Week"},
            "1month": {"duration_days": 30, "price": 35, "name": "1 Month"}
        }

    if "subscriptions" not in config["storage"]:
        config["storage"]["subscriptions"] = "data/subscriptions.json"
    if "payment_requests" not in config["storage"]:
        config["storage"]["payment_requests"] = "data/payment_requests.json"
    if "telegram_mapping" not in config["storage"]:
        config["storage"]["telegram_mapping"] = "data/telegram_mapping.json"

    return config, secrets_config, strings
