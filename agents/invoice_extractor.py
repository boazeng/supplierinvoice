"""
InvoiceExtractorAgent — חילוץ נתונים מחשבונית באמצעות Claude Vision API
"""
import base64
import json
import logging
from pathlib import Path

import anthropic
import httpx

from agents.models import InvoiceData, InvoiceLine, SupplierInfo, CustomerInfo
from config.settings import ANTHROPIC_API_KEY, AI_MODEL

logger = logging.getLogger("סוכן.חילוץ")

EXTRACTION_PROMPT = """אתה מערכת לחילוץ נתונים מחשבוניות ספק ישראליות בעברית.

## מבנה חשבונית ישראלית — חשוב מאוד!

חשבונית ישראלית בנויה כך (מלמעלה למטה):

### 1. חלק עליון — פרטי הספק (מנפיק החשבונית)
   - שם העסק / לוגו של הספק
   - ע.מ (עוסק מורשה) או ח.פ (חברה פרטית) של הספק
   - כתובת וטלפון של הספק
   - מספר הקצאה (מספרי אישור ניכוי מס במקור מרשות המיסים)
   ⚠️ הספק הוא תמיד מי שמנפיק את החשבונית — הפרטים שלו בראש הדף!

### 2. חלק אמצעי — פרטי הלקוח (מקבל החשבונית)
   - מופיע אחרי כיתוב "לכבוד:" או "נמען:" או "Bill To:"
   - שם הלקוח (החברה שמקבלת את החשבונית)
   - ח.פ או ע.מ של הלקוח
   - כתובת הלקוח
   ⚠️ הלקוח הוא מי שמשלם — הפרטים שלו מופיעים אחרי "לכבוד"

### 3. פרטי החשבונית — מספר, תאריך, שורות, סכומים

## סוגי מזהים
- **ע.מ** (עוסק מורשה) — ליד הכיתוב "ע.מ" או "עוסק מורשה", בדרך כלל 9 ספרות
- **ח.פ** (חברה פרטית) — ליד הכיתוב "ח.פ" או "ח.פ.", בדרך כלל 9 ספרות
- חפש את המזהים ליד הכיתובים האלה בכל מקום בחשבונית

## מספר הקצאה
מספר הקצאה הוא מספר אישור ניכוי מס במקור שמונפק על ידי רשות המיסים.
הוא מופיע בדרך כלל בחלק העליון של החשבונית ליד הכיתוב "מס' הקצאה" או "הקצאה".

החזר JSON מדויק עם השדות הבאים:

{
  "invoice_number": "מספר חשבונית",
  "invoice_date": "YYYY-MM-DD",
  "allocation_number": "מספר הקצאה — מספר אישור ניכוי מס במקור",
  "supplier": {
    "name": "שם הספק — מהחלק העליון של החשבונית",
    "tax_id": "ספרות בלבד — ע.מ או ח.פ מהחלק העליון",
    "tax_id_type": "ע.מ או ח.פ",
    "address": "כתובת הספק",
    "phone": "טלפון הספק"
  },
  "customer": {
    "name": "שם הלקוח — מופיע אחרי לכבוד:",
    "tax_id": "ספרות בלבד — ע.מ או ח.פ של הלקוח",
    "tax_id_type": "ע.מ או ח.פ",
    "address": "כתובת הלקוח",
    "branch": "שם הסניף או קוד הסניף אם מופיע בחשבונית (לדוגמה: ת.א, ירושלים, BCC-TLV וכו') — ריק אם לא מופיע"
  },
  "lines": [
    {
      "line_number": 1,
      "description": "תיאור הפריט",
      "catalog_number": "מק\\"ט אם קיים",
      "quantity": 0.0,
      "unit_price": 0.0,
      "total_price": 0.0,
      "vat_amount": 0.0
    }
  ],
  "subtotal": 0.0,
  "vat_amount": 0.0,
  "total_amount": 0.0,
  "currency": "ILS",
  "confidence_score": 0.95,
  "extraction_warnings": ["אזהרות אם יש"]
}

כללים:
- החזר JSON בלבד, ללא טקסט נוסף
- תאריכים בפורמט YYYY-MM-DD
- סכומים כמספרים (לא מחרוזות)
- tax_id חייב להכיל ספרות בלבד (הסר מקפים, רווחים ונקודות)
- tax_id_type חייב להיות "ח.פ" או "ע.מ" בלבד
- פרטי הספק תמיד מהחלק העליון של החשבונית!
- פרטי הלקוח תמיד מהחלק שמופיע אחרי "לכבוד"!
- שדה branch: אם בחשבונית מופיע שם סניף, קוד סניף, או כינוי כמו "ת.א", "ירושלים", "צפון" וכו' — שים אותו ב-branch. אחרת — ריק.
- אם שדה לא נמצא — החזר ערך ריק מתאים
- confidence_score: 0.0-1.0 לפי רמת הוודאות שלך
- הוסף אזהרות ב-extraction_warnings אם יש חוסר בהירות
- **מע"מ — חובה לחשב תמיד (18%)**: גם אם מע"מ לא מצוין בנפרד בחשבונית, יש לחשבו.
  אם subtotal ידוע: vat_amount = total_amount - subtotal
  אם subtotal לא ידוע: subtotal = round(total_amount / 1.18, 2), vat_amount = total_amount - subtotal
  **לעולם אל תחזיר vat_amount=0** אלא אם total_amount=0 או שמצוין מפורשות "פטור ממע"מ".
"""


def _read_file_as_base64(file_path: str) -> tuple[str, str]:
    """קורא קובץ ומחזיר base64 וסוג media."""
    path = Path(file_path)
    suffix = path.suffix.lower()

    media_type_map = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    media_type = media_type_map.get(suffix, "image/jpeg")

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")

    return data, media_type


def _parse_response_to_invoice_data(raw: dict) -> InvoiceData:
    """ממיר את ה-JSON שהתקבל מ-Claude למודל InvoiceData."""
    supplier_raw = raw.get("supplier", {})
    supplier = SupplierInfo(
        name=supplier_raw.get("name", ""),
        tax_id=supplier_raw.get("tax_id", ""),
        tax_id_type=supplier_raw.get("tax_id_type", ""),
        address=supplier_raw.get("address", ""),
        phone=supplier_raw.get("phone", ""),
    )

    customer_raw = raw.get("customer", {})
    customer = CustomerInfo(
        name=customer_raw.get("name", ""),
        tax_id=customer_raw.get("tax_id", ""),
        tax_id_type=customer_raw.get("tax_id_type", ""),
        address=customer_raw.get("address", ""),
        branch=customer_raw.get("branch", ""),
    )

    lines = []
    for line_raw in raw.get("lines", []):
        lines.append(InvoiceLine(
            line_number=line_raw.get("line_number", 0),
            description=line_raw.get("description", ""),
            catalog_number=line_raw.get("catalog_number", ""),
            quantity=float(line_raw.get("quantity", 0)),
            unit_price=float(line_raw.get("unit_price", 0)),
            total_price=float(line_raw.get("total_price", 0)),
            vat_amount=float(line_raw.get("vat_amount", 0)),
        ))

    subtotal     = float(raw.get("subtotal", 0))
    vat_amount   = float(raw.get("vat_amount", 0))
    total_amount = float(raw.get("total_amount", 0))

    # אם Claude החזיר vat=0 אבל יש total — חשב מע"מ 18% (כלל ברזל)
    if total_amount > 0 and vat_amount == 0:
        if subtotal > 0 and subtotal < total_amount:
            vat_amount = round(total_amount - subtotal, 2)
        else:
            subtotal   = round(total_amount / 1.18, 2)
            vat_amount = round(total_amount - subtotal, 2)

    return InvoiceData(
        invoice_number=raw.get("invoice_number", ""),
        invoice_date=raw.get("invoice_date", ""),
        allocation_number=raw.get("allocation_number", ""),
        supplier=supplier,
        customer=customer,
        lines=lines,
        subtotal=subtotal,
        vat_amount=vat_amount,
        total_amount=total_amount,
        currency=raw.get("currency", "ILS"),
        confidence_score=float(raw.get("confidence_score", 0)),
        extraction_warnings=raw.get("extraction_warnings", []),
    )


async def extract_invoice(file_path: str) -> InvoiceData:
    """מנתח חשבונית באמצעות Claude Vision ומחזיר InvoiceData."""
    import time
    t0 = time.monotonic()
    logger.info("מתחיל חילוץ נתונים מקובץ: %s", file_path)

    file_data, media_type = _read_file_as_base64(file_path)

    import os
    dev_mode = os.getenv("ENV", "production") == "development"
    http_client = httpx.AsyncClient(verify=not dev_mode)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, http_client=http_client)

    # בניית הבקשה עם תמונה
    if media_type == "application/pdf":
        content_block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": file_data,
            },
        }
    else:
        content_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": file_data,
            },
        }

    async with http_client:
        response = await client.messages.create(
            model=AI_MODEL,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        content_block,
                        {"type": "text", "text": EXTRACTION_PROMPT},
                    ],
                }
            ],
        )

    # חילוץ ה-JSON מהתשובה
    raw_text = response.content[0].text.strip()

    # ניקוי אם התשובה עטופה ב-markdown
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        raw_text = raw_text.rsplit("```", 1)[0]

    raw_data = json.loads(raw_text)
    invoice_data = _parse_response_to_invoice_data(raw_data)

    logger.info(
        "חילוץ הושלם — חשבונית: %s, ספק: %s, שורות: %d, ביטחון: %.2f, זמן: %.1fs",
        invoice_data.invoice_number,
        invoice_data.supplier.name,
        len(invoice_data.lines),
        invoice_data.confidence_score,
        time.monotonic() - t0,
    )

    return invoice_data


REEXTRACT_PROMPT = """אתה מערכת לחילוץ נתונים מחשבוניות ספק ישראליות.

המשתמש סימן אזור ספציפי בחשבונית (מוצג כחיתוך).
אני שולח לך שתי תמונות:
1. התמונה הראשונה — החשבונית המלאה
2. התמונה השנייה — חיתוך של האזור שהמשתמש סימן

שים לב במיוחד לאזור שסומן (התמונה השנייה) — זה המקום שבו הלקוח ציין שנמצאים פרטי הזיהוי.

נתח מחדש את כל החשבונית עם דגש מיוחד על:
- זיהוי נכון של הספק (מנפיק החשבונית — חלק עליון)
- זיהוי נכון של הלקוח (נמען — אחרי "לכבוד")
- ע.מ או ח.פ של שני הצדדים
- מספר הקצאה

## מבנה חשבונית ישראלית:
- חלק עליון = פרטי הספק (שם, ע.מ/ח.פ, כתובת, טלפון)
- אחרי "לכבוד:" = פרטי הלקוח (שם, ח.פ/ע.מ)
- אח"כ = פרטי החשבונית

החזר JSON מדויק בפורמט הבא:
{
  "invoice_number": "מספר חשבונית",
  "invoice_date": "YYYY-MM-DD",
  "allocation_number": "מספר הקצאה",
  "supplier": {
    "name": "שם הספק",
    "tax_id": "ספרות בלבד",
    "tax_id_type": "ע.מ או ח.פ",
    "address": "כתובת",
    "phone": "טלפון"
  },
  "customer": {
    "name": "שם הלקוח",
    "tax_id": "ספרות בלבד",
    "tax_id_type": "ע.מ או ח.פ",
    "address": "כתובת",
    "branch": "שם או קוד סניף אם מופיע — אחרת ריק"
  },
  "lines": [{"line_number": 1, "description": "", "catalog_number": "", "quantity": 0.0, "unit_price": 0.0, "total_price": 0.0, "vat_amount": 0.0}],
  "subtotal": 0.0,
  "vat_amount": 0.0,
  "total_amount": 0.0,
  "currency": "ILS",
  "confidence_score": 0.95,
  "extraction_warnings": []
}

כללים:
- החזר JSON בלבד, ללא טקסט נוסף
- tax_id — ספרות בלבד (הסר מקפים, רווחים ונקודות)
- tax_id_type — "ח.פ" או "ע.מ" בלבד
- **מע"מ — חובה לחשב תמיד (18%)**: אם vat_amount=0 ו-total_amount>0, חשב: subtotal=round(total/1.18,2), vat_amount=total-subtotal
"""


async def reextract_invoice(file_path: str, crop_coords: dict) -> InvoiceData:
    """פענוח חוזר עם חיתוך אזור שהמשתמש סימן."""
    logger.info("פענוח חוזר מקובץ: %s עם קואורדינטות: %s", file_path, crop_coords)

    file_data, media_type = _read_file_as_base64(file_path)
    import os
    dev_mode = os.getenv("ENV", "production") == "development"
    http_client = httpx.AsyncClient(verify=not dev_mode)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, http_client=http_client)

    content_blocks = []

    # תמונה/PDF מלא
    if media_type == "application/pdf":
        content_blocks.append({
            "type": "document",
            "source": {"type": "base64", "media_type": media_type, "data": file_data},
        })
    else:
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": file_data},
        })

    # חיתוך האזור שהמשתמש סימן (רק לתמונות)
    if media_type != "application/pdf":
        try:
            cropped_data = _crop_image(file_path, crop_coords)
            if cropped_data:
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": cropped_data},
                })
        except Exception as e:
            logger.warning("לא ניתן לחתוך תמונה: %s", e)

    content_blocks.append({"type": "text", "text": REEXTRACT_PROMPT})

    async with http_client:
        response = await client.messages.create(
            model=AI_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": content_blocks}],
        )

    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        raw_text = raw_text.rsplit("```", 1)[0]

    raw_data = json.loads(raw_text)
    invoice_data = _parse_response_to_invoice_data(raw_data)

    logger.info(
        "פענוח חוזר הושלם — ספק: %s (%s %s), לקוח: %s (%s %s)",
        invoice_data.supplier.name, invoice_data.supplier.tax_id_type, invoice_data.supplier.tax_id,
        invoice_data.customer.name, invoice_data.customer.tax_id_type, invoice_data.customer.tax_id,
    )

    return invoice_data


def _crop_image(file_path: str, coords: dict) -> str | None:
    """חותך אזור מהתמונה לפי אחוזים ומחזיר base64 PNG."""
    from PIL import Image
    import io

    img = Image.open(file_path)
    w, h = img.size

    # coords באחוזים: left, top, width, height (0-100)
    x = int(w * coords.get("left", 0) / 100)
    y = int(h * coords.get("top", 0) / 100)
    cw = int(w * coords.get("width", 50) / 100)
    ch = int(h * coords.get("height", 15) / 100)

    # הרחבת האזור ב-10% לכל כיוון
    pad_x = int(cw * 0.1)
    pad_y = int(ch * 0.1)
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(w, x + cw + pad_x)
    y2 = min(h, y + ch + pad_y)

    cropped = img.crop((x1, y1, x2, y2))

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")
