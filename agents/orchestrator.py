"""
Orchestrator — מנהל את ה-pipeline: PENDING → PROCESSING → REVIEW
"""
import asyncio
import logging
from datetime import datetime

from agents.invoice_extractor import extract_invoice
from agents.data_validator import validate_invoice_data
from agents.models import Invoice, InvoiceStatus
from priority.priority_client import PriorityClient
from tools.invoice_store import InvoiceStore
from database import db as companies_db
from database import expense_recommendations_db as recs_db

logger = logging.getLogger("סוכן.תזמור")


def enrich_invoice_from_db(invoice: Invoice) -> None:
    """העשרת פרטי ספק ולקוח מ-DB לפי ח.פ/ע.מ — ממלא priority_supplier_code / priority_customer_code.

    ניתן לקריאה גם מהפענוח הראשוני (Orchestrator) וגם מהפענוח החוזר (server.py).
    """
    data = invoice.extracted_data
    if not data:
        return

    # העשרת ספק
    if data.supplier and data.supplier.tax_id:
        match = companies_db.find_by_tax_id(data.supplier.tax_id, "supplier")
        if match:
            logger.info("ספק נמצא ב-DB: %s (קוד %s)", match["name"], match["priority_code"])
            data.supplier.name = match["name"]
            data.supplier.priority_supplier_code = match["priority_code"]
            data.supplier.priority_match_found = True
            data.supplier.tax_id = match["tax_id"] or data.supplier.tax_id
            if match.get("tax_id_type"):
                data.supplier.tax_id_type = match["tax_id_type"]
            if match.get("address"):
                data.supplier.address = match["address"]
            # חשבון הוצאות — מאגר ההמלצות גובר על הצעת AI:
            #   1. אם יש המלצה במאגר לפי (ספק[, סניף]) — נשתמש בה (לרוב אותו ספק = אותו חשבון).
            #   2. כברירת מחדל אחורית, אם המאגר ריק — בודקים גם את הטבלה הישנה supplier_expense_accounts.
            #   3. אם לא נמצא דבר — מנקים את הצעת ה-AI (יש למלא ידנית).
            branch_for_rec = (data.customer.branch or "") if data.customer else ""
            top_rec = recs_db.top(match["priority_code"], branch=branch_for_rec)
            if top_rec:
                data.expense_account = top_rec["expense_account"]
                logger.info(
                    "חשבון הוצאות מהמלצות: %s (×%d, confidence=%d%%, ספק %s סניף %s)",
                    top_rec["expense_account"], top_rec["times_used"], top_rec["confidence"],
                    match["priority_code"], branch_for_rec or "—",
                )
            else:
                saved = companies_db.get_supplier_expense_account(match["priority_code"])
                if saved:
                    data.expense_account = saved
                    logger.info("חשבון הוצאות נטען מהיסטוריה הישנה: %s", saved)
                else:
                    data.expense_account = ""
                    logger.info("אין המלצה לספק %s — חשבון הוצאות יישאר ריק", match["priority_code"])
            # סוג תנועה ושיעור מע"מ לפי FNCSUP.FNCPATNAME
            fncpatname = companies_db.get_supplier_fncpatname(match["priority_code"])
            data.fncpatname = fncpatname
            if fncpatname == "2/3":
                data.vat_type = "two_thirds"
                logger.info("ספק %s — מע\"מ 2/3 (FNCPATNAME=2/3)", match["priority_code"])
            else:
                data.vat_type = "full"

    # העשרת לקוח — בחשבונית ספק הלקוח הוא תמיד אחת מ"החברות שלנו"
    # (תת-חברה / COMPANIES). מאתרים אותו בטבלת branches לפי ח.פ, ובהיעדר
    # התאמה — לפי שם. אם לא נמצא — ייתכן שזו אינה חשבונית ספק.
    if data.customer:
        branch = None
        if data.customer.tax_id:
            branch = companies_db.find_branch_by_tax_id(data.customer.tax_id)
        if not branch and data.customer.name:
            name_matches = companies_db.find_branch_by_name(data.customer.name)
            if len(name_matches) == 1:
                branch = name_matches[0]
        if branch:
            data.customer.branch = branch["branch_code"]
            data.customer.name = branch["name"]
            data.customer.priority_match_found = True
            if branch.get("tax_id"):
                data.customer.tax_id = branch["tax_id"]
            if branch.get("address"):
                data.customer.address = branch["address"]
            # קוד הלקוח ב-CUSTOMERS — לצורך קליטה בפריורטי, אם קיים
            cust = companies_db.find_by_tax_id(branch["tax_id"], "customer") \
                if branch.get("tax_id") else None
            if cust:
                data.customer.priority_customer_code = cust["priority_code"]
            logger.info("לקוח זוהה כתת-חברה: %s (סניף %s)",
                        branch["name"], branch["branch_code"])
        else:
            data.customer.priority_match_found = False
            warn = "הלקוח אינו אחת מהחברות שלנו — ייתכן שזו אינה חשבונית ספק"
            if warn not in data.extraction_warnings:
                data.extraction_warnings.append(warn)
            logger.warning("הלקוח לא זוהה כתת-חברה — ח.פ=%s שם=%s",
                           data.customer.tax_id, data.customer.name)


class Orchestrator:
    """מנהל את זרימת העיבוד של חשבוניות."""

    def __init__(self, store: InvoiceStore, priority_client: PriorityClient) -> None:
        self.store = store
        self.priority_client = priority_client

    async def process_invoice(self, invoice_id: str) -> Invoice:
        """
        מעבד חשבונית — חילוץ נתונים + וולידציה מול Priority.
        מעביר את הסטטוס: PENDING → PROCESSING → REVIEW (או ERROR).
        """
        invoice = self.store.get(invoice_id)
        if not invoice:
            raise ValueError(f"חשבונית {invoice_id} לא נמצאה")

        logger.info("מתחיל עיבוד חשבונית: %s", invoice_id)

        try:
            # שלב 1: חילוץ נתונים מהקובץ
            logger.info("שלב 1 — חילוץ נתונים מהקובץ")
            invoice.extracted_data = await extract_invoice(invoice.file_path)

            # שלב 1.5: העשרת נתונים מ-DB
            self._enrich_from_db(invoice)

            # שלב 2: וולידציה מול Priority
            logger.info("שלב 2 — וולידציה מול Priority")
            invoice.priority_validation = await validate_invoice_data(
                invoice.extracted_data,
                self.priority_client,
            )

            # פוענח בהצלחה → ממתין לקליטה
            invoice.extraction_ok = True
            invoice.error_message = ""
            invoice.status = InvoiceStatus.PENDING_SUBMISSION
            logger.info("חשבונית %s פוענחה — ממתינה לקליטה", invoice_id)

        except Exception as e:
            # פענוח נכשל → נשאר "ממתין לפענוח", סימון כשל (X)
            invoice.extraction_ok = False
            invoice.status = InvoiceStatus.PENDING_EXTRACTION
            invoice.error_message = str(e)
            logger.error("שגיאה בעיבוד חשבונית %s: %s", invoice_id, e)

        invoice.updated_at = datetime.now().isoformat()
        self.store.save(invoice)

        return invoice

    def _enrich_from_db(self, invoice: Invoice) -> None:
        """העשרת פרטי ספק ולקוח מ-DB — מאציל לפונקציה המודולית."""
        enrich_invoice_from_db(invoice)

    def start_background_processing(self, invoice_id: str) -> None:
        """מתחיל עיבוד ברקע — לשימוש מה-API."""
        asyncio.create_task(self.process_invoice(invoice_id))
