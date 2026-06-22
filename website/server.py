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

from fastapi import FastAPI, File, UploadFile, HTTPException, Query, Depends, Request, BackgroundTasks, Body
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from shared_auth import install_auth, require_role
from website.ledger_routes import register_ledger_routes

from agents.models import Invoice, InvoiceSource, InvoiceStatus
from agents.orchestrator import Orchestrator
from priority.priority_client import PriorityClient
from priority.invoice_submitter import submit_approved_invoice, finalize_invoice_background, submit_invoice_odata_only, _is_temp_ivnum
from priority.sync_agent import sync_from_priority, get_sync_status
from tools.invoice_store import InvoiceStore
from config.settings import INVOICES_DIR, BASE_DIR, DATA_DIR
from database import db as companies_db
from database import ledger_db
from database import expense_recommendations_db as recs_db
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


@app.get("/recommendations", response_class=HTMLResponse, include_in_schema=False)
async def recommendations_page():
    """מסך מאגר המלצות לחשבון הוצאות."""
    return HTMLResponse((TEMPLATES_DIR / "recommendations.html").read_text(encoding="utf-8"))


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

    # יצירת רשומת חשבונית — פענוח אוטומטי מיד עם ההעלאה
    invoice = Invoice(
        id=file_id,
        source=InvoiceSource.UPLOAD,
        file_path=str(save_path),
        file_type="pdf" if ext == ".pdf" else "image",
        status=InvoiceStatus.PENDING_EXTRACTION,
    )
    store.save(invoice)
    orchestrator.start_background_processing(file_id)

    return {
        "id": invoice.id,
        "status": invoice.status.value,
        "message": "החשבונית הועלתה — פענוח מתחיל אוטומטית",
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


@app.post("/api/invoices/{invoice_id}/clear-extraction")
async def clear_extraction(invoice_id: str):
    """מחיקת נתוני הפענוח — החשבונית חוזרת לסטטוס 'ממתין לפענוח'."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    invoice.extracted_data = None
    invoice.extraction_ok = None
    invoice.priority_validation = {}
    invoice.error_message = ""
    invoice.status = InvoiceStatus.PENDING_EXTRACTION
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    logger.info("נתוני הפענוח של חשבונית %s נמחקו", invoice_id)
    return {"id": invoice.id, "status": invoice.status.value, "message": "נתוני הפענוח נמחקו"}


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
                # מאגר המלצות — רושם שימוש (supplier + account [+ branch]) לטובת המלצה עתידית
                branch = (invoice.extracted_data.customer.branch or "") if invoice.extracted_data.customer else ""
                recs_db.record(sup_code, str(value), branch=branch)
                logger.info("נשמר חשבון הוצאות %s לספק %s (סניף %s) במאגר ההמלצות", value, sup_code, branch or "—")

        return {"ok": True, "path": path, "value": value}
    else:
        raise HTTPException(status_code=400, detail=f"שדה לא קיים: {field}")


@app.post("/api/invoices/{invoice_id}/journal-lines")
async def update_journal_lines(invoice_id: str, body: dict = {}):
    """שמירת שורות פקודת יומן שנערכו ידנית. שורות חיוב נרשמות גם במאגר ההמלצות."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    if not invoice.extracted_data:
        raise HTTPException(status_code=400, detail="אין נתונים מחולצים")
    lines = body.get("lines", [])
    invoice.extracted_data.journal_lines = lines
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    # רישום במאגר ההמלצות — כל שורת חיוב (debit) שיש לה חשבון נכנסת כשימוש לזוג (ספק, חשבון, סניף)
    sup_code = invoice.extracted_data.supplier.priority_supplier_code or ""
    branch = (invoice.extracted_data.customer.branch or "") if invoice.extracted_data.customer else ""
    if sup_code:
        for ln in lines:
            if ln.get("type") == "debit":
                acc = (ln.get("account") or "").strip()
                if acc:
                    recs_db.record(sup_code, acc, account_desc=(ln.get("description") or "").strip(), branch=branch)

    return {"ok": True, "count": len(lines)}


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
    branch_code = (d.customer.branch if d else "") or ""
    customer_name = (d.customer.name if d else "") or ""

    # שם הסניף מ-Priority לפי קוד הסניף — שם זה ישמש לחיפוש חוצץ
    branch_record = companies_db.get_branch_by_code(branch_code) if branch_code else None
    branch_name = branch_record["name"] if branch_record else customer_name

    # שם החברה לחיפוש — שם הלקוח שחולץ
    company_name = re.sub(r'[\\/:*?"<>|]', '_', customer_name or branch_name or "כללי")
    supplier_name = re.sub(r'[\\/:*?"<>|]', '_', (d.supplier.name if d else "") or "ספק")
    invoice_num = re.sub(r'[\\/:*?"<>|]', '_', (d.invoice_number if d else "") or invoice_id[:8])
    invoice_date = (d.invoice_date if d else "") or date_cls.today().isoformat()
    try:
        year = int(invoice_date[:4])
    except (ValueError, TypeError):
        year = date_cls.today().year

    # חיפוש חברה קיימת בלבד — לא יוצרים חברה חדשה אוטומטית
    company_id = ledger_db.find_best_matching_company(company_name)
    if not company_id:
        raise HTTPException(
            status_code=400,
            detail=f"לא נמצאה חברה מתאימה לשם: '{company_name}'. פתחי חברה חדשה בספרי הנהלת חשבונות ונסי שוב."
        )
    book_id = ledger_db.find_or_create_book(company_id, year)
    # חיפוש חוצץ לפי שם הסניף מ-Priority (מדויק יותר מהספק)
    divider_id = ledger_db.find_best_matching_divider(book_id, branch_name)

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
    # שם החוצץ לתצוגה ב-toast
    divider_name = ""
    if divider_id:
        dividers = ledger_db.list_dividers(book_id)
        div_map = {d["id"]: d["name"] for d in dividers}
        divider_name = div_map.get(divider_id, "")
    logger.info("חשבונית %s תויקה בספרי הנהלת חשבונות — %s/%d/%s",
                invoice_id, company_name, year, divider_name or "ללא חוצץ")
    return {"ok": True, "branch": branch_name, "company_name": company_name,
            "year": year, "divider_name": divider_name}


@app.post("/api/admin/ledger/fix-last-filed")
async def fix_last_filed_document(request: Request):
    """תיקון חוצץ של המסמך האחרון שתויק — מחפש חוצץ קיים מתאים ומעביר אליו."""
    if not request.session.get("user_email"):
        raise HTTPException(status_code=401, detail="נדרשת התחברות")

    conn = ledger_db._conn()
    doc_row = conn.execute("""
        SELECT d.*, div.name AS divider_name
        FROM ledger_documents d
        LEFT JOIN ledger_dividers div ON div.id = d.divider_id
        WHERE d.invoice_id != '' AND d.invoice_id IS NOT NULL
        ORDER BY d.id DESC LIMIT 1
    """).fetchone()
    conn.close()

    if not doc_row:
        return {"ok": False, "error": "לא נמצא מסמך שתויק"}

    doc = dict(doc_row)
    book_id = doc["book_id"]
    current_divider_id = doc.get("divider_id")
    current_divider_name = doc.get("divider_name") or "ללא חוצץ"

    # ניסיון לקחת שם ספק מחשבונית המקורית אם עדיין קיימת
    invoice = store.get(doc["invoice_id"]) if doc.get("invoice_id") else None
    if invoice and invoice.extracted_data and invoice.extracted_data.supplier:
        supplier_name = invoice.extracted_data.supplier.name or ""
    else:
        # fallback: הספק הוא החלק הראשון של הכותרת
        supplier_name = (doc.get("title") or "").split(" ")[0].replace("_", " ")

    # כל החוצצים בספר (ממוין — ייתכן שהחוצץ הנוכחי הוא הלא-נכון)
    all_dividers = ledger_db.list_dividers(book_id)

    # מחפש חוצץ מתאים מבין כולם (כולל הנוכחי)
    best_id = ledger_db.find_best_matching_divider(book_id, supplier_name)

    if not best_id or best_id == current_divider_id:
        return {
            "ok": True, "moved": False,
            "doc_title": doc.get("title"), "supplier_name": supplier_name,
            "current_divider": current_divider_name,
            "available_dividers": [d["name"] for d in all_dividers],
            "message": "לא נמצא חוצץ מתאים יותר — העבר ידנית",
        }

    ledger_db.update_document(doc["id"], divider_id=best_id)

    deleted_old = False
    if current_divider_id:
        remaining = ledger_db.list_documents(book_id, divider_id=current_divider_id)
        if not remaining:
            ledger_db.delete_divider(current_divider_id)
            deleted_old = True

    target = next((d for d in all_dividers if d["id"] == best_id), {})
    return {
        "ok": True, "moved": True,
        "doc_id": doc["id"], "doc_title": doc.get("title"),
        "supplier_name": supplier_name,
        "from_divider": current_divider_name,
        "to_divider": target.get("name", str(best_id)),
        "old_divider_deleted": deleted_old,
    }


@app.get("/api/admin/ledger/diagnose")
async def diagnose_last_filed(request: Request):
    """מידע על המסמך האחרון שתויק וכל החברות הקיימות."""
    if not request.session.get("user_email"):
        raise HTTPException(status_code=401, detail="נדרשת התחברות")
    conn = ledger_db._conn()
    doc_row = conn.execute("""
        SELECT d.id, d.title, d.invoice_id, d.divider_id,
               b.id AS book_id, b.year, b.company_id,
               c.id AS cid, c.name AS company_name,
               div.name AS divider_name
        FROM ledger_documents d
        JOIN ledger_books b ON b.id = d.book_id
        JOIN ledger_companies c ON c.id = b.company_id
        LEFT JOIN ledger_dividers div ON div.id = d.divider_id
        WHERE d.invoice_id != '' AND d.invoice_id IS NOT NULL
        ORDER BY d.id DESC LIMIT 1
    """).fetchone()
    all_companies = conn.execute(
        "SELECT c.id, c.name, COUNT(d.id) AS doc_count "
        "FROM ledger_companies c "
        "LEFT JOIN ledger_books b ON b.company_id = c.id "
        "LEFT JOIN ledger_documents d ON d.book_id = b.id "
        "GROUP BY c.id ORDER BY c.id"
    ).fetchall()
    conn.close()
    return {
        "last_filed_doc": dict(doc_row) if doc_row else None,
        "all_companies": [dict(r) for r in all_companies],
    }


@app.post("/api/admin/ledger/move-to-company")
async def move_doc_to_company(request: Request):
    """מעביר את המסמך האחרון שתויק לחברה הנכונה ומוחק את החברה שנוצרה אוטומטית."""
    if not request.session.get("user_email"):
        raise HTTPException(status_code=401, detail="נדרשת התחברות")

    body = await request.json()
    target_company_id = int(body.get("target_company_id", 0))
    if not target_company_id:
        raise HTTPException(status_code=400, detail="חסר target_company_id")

    conn = ledger_db._conn()
    doc_row = conn.execute("""
        SELECT d.id, d.title, d.invoice_id, d.divider_id,
               b.id AS book_id, b.year, b.company_id,
               c.name AS company_name
        FROM ledger_documents d
        JOIN ledger_books b ON b.id = d.book_id
        JOIN ledger_companies c ON c.id = b.company_id
        WHERE d.invoice_id != '' AND d.invoice_id IS NOT NULL
        ORDER BY d.id DESC LIMIT 1
    """).fetchone()
    conn.close()

    if not doc_row:
        return {"ok": False, "error": "לא נמצא מסמך שתויק"}

    doc = dict(doc_row)
    old_company_id = doc["company_id"]
    old_company_name = doc["company_name"]
    year = doc["year"]

    # ספר בחברה הנכונה לאותה שנה
    target_book_id = ledger_db.find_or_create_book(target_company_id, year)

    # חוצץ מתאים בחברה הנכונה
    invoice = store.get(doc["invoice_id"]) if doc.get("invoice_id") else None
    if invoice and invoice.extracted_data and invoice.extracted_data.supplier:
        supplier_name = invoice.extracted_data.supplier.name or ""
    else:
        supplier_name = (doc.get("title") or "").split(" ")[0].replace("_", " ")
    target_divider_id = ledger_db.find_best_matching_divider(target_book_id, supplier_name)

    # העברת המסמך
    conn = ledger_db._conn()
    conn.execute("UPDATE ledger_documents SET book_id = ?, divider_id = ? WHERE id = ?",
                 (target_book_id, target_divider_id, doc["id"]))
    conn.commit()
    conn.close()

    # מחיקת ספר + חברה ישנים אם ריקים
    old_books = ledger_db.list_books(old_company_id)
    company_deleted = False
    conn = ledger_db._conn()
    for book in old_books:
        remaining = conn.execute("SELECT COUNT(*) FROM ledger_documents WHERE book_id = ?",
                                 (book["id"],)).fetchone()[0]
        if remaining == 0:
            conn.execute("DELETE FROM ledger_dividers WHERE book_id = ?", (book["id"],))
            conn.execute("DELETE FROM ledger_books WHERE id = ?", (book["id"],))
    conn.commit()
    remaining_books = conn.execute("SELECT COUNT(*) FROM ledger_books WHERE company_id = ?",
                                    (old_company_id,)).fetchone()[0]
    if remaining_books == 0:
        conn.execute("DELETE FROM ledger_companies WHERE id = ?", (old_company_id,))
        company_deleted = True
    conn.commit()
    conn.close()

    divider_name = ""
    if target_divider_id:
        div_map = {d["id"]: d["name"] for d in ledger_db.list_dividers(target_book_id)}
        divider_name = div_map.get(target_divider_id, "")

    return {
        "ok": True,
        "doc_title": doc.get("title"),
        "from_company": old_company_name,
        "to_company_id": target_company_id,
        "year": year,
        "divider": divider_name or "ללא חוצץ",
        "old_company_deleted": company_deleted,
    }



@app.get("/api/debug/ledger-state-xK9m")
async def ledger_state():
    """מידע על החברות, ספרים, חוצצים והמסמך האחרון שתויק."""
    conn = ledger_db._conn()
    companies = conn.execute(
        "SELECT c.id, c.name, COUNT(d.id) AS doc_count "
        "FROM ledger_companies c "
        "LEFT JOIN ledger_books b ON b.company_id = c.id "
        "LEFT JOIN ledger_documents d ON d.book_id = b.id "
        "GROUP BY c.id ORDER BY c.id"
    ).fetchall()
    dividers = conn.execute(
        "SELECT div.id, div.name AS div_name, b.year, c.name AS company "
        "FROM ledger_dividers div "
        "JOIN ledger_books b ON b.id = div.book_id "
        "JOIN ledger_companies c ON c.id = b.company_id"
    ).fetchall()
    last_doc = conn.execute("""
        SELECT d.id, d.title, d.invoice_id, b.year, c.id AS cid, c.name AS company,
               div.name AS divider
        FROM ledger_documents d
        JOIN ledger_books b ON b.id = d.book_id
        JOIN ledger_companies c ON c.id = b.company_id
        LEFT JOIN ledger_dividers div ON div.id = d.divider_id
        ORDER BY d.id DESC LIMIT 3
    """).fetchall()
    conn.close()
    return {
        "companies": [dict(r) for r in companies],
        "dividers": [dict(r) for r in dividers],
        "last_docs": [dict(r) for r in last_doc],
    }


@app.get("/api/debug/move-doc-xK9m")
async def move_doc_debug(doc_id: int, target_company_id: int):
    """חד-פעמי: העברת מסמך לחברה הנכונה + מחיקת החברה הריקה שנוצרה אוטומטית."""
    conn = ledger_db._conn()
    doc = conn.execute(
        "SELECT d.id, d.book_id, b.company_id, b.year "
        "FROM ledger_documents d JOIN ledger_books b ON b.id = d.book_id WHERE d.id = ?",
        (doc_id,)
    ).fetchone()
    if not doc:
        conn.close()
        return {"ok": False, "error": f"מסמך {doc_id} לא נמצא"}
    doc = dict(doc)
    old_company_id = doc["company_id"]
    year = doc["year"]

    # ספר ביעד
    target_book_id = ledger_db.find_or_create_book(target_company_id, year)

    # חוצץ מתאים
    invoice = store.get(dict(conn.execute(
        "SELECT invoice_id FROM ledger_documents WHERE id=?", (doc_id,)).fetchone() or {}).get("invoice_id", ""))
    supplier_name = ""
    if invoice and invoice.extracted_data and invoice.extracted_data.supplier:
        supplier_name = invoice.extracted_data.supplier.name or ""
    target_divider_id = ledger_db.find_best_matching_divider(target_book_id, supplier_name)

    conn.execute("UPDATE ledger_documents SET book_id=?, divider_id=? WHERE id=?",
                 (target_book_id, target_divider_id, doc_id))

    # מחיקת ספר + חברה ישנים אם ריקים
    old_book_id = doc["book_id"]
    remaining = conn.execute("SELECT COUNT(*) FROM ledger_documents WHERE book_id=?",
                              (old_book_id,)).fetchone()[0]
    company_deleted = False
    if remaining == 0:
        conn.execute("DELETE FROM ledger_dividers WHERE book_id=?", (old_book_id,))
        conn.execute("DELETE FROM ledger_books WHERE id=?", (old_book_id,))
        books_left = conn.execute("SELECT COUNT(*) FROM ledger_books WHERE company_id=?",
                                   (old_company_id,)).fetchone()[0]
        if books_left == 0:
            conn.execute("DELETE FROM ledger_companies WHERE id=?", (old_company_id,))
            company_deleted = True
    conn.commit()

    target_company = conn.execute("SELECT name FROM ledger_companies WHERE id=?",
                                   (target_company_id,)).fetchone()
    div_name = ""
    if target_divider_id:
        div = conn.execute("SELECT name FROM ledger_dividers WHERE id=?",
                            (target_divider_id,)).fetchone()
        div_name = dict(div)["name"] if div else ""
    conn.close()
    return {"ok": True, "doc_id": doc_id,
            "to_company": dict(target_company)["name"] if target_company else target_company_id,
            "divider": div_name or "ללא חוצץ",
            "old_company_deleted": company_deleted}


@app.get("/api/debug/fix-divider-doc2-xK9m2026-done")
async def fix_divider_doc2():
    """חד-פעמי: שיוך מסמך 2 לחוצץ 'חניה מקבוצה אורבנית בע\"מ' — חיפוש גלובלי."""
    conn = ledger_db._conn()
    # חיפוש החוצץ בכל הספרים
    divider = conn.execute(
        "SELECT d.id AS div_id, d.name AS div_name, d.book_id, "
        "b.year, b.company_id, c.name AS company_name "
        "FROM ledger_dividers d "
        "JOIN ledger_books b ON b.id = d.book_id "
        "JOIN ledger_companies c ON c.id = b.company_id "
        "WHERE d.name LIKE ? OR d.name LIKE ?",
        ("%חניה%אורבנית%", "%hanaya%")
    ).fetchone()
    if not divider:
        # החוצץ לא קיים — יוצרים אותו בספר הנכון לפי מסמך 2
        doc_row = conn.execute("SELECT book_id FROM ledger_documents WHERE id = 2").fetchone()
        if not doc_row:
            conn.close()
            return {"ok": False, "error": "מסמך 2 לא נמצא"}
        target_book_id = dict(doc_row)["book_id"]
        new_div_name = 'חניה מקבוצה אורבנית בע"מ'
        cur = conn.execute("INSERT INTO ledger_dividers (book_id, name) VALUES (?, ?)",
                           (target_book_id, new_div_name))
        conn.commit()
        divider = conn.execute(
            "SELECT d.id AS div_id, d.name AS div_name, d.book_id, "
            "b.year, b.company_id, c.name AS company_name "
            "FROM ledger_dividers d JOIN ledger_books b ON b.id=d.book_id "
            "JOIN ledger_companies c ON c.id=b.company_id WHERE d.id=?",
            (cur.lastrowid,)
        ).fetchone()

    div = dict(divider)
    doc = conn.execute("SELECT id, book_id FROM ledger_documents WHERE id = 2").fetchone()
    if not doc:
        conn.close()
        return {"ok": False, "error": "מסמך 2 לא נמצא"}

    # אם המסמך לא בספר הנכון — מעביר גם את הספר
    if dict(doc)["book_id"] != div["book_id"]:
        conn.execute("UPDATE ledger_documents SET book_id = ?, divider_id = ? WHERE id = 2",
                     (div["book_id"], div["div_id"]))
    else:
        conn.execute("UPDATE ledger_documents SET divider_id = ? WHERE id = 2", (div["div_id"],))

    # מנקה ספר ריק שנוצר בטעות
    old_book_id = dict(doc)["book_id"]
    if old_book_id != div["book_id"]:
        remaining = conn.execute("SELECT COUNT(*) FROM ledger_documents WHERE book_id = ?",
                                 (old_book_id,)).fetchone()[0]
        if remaining == 0:
            conn.execute("DELETE FROM ledger_dividers WHERE book_id = ?", (old_book_id,))
            conn.execute("DELETE FROM ledger_books WHERE id = ?", (old_book_id,))

    conn.commit()
    conn.close()
    return {"ok": True, "doc_id": 2, "divider": div["div_name"],
            "company": div["company_name"], "year": div["year"]}


@app.post("/api/invoices/{invoice_id}/approve")
async def approve_invoice(invoice_id: str, background_tasks: BackgroundTasks, body: dict = {}):
    """
    אישור קליטה בפריורטי.
    שלב 1 (OData POST — שניות): מיידי, מחזיר תשובה ל-client.
    שלב 2 (CLOSEPRINTPIV — דקות): רץ ברקע אחרי ה-response.
    """
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")

    if invoice.status != InvoiceStatus.PENDING_SUBMISSION:
        raise HTTPException(status_code=400, detail=f"לא ניתן לקלוט חשבונית בסטטוס {invoice.status.value}")

    invoice.user_notes = body.get("notes", "")

    # שלב 1: OData POST בלבד — מהיר, לא חוסם
    try:
        invoice = await submit_invoice_odata_only(invoice, priority_client, store)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not invoice.priority_invoice_id:
        # כישלון בשלב 1 — אין T-number
        return {
            "id": invoice.id,
            "status": invoice.status.value,
            "priority_invoice_id": "",
            "message": invoice.error_message or "שגיאה בקליטה",
        }

    if not _is_temp_ivnum(invoice.priority_invoice_id):
        # IVNUM סופי כבר (חשבונית כפולה שכבר הוסבה) — אין צורך בשלב 2
        invoice.status = InvoiceStatus.PENDING_FILING
        invoice.updated_at = datetime.now().isoformat()
        store.save(invoice)
        return {
            "id": invoice.id,
            "status": invoice.status.value,
            "priority_invoice_id": invoice.priority_invoice_id,
            "message": "החשבונית נקלטה בפריורטי בהצלחה!",
        }

    # שלב 2: CLOSEPRINTPIV ברקע — לאחר ה-response
    background_tasks.add_task(
        finalize_invoice_background, invoice.id, priority_client, store
    )
    logger.info("CLOSEPRINTPIV תוזמן ברקע — %s IVNUM: %s", invoice.id[:8], invoice.priority_invoice_id)

    return {
        "id": invoice.id,
        "status": "pending_filing",
        "priority_invoice_id": invoice.priority_invoice_id,
        "message": "החשבונית קלוטה — מסגרת ברקע (עד מספר דקות)",
    }


@app.post("/api/invoices/{invoice_id}/submit-draft")
async def submit_draft_to_priority(invoice_id: str, body: dict = Body(default={})):
    """שלב 1: שולח ל-OData ומקבל T-number — ללא CLOSEPRINTPIV."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    if invoice.status != InvoiceStatus.PENDING_SUBMISSION:
        raise HTTPException(status_code=400, detail=f"לא ניתן לקלוט טיוטה בסטטוס {invoice.status.value}")

    invoice.user_notes = body.get("notes", "")
    try:
        invoice = await submit_invoice_odata_only(invoice, priority_client, store)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not invoice.priority_invoice_id:
        return {"id": invoice.id, "status": invoice.status.value, "priority_invoice_id": "", "message": invoice.error_message or "שגיאה בקליטת טיוטה"}

    invoice.status = InvoiceStatus.DRAFT_SUBMITTED
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)
    logger.info("טיוטה נקלטה — %s IVNUM: %s", invoice.id[:8], invoice.priority_invoice_id)

    return {"id": invoice.id, "status": invoice.status.value, "priority_invoice_id": invoice.priority_invoice_id, "message": f"טיוטה נקלטה בפריורטי — {invoice.priority_invoice_id}"}


@app.post("/api/invoices/{invoice_id}/finalize")
async def finalize_draft(invoice_id: str, background_tasks: BackgroundTasks):
    """שלב 2: מריץ CLOSEPRINTPIV על T-number קיים."""
    invoice = store.get(invoice_id)
    if not invoice:
        raise HTTPException(status_code=404, detail="חשבונית לא נמצאה")
    if invoice.status != InvoiceStatus.DRAFT_SUBMITTED:
        raise HTTPException(status_code=400, detail=f"לא ניתן לסגור חשבונית בסטטוס {invoice.status.value}")
    if not invoice.priority_invoice_id or not _is_temp_ivnum(invoice.priority_invoice_id):
        raise HTTPException(status_code=400, detail="לא נמצא T-number תקין — לא ניתן לסגור")

    background_tasks.add_task(finalize_invoice_background, invoice.id, priority_client, store)
    logger.info("קליטה סופית תוזמנה ברקע — %s IVNUM: %s", invoice.id[:8], invoice.priority_invoice_id)

    return {"id": invoice.id, "status": "pending_filing", "priority_invoice_id": invoice.priority_invoice_id, "message": "קליטה סופית בתהליך — תסגר תוך מספר דקות"}


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
async def search_accounts_api(
    q: str = Query(default=""),
    branch: str = Query(default=""),
    account_type: str = Query(default=""),
):
    """חיפוש חשבון GL לפי קוד או שם. q ריק = כל החשבונות (עד 200).
    branch — מסנן לפי סיומת סניף (למשל '110'). account_type — 'expense' או 'supplier'.
    אם ה-DB ריק — מסנכרן מפריורטי תחילה."""
    q = q.strip()
    branch = branch.strip()
    account_type = account_type.strip()
    if companies_db.get_accounts_count() == 0:
        try:
            from database.sync import sync_accounts
            client = PriorityClient()
            await sync_accounts(client)
            await client.close()
        except Exception as e:
            logger.warning("לא ניתן לסנכרן חשבונות: %s", e)
    if q:
        results = companies_db.search_accounts(q, limit=500, branch=branch, account_type=account_type)
        if not results and branch:
            results = companies_db.search_accounts(q, limit=500, branch="", account_type=account_type)
    else:
        results = companies_db.get_all_accounts(limit=500, branch=branch, account_type=account_type)
        if not results and branch:
            results = companies_db.get_all_accounts(limit=500, branch="", account_type=account_type)
    return {"results": results}


@app.get("/api/db/suppliers/journal-accounts")
async def supplier_journal_accounts(branch: str = Query(default=""), q: str = Query(default="")):
    """חשבונות ספקים בפורמט {קוד}-{סניף} לפקודת יומן."""
    suppliers = companies_db.get_all(company_type='supplier')
    q_lower = q.strip().lower()
    results = []
    for s in suppliers:
        code = s.get('priority_code', '') or ''
        name = s.get('name', '') or ''
        acc_code = f"{code}-{branch}" if branch else code
        if not q_lower or q_lower in acc_code.lower() or q_lower in name.lower():
            results.append({"account_code": acc_code, "account_name": name})
    return {"results": results[:500]}


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


# === מאגר המלצות לחשבון הוצאות ===

def _enrich_recommendation(rec: dict) -> dict:
    """מוסיף לרשומת המלצה: supplier_name (מ-companies) + account_name (מ-accounts)
    בעדיפות על account_desc אם הוא ריק. לא דורס שדות קיימים."""
    sup_code = rec.get("supplier_code") or ""
    if sup_code:
        sup = companies_db.find_by_priority_code(sup_code, "supplier")
        rec["supplier_name"] = sup["name"] if sup else ""
    else:
        rec["supplier_name"] = ""

    acc_code = rec.get("expense_account") or ""
    if acc_code:
        acc = companies_db.find_account_by_code(acc_code)
        rec["account_name"] = acc["account_name"] if acc else ""
        # אם account_desc ריק — נופלים לשם החשבון מטבלת accounts
        if not (rec.get("account_desc") or "").strip() and acc:
            rec["account_desc"] = acc["account_name"]
    else:
        rec["account_name"] = ""
    return rec


@app.get("/api/recommendations/expense-account/match")
async def recommend_expense_account(
    supplier_code: str = Query(..., min_length=1),
    branch: str = Query(default=""),
    limit: int = Query(default=5, ge=1, le=20),
):
    """מחזיר עד `limit` המלצות לחשבון הוצאות עבור הספק (מסונן אופציונלית לסניף)."""
    results = [_enrich_recommendation(r) for r in recs_db.match(supplier_code, branch=branch, limit=limit)]
    return {"supplier_code": supplier_code, "branch": branch, "results": results}


@app.get("/api/recommendations")
async def recommendations_list(
    q: str = Query(default=""),
    limit: int = Query(default=500, ge=1, le=2000),
):
    """רשימת כל ההמלצות (לניהול). q מסנן לפי קוד ספק / חשבון / תיאור."""
    results = [_enrich_recommendation(r) for r in recs_db.list_all(q=q, limit=limit)]
    return {"results": results, "count": recs_db.count()}


@app.post("/api/recommendations")
async def recommendations_add(body: dict = Body(default={})):
    """הוספה ידנית של המלצה (או הגדלת times_used לצמד קיים)."""
    sup = (body.get("supplier_code") or "").strip()
    acc = (body.get("expense_account") or "").strip()
    if not sup or not acc:
        raise HTTPException(status_code=400, detail="supplier_code ו-expense_account חובה")
    desc = (body.get("account_desc") or "").strip()
    branch = (body.get("branch") or "").strip()
    recs_db.record(sup, acc, account_desc=desc, branch=branch)
    return {"ok": True}


@app.post("/api/recommendations/{rec_id}/update")
async def recommendations_update(rec_id: str, body: dict = Body(default={})):
    ok = recs_db.update(
        rec_id,
        expense_account=(body.get("expense_account") or "").strip(),
        account_desc=(body.get("account_desc") or "").strip(),
        branch=(body.get("branch") or "").strip(),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="המלצה לא נמצאה")
    return {"ok": True}


@app.post("/api/recommendations/{rec_id}/delete")
async def recommendations_delete(rec_id: str):
    if not recs_db.delete(rec_id):
        raise HTTPException(status_code=404, detail="המלצה לא נמצאה")
    return {"ok": True}
