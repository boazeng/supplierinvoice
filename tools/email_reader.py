"""קורא מייל — משיכת חשבוניות מתיבת ה-IMAP הייעודית.

משתמש בספריות התקן imaplib + email בלבד (ללא תלויות חיצוניות).
מוגדר דרך INVOICE_INBOX_* ב-env (תיבה ייעודית לחשבוניות בלבד).
"""
import base64
import email
import imaplib
import logging
from email.header import decode_header

from config.settings import (
    INVOICE_INBOX_USER,
    INVOICE_INBOX_APP_PASSWORD,
    INVOICE_INBOX_HOST,
    INVOICE_INBOX_PORT,
)

logger = logging.getLogger("כלים.מייל")

_ALLOWED_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".gif")

# שם התווית ב-Gmail שאליה מועברות הודעות שעובדו (התווית קיימת בתיבה)
_MOVED_LABEL = "הועבר"


def _mutf7_encode(s: str) -> str:
    """קידוד למחרוזת IMAP modified UTF-7 (RFC 3501) — נדרש לתוויות בעברית."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).rstrip(b"=")
            out.append("&" + b64.decode("ascii").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if ch == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


_MOVED_LABEL_IMAP = _mutf7_encode(_MOVED_LABEL)


def inbox_configured() -> bool:
    """האם הוגדרו פרטי תיבת המייל הייעודית."""
    return bool(INVOICE_INBOX_USER and INVOICE_INBOX_APP_PASSWORD)


def _decode_header(value: str) -> str:
    """פענוח כותרת מייל מקודדת (לדוגמה שם קובץ בעברית)."""
    out = ""
    for text, enc in decode_header(value or ""):
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def fetch_invoice_attachments() -> list[dict]:
    """מתחבר לתיבה, מושך הודעות שלא נקראו ומחזיר את הקבצים המצורפים.

    כל פריט: {"filename": str, "content": bytes, "from": str}.
    ההודעות שנמשכו מסומנות כ"נקראו" — כך שמשיכה חוזרת לא תכפיל אותן.
    """
    if not inbox_configured():
        raise RuntimeError(
            "תיבת המייל לא הוגדרה — חסרים INVOICE_INBOX_USER / INVOICE_INBOX_APP_PASSWORD"
        )

    attachments: list[dict] = []
    conn = imaplib.IMAP4_SSL(INVOICE_INBOX_HOST, INVOICE_INBOX_PORT)
    try:
        conn.login(INVOICE_INBOX_USER, INVOICE_INBOX_APP_PASSWORD)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []
        msg_ids = data[0].split()
        logger.info("נמצאו %d הודעות חדשות בתיבה", len(msg_ids))

        for msg_id in msg_ids:
            # שליפת ההודעה (FETCH של RFC822 מסמן אותה אוטומטית כ"נקראה")
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = _decode_header(msg.get("From", ""))

            msg_attachments: list[dict] = []
            for part in msg.walk():
                if part.get_content_disposition() != "attachment":
                    continue
                filename = part.get_filename()
                if not filename:
                    continue
                filename = _decode_header(filename)
                if not filename.lower().endswith(_ALLOWED_EXT):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    msg_attachments.append(
                        {"filename": filename, "content": payload, "from": sender}
                    )

            attachments.extend(msg_attachments)

            # הודעה שנקלטה — מתייגים "הועבר" ומסירים מ-INBOX (ארכוב)
            if msg_attachments:
                try:
                    conn.store(msg_id, "+X-GM-LABELS", f'("{_MOVED_LABEL_IMAP}")')
                    conn.store(msg_id, "-X-GM-LABELS", "(\\Inbox)")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("תיוג/ארכוב נכשל להודעה %s: %s", msg_id, exc)

        logger.info("חולצו %d קבצים מצורפים מ-%d הודעות",
                    len(attachments), len(msg_ids))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return attachments
