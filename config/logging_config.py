"""
הגדרת לוגים — קונסול + קובץ
"""
import logging
import sys
from pathlib import Path
from config.settings import LOGS_DIR


def setup_logging() -> None:
    """מגדיר logging לכל המערכת — קונסול + קובץ."""
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # מנקה handlers קיימים
    root_logger.handlers.clear()

    # --- Console handler (עם תמיכה ב-UTF-8 ל-Windows) ---
    console_stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root_logger.addHandler(console_handler)

    # --- File handler ---
    log_file = LOGS_DIR / "supplierinvoice.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))
    root_logger.addHandler(file_handler)

    # הנמכת רמת לוגים של ספריות חיצוניות
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
