# Jellyfin Telegram Bot - Documentation Part 3

## Technical Details, Logging, and Data Management

---

## 10. Data Files

### 10.1 Overview

The bot uses JSON files for data persistence. All files are stored in the `data/` directory.

| File | Purpose | Auto-Generated | Source of Truth |
|------|---------|----------------|-----------------|
| `users.json` | User database | No | **YES** |
| `admins.json` | Admin lookup | Yes | No |
| `pending.json` | Pending requests | No | Yes (for requests) |
| `subscriptions.json` | Active subscriptions | No | Yes (for subs) |
| `payment_requests.json` | Payment history | No | Yes (for payments) |
| `telegram_mapping.json` | ID mappings | Yes | No |

### 10.2 users.json

**Purpose:** Main user database containing all user information

**Structure:**
```json
{
  "jellyfin_user_id": {
    "jellyfin_id": "4582cfb2254e49bc8b03df765837cde4",
    "username": "johndoe",
    "telegram_id": 987654321,
    "created_at": 1769951475,
    "is_admin": false,
    "role": "regular"
  }
}
```

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `jellyfin_id` | string | Jellyfin user UUID (unique) |
| `username` | string | Jellyfin username |
| `telegram_id` | integer/null | Linked Telegram user ID |
| `created_at` | integer | Unix timestamp of creation |
| `is_admin` | boolean | Admin status |
| `role` | string | User role (admin/privileged/regular) |

**Roles:**
- **admin** - Full administrator access
- **privileged** - Pre-existing Jellyfin user
- **regular** - New user registered via bot

**Example Entry (Admin):**
```json
{
  "b47443c846d343f8bd1b76fee2543236": {
    "jellyfin_id": "b47443c846d343f8bd1b76fee2543236",
    "username": "admin",
    "telegram_id": 123456789,
    "created_at": 1769951392,
    "is_admin": true,
    "role": "admin"
  }
}
```

**Example Entry (Regular User):**
```json
{
  "4582cfb2254e49bc8b03df765837cde4": {
    "jellyfin_id": "4582cfb2254e49bc8b03df765837cde4",
    "username": "johndoe",
    "telegram_id": 987654321,
    "created_at": 1769951475,
    "is_admin": false,
    "role": "regular"
  }
}
```

**Example Entry (Unlinked User):**
```json
{
  "ac09fc7f798b40339d852d9c6edce930": {
    "jellyfin_id": "ac09fc7f798b40339d852d9c6edce930",
    "username": "olduser",
    "telegram_id": null,
    "created_at": 1769951392,
    "is_admin": false,
    "role": "privileged"
  }
}
```

**Important Notes:**
- Keyed by Jellyfin user ID (not Telegram ID!)
- `telegram_id` can be null (not linked)
- `username` must match Jellyfin exactly
- Source of truth for user data

---

### 10.3 admins.json

**Purpose:** Fast lookup table for admin verification

**Auto-Generated:** Yes (rebuilt on each startup from users.json)

**Structure:**
```json
{
  "telegram_id": {
    "user_id": "jellyfin_user_id",
    "username": "admin_username",
    "added_at": 1769951392
  }
}
```

**Example:**
```json
{
  "123456789": {
    "user_id": "b47443c846d343f8bd1b76fee2543236",
    "username": "admin",
    "added_at": 1769951392
  }
}
```

**How It's Built:**
```python
# Automatically on startup
for user_id, user_data in users.items():
    if user_data.get("is_admin") and user_data.get("telegram_id"):
        telegram_id = str(user_data["telegram_id"])
        admins[telegram_id] = {
            "user_id": user_id,
            "username": user_data["username"],
            "added_at": user_data.get("created_at")
        }
```

**DO NOT EDIT MANUALLY** - Changes will be overwritten on next restart.

---

### 10.4 pending.json

**Purpose:** Store pending approval requests

**Structure:**
```json
{
  "request_id": {
    "type": "registration|link|unlink",
    "username": "username",
    "telegram_id": 987654321,
    "name": "User Name",
    "requested_at": 1769951485,
    "jellyfin_user_id": "uuid_here"
  }
}
```

**Request Types:**

#### Registration Request
```json
{
  "987654321": {
    "name": "John Doe",
    "username": "johndoe",
    "requested_at": 1769951485
  }
}
```

**Key:** Telegram ID (as string)

#### Link Request
```json
{
  "123456789": {
    "type": "link",
    "username": "existinguser",
    "name": "Jane Doe",
    "jellyfin_user_id": "abc123...",
    "requested_at": 1769951500
  }
}
```

#### Unlink Request
```json
{
  "555666777": {
    "type": "unlink",
    "username": "someuser",
    "requested_at": 1769951600
  }
}
```

**Cleanup:**
- Expired requests (> 7 days) automatically removed
- Processed requests immediately removed
- File saved after each change

---

### 10.5 subscriptions.json

**Purpose:** Track active subscriptions

**Structure:**
```json
{
  "jellyfin_user_id": {
    "activated_at": 1769951511.151,
    "expires_at": 1770037911.151,
    "duration_days": 1
  }
}
```

**Example:**
```json
{
  "4582cfb2254e49bc8b03df765837cde4": {
    "activated_at": 1769951511.151,
    "expires_at": 1770037911.151,
    "duration_days": 30
  }
}
```

**Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `activated_at` | float | Unix timestamp of activation |
| `expires_at` | float | Unix timestamp of expiration |
| `duration_days` | integer | Original plan duration |

**Keyed By:** Jellyfin user ID (matches users.json keys)

**Subscription States:**

**Active:**
```python
current_time < expires_at  # User has access
```

**Expired:**
```python
current_time >= expires_at  # User disabled
```

**Monitoring:**
- Background thread checks every hour
- Disables accounts when expired
- Notifies users before expiration

---

### 10.6 payment_requests.json

**Purpose:** Track payment requests and history

**Structure:**
```json
{
  "request_id": {
    "user_id": "jellyfin_user_id",
    "telegram_id": "telegram_id",
    "plan_id": "1month",
    "amount": 35,
    "created_at": 1769951485,
    "status": "pending|approved|rejected",
    "approved_by": "admin_telegram_id",
    "approved_at": 1769951511
  }
}
```

**Request ID Format:** `{telegram_id}_{timestamp}`

**Example (Pending):**
```json
{
  "987654321_1769951485": {
    "user_id": "4582cfb2254e49bc8b03df765837cde4",
    "telegram_id": "987654321",
    "plan_id": "1month",
    "amount": 35,
    "created_at": 1769951485,
    "status": "pending"
  }
}
```

**Example (Approved):**
```json
{
  "987654321_1769951485": {
    "user_id": "4582cfb2254e49bc8b03df765837cde4",
    "telegram_id": "987654321",
    "plan_id": "1month",
    "amount": 35,
    "created_at": 1769951485,
    "status": "approved",
    "approved_by": "123456789",
    "approved_at": 1769951511
  }
}
```

**Status Values:**
- `pending` - Awaiting admin approval
- `approved` - Payment accepted, subscription activated
- `rejected` - Payment denied

**Cleanup:**
- Approved requests kept permanently (history)
- Rejected requests kept for 30 days
- Pending > 7 days auto-rejected

---

### 10.7 telegram_mapping.json

**Purpose:** Fast reverse lookup from Telegram ID to Jellyfin user ID

**Auto-Generated:** Yes (but persisted for performance)

**Structure:**
```json
{
  "telegram_id": "jellyfin_user_id"
}
```

**Example:**
```json
{
  "123456789": "b47443c846d343f8bd1b76fee2543236",
  "987654321": "4582cfb2254e49bc8b03df765837cde4"
}
```

**Purpose:**
Enables O(1) lookup: `telegram_id` → `jellyfin_user_id`

**Without mapping:**
```python
# O(n) - Must iterate through all users
for user_id, user_data in users.items():
    if user_data.get("telegram_id") == telegram_id:
        return user_id
```

**With mapping:**
```python
# O(1) - Direct hash lookup
user_id = telegram_mapping.get(str(telegram_id))
```

**Maintenance:**
- Updated on link/unlink
- Rebuilt on startup from users.json
- Stale entries automatically cleaned
- Validates against users.json

---

## 11. Logging System

### 11.1 Overview

The bot uses a comprehensive 4-tier logging system:

```
┌─────────────────────────────────────────┐
│         All Events (DEBUG+)             │
└──────────┬──────────────────────────────┘
           │
           ├──► Console (INFO+)
           │
           ├──► logs/bot.log (INFO+)
           │
           ├──► logs/debug.log (DEBUG+)
           │
           ├──► logs/error.log (ERROR+)
           │
           └──► logs/user_activity.log (Custom)
```

### 11.2 Log Files

#### bot.log
**Level:** INFO and above  
**Purpose:** General operational logs  
**Format:** Simple timestamp + level + message

**Example:**
```
2026-02-01 10:30:45 [INFO] Bot started (long-polling mode)
2026-02-01 10:30:45 [INFO] Telegram API: https://api.telegram.org/bot...
2026-02-01 10:30:45 [INFO] Jellyfin URL: http://192.168.1.100:8096
2026-02-01 10:31:22 [INFO] User 987654321 (johndoe) approved by admin 123456789
2026-02-01 10:32:15 [INFO] Password reset for user 4582...cde4 (telegram 987654321) approved
2026-02-01 10:33:00 [WARNING] Failed to broadcast to abc123...: User blocked bot
```

**Use Case:** General monitoring, normal operations

---

#### debug.log
**Level:** DEBUG and above (ALL messages)  
**Purpose:** Detailed debugging information  
**Format:** Detailed with function name, line number

**Example:**
```
2026-02-01 10:30:45 | DEBUG    | handle_update         | Line 1914 | Received update: {...}
2026-02-01 10:30:45 | DEBUG    | handle_update         | Line 1931 | Callback from user 987654321 (@johndoe): plan:1month
2026-02-01 10:30:46 | INFO     | jellyfin_enable_user  | Line 784  | Jellyfin user 'johndoe' enabled successfully
2026-02-01 10:30:47 | DEBUG    | handle_subscribe      | Line 1165 | User 987654321 selected plan: 1month
2026-02-01 10:30:48 | DEBUG    | generate_upi_qr       | Line 915  | Generated UPI link for ₹35
```

**Use Case:** Debugging issues, understanding flow, troubleshooting

**Information Included:**
- Timestamp (second precision)
- Log level
- Function name
- Line number
- Detailed message
- Stack traces for errors

---

#### error.log
**Level:** ERROR and CRITICAL only  
**Purpose:** Error tracking and critical issues  
**Format:** Same as debug.log

**Example:**
```
2026-02-01 10:45:23 | ERROR    | jellyfin_create_user  | Line 730  | Failed to create Jellyfin user 'baduser': 409 - Username already exists
2026-02-01 10:46:15 | ERROR    | handle_update         | Line 1947 | Update handling failed for TG_ID: 987654321 (@johndoe): KeyError: 'user_id'
2026-02-01 10:46:15 | ERROR    | handle_update         | Line 1948 | Update data: {...full update json...}
2026-02-01 10:50:00 | CRITICAL | safe_file_save        | Line 330  | Failed to save subscriptions.json: Permission denied
```

**Use Case:** Error monitoring, troubleshooting failures

---

#### user_activity.log
**Level:** Custom activity logger  
**Purpose:** Track all user interactions  
**Format:** Simple timestamp + formatted activity

**Example:**
```
2026-02-01 10:30:45 | MESSAGE | User: John Doe (@johndoe) | TG_ID: 987654321 | Type: text | Content: /start
2026-02-01 10:31:00 | COMMAND | User: John Doe (@johndoe) | TG_ID: 987654321 | Command: /register | Full: /register
2026-02-01 10:31:15 | REGISTRATION_INPUT | User: John Doe (@johndoe) | TG_ID: 987654321 | Username: johndoe123
2026-02-01 10:32:30 | CALLBACK | User: John Doe (@johndoe) | TG_ID: 987654321 | Data: plan:1month
2026-02-01 10:33:00 | NON_COMMAND_TEXT | User: Jane Smith (@janesmith) | TG_ID: 123456789 | Text: Hello bot
2026-02-01 10:34:00 | UNKNOWN_COMMAND | User: Bob Jones (@bobjones) | TG_ID: 555666777 | Command: /unknown
2026-02-01 10:35:00 | ERROR | TG_ID: 987654321 (@johndoe) | Exception: Division by zero
```

**Tracked Events:**
- **MESSAGE** - All incoming messages
- **COMMAND** - Command execution
- **CALLBACK** - Button clicks
- **REGISTRATION_INPUT** - Username submissions
- **NON_COMMAND_TEXT** - Non-command text (potential mistakes)
- **UNKNOWN_COMMAND** - Invalid commands
- **ERROR** - User-related errors

**Use Case:** 
- User behavior analysis
- Support ticket investigation
- Identifying user confusion
- Tracking usage patterns

---

### 11.3 Log Format Reference

#### Simple Format (bot.log, user_activity.log)
```
YYYY-MM-DD HH:MM:SS [LEVEL] Message
```

#### Detailed Format (debug.log, error.log)
```
YYYY-MM-DD HH:MM:SS | LEVEL | Function Name | Line NNNN | Message
```

**Timestamp Format:** `%Y-%m-%d %H:%M:%S`  
**Example:** `2026-02-01 10:30:45`

### 11.4 Log Rotation

**Manual Rotation:**
The bot does not automatically rotate logs. You must set up log rotation:

**Using logrotate:**

Create `/etc/logrotate.d/jellyfin-bot`:
```
/home/user/jellyfin-bot/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0644 user user
    postrotate
        systemctl reload jellyfin-bot
    endscript
}
```

**Manual Archival:**
```bash
cd logs/
tar -czf archive-$(date +%Y%m%d).tar.gz *.log
rm *.log
```

### 11.5 Log Analysis

#### Find Errors:
```bash
grep ERROR logs/error.log
```

#### Track User:
```bash
grep "TG_ID: 987654321" logs/user_activity.log
```

#### Command Usage:
```bash
grep "COMMAND |" logs/user_activity.log | wc -l
```

#### Recent Activity:
```bash
tail -f logs/user_activity.log
```

#### Errors Today:
```bash
grep "$(date +%Y-%m-%d)" logs/error.log
```

### 11.6 Debugging with Logs

**Problem:** User reports command not working

**Investigation:**
1. Get user's Telegram ID
2. Search user_activity.log:
   ```bash
   grep "TG_ID: 987654321" logs/user_activity.log | tail -20
   ```
3. Check what command they sent
4. Search debug.log for that timeframe:
   ```bash
   grep "2026-02-01 10:3" logs/debug.log | grep "987654321"
   ```
5. Check error.log:
   ```bash
   grep "987654321" logs/error.log
   ```

**Problem:** Bot crashed

**Investigation:**
1. Check error.log for CRITICAL:
   ```bash
   grep CRITICAL logs/error.log | tail -10
   ```
2. Check last entries in debug.log:
   ```bash
   tail -50 logs/debug.log
   ```
3. Check systemd logs:
   ```bash
   sudo journalctl -u jellyfin-bot -n 100
   ```

---

*[Continued in Part 4...]*