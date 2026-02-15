import logging


def fetch_users(base_url, api_key, session, timeout):
    resp = session.get(
        f"{base_url}/Users",
        headers={"X-Emby-Token": api_key},
        timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


def create_user(base_url, api_key, session, timeout, username, password):
    try:
        resp = session.post(
            f"{base_url}/Users/New",
            headers={"X-Emby-Token": api_key, "Content-Type": "application/json"},
            json={"Name": username, "Password": password},
            timeout=timeout
        )
        if resp.status_code == 200:
            logging.info(f"Jellyfin user '{username}' created successfully")
            return True
        logging.error(f"Failed to create Jellyfin user '{username}': {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Error creating Jellyfin user '{username}': {e}")
        return False


def get_user_id(base_url, api_key, session, timeout, username):
    try:
        resp = session.get(
            f"{base_url}/Users",
            headers={"X-Emby-Token": api_key},
            timeout=timeout
        )
        if resp.status_code == 200:
            for user in resp.json():
                if user["Name"].lower() == username.lower():
                    return user["Id"]
        return None
    except Exception as e:
        logging.error(f"Error getting Jellyfin user ID for '{username}': {e}")
        return None


def set_user_enabled(base_url, api_key, session, timeout, username, enabled):
    user_id = get_user_id(base_url, api_key, session, timeout, username)
    if not user_id:
        logging.error(f"Cannot update user '{username}': User not found")
        return False

    try:
        resp = session.get(
            f"{base_url}/Users/{user_id}",
            headers={"X-Emby-Token": api_key},
            timeout=timeout
        )
        if resp.status_code != 200:
            logging.error(f"Failed to get user policy for '{username}'")
            return False

        user_data = resp.json()
        policy = user_data.get("Policy", {})
        policy["IsDisabled"] = not enabled

        resp = session.post(
            f"{base_url}/Users/{user_id}/Policy",
            headers={"X-Emby-Token": api_key, "Content-Type": "application/json"},
            json=policy,
            timeout=timeout
        )

        if resp.status_code in (200, 204):
            action = "enabled" if enabled else "disabled"
            logging.info(f"Jellyfin user '{username}' {action} successfully")
            return True
        logging.error(f"Failed to update Jellyfin user '{username}': {resp.status_code}")
        return False
    except Exception as e:
        logging.error(f"Error updating Jellyfin user '{username}': {e}")
        return False


def reset_password(base_url, api_key, session, timeout, username, new_password):
    user_id = get_user_id(base_url, api_key, session, timeout, username)
    if not user_id:
        logging.error(f"Cannot reset password for '{username}': User not found")
        return False

    try:
        resp = session.post(
            f"{base_url}/Users/{user_id}/Password",
            headers={"X-Emby-Token": api_key, "Content-Type": "application/json"},
            json={"NewPw": new_password},
            timeout=timeout
        )

        if resp.status_code in (200, 204):
            logging.info(f"Password reset for Jellyfin user '{username}' successful")
            return True
        logging.error(f"Failed to reset password for '{username}': {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Error resetting password for '{username}': {e}")
        return False


def username_available(base_url, api_key, session, timeout, username):
    try:
        resp = session.get(
            f"{base_url}/Users",
            headers={"X-Emby-Token": api_key},
            timeout=timeout
        )
        if resp.status_code != 200:
            logging.error(f"Jellyfin API error while checking username: {resp.status_code} - {resp.text}")
            return False

        existing_users = resp.json()
        for user in existing_users:
            if user["Name"].lower() == username.lower():
                return False
        return True
    except Exception as e:
        logging.error(f"Error checking username availability for '{username}': {e}")
        return False


def delete_user(base_url, api_key, session, timeout, user_id, username):
    try:
        resp = session.delete(
            f"{base_url}/Users/{user_id}",
            headers={"X-Emby-Token": api_key},
            timeout=timeout
        )
        if resp.status_code in (200, 204):
            logging.info(f"Jellyfin user '{username}' deleted successfully")
            return True
        logging.error(f"Failed to delete Jellyfin user '{username}': {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Error deleting Jellyfin user '{username}': {e}")
        return False


def get_top_items(base_url, api_key, session, timeout, item_type, limit=10, user_id=None):
    params = {
        "Recursive": "true",
        "IncludeItemTypes": item_type,
        "SortBy": "PlayCount",
        "SortOrder": "Descending",
        "Limit": str(limit),
        "Fields": "PlayCount",
    }
    if user_id:
        params["UserId"] = user_id
    try:
        resp = session.get(
            f"{base_url}/Items",
            headers={"X-Emby-Token": api_key},
            params=params,
            timeout=timeout,
        )
        if resp.status_code != 200:
            logging.error(f"Failed to fetch top {item_type} items: {resp.status_code} - {resp.text}")
            return []
        items = resp.json().get("Items", [])
        return [(item.get("Name", "Unknown"), item.get("UserData", {}).get("PlayCount", 0)) for item in items]
    except Exception as e:
        logging.error(f"Error fetching top {item_type} items: {e}")
        return []


def get_user_played_runtime(base_url, api_key, session, timeout, user_id, limit=500):
    params = {
        "Recursive": "true",
        "Filters": "IsPlayed",
        "IncludeItemTypes": "Movie,Episode",
        "Fields": "RunTimeTicks",
        "Limit": str(limit),
        "UserId": user_id,
    }
    total_ticks = 0
    try:
        resp = session.get(
            f"{base_url}/Items",
            headers={"X-Emby-Token": api_key},
            params=params,
            timeout=timeout,
        )
        if resp.status_code != 200:
            logging.error(f"Failed to fetch runtime for user {user_id}: {resp.status_code} - {resp.text}")
            return 0
        items = resp.json().get("Items", [])
        for item in items:
            total_ticks += item.get("RunTimeTicks", 0) or 0
        return total_ticks
    except Exception as e:
        logging.error(f"Error fetching runtime for user {user_id}: {e}")
        return 0


def get_user_policy(base_url, api_key, session, timeout, user_id):
    try:
        resp = session.get(
            f"{base_url}/Users/{user_id}",
            headers={"X-Emby-Token": api_key},
            timeout=timeout
        )
        if resp.status_code != 200:
            logging.error(f"Failed to fetch user policy for '{user_id}': {resp.status_code} - {resp.text}")
            return None
        return resp.json().get("Policy", {})
    except Exception as e:
        logging.error(f"Error fetching user policy for '{user_id}': {e}")
        return None


def set_user_policy(base_url, api_key, session, timeout, user_id, policy):
    try:
        resp = session.post(
            f"{base_url}/Users/{user_id}/Policy",
            headers={"X-Emby-Token": api_key, "Content-Type": "application/json"},
            json=policy,
            timeout=timeout
        )
        if resp.status_code in (200, 204):
            return True
        logging.error(f"Failed to set user policy for '{user_id}': {resp.status_code} - {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Error setting user policy for '{user_id}': {e}")
        return False


def get_library_folders(base_url, api_key, session, timeout):
    try:
        resp = session.get(
            f"{base_url}/Library/VirtualFolders",
            headers={"X-Emby-Token": api_key},
            timeout=timeout
        )
        if resp.status_code != 200:
            logging.error(f"Failed to fetch library folders: {resp.status_code} - {resp.text}")
            return []

        folders = []
        for folder in resp.json():
            folder_id = folder.get("ItemId") or folder.get("Id")
            if not folder_id:
                continue
            folders.append({
                "id": folder_id,
                "name": folder.get("Name", "Unknown")
            })
        return folders
    except Exception as e:
        logging.error(f"Error fetching library folders: {e}")
        return []
