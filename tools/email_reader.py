"""
EmailReader — קריאת חשבוניות מ-IMAP (אופציונלי)
"""
import email
import imaplib
import logging
import os
from email.header import decode_header
from pathlib import Path
from typing import Optional

from config.settings import (
    EMAIL_HOST, EMAIL_PORT, EMAIL_USER, EMAIL_PASS, EMAIL_FOLDER, INVOICES_DIR,
)

logger = logging.getLogger("כלים.אימייל")


class EmailReader:
    """קורא חשבוניות מצורפות מתיבת אימייל."""

    def __init__(self) -> None:
        self.host = EMAIL_HOST
        self.port = EMAIL_PORT
        self.user = EMAIL_USER
        self.password = EMAIL_PASS
        self.folder = EMAIL_FOLDER

    def is_configured(self) -> bool:
        """בודק אם הגדרות האימייל קיימות."""
        return bool(self.host and self.user and self.password)

    async def fetch_invoices(self) -> list[str]:
        """
        מתחבר ל-IMAP, מחפש הודעות עם קבצים מצורפים,
        ושומר קבצי PDF/תמונה לתיקיית invoices.
        מחזיר רשימת נתיבים של קבצים שנשמרו.
        """
        if not self.is_configured():
            logger.warning("הגדרות אימייל חסרות — מדלג")
            return []

        logger.info("מתחבר ל-%s...", self.host)
        saved_files: list[str] = []

        try:
            mail = imaplib.IMAP4_SSL(self.host, self.port)
            mail.login(self.user, self.password)
            mail.select(self.folder)

            # חיפוש הודעות שלא נקראו
            status, messages = mail.search(None, "UNSEEN")
            if status != "OK":
                logger.warning("לא הצלחתי לחפש הודעות")
                return []

            message_ids = messages[0].split()
            logger.info("נמצאו %d הודעות חדשות", len(message_ids))

            for msg_id in message_ids:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                for part in msg.walk():
                    if part.get_content_maintype() == "multipart":
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue

                    # פענוח שם הקובץ
                    decoded, charset = decode_header(filename)[0]
                    if isinstance(decoded, bytes):
                        filename = decoded.decode(charset or "utf-8")

                    # בדיקה שזה קובץ רלוונטי
                    ext = Path(filename).suffix.lower()
                    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"):
                        continue

                    # שמירה
                    save_path = INVOICES_DIR / filename
                    counter = 1
                    while save_path.exists():
                        save_path = INVOICES_DIR / f"{Path(filename).stem}_{counter}{ext}"
                        counter += 1

                    with open(save_path, "wb") as f:
                        f.write(part.get_payload(decode=True))

                    saved_files.append(str(save_path))
                    logger.info("נשמר קובץ מאימייל: %s", save_path.name)

            mail.logout()

        except Exception as e:
            logger.error("שגיאה בקריאת אימייל: %s", e)

        return saved_files
