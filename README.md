# Jellyfin Telegram Bot - Documentation Summary

## Version 1.0 - Complete Documentation Package

---

## 📦 What's Included

This documentation package includes:

1. **DOCUMENTATION_PART_1.md** - Overview, Features, Architecture, Installation
2. **DOCUMENTATION_PART_2.md** - Complete Command Reference
3. **DOCUMENTATION_PART_3.md** - Data Files, Logging System, Technical Details
4. **jelly_admin_with_upi.py** - Enhanced bot script with comprehensive logging
5. **telegram_mapping.json** - Sample telegram mapping file
6. **config.json** - Sample configuration (from uploads)

---

## 🚀 Quick Start

### For New Installations

1. Read **DOCUMENTATION_PART_1.md** - Section 4: Installation Guide
2. Follow all 12 installation steps
3. Configure your bot using the provided config.json
4. Start the bot and test with `/start`

### For Existing Users Upgrading

1. **Backup** your current data/ directory
2. **Replace** jelly_admin_with_upi.py with the new version
3. **Add** to config.json:
   ```json
   "storage": {
     "telegram_mapping": "data/telegram_mapping.json"
   }
   ```
4. **Restart** the bot
5. Bot will automatically create telegram_mapping.json
6. Check logs/ directory for new log files

---

## 📖 Documentation Structure

### Part 1: Foundation (DOCUMENTATION_PART_1.md)

**Sections:**
- 1. Overview - What the bot is and does
- 2. Features - Complete feature list
- 3. System Architecture - How it works
- 4. Installation Guide - Step-by-step setup (12 steps)
- 5. Configuration - config.json reference

**Read this if you are:**
- Setting up the bot for the first time
- Understanding the system architecture
- Configuring subscription plans

**Key Topics:**
- Prerequisites and requirements
- Creating Telegram bot
- Getting Jellyfin API key
- Configuring admins
- Running as systemd service

---

### Part 2: Commands (DOCUMENTATION_PART_2.md)

**Sections:**
- 6. User Commands - All user-facing commands
- 7. Admin Commands - All administrative commands

**Commands Documented:**

**User Commands:**
- `/start` - Welcome and menu
- `/register` - Create new account
- `/subscribe` - Get subscription
- `/status` - Check subscription
- `/resetpw` - Reset password
- `/linkme` - Link existing account
- `/unlinkme` - Unlink account
- `/cancel` - Cancel operation

**Admin Commands:**
- `/pending` - View pending requests
- `/users` - List all users
- `/broadcast` - Message all users
- `/message` - Message specific user
- `/stats` - View statistics
- `/payments` - Manage payments
- `/subinfo` - View subscription
- `/subextend` - Extend subscription
- `/subend` - End subscription
- `/link` - Force link accounts
- `/unlink` - Force unlink accounts

**Read this if you are:**
- Learning how to use the bot
- Training users or admins
- Creating user documentation
- Understanding command workflows

---

### Part 3: Technical Details (DOCUMENTATION_PART_3.md)

**Sections:**
- 10. Data Files - All JSON file structures
- 11. Logging System - Complete logging documentation

**Data Files Documented:**
- users.json - User database
- admins.json - Admin lookup table
- pending.json - Pending requests
- subscriptions.json - Active subscriptions
- payment_requests.json - Payment history
- telegram_mapping.json - ID mapping (NEW!)

**Logging System:**
- bot.log - General operations (INFO+)
- debug.log - Detailed debugging (ALL)
- error.log - Errors only (ERROR+)
- user_activity.log - User interactions (CUSTOM)

**Read this if you are:**
- Debugging issues
- Understanding data structures
- Analyzing logs
- Developing integrations
- Troubleshooting errors

---

## 🆕 What's New in This Version

### 1. Enhanced Logging System

**4 Separate Log Files:**
- `logs/bot.log` - General operations
- `logs/debug.log` - Detailed debugging
- `logs/error.log` - Error tracking
- `logs/user_activity.log` - User interaction tracking

**Features:**
- Timestamps on all entries
- Function names and line numbers (debug/error)
- Comprehensive user activity tracking
- Logs ALL user input (even mistakes)
- Better error context

### 2. Persistent Telegram Mapping

**New File:** `data/telegram_mapping.json`

**Benefits:**
- O(1) lookup speed (instant)
- Persists across restarts
- Auto-rebuilt if corrupted
- Stale entry cleanup

**Structure:**
```json
{
  "telegram_id": "jellyfin_user_id"
}
```

### 3. Bug Fixes

**Password Reset Fixed:**
- Was checking wrong user lookup
- Now uses proper telegram_to_userid mapping
- Both approve and reject work correctly

---

## 📊 System Requirements

### Minimum

- **OS:** Linux (any modern distribution)
- **Python:** 3.7+
- **RAM:** 512MB
- **Disk:** 1GB
- **Jellyfin:** 10.8.0+

### Recommended

- **OS:** Ubuntu 20.04 LTS or later
- **Python:** 3.9+
- **RAM:** 1GB
- **Disk:** 5GB (for logs and data)
- **Jellyfin:** Latest stable version

---

## 🔧 Common Tasks

### View Real-Time Logs
```bash
tail -f logs/user_activity.log
```

### Check for Errors
```bash
grep ERROR logs/error.log
```

### Find User Activity
```bash
grep "TG_ID: 987654321" logs/user_activity.log
```

### Restart Bot (systemd)
```bash
sudo systemctl restart jellyfin-bot
```

### Check Bot Status
```bash
sudo systemctl status jellyfin-bot
```

### View Systemd Logs
```bash
sudo journalctl -u jellyfin-bot -f
```

### Backup Data
```bash
tar -czf backup-$(date +%Y%m%d).tar.gz data/ logs/ config.json
```

---

## 🐛 Troubleshooting Quick Reference

### Bot Won't Start

1. Check config.json is valid JSON
2. Verify bot token is correct
3. Verify Jellyfin URL is reachable
4. Check Python version: `python3 --version`
5. Check logs: `tail -50 logs/error.log`

### Commands Not Working

1. Check user_activity.log for the command
2. Check debug.log for execution details
3. Check error.log for any errors
4. Verify user is properly linked

### Admin Not Recognized

1. Check data/users.json
2. Verify `is_admin: true`
3. Verify telegram_id is set correctly
4. Restart bot to rebuild admins.json

### Subscription Not Working

1. Check subscriptions.json for entry
2. Verify expiration date is future
3. Check Jellyfin account is enabled
4. Check debug.log for disable messages

### Logs Getting Too Large

Set up log rotation:
```bash
sudo nano /etc/logrotate.d/jellyfin-bot
```

Add:
```
/home/user/jellyfin-bot/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
```

---

## 📞 Support & Resources

### Documentation Files

1. **DOCUMENTATION_PART_1.md** - Setup and architecture
2. **DOCUMENTATION_PART_2.md** - Commands reference
3. **DOCUMENTATION_PART_3.md** - Data and logging

### Configuration Files

- **config.json** - Main configuration
- **jelly_admin_with_upi.py** - Bot source code

### Data Files Location

All data files are in `data/` directory:
- users.json
- admins.json
- pending.json
- subscriptions.json
- payment_requests.json
- telegram_mapping.json

### Log Files Location

All log files are in `logs/` directory:
- bot.log
- debug.log
- error.log
- user_activity.log

---

## ⚠️ Important Notes

### Do NOT Edit

These files are auto-generated:
- `data/admins.json` - Rebuilt on startup
- `data/telegram_mapping.json` - Auto-maintained

Edit `data/users.json` instead!

### Always Backup

Before any changes:
```bash
cp -r data/ data.backup/
cp -r logs/ logs.backup/
cp config.json config.json.backup
```

### Log File Growth

Logs can grow large. Set up log rotation or manually archive:
```bash
cd logs/
tar -czf archive-$(date +%Y%m%d).tar.gz *.log
rm *.log
```

---

## 📈 Next Steps

### After Installation

1. **Test user registration** - Have a test user register
2. **Test subscription** - Process a test payment
3. **Set up log rotation** - Prevent disk space issues
4. **Configure backup** - Regular data backups
5. **Train admins** - Share admin command documentation

### Ongoing Maintenance

1. **Monitor logs** - Check error.log daily
2. **Review activity** - Check user_activity.log weekly
3. **Clean old data** - Archive old logs monthly
4. **Update bot** - Check for updates quarterly
5. **Backup data** - Automated weekly backups

---

## 📝 Version History

### Version 1.0 (February 1, 2026)

**Features:**
- Complete subscription management system
- UPI payment processing
- Comprehensive logging (4 log files)
- Persistent telegram mapping
- Auto-generated admin lookup
- Background subscription monitoring
- Data cleanup automation

**Fixes:**
- Password reset callback bug
- Telegram ID lookup optimization
- Mapping persistence across restarts

---

## 🎯 Quick Reference Card

### User Quick Commands
- `/start` → Main menu
- `/register` → New account
- `/subscribe` → Get subscription
- `/status` → Check status

### Admin Quick Commands
- `/pending` → Approve requests
- `/users` → List users
- `/payments` → Manage payments
- `/stats` → View statistics

### File Locations
- **Code:** `jelly_admin_with_upi.py`
- **Config:** `config.json`
- **Data:** `data/*.json`
- **Logs:** `logs/*.log`

### Log Files
- **General:** `logs/bot.log`
- **Debug:** `logs/debug.log`
- **Errors:** `logs/error.log`
- **Activity:** `logs/user_activity.log`

---

**Documentation Version:** 1.0  
**Release Date:** February 1, 2026  
**Status:** Production Ready ✅