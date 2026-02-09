import logging


def send_message(session, timeout, api_base, chat_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        response = session.post(f"{api_base}/sendMessage", json=payload, timeout=timeout)
        if response.status_code != 200:
            logging.error(f"Failed to send message to {chat_id}: {response.status_code} - {response.text}")
            return None
        data = response.json()
        if not data.get("ok"):
            logging.error(f"Telegram API error sending message to {chat_id}: {data}")
            return None
        return data.get("result", {}).get("message_id")
    except Exception as e:
        logging.error(f"Failed to send message to {chat_id}: {e}")
        return None


def send_photo(session, timeout, api_base, chat_id, photo, caption=None, reply_markup=None):
    payload = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = session.post(f"{api_base}/sendPhoto", json=payload, timeout=timeout)
        if response.status_code != 200:
            logging.error(f"Failed to send photo to {chat_id}: {response.status_code} - {response.text}")
            return None
        data = response.json()
        if not data.get("ok"):
            logging.error(f"Telegram API error sending photo to {chat_id}: {data}")
            return None
        logging.info(f"Photo sent successfully to {chat_id}")
        return data.get("result", {}).get("message_id")
    except Exception as e:
        logging.error(f"Failed to send photo to {chat_id}: {e}")
        return None


def send_video(session, timeout, api_base, chat_id, video, caption=None, reply_markup=None):
    payload = {"chat_id": chat_id, "video": video}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = session.post(f"{api_base}/sendVideo", json=payload, timeout=timeout)
        if response.status_code != 200:
            logging.error(f"Failed to send video to {chat_id}: {response.status_code} - {response.text}")
            return None
        data = response.json()
        if not data.get("ok"):
            logging.error(f"Telegram API error sending video to {chat_id}: {data}")
            return None
        logging.info(f"Video sent successfully to {chat_id}")
        return data.get("result", {}).get("message_id")
    except Exception as e:
        logging.error(f"Failed to send video to {chat_id}: {e}")
        return None


def delete_message(session, timeout, api_base, chat_id, message_id):
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        response = session.post(f"{api_base}/deleteMessage", json=payload, timeout=timeout)
        if response.status_code != 200:
            logging.error(f"Failed to delete message {message_id} for {chat_id}: {response.status_code} - {response.text}")
            return False
        data = response.json()
        if not data.get("ok"):
            logging.error(f"Telegram API error deleting message {message_id} for {chat_id}: {data}")
            return False
        return True
    except Exception as e:
        logging.error(f"Failed to delete message {message_id} for {chat_id}: {e}")
        return False


def edit_message_reply_markup(session, timeout, api_base, chat_id, message_id, reply_markup):
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
    try:
        response = session.post(f"{api_base}/editMessageReplyMarkup", json=payload, timeout=timeout)
        if response.status_code != 200:
            logging.error(
                f"Failed to edit reply markup for {message_id} in {chat_id}: "
                f"{response.status_code} - {response.text}"
            )
            return False
        data = response.json()
        if not data.get("ok"):
            logging.error(f"Telegram API error editing reply markup for {message_id}: {data}")
            return False
        return True
    except Exception as e:
        logging.error(f"Failed to edit reply markup for {message_id} in {chat_id}: {e}")
        return False
