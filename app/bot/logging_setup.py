import logging
import sys
from .config_loader import LOGS_DIR


def setup_logging():
    """Setup comprehensive logging system with separate info and debug logs"""

    LOGS_DIR.mkdir(exist_ok=True)

    detailed_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(funcName)-20s | Line %(lineno)-4d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    simple_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(simple_formatter)
    logger.addHandler(console_handler)

    info_handler = logging.FileHandler(LOGS_DIR / "bot.log", encoding='utf-8')
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(simple_formatter)
    logger.addHandler(info_handler)

    debug_handler = logging.FileHandler(LOGS_DIR / "debug.log", encoding='utf-8')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(detailed_formatter)
    logger.addHandler(debug_handler)

    error_handler = logging.FileHandler(LOGS_DIR / "error.log", encoding='utf-8')
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    logger.addHandler(error_handler)

    activity_logger = logging.getLogger('user_activity')
    activity_logger.setLevel(logging.DEBUG)
    activity_logger.propagate = False

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
    logging.info("Console Output: INFO level and above")
    logging.info(f"General Log: {LOGS_DIR / 'bot.log'} (INFO+)")
    logging.info(f"Debug Log: {LOGS_DIR / 'debug.log'} (ALL messages)")
    logging.info(f"Error Log: {LOGS_DIR / 'error.log'} (ERROR+)")
    logging.info(f"Activity Log: {LOGS_DIR / 'user_activity.log'} (All user interactions)")
    logging.info("=" * 80)
