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

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from shared_auth import install_auth, require_role
from website.ledger_routes import register_ledger_routes

from agents.models import Invoice, InvoiceSource, InvoiceStatus
from agents.orchestrator import Orchestrator
from priority.priority_client import PriorityClient
from priority.invoice_submitter import submit_approved_invoice
from priority.sync_agent import sync_from_priority, get_sync_status
from tools.invoice_store import InvoiceStore
from config.settings import INVOICES_DIR, BASE_DIR, DATA_DIR
from database import db as companies_db
from database import ledger_db
from database.sync import sync_all as sync_companies_from_priority

logger = logging.getLogger("אתר.שרת")

app = FastAPI(title="SupplierInvoice", version="1.0.0")

# --- Static files ---
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- אימות והרשאות (shared-auth) ---
auth = install_auth(
    app,
    db_path=BASE_DIR / "database" / "auth.db",
    redirect_uri="https://bookkeeping.newavera.co.il/auth/callback",
    initial_users=[
        {"email": "boazen@gmail.com", "role": "admin"},
        {"email": "boen01@gmail.com", "role": "admin"},
        {"email": "yael.israel303@gmail.com", "role": "admin"},
    ],
    public_prefixes=("/api/health", "/api/cron/", "/api/deploy", "/api/debug/"),
)

# --- מערכת ספרי הנהלת חשבונות ---
register_ledger_routes(app, TEMPLATES_DIR)

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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """סמל הלשונית — חשבונית (SVG)."""
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/admin/users", response_class=HTMLResponse, include_in_schema=False,
         dependencies=[Depends(require_role("admin"))])
async def admin_users_page():
    """מסך ניהול משתמשים — admin בלבד."""
    return HTMLResponse((TEMPLATES_DIR / "admin_users.html").read_text(encoding="utf-8"))


@app.post("/api/deploy")
async def github_deploy(request: Request):
    """GitHub webhook — git pull + systemctl restart בכל push."""
    import hmac, hashlib, subprocess, os
    secret = os.getenv("DEPLOY_SECRET", "")
    sig = request.headers.get("X-Hub-Signature-256", "")
    body = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail="invalid signature")
    # הנתיב נגזר מהמיקום האמיתי של הקובץ הנוכחי (עובד עם ec2-user ועם ubuntu)
    import os as _os
    repo_dir = str(Path(__file__).resolve().parent.parent)
    subprocess.Popen(["bash", "-c",
        f"sleep 1"
        f" && git -C {repo_dir} pull"
        f" && cd {repo_dir}/priority && npm install --production --silent"
        f" && sudo systemctl restart supplierinvoice"
    ])
    return {"ok": True}


@app.get("/api/debug/finalize/{ivnum}")
async def debug_finalize(ivnum: str):
    """מריץ את finalize_invoice.js עם IVNUM נתון ומחזיר stdout+stderr מלאים."""
    import asyncio, shutil
    for cmd in ["node", "nodejs"]:
        if shutil.which(cmd):
            node_cmd = cmd
            break
    else:
        return {"error": "node not found"}
    js_script = BASE_DIR / "priority" / "finalize_invoice.js"
    if not js_script.exists():
        return {"error": "finalize_invoice.js not found"}
    try:
        proc = await asyncio.create_subprocess_exec(
            node_cmd, str(js_script), ivnum,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR / "priority"),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/debug/close_odata/{ivnum}")
async def debug_close_odata(ivnum: str):
    """נסה לסגור חשבונית דרך OData bound action — ללא WCF."""
    import httpx as _httpx
    post_result = None
    post_error = None
    try:
        post_result = await priority_client._post(
            f"PINVOICES(IVNUM='{ivnum}',IVTYPE='P',DEBIT='D')/CLOSEPIV",
            {},
        )
    except _httpx.HTTPStatusError as e:
        post_error = {"status": e.response.status_code, "body": e.response.text[:500]}
    except Exception as e:
        post_error = {"error": str(e)}

    after = await priority_client._get(
        "PINVOICES",
        params={"$filter": f"IVNUM eq '{ivnum}'", "$select": "IVNUM,FNCNUM", "$top": "1"},
    )
    return {"post_result": post_result, "post_error": post_error, "after": after}


@app.get("/api/debug/node")
async def debug_node():
    """אבחון זמינות Node.js על השרת — ללא אימות."""
    import asyncio, shutil
    result = {}
    for cmd in ["node", "nodejs"]:
        path = shutil.which(cmd)
        result[cmd] = {"found": path is not None, "path": path}
        if path:
            try:
                proc = await asyncio.create_subprocess_exec(
                    cmd, "--version",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                result[cmd]["version"] = out.decode().strip()
            except Exception as e:
                result[cmd]["error"] = str(e)
    node_modules = BASE_DIR / "priority" / "node_modules"
    result["node_modules_exists"] = node_modules.exists()
    result["node_modules_path"] = str(node_modules)
    return result


@app.get("/api/debug/recent")
async def debug_recent():
    """מחזיר סטטוס חשבוניות אחרונות — ללא נתונים רגישים."""
    invs = sorted(store.get_all(), key=lambda i: i.updated_at or "", reverse=True)[:10]
    return [
        {
            "id": i.id[:8],
            "status": i.status.value,
            "priority_invoice_id": i.priority_invoice_id,
            "priority_journal_id": i.priority_journal_id,
            "error_message": (i.error_message or "")[:200],
            "updated_at": i.updated_at,
        }
        for i in invs
    ]


@app.get("/api/health")
async def health_check():
    """בדיקת תקינות השרת."""
    import subprocess as _sp
    priority_ok = False
    if priority_client:
        try:
            priority_ok = await priority_client.health_check()
        except Exception:
            pass

    git_commit = ""
    try:
        git_commit = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(BASE_DIR), text=True).strip()
    except Exception:
        pass

    return {
        "status": "ok",
        "priority_connected": priority_ok,
        "invoices_count": len(store.get_all()) if store else 0,
        "git_commit": git_commit,
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

    # יצירת רשומת חשבונית — העלאה ידנית גם ממתינה לאישור (מקור = upload)
    invoice = Invoice(
        id=file_id,
        source=InvoiceSource.UPLOAD,
        file_path=str(save_path),
        file_type="pdf" if ext == ".pdf" else "image",
        status=InvoiceStatus.PENDING_APPROVAL,
    )
    store.save(invoice)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "message": "החשבונית הועלתה — ממתינה לאישור",
    }


async def _fetch_and_store_email_invoices() -> dict:
    """לוגיקה משותפת — מושך את המייל ויוצר רשומות חשבונית בסטטוס 'ממתין לאישור'."""
    import asyncio
    from tools.email_reader import fetch_invoice_attachments, inbox_configured

    if not inbox_configured():
        raise HTTPException(status_code=400, detail="תיבת המייל לא הוגדרה עדיין")

    try:
        attachments = await asyncio.to_thread(fetch_invoice_attachments)
    except Exception as e:  # noqa: BLE001
        logger.error("שגיאה במשיכה מהמייל: %s", e)
        raise HTTPException(status_code=502, detail=f"שגיאה במשיכה מהמייל: {e}")

    created = 0
    for att in attachments:
        ext = Path(att["filename"]).suffix.lower()
        file_id = str(uuid.uuid4())
        save_path = INVOICES_DIR / f"{file_id}{ext}"
        with open(save_path, "wb") as f:
            f.write(att["content"])
        invoice = Invoice(
            id=file_id,
            source=InvoiceSource.EMAIL,
            file_path=str(save_path),
            file_type="pdf" if ext == ".pdf" else "image",
            status=InvoiceStatus.PENDING_APPROVAL,
        )
        store.save(invoice)
        created += 1
        logger.info("חשבונית מהמייל נקלטה: %s (%s)", att["filename"], att.get("from", ""))

    if created:
        logger.info("נמשכו %d חשבוניות מהמייל", created)
    return {"fetched": created}


@app.post("/api/invoices/fetch-email")
async def fetch_email_invoices():
    """משיכה ידנית של חשבוניות מתיבת המייל הייעודית — דורש משתמש מחובר."""
    return await _fetch_and_store_email_invoices()


@app.post("/api/cron/fetch-email")
async def cron_fetch_email(request: Request):
    """משיכה אוטומטית של חשבוניות מהמייל — מופעל על-ידי systemd timer בשרת.
    דורש כותרת X-Cron-Token התואמת ל-AUTH_EMERGENCY_TOKEN."""
    import os
    expected = os.getenv("AUTH_EMERGENCY_TOKEN", "")
    if not expected or request.headers.get("X-Cron-Token", "") != expected:
        raise HTTPException(status_code=403, detail="forbidden")
    return await _fetch_and_store_email_invoices()


@app.post("/api/invoices/{invoice_id}/extract")
async def extract_invoice_api(invoice_id: str):
    """מפעיל פענוח על חשבונית שממתינה לאישור לפענוח (נקלטה ממייל)."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    orchestrator.start_background_processing(invoice_id)
    logger.info("פענוח הופעל ידנית לחשבונית %s", invoice_id)
    return {"id": invoice_id, "status": "processing", "message": "הפענוח החל"}


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
    from agents.orchestrator import enrich_invoice_from_db

    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    crop_coords = body.get("crop_coords", {})
    logger.info("פענוח חוזר לחשבונית %s עם coords: %s", invoice_id, crop_coords)

    try:
        invoice.extracted_data = await reextract_invoice(invoice.file_path, crop_coords)
        # העשרת ספק/לקוח מ-DB → ממלא priority_supplier_code / priority_customer_code
        enrich_invoice_from_db(invoice)
        invoice.priority_validation = await validate_invoice_data(
            invoice.extracted_data, priority_client
        )
        invoice.extraction_ok = True
        invoice.status = InvoiceStatus.PENDING_SUBMISSION
        invoice.error_message = ""
    except Exception as e:
        invoice.extraction_ok = False
        invoice.status = InvoiceStatus.PENDING_EXTRACTION
        invoice.error_message = str(e)
        logger.error("שגיאה בפענוח חוזר %s: %s", invoice_id, e)

    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "extracted_data": asdict(invoice.extracted_data) if invoice.extracted_data else None,
        "message": "פענוח חוזר הושלם" if invoice.status == InvoiceStatus.PENDING_SUBMISSION else invoice.error_message,
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

        # זיכרון לטווח ארוך — חשבון הוצאות לספק שיוצג אוטומטית בחשבוניות עתידיות שלו
        if path == "expense_account" and invoice.extracted_data:
            sup_code = invoice.extracted_data.supplier.priority_supplier_code
            if sup_code and value:
                companies_db.set_supplier_expense_account(sup_code, str(value))
                logger.info("נשמר חשבון הוצאות %s לספק %s", value, sup_code)

        return {"ok": True, "path": path, "value": value}
    else:
        raise HTTPException(status_code=400, detail=f"שדה לא קיים: {field}")


@app.post("/api/invoices/{invoice_id}/file-to-ledger")
async def file_invoice_to_ledger(invoice_id: str):
    """תיוק חשבונית בספרי הנהלת חשבונות — לפי סניף + שנת חשבונית."""
    import re
    from datetime import date as date_cls
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    if invoice.status != InvoiceStatus.PENDING_FILING:
        raise HTTPException(status_code=400, detail="ניתן לתייק רק חשבוניות בסטטוס 'ממתין לתיוק'")

    d = invoice.extracted_data
    branch = (d.customer.branch if d else "") or "כללי"
    # שם החברה בספרים — שם הלקוח המלא, ואם חסר — קוד הסניף
    customer_name = (d.customer.name if d else "") or ""
    company_name = re.sub(r'[\\/:*?"<>|]', '_', customer_name or branch)
    supplier_name = re.sub(r'[\\/:*?"<>|]', '_', (d.supplier.name if d else "") or "ספק")
    invoice_num = re.sub(r'[\\/:*?"<>|]', '_', (d.invoice_number if d else "") or invoice_id[:8])
    invoice_date = (d.invoice_date if d else "") or date_cls.today().isoformat()
    try:
        year = int(invoice_date[:4])
    except (ValueError, TypeError):
        year = date_cls.today().year

    company_id = ledger_db.find_or_create_company(company_name)
    book_id = ledger_db.find_or_create_book(company_id, year)
    divider_id = ledger_db.find_or_create_divider(book_id, supplier_name)

    ext = Path(invoice.file_path).suffix.lower()
    filename = f"{supplier_name}_{invoice_num}{ext}"
    supplier_dir = DATA_DIR / "ledger" / str(book_id) / supplier_name
    supplier_dir.mkdir(parents=True, exist_ok=True)
    dest = supplier_dir / filename
    shutil.copy2(invoice.file_path, dest)

    ledger_db.create_document(
        book_id=book_id,
        file_path=str(dest),
        original_filename=filename,
        file_type=ext.lstrip("."),
        document_date=invoice_date,
        scan_date=date_cls.today().isoformat(),
        date_source="document",
        title=f"{supplier_name} {invoice_num}",
        divider_id=divider_id,
        invoice_id=invoice_id,
    )

    invoice.status = InvoiceStatus.FILED
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)
    logger.info("חשבונית %s תויקה בספרי הנהלת חשבונות — %s/%d", invoice_id, company_name, year)
    return {"ok": True, "branch": branch, "company_name": company_name, "year": year}


@app.post("/api/invoices/{invoice_id}/approve")
async def approve_invoice(invoice_id: str, body: dict = {}):
    """אישור קליטה בפריורטי."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    if invoice.status != InvoiceStatus.PENDING_SUBMISSION:
        raise HTTPException(status_code=400, detail=f"לא ניתן לקלוט חשבונית בסטטוס {invoice.status.value}")

    # עדכון הערות משתמש
    invoice.user_notes = body.get("notes", "")

    # קליטה בפריורטי
    invoice = await submit_approved_invoice(invoice, priority_client, store)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "priority_invoice_id": invoice.priority_invoice_id,
        "message": "החשבונית נקלטה בפריורטי" if invoice.status == InvoiceStatus.PENDING_FILING else invoice.error_message,
    }


# מעברי סטטוס ידניים — אישור, החזרה לאישור, העברה להמתנה, ביטול
_MANUAL_STATUS = {
    "pending_approval": InvoiceStatus.PENDING_APPROVAL,      # החזרה לאישור (מהמתנה/בוטל)
    "pending_extraction": InvoiceStatus.PENDING_EXTRACTION,  # אישור לפענוח
    "pending_filing": InvoiceStatus.PENDING_FILING,          # העברה ידנית לממתין לתיוק
    "on_hold": InvoiceStatus.ON_HOLD,                        # העברה להמתנה
    "cancelled": InvoiceStatus.CANCELLED,                    # ביטול
}


@app.post("/api/invoices/{invoice_id}/status")
async def set_invoice_status(invoice_id: str, body: dict = {}):
    """שינוי סטטוס ידני: אישור לפענוח / העברה להמתנה / ביטול / החזרה לתהליך."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    target = body.get("status", "")
    if target not in _MANUAL_STATUS:
        raise HTTPException(status_code=400, detail=f"סטטוס לא חוקי: {target}")
    invoice.status = _MANUAL_STATUS[target]
    if body.get("notes"):
        invoice.user_notes = body["notes"]
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)
    logger.info("סטטוס חשבונית %s שונה ידנית ל-%s", invoice_id, target)
    return {"id": invoice.id, "status": invoice.status.value}


@app.post("/api/test/create-filing-test")
async def create_filing_test():
    """יוצר חשבונית בדיקה בסטטוס ממתין לתיוק — לבדיקת כפתור התיוק."""
    from agents.models import SupplierInfo, CustomerInfo, InvoiceData
    import shutil as _shutil
    test_id = str(uuid.uuid4())
    # קובץ ריק לבדיקה
    test_path = INVOICES_DIR / f"{test_id}.pdf"
    # מחפש קובץ PDF קיים כלשהו להעתקה, אחרת יוצר ריק
    existing = list(INVOICES_DIR.glob("*.pdf"))
    if existing:
        _shutil.copy2(existing[0], test_path)
    else:
        test_path.write_bytes(b"%PDF-1.4 test")

    supplier = SupplierInfo(name="ספק בדיקה", tax_id="123456789",
                            priority_supplier_code="TEST001", priority_match_found=True)
    customer = CustomerInfo(name="החברה שלנו", branch="סניף מרכז")
    data = InvoiceData(invoice_number="INV-TEST-001", invoice_date="2026-01-15",
                       supplier=supplier, customer=customer,
                       subtotal=1000.0, vat_amount=170.0, total_amount=1170.0)
    invoice = Invoice(id=test_id, source=InvoiceSource.UPLOAD,
                      file_path=str(test_path), file_type="pdf",
                      status=InvoiceStatus.PENDING_FILING,
                      extracted_data=data, extraction_ok=True,
                      priority_invoice_id="TEST-PRIORITY-001")
    store.save(invoice)
    return {"id": test_id, "message": "חשבונית בדיקה נוצרה — פתחי אותה ובדקי את כפתור התיוק"}


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


@app.get("/api/db/accounts/search")
async def search_accounts_api(q: str = Query(default="")):
    """חיפוש חשבון GL לפי קוד או שם. q ריק = כל החשבונות (עד 200).
    אם ה-DB ריק — מסנכרן מפריורטי תחילה."""
    q = q.strip()
    # אם ה-DB ריק — נסנכרן מפריורטי ונחזיר תוצאות
    if companies_db.get_accounts_count() == 0:
        try:
            from database.sync import sync_accounts
            client = PriorityClient()
            await sync_accounts(client)
            await client.close()
        except Exception as e:
            logger.warning("לא ניתן לסנכרן חשבונות: %s", e)
    results = companies_db.search_accounts(q, limit=200) if q else companies_db.get_all_accounts(limit=200)
    return {"results": results}


@app.get("/api/db/branches/search")
async def search_branches_api(q: str = Query(..., min_length=1)):
    """חיפוש סניף לפי שם או ח.פ/ע.מ."""
    q = q.strip()
    if q.replace("-", "").replace(" ", "").isdigit():
        branch = companies_db.find_branch_by_tax_id(q)
        return {"results": [branch] if branch else []}
    return {"results": companies_db.find_branch_by_name(q)}


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
