import logging


def send_message(session, timeout, api_base, chat_id, text, reply_markup=None, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        session.post(f"{api_base}/sendMessage", json=payload, timeout=timeout)
    except Exception as e:
        logging.error(f"Failed to send message to {chat_id}: {e}")


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
        else:
            logging.info(f"Photo sent successfully to {chat_id}")
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Failed to send photo to {chat_id}: {e}")
        return False


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
        else:
            logging.info(f"Video sent successfully to {chat_id}")
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Failed to send video to {chat_id}: {e}")
        return False
