"""זיהוי תאריך המסמך באמצעות Claude Vision.

משמש את מערכת ספרי הנהלת החשבונות — כשמעלים מסמך, המערכת מנסה לזהות
את התאריך שעל המסמך כדי לתייק אותו לפי החודש/יום הנכון.
"""
import base64
import logging
import re
from pathlib import Path

import anthropic

from config.settings import ANTHROPIC_API_KEY, AI_MODEL

logger = logging.getLogger("סוכן.תאריך")

_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
}

_PROMPT = """מצא את התאריך הראשי של המסמך — התאריך שבו המסמך הונפק או נכתב
(תאריך החשבונית / הקבלה / המכתב / הדוח).
אם מופיעים כמה תאריכים, בחר את התאריך הרשמי של המסמך עצמו.
החזר אך ורק תאריך בפורמט YYYY-MM-DD — בלי שום טקסט נוסף.
אם אין במסמך תאריך כלשהו, החזר את המילה NONE."""


def detect_document_date(file_path: str) -> str | None:
    """מחזיר תאריך מסמך בפורמט YYYY-MM-DD, או None אם לא זוהה."""
    suffix = Path(file_path).suffix.lower()
    media_type = _MEDIA_TYPES.get(suffix, "image/jpeg")

    try:
        with open(file_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
    except OSError as exc:
        logger.error("קריאת הקובץ נכשלה: %s", exc)
        return None

    if media_type == "application/pdf":
        block = {"type": "document",
                 "source": {"type": "base64", "media_type": media_type, "data": data}}
    else:
        block = {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": data}}

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": _PROMPT}]}],
        )
        text = resp.content[0].text.strip()
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if match:
            logger.info("תאריך מסמך זוהה: %s", match.group(0))
            return match.group(0)
        logger.info("לא זוהה תאריך במסמך")
    except Exception as exc:  # noqa: BLE001
        logger.error("זיהוי תאריך נכשל: %s", exc)
    return None
