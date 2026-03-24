"""
FastAPI Server — כל נקודות ה-API של המערכת
"""
import logging
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agents.models import Invoice, InvoiceSource, InvoiceStatus
from agents.orchestrator import Orchestrator
from priority.priority_client import PriorityClient
from priority.invoice_submitter import submit_approved_invoice
from priority.sync_agent import sync_from_priority, get_sync_status
from tools.invoice_store import InvoiceStore
from config.settings import INVOICES_DIR
from database import db as companies_db
from database.sync import sync_all as sync_companies_from_priority

logger = logging.getLogger("אתר.שרת")

app = FastAPI(title="SupplierInvoice", version="1.0.0")

# --- Static files ---
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Shared state (initialized in main.py) ---
store: Optional[InvoiceStore] = None
priority_client: Optional[PriorityClient] = None
orchestrator: Optional[Orchestrator] = None


def init_dependencies(s: InvoiceStore, pc: PriorityClient, o: Orchestrator) -> None:
    """אתחול התלויות — נקרא מ-main.py."""
    global store, priority_client, orchestrator
    store = s
    priority_client = pc
    orchestrator = o


# ===================== ROUTES =====================

@app.get("/", response_class=HTMLResponse)
async def serve_spa():
    """הגשת ה-SPA (index.html)."""
    index_file = TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health_check():
    """בדיקת תקינות השרת."""
    priority_ok = False
    if priority_client:
        try:
            priority_ok = await priority_client.health_check()
        except Exception:
            pass

    return {
        "status": "ok",
        "priority_connected": priority_ok,
        "invoices_count": len(store.get_all()) if store else 0,
    }


# --- חשבוניות ---

@app.get("/api/invoices")
async def list_invoices(status: Optional[str] = Query(None)):
    """רשימת חשבוניות — אפשר לסנן לפי סטטוס."""
    invoices = store.get_all(status=status)
    return {"invoices": [asdict(inv) for inv in invoices]}


@app.get("/api/invoices/{invoice_id}")
async def get_invoice(invoice_id: str):
    """פרטי חשבונית ספציפית."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    return asdict(invoice)


@app.get("/api/invoices/{invoice_id}/file")
async def get_invoice_file(invoice_id: str):
    """הורדת קובץ מקורי (PDF/תמונה)."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    file_path = Path(invoice.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="קובץ לא נמצא")

    # קביעת סוג MIME נכון להצגה בדפדפן
    suffix = file_path.suffix.lower()
    mime_map = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    media_type = mime_map.get(suffix, "application/octet-stream")

    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type=media_type,
        content_disposition_type="inline",
    )


@app.post("/api/invoices/upload")
async def upload_invoice(file: UploadFile = File(...)):
    """העלאת חשבונית + הפעלת pipeline."""
    # בדיקת סוג קובץ
    ext = Path(file.filename).suffix.lower()
    allowed = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"סוג קובץ לא נתמך: {ext}")

    # שמירת הקובץ
    file_id = str(uuid.uuid4())
    save_name = f"{file_id}{ext}"
    save_path = INVOICES_DIR / save_name

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    logger.info("קובץ הועלה: %s → %s", file.filename, save_path)

    # יצירת רשומת חשבונית
    invoice = Invoice(
        id=file_id,
        source=InvoiceSource.UPLOAD,
        file_path=str(save_path),
        file_type="pdf" if ext == ".pdf" else "image",
    )
    store.save(invoice)

    # הפעלת עיבוד ברקע
    orchestrator.start_background_processing(invoice.id)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "message": "החשבונית הועלתה ונכנסה לעיבוד",
    }


@app.post("/api/invoices/{invoice_id}/ocr-crop")
async def ocr_crop_api(invoice_id: str, body: dict = {}):
    """OCR על אזור חתוך — לבדיקה."""
    from agents.invoice_extractor import _crop_image, _read_file_as_base64, ANTHROPIC_API_KEY, AI_MODEL
    import anthropic, base64

    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    crop_coords = body.get("crop_coords", {})
    logger.info("OCR crop לחשבונית %s — coords: %s", invoice_id, crop_coords)

    try:
        cropped_b64 = _crop_image(invoice.file_path, crop_coords)
        if not cropped_b64:
            return {"error": "לא ניתן לחתוך תמונה"}

        # שמירת החיתוך לדיבוג
        import io
        from pathlib import Path
        debug_path = Path(invoice.file_path).parent / f"debug_crop_{invoice_id[:8]}.png"
        with open(debug_path, "wb") as f:
            f.write(base64.b64decode(cropped_b64))
        logger.info("חיתוך נשמר: %s", debug_path)

        # שליחה ל-Claude לזיהוי טקסט
        target = body.get("target", "supplier")
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        if target == "allocation":
            ocr_prompt = """קרא את התמונה והחזר JSON בלבד:
{
  "ocr_text": "כל הטקסט שאתה רואה, שורה אחרי שורה",
  "allocation_number": "מספר הקצאה — רצף של ספרות בלבד (בדרך כלל 9 ספרות)"
}
מספר הקצאה הוא מספר אישור ניכוי מס במקור שמונפק על ידי רשות המיסים.
החזר JSON בלבד ללא טקסט נוסף."""
        else:
            ocr_prompt = """קרא את התמונה והחזר JSON בלבד:
{
  "ocr_text": "כל הטקסט שאתה רואה, שורה אחרי שורה",
  "tax_id": "מספר ח.פ או ע.מ אם יש — ספרות בלבד",
  "tax_id_type": "ח.פ או ע.מ",
  "name": "שם העסק או האדם אם מופיע"
}
החזר JSON בלבד ללא טקסט נוסף."""

        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": cropped_b64}},
                    {"type": "text", "text": ocr_prompt},
                ],
            }],
        )
        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
            raw_text = raw_text.rsplit("```", 1)[0]

        import json as json_mod
        ocr_data = json_mod.loads(raw_text)
        logger.info("OCR result: %s", ocr_data)

        # עדכון בחשבונית
        updated_field = None
        db_match = None

        # טיפול במספר הקצאה
        if target == "allocation" and invoice.extracted_data:
            alloc = ocr_data.get("allocation_number", "")
            if alloc:
                invoice.extracted_data.allocation_number = alloc
                updated_field = "allocation"
                invoice.updated_at = datetime.now().isoformat()
                store.save(invoice)
                logger.info("עודכן מספר הקצאה: %s", alloc)
            return {
                "crop_coords": crop_coords,
                "ocr_data": ocr_data,
                "updated_field": updated_field,
                "db_match": None,
                "extracted_data": asdict(invoice.extracted_data) if invoice.extracted_data else None,
            }

        if ocr_data.get("tax_id") and invoice.extracted_data:
            # חיפוש ב-DB לפי ח.פ/ע.מ
            db_match = companies_db.find_by_tax_id(ocr_data["tax_id"], target)
            if db_match:
                logger.info("נמצא ב-DB: %s (קוד %s)", db_match["name"], db_match["priority_code"])

            if target == "supplier":
                if db_match:
                    invoice.extracted_data.supplier.name = db_match["name"]
                    invoice.extracted_data.supplier.tax_id = db_match["tax_id"] or ocr_data["tax_id"]
                    invoice.extracted_data.supplier.tax_id_type = db_match.get("tax_id_type") or ocr_data.get("tax_id_type", "")
                    invoice.extracted_data.supplier.priority_supplier_code = db_match["priority_code"]
                    invoice.extracted_data.supplier.priority_match_found = True
                    if db_match.get("address"):
                        invoice.extracted_data.supplier.address = db_match["address"]
                else:
                    invoice.extracted_data.supplier.tax_id = ocr_data["tax_id"]
                    if ocr_data.get("tax_id_type"):
                        invoice.extracted_data.supplier.tax_id_type = ocr_data["tax_id_type"]
                    if ocr_data.get("name"):
                        invoice.extracted_data.supplier.name = ocr_data["name"]
                    invoice.extracted_data.supplier.priority_match_found = False
                updated_field = "supplier"
            else:
                if db_match:
                    invoice.extracted_data.customer.name = db_match["name"]
                    invoice.extracted_data.customer.tax_id = db_match["tax_id"] or ocr_data["tax_id"]
                    invoice.extracted_data.customer.tax_id_type = db_match.get("tax_id_type") or ocr_data.get("tax_id_type", "")
                    invoice.extracted_data.customer.priority_customer_code = db_match["priority_code"]
                    invoice.extracted_data.customer.priority_match_found = True
                    if db_match.get("address"):
                        invoice.extracted_data.customer.address = db_match["address"]
                    # חיפוש סניף לפי ח.פ/ע.מ
                    branch = companies_db.find_branch_by_tax_id(ocr_data["tax_id"])
                    if branch:
                        invoice.extracted_data.customer.branch = branch["branch_code"]
                        logger.info("סניף נמצא: %s (%s)", branch["branch_code"], branch["name"])
                else:
                    invoice.extracted_data.customer.tax_id = ocr_data["tax_id"]
                    if ocr_data.get("tax_id_type"):
                        invoice.extracted_data.customer.tax_id_type = ocr_data["tax_id_type"]
                    if ocr_data.get("name"):
                        invoice.extracted_data.customer.name = ocr_data["name"]
                    invoice.extracted_data.customer.priority_match_found = False
                updated_field = "customer"

            invoice.updated_at = datetime.now().isoformat()
            store.save(invoice)
            logger.info("עודכן %s tax_id=%s (%s) — DB match: %s",
                        target, ocr_data["tax_id"], ocr_data.get("tax_id_type"),
                        "כן" if db_match else "לא")

        return {
            "crop_coords": crop_coords,
            "ocr_data": ocr_data,
            "updated_field": updated_field,
            "db_match": db_match,
            "extracted_data": asdict(invoice.extracted_data) if invoice.extracted_data else None,
        }
    except Exception as e:
        logger.error("שגיאת OCR crop: %s", e)
        return {"error": str(e)}


@app.post("/api/invoices/{invoice_id}/reextract")
async def reextract_invoice_api(invoice_id: str, body: dict = {}):
    """פענוח חוזר של חשבונית עם קואורדינטות אזור שסומן."""
    from agents.invoice_extractor import reextract_invoice
    from agents.data_validator import validate_invoice_data

    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    crop_coords = body.get("crop_coords", {})
    logger.info("פענוח חוזר לחשבונית %s עם coords: %s", invoice_id, crop_coords)

    invoice.status = InvoiceStatus.PROCESSING
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    try:
        invoice.extracted_data = await reextract_invoice(invoice.file_path, crop_coords)
        invoice.priority_validation = await validate_invoice_data(
            invoice.extracted_data, priority_client
        )
        invoice.status = InvoiceStatus.REVIEW
        invoice.error_message = ""
    except Exception as e:
        invoice.status = InvoiceStatus.ERROR
        invoice.error_message = str(e)
        logger.error("שגיאה בפענוח חוזר %s: %s", invoice_id, e)

    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "extracted_data": asdict(invoice.extracted_data) if invoice.extracted_data else None,
        "message": "פענוח חוזר הושלם" if invoice.status == InvoiceStatus.REVIEW else invoice.error_message,
    }


@app.post("/api/invoices/{invoice_id}/update-field")
async def update_invoice_field(invoice_id: str, body: dict = {}):
    """עדכון שדה בודד בנתוני החשבונית."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    if not invoice.extracted_data:
        raise HTTPException(status_code=400, detail="אין נתונים מחולצים")

    path = body.get("path", "")
    value = body.get("value", "")
    parts = path.split(".")

    # ניווט לאובייקט הנכון
    obj = invoice.extracted_data
    for part in parts[:-1]:
        if isinstance(obj, list) and part.isdigit():
            idx = int(part)
            if idx < len(obj):
                obj = obj[idx]
            else:
                raise HTTPException(status_code=400, detail=f"אינדקס לא קיים: {idx}")
        else:
            obj = getattr(obj, part, None)
            if obj is None:
                raise HTTPException(status_code=400, detail=f"שדה לא קיים: {path}")

    field = parts[-1]
    if hasattr(obj, field):
        # המרה לסוג הנכון
        current = getattr(obj, field)
        if isinstance(current, float):
            try:
                value = float(value) if value else 0.0
            except ValueError:
                value = 0.0
        elif isinstance(current, int):
            try:
                value = int(value) if value else 0
            except ValueError:
                value = 0
        setattr(obj, field, value)
        invoice.updated_at = datetime.now().isoformat()
        store.save(invoice)
        logger.info("שדה %s עודכן ל: %s", path, value)
        return {"ok": True, "path": path, "value": value}
    else:
        raise HTTPException(status_code=400, detail=f"שדה לא קיים: {field}")


@app.post("/api/invoices/{invoice_id}/approve")
async def approve_invoice(invoice_id: str, body: dict = {}):
    """אישור קליטה בפריורטי."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    if invoice.status != InvoiceStatus.REVIEW:
        raise HTTPException(status_code=400, detail=f"לא ניתן לאשר חשבונית בסטטוס {invoice.status.value}")

    # עדכון הערות משתמש
    invoice.user_notes = body.get("notes", "")

    # קליטה בפריורטי
    invoice = await submit_approved_invoice(invoice, priority_client, store)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "priority_invoice_id": invoice.priority_invoice_id,
        "message": "החשבונית נקלטה בפריורטי" if invoice.status == InvoiceStatus.SUBMITTED else invoice.error_message,
    }


@app.post("/api/invoices/{invoice_id}/reject")
async def reject_invoice(invoice_id: str, body: dict = {}):
    """דחיית חשבונית."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    invoice.status = InvoiceStatus.REJECTED
    invoice.user_notes = body.get("reason", "")
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    logger.info("חשבונית %s נדחתה — סיבה: %s", invoice_id, invoice.user_notes)

    return {"id": invoice.id, "status": "rejected", "message": "החשבונית נדחתה"}


@app.delete("/api/invoices/{invoice_id}")
async def delete_invoice(invoice_id: str):
    """מחיקת חשבונית כולל הקובץ."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    # מחיקת הקובץ
    file_path = Path(invoice.file_path)
    if file_path.exists():
        file_path.unlink()
        logger.info("קובץ נמחק: %s", file_path)

    # מחיקת דיבוג crop אם קיים
    debug_crop = file_path.parent / f"debug_crop_{invoice_id[:8]}.png"
    if debug_crop.exists():
        debug_crop.unlink()

    # מחיקה מה-store
    store.delete(invoice_id)
    logger.info("חשבונית %s נמחקה", invoice_id)

    return {"id": invoice_id, "message": "החשבונית נמחקה"}


# --- סנכרון ---

@app.post("/api/sync/priority")
async def sync_priority():
    """הפעלת סנכרון ספקים/פריטים מפריורטי."""
    result = await sync_from_priority(priority_client)
    return result


@app.get("/api/sync/status")
async def sync_status():
    """מצב סנכרון אחרון."""
    return get_sync_status()


# --- בסיס נתונים חברות ---

@app.post("/api/db/sync")
async def sync_companies_db():
    """סנכרון ספקים ולקוחות מפריורטי ל-DB."""
    try:
        result = await sync_companies_from_priority()
        return {"status": "ok", **result}
    except Exception as e:
        logger.error("שגיאת סנכרון DB: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/db/stats")
async def db_stats():
    """סטטיסטיקות בסיס הנתונים."""
    return companies_db.get_stats()


@app.get("/api/db/search")
async def search_companies(
    q: str = Query(..., min_length=1),
    type: Optional[str] = Query(None),
):
    """חיפוש חברה לפי שם או ח.פ/ע.מ."""
    # ניסיון חיפוש לפי מספר (ח.פ/ע.מ)
    if q.strip().isdigit():
        result = companies_db.find_by_tax_id(q.strip(), type)
        if result:
            return {"results": [result]}

    # חיפוש לפי שם
    results = companies_db.find_by_name(q, type)
    return {"results": results}
