"""נתיבי API ודף עבור מערכת ספרי הנהלת חשבונות.

נרשם על אפליקציית ה-FastAPI הראשית דרך register_ledger_routes(app, templates_dir).
"""
import logging
import shutil
import uuid
from datetime import date
from pathlib import Path

from fastapi import HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse, HTMLResponse

from config.settings import DATA_DIR
from database import ledger_db
from database import db as companies_db
from agents.date_detector import detect_document_date

logger = logging.getLogger("אתר.ספרים")

LEDGER_DIR = DATA_DIR / "ledger"
LEDGER_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".gif"}
_MIME = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".tiff": "image/tiff", ".tif": "image/tiff",
    ".webp": "image/webp", ".gif": "image/gif",
}


def register_ledger_routes(app, templates_dir: Path) -> None:
    """רושם את כל נתיבי מערכת הספרים על האפליקציה."""
    ledger_db.init_db()

    # ---------- דף ----------
    @app.get("/ledger", response_class=HTMLResponse, include_in_schema=False)
    async def ledger_page():
        return HTMLResponse((templates_dir / "ledger.html").read_text(encoding="utf-8"))

    # ---------- חברות ----------
    @app.get("/api/ledger/companies")
    async def list_companies():
        return {"companies": ledger_db.list_companies()}

    @app.post("/api/ledger/companies")
    async def create_company(body: dict):
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "חסר שם חברה")
        cid = ledger_db.create_company(name, body.get("tax_id", ""))
        return {"id": cid, "name": name}

    @app.post("/api/ledger/import-companies")
    async def import_companies_from_priority():
        """ייבוא כל תתי-החברות מ-Priority כחברות ב-ledger."""
        branches = companies_db.get_all_branches()
        imported, skipped = 0, 0
        for b in branches:
            name = (b.get("name") or "").strip()
            if not name:
                continue
            existing = ledger_db.list_companies()
            if any(c["name"] == name for c in existing):
                skipped += 1
                continue
            ledger_db.create_company(name, b.get("tax_id") or "")
            imported += 1
        return {"imported": imported, "skipped": skipped, "total": len(branches)}

    # ---------- ספרים ----------
    @app.get("/api/ledger/companies/{company_id}/books")
    async def list_books(company_id: int):
        if not ledger_db.get_company(company_id):
            raise HTTPException(404, "חברה לא נמצאה")
        return {"books": ledger_db.list_books(company_id)}

    @app.post("/api/ledger/companies/{company_id}/books")
    async def create_book(company_id: int, body: dict):
        if not ledger_db.get_company(company_id):
            raise HTTPException(404, "חברה לא נמצאה")
        try:
            year = int(body.get("year"))
        except (TypeError, ValueError):
            raise HTTPException(400, "שנה לא תקינה")
        if year < 2000 or year > 2100:
            raise HTTPException(400, "שנה מחוץ לטווח")
        try:
            bid = ledger_db.create_book(company_id, year)
        except Exception:  # noqa: BLE001 — UNIQUE constraint
            raise HTTPException(400, f"כבר קיים ספר לשנת {year}")
        return {"id": bid, "year": year}

    @app.get("/api/ledger/books/{book_id}")
    async def get_book(book_id: int):
        book = ledger_db.get_book(book_id)
        if not book:
            raise HTTPException(404, "ספר לא נמצא")
        book["dividers"] = ledger_db.list_dividers(book_id)
        book["categories"] = ledger_db.list_categories(book_id)
        return book

    # ---------- חוצצים ----------
    @app.post("/api/ledger/books/{book_id}/dividers")
    async def add_divider(book_id: int, body: dict):
        if not ledger_db.get_book(book_id):
            raise HTTPException(404, "ספר לא נמצא")
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "חסר שם חוצץ")
        did = ledger_db.create_divider(book_id, name)
        return {"id": did, "name": name}

    @app.delete("/api/ledger/dividers/{divider_id}")
    async def remove_divider(divider_id: int):
        ledger_db.delete_divider(divider_id)
        return {"ok": True}

    # ---------- מסמכים ----------
    @app.get("/api/ledger/books/{book_id}/documents")
    async def list_documents(book_id: int, q: str = "", date_from: str = "",
                             date_to: str = "", divider_id: int | None = None,
                             category: str = ""):
        if not ledger_db.get_book(book_id):
            raise HTTPException(404, "ספר לא נמצא")
        docs = ledger_db.list_documents(book_id, q=q, date_from=date_from,
                                        date_to=date_to, divider_id=divider_id,
                                        category=category)
        return {"documents": docs}

    @app.post("/api/ledger/books/{book_id}/documents")
    async def upload_document(book_id: int, file: UploadFile = File(...),
                              divider_id: int | None = Query(None)):
        """מעלה מסמך לספר — שומר את הקובץ ומזהה אוטומטית את תאריך המסמך."""
        if not ledger_db.get_book(book_id):
            raise HTTPException(404, "ספר לא נמצא")
        ext = Path(file.filename or "").suffix.lower()
        if ext not in _ALLOWED_EXT:
            raise HTTPException(400, f"סוג קובץ לא נתמך: {ext}")

        book_dir = LEDGER_DIR / str(book_id)
        book_dir.mkdir(parents=True, exist_ok=True)
        save_path = book_dir / f"{uuid.uuid4()}{ext}"
        with open(save_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        scan_date = date.today().isoformat()
        # ניסיון לזהות את התאריך שעל המסמך
        detected = detect_document_date(str(save_path))
        if detected:
            document_date, date_source = detected, "document"
        else:
            document_date, date_source = scan_date, "scan"

        doc_id = ledger_db.create_document(
            book_id=book_id, file_path=str(save_path),
            original_filename=file.filename or "", file_type=ext.lstrip("."),
            document_date=document_date, scan_date=scan_date,
            date_source=date_source, title=Path(file.filename or "").stem,
            divider_id=divider_id,
        )
        logger.info("מסמך %d הועלה לספר %d — תאריך %s (%s)",
                    doc_id, book_id, document_date, date_source)
        return ledger_db.get_document(doc_id)

    @app.get("/api/ledger/documents/{doc_id}/file")
    async def get_document_file(doc_id: int):
        doc = ledger_db.get_document(doc_id)
        if not doc:
            raise HTTPException(404, "מסמך לא נמצא")
        path = Path(doc["file_path"])
        if not path.exists():
            raise HTTPException(404, "הקובץ חסר")
        ext = path.suffix.lower()
        return FileResponse(str(path), media_type=_MIME.get(ext, "application/octet-stream"),
                            filename=doc["original_filename"] or path.name,
                            content_disposition_type="inline")

    @app.post("/api/ledger/documents/{doc_id}")
    async def update_document(doc_id: int, body: dict):
        if not ledger_db.get_document(doc_id):
            raise HTTPException(404, "מסמך לא נמצא")
        fields = {}
        for key in ("title", "document_date", "category1", "category2"):
            if key in body:
                fields[key] = body[key]
        if "divider_id" in body:
            fields["divider_id"] = body["divider_id"] or None
        if "document_date" in fields:
            # שינוי ידני של התאריך → מקור 'document'
            fields["date_source"] = "document"
        ledger_db.update_document(doc_id, **fields)
        return ledger_db.get_document(doc_id)

    @app.delete("/api/ledger/documents/{doc_id}")
    async def delete_document(doc_id: int):
        doc = ledger_db.delete_document(doc_id)
        if not doc:
            raise HTTPException(404, "מסמך לא נמצא")
        try:
            Path(doc["file_path"]).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("מחיקת קובץ נכשלה: %s", exc)
        return {"ok": True}

    @app.post("/api/ledger/documents/{doc_id}/restore-invoice")
    async def restore_invoice_from_ledger(doc_id: int):
        """החזרת חשבונית מתויקת לרשימת חשבוניות ספק."""
        from agents.models import InvoiceStatus
        from datetime import datetime
        doc = ledger_db.get_document(doc_id)
        if not doc:
            raise HTTPException(404, "מסמך לא נמצא")
        invoice_id = doc.get("invoice_id", "")
        if not invoice_id:
            raise HTTPException(400, "מסמך זה לא קשור לחשבונית")

        from website.server import store as invoice_store
        invoice = invoice_store.get(invoice_id)
        if not invoice:
            raise HTTPException(404, "החשבונית המקורית לא נמצאה")

        invoice.status = InvoiceStatus.PENDING_FILING
        invoice.updated_at = datetime.now().isoformat()
        invoice_store.save(invoice)
        logger.info("חשבונית %s הוחזרה לרשימה מספרי הנהלת חשבונות", invoice_id)
        return {"ok": True, "invoice_id": invoice_id}

    logger.info("נתיבי ספרי הנהלת חשבונות נרשמו")
