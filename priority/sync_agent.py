"""
SyncAgent — סנכרון ספקים ופריטים מפריורטי ל-cache מקומי
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from config.settings import CACHE_DIR
from priority.priority_client import PriorityClient

logger = logging.getLogger("פריורטי.סנכרון")

SUPPLIERS_CACHE = CACHE_DIR / "suppliers.json"
PARTS_CACHE = CACHE_DIR / "parts.json"
SYNC_STATUS_FILE = CACHE_DIR / "sync_status.json"


def _load_cache(path: Path) -> list[dict]:
    """טוען cache מקובץ JSON."""
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_cache(path: Path, data: list[dict]) -> None:
    """שומר cache לקובץ JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_cached_suppliers() -> list[dict]:
    """מחזיר רשימת ספקים מה-cache."""
    return _load_cache(SUPPLIERS_CACHE)


def get_cached_parts() -> list[dict]:
    """מחזיר רשימת פריטים מה-cache."""
    return _load_cache(PARTS_CACHE)


def get_sync_status() -> dict:
    """מחזיר את מצב הסנכרון האחרון."""
    if SYNC_STATUS_FILE.exists():
        with open(SYNC_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_sync": None, "suppliers_count": 0, "parts_count": 0, "status": "לא סונכרן"}


async def sync_from_priority(priority_client: PriorityClient) -> dict:
    """מסנכרן ספקים ופריטים מפריורטי ל-cache מקומי."""
    logger.info("מתחיל סנכרון מפריורטי...")

    status = {
        "last_sync": datetime.now().isoformat(),
        "suppliers_count": 0,
        "parts_count": 0,
        "status": "מסנכרן...",
        "errors": [],
    }

    # סנכרון ספקים
    try:
        suppliers = await priority_client.get_all_suppliers()
        _save_cache(SUPPLIERS_CACHE, suppliers)
        status["suppliers_count"] = len(suppliers)
        logger.info("סונכרנו %d ספקים", len(suppliers))
    except Exception as e:
        status["errors"].append(f"שגיאה בסנכרון ספקים: {e}")
        logger.error("שגיאה בסנכרון ספקים: %s", e)

    # סנכרון פריטים
    try:
        parts = await priority_client.get_all_parts()
        _save_cache(PARTS_CACHE, parts)
        status["parts_count"] = len(parts)
        logger.info("סונכרנו %d פריטים", len(parts))
    except Exception as e:
        status["errors"].append(f"שגיאה בסנכרון פריטים: {e}")
        logger.error("שגיאה בסנכרון פריטים: %s", e)

    status["status"] = "הושלם" if not status["errors"] else "הושלם עם שגיאות"

    # שמירת סטטוס
    with open(SYNC_STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    logger.info("סנכרון הושלם — ספקים: %d, פריטים: %d", status["suppliers_count"], status["parts_count"])
    return status
