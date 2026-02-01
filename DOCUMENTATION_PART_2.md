# Jellyfin Telegram Bot - Documentation Part 2

## Commands and Usage Guide

---

## 6. User Commands

### 6.1 /start
**Description:** Display welcome message and available commands

**Usage:**
```
/start
```

**Response:**
Shows personalized greeting based on user status:
- **New User:** Registration instructions
- **Registered User:** Available commands menu
- **Admin:** Full command list including admin commands

**Example Output:**
```
👋 Welcome John!

👤 User Commands:
/start - Show this menu
/register - Create new account
/subscribe - Subscribe to a plan
/status - Check subscription status
/resetpw - Request password reset
/linkme <username> - Link existing account
/unlinkme - Unlink your account

Need help? Contact an admin.
```

---

### 6.2 /register
**Description:** Register for a new Jellyfin account

**Usage:**
```
/register
```

**Workflow:**
1. User sends `/register`
2. Bot requests username
3. User provides desired username
4. Bot validates username:
   - 3-20 characters
   - Alphanumeric + underscore only
   - Not already taken
5. Request added to pending queue
6. Admins notified
7. Admin approves/rejects
8. User receives Jellyfin credentials

**Validation Rules:**
- **Minimum:** 3 characters
- **Maximum:** 20 characters
- **Allowed:** a-z, A-Z, 0-9, underscore (_)
- **Not allowed:** Spaces, special characters
- **Case:** Insensitive (Username and username treated as same)

**Example Session:**
```
User: /register
Bot: 📝 Please enter your desired Jellyfin username:

User: johndoe123
Bot: ✅ Registration request submitted!

     Username: johndoe123
     
     ⏳ Please wait for an admin to approve your request.

[After admin approval]

Bot: ✅ Registration approved!

     🎉 Your Jellyfin account has been created:
     
     Username: johndoe123
     Password: xK9mP2qL
     
     ⚠️ Your account is currently disabled.
     
     Please subscribe using /subscribe to activate your access!
```

**Cancel Registration:**
Send `/cancel` anytime during username input.

---

### 6.3 /subscribe
**Description:** Subscribe to a plan and get access

**Usage:**
```
/subscribe
```

**Workflow:**
1. User sends `/subscribe`
2. Bot displays available plans
3. User selects plan via inline buttons
4. Bot generates UPI payment link
5. User makes payment
6. User submits payment proof to admins
7. Admin approves payment
8. Subscription activated
9. Jellyfin account enabled

**Example Output:**
```
📋 Available Subscription Plans:

[1 Day] ₹5
[1 Week] ₹10
[1 Month] ₹35
```

**After Plan Selection:**
```
💳 Payment Information

Plan: 1 Month
Amount: ₹35
UPI ID: 9876543210@paytm
Name: John Doe

📱 Tap to pay:
[Pay with UPI]

After payment:
1. Take screenshot of payment confirmation
2. Send it to any admin
3. Wait for approval

Your request has been forwarded to admins.
```

**Payment Notes:**
- Payment must be made to shown UPI ID
- Include reference/note if possible
- Screenshot must show transaction details
- Approval typically within 24 hours

---

### 6.4 /status
**Description:** Check current subscription status

**Usage:**
```
/status
```

**Response (Active Subscription):**
```
📊 Subscription Status

✅ Status: ACTIVE
📅 Expires: 2026-03-01 15:30:45
⏰ Time Left: 28 days, 14 hours

Your account is enabled and active.
```

**Response (No Subscription):**
```
📊 Subscription Status

❌ Status: INACTIVE
📅 Expires: Never

You don't have an active subscription.
Use /subscribe to get access!
```

**Response (Expired):**
```
📊 Subscription Status

⚠️ Status: EXPIRED
📅 Expired: 2026-01-25 10:00:00
⏰ Expired: 7 days ago

Your account has been disabled.
Subscribe using /subscribe to renew access.
```

---

### 6.5 /resetpw
**Description:** Request password reset for your account

**Usage:**
```
/resetpw
```

**Workflow:**
1. User sends `/resetpw`
2. Request sent to admins
3. Admin approves/rejects
4. If approved: New password generated
5. User receives new password securely

**Example:**
```
User: /resetpw

Bot: 🔐 Password reset request submitted.
     
     Please wait for admin approval.

[After admin approval]

Bot: ✅ Password reset approved!

     🔐 Your new Jellyfin password:
     
     nQ7vR3wK
     
     Please save this securely.
```

**Security Notes:**
- Only admins can approve resets
- New password is randomly generated
- Old password immediately invalidated
- Change password in Jellyfin after first login

---

### 6.6 /linkme
**Description:** Link existing Jellyfin account to Telegram

**Usage:**
```
/linkme <jellyfin_username>
```

**Arguments:**
- `jellyfin_username` - Your existing Jellyfin username

**Example:**
```
/linkme johndoe

Bot: 🔗 Link request submitted!
     
     Jellyfin User: johndoe
     
     ⏳ Waiting for admin approval...

[After admin approval]

Bot: ✅ Link request approved!
     
     🎬 Your Telegram is now linked to Jellyfin user: johndoe
     
     You can now use all bot features. Use /start to see available commands.
```

**Use Cases:**
- You have existing Jellyfin account
- Want to use bot features
- Avoid creating duplicate account
- Link multiple Telegram users to same Jellyfin account

**Requirements:**
- Jellyfin account must exist
- Account must not be linked to another Telegram
- Admin approval required

---

### 6.7 /unlinkme
**Description:** Unlink your Telegram from Jellyfin account

**Usage:**
```
/unlinkme
```

**Workflow:**
1. User sends `/unlinkme`
2. Request sent to admins
3. Admin approves/rejects
4. If approved: Telegram unlinked
5. User can link to different account

**Example:**
```
User: /unlinkme

Bot: 🔓 Unlink request submitted!
     
     Account: johndoe
     
     ⏳ Waiting for admin approval...

[After admin approval]

Bot: ✅ Unlink request approved!
     
     Your Telegram account has been unlinked from Jellyfin user: johndoe
     
     You can link to a different account using /linkme <username> or create a new account with /register
```

**Important:**
- Unlink does NOT delete Jellyfin account
- Subscription remains with Jellyfin account
- You can re-link to same account later
- Can link to different account after unlinking

---

### 6.8 /cancel
**Description:** Cancel current operation

**Usage:**
```
/cancel
```

**Cancels:**
- Username input during registration
- Broadcast message composition (admin)
- Any pending input request

**Example:**
```
Bot: Please enter your desired username:

User: /cancel

Bot: ✅ Cancelled.
```

---

## 7. Admin Commands

### 7.1 /pending
**Description:** View all pending requests

**Usage:**
```
/pending
```

**Shows:**
- Pending registrations
- Link requests
- Unlink requests

**Example Output:**
```
📋 Pending Requests (3)

━━━━━━━━━━━━━━━━━━━━

👤 Registration Request #1
Name: John Doe
Username: johndoe123
Telegram ID: 987654321
Requested: 2 hours ago

[✅ Approve] [❌ Reject]

━━━━━━━━━━━━━━━━━━━━

🔗 Link Request #2
Username: existinguser
Telegram ID: 123456789
Requested: 5 hours ago

[✅ Approve] [❌ Reject]

━━━━━━━━━━━━━━━━━━━━

🔓 Unlink Request #3
Username: someuser
Telegram ID: 555666777
Requested: 1 day ago

[✅ Approve] [❌ Reject]
```

**Actions:**
- **Approve** - Process the request
- **Reject** - Deny the request

**Empty State:**
```
ℹ️ No pending requests
```

---

### 7.2 /users
**Description:** List all registered users

**Usage:**
```
/users
```

**Output Format:**
```
👥 All Users (22 total)

━━━━━━━━━━━━━━━━━━━━━━━━

👤 admin
🆔 Jellyfin: b474...3236
📱 Telegram: 123456789
🎭 Role: admin
✅ Enabled

━━━━━━━━━━━━━━━━━━━━━━━━

👤 johndoe
🆔 Jellyfin: 4582...cde4
📱 Telegram: 987654321
🎭 Role: regular
✅ Enabled
📅 Expires: 2026-03-01

━━━━━━━━━━━━━━━━━━━━━━━━

👤 eshwar
🆔 Jellyfin: 6cb1...df6a
📱 Telegram: Not linked
🎭 Role: privileged
✅ Enabled

━━━━━━━━━━━━━━━━━━━━━━━━

[... more users ...]
```

**Information Shown:**
- Username
- Jellyfin User ID (truncated)
- Telegram ID (if linked)
- User role (admin/privileged/regular)
- Account status (Enabled/Disabled)
- Subscription expiry (if active)

---

### 7.3 /broadcast
**Description:** Send message to all non-admin users

**Usage:**
```
/broadcast
```

**Supported Types:**
- Text messages
- Photos with captions
- Videos with captions

**Workflow:**
1. Send `/broadcast`
2. Bot enters broadcast mode
3. Send your message/photo/video
4. Bot distributes to all users
5. Shows success count

**Example:**
```
Admin: /broadcast

Bot: 📢 Broadcast mode enabled.
     
     Send the message you want to broadcast (text/photo/video).
     
     Send /cancel to cancel.

Admin: [sends message]
"Server maintenance tonight 10 PM - 11 PM. Please plan accordingly."

Bot: ✅ Broadcast sent to 21/21 users
```

**Notes:**
- Only sent to non-admin users
- Admins are excluded
- Failed sends are logged
- Delivery is best-effort

---

### 7.4 /message
**Description:** Send targeted message to specific user

**Usage:**
```
/message <username>
```

**Arguments:**
- `username` - Jellyfin username of recipient

**Supported Types:**
- Text messages
- Photos with captions
- Videos with captions

**Example:**
```
Admin: /message johndoe

Bot: 💬 Message mode enabled for: johndoe
     
     Send the message you want to send (text/photo/video).
     
     Send /cancel to cancel.

Admin: [sends message]
"Hi John, your subscription expires tomorrow. Please renew!"

Bot: ✅ Message sent to johndoe
```

**Use Cases:**
- Customer support
- Subscription reminders
- Account notifications
- Personal communication

---

### 7.5 /stats
**Description:** View bot statistics

**Usage:**
```
/stats
```

**Output:**
```
📊 Bot Statistics

👥 Users: 22 total
   • 1 admins
   • 5 privileged
   • 16 regular

📅 Subscriptions:
   • 12 active
   • 4 expired
   • 6 never subscribed

📋 Pending:
   • 3 registration requests
   • 1 link requests
   • 2 unlink requests

💳 Payments:
   • 5 pending approval
   • 45 total approved
   • ₹1,575 total revenue
```

**Metrics:**
- User counts by role
- Subscription statistics
- Pending request counts
- Payment statistics
- Revenue tracking

---

### 7.6 /payments
**Description:** View payment requests

**Usage:**
```
/payments
```

**Shows:**
- Pending payment requests
- Request details
- User information

**Example Output:**
```
💳 Payment Requests (2)

━━━━━━━━━━━━━━━━━━━━

Payment Request #1
User: johndoe (987654321)
Plan: 1 Month
Amount: ₹35
Requested: 1 hour ago
Status: Pending

[✅ Approve] [❌ Reject]

━━━━━━━━━━━━━━━━━━━━

Payment Request #2
User: janedoe (123456789)
Plan: 1 Week  
Amount: ₹10
Requested: 3 hours ago
Status: Pending

[✅ Approve] [❌ Reject]
```

**Actions:**
- **Approve** - Activate subscription
- **Reject** - Deny request

**On Approval:**
1. Subscription activated
2. Account enabled
3. Expiry date set
4. User notified

---

### 7.7 /subinfo
**Description:** View user subscription details

**Usage:**
```
/subinfo <username>
```

**Arguments:**
- `username` - Jellyfin username to check

**Example:**
```
Admin: /subinfo johndoe

Bot: 📊 Subscription Info: johndoe

     ✅ Status: ACTIVE
     📅 Activated: 2026-02-01 10:00:00
     📅 Expires: 2026-03-01 10:00:00
     ⏰ Time Left: 28 days, 14 hours
     📦 Plan: 1 Month (30 days)
     
     Account is enabled and active.
```

**No Subscription:**
```
Bot: 📊 Subscription Info: johndoe

     ❌ No active subscription
     
     User has never subscribed.
```

---

### 7.8 /subextend
**Description:** Extend user subscription

**Usage:**
```
/subextend <username> <days>
```

**Arguments:**
- `username` - Jellyfin username
- `days` - Number of days to add

**Example:**
```
Admin: /subextend johndoe 7

Bot: ✅ Subscription extended!

     User: johndoe
     Extended: 7 days
     New expiry: 2026-03-08 10:00:00
     
     User has been notified.
```

**Use Cases:**
- Compensation for service issues
- Loyalty rewards
- Special promotions
- Manual adjustments

**Notes:**
- Can extend active subscriptions
- Can extend expired subscriptions
- Account automatically enabled
- User receives notification

---

### 7.9 /subend
**Description:** End user subscription immediately

**Usage:**
```
/subend <username>
```

**Arguments:**
- `username` - Jellyfin username

**Example:**
```
Admin: /subend johndoe

Bot: ⚠️ Subscription Termination

     User: johndoe
     Current Expiry: 2026-03-01 10:00:00
     
     Are you sure you want to end this subscription?
     
     [✅ Confirm] [❌ Cancel]

[After confirmation]

Bot: ✅ Subscription ended!

     User: johndoe
     • Subscription removed
     • Account disabled
     • User notified
```

**Effects:**
- Subscription removed
- Account disabled
- User notified
- Jellyfin access revoked

**Use Cases:**
- Terms of service violations
- Chargebacks
- User request
- Account suspension

---

### 7.10 /link
**Description:** Force link Telegram to Jellyfin account (admin)

**Usage:**
```
/link <telegram_id> <jellyfin_username>
```

**Arguments:**
- `telegram_id` - User's Telegram ID
- `jellyfin_username` - Jellyfin account username

**Example:**
```
Admin: /link 987654321 johndoe

Bot: ✅ Account linked!

     Telegram ID: 987654321
     Jellyfin User: johndoe
     
     Link successful.
```

**Use Cases:**
- Manual account linking
- Fix linking issues
- Administrative corrections

**Requirements:**
- Jellyfin account must exist
- Account not already linked
- Valid Telegram ID

---

### 7.11 /unlink
**Description:** Force unlink Telegram from Jellyfin account (admin)

**Usage:**
```
/unlink <jellyfin_username>
```

**Arguments:**
- `jellyfin_username` - Username to unlink

**Example:**
```
Admin: /unlink johndoe

Bot: ✅ Account unlinked!

     User: johndoe
     Telegram ID: 987654321 (removed)
     
     Unlink successful.
```

**Effects:**
- Telegram ID removed from user record
- User can no longer use bot
- Jellyfin account unchanged
- Can be re-linked later

---

*[Continued in Part 3...]*