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

logger = logging.getLogger("סוכן.תזמור")


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

        # --- PROCESSING ---
        invoice.status = InvoiceStatus.PROCESSING
        invoice.updated_at = datetime.now().isoformat()
        self.store.save(invoice)

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

            # --- REVIEW ---
            invoice.status = InvoiceStatus.REVIEW
            logger.info("חשבונית %s מוכנה לבקרה", invoice_id)

        except Exception as e:
            invoice.status = InvoiceStatus.ERROR
            invoice.error_message = str(e)
            logger.error("שגיאה בעיבוד חשבונית %s: %s", invoice_id, e)

        invoice.updated_at = datetime.now().isoformat()
        self.store.save(invoice)

        return invoice

    def _enrich_from_db(self, invoice: Invoice) -> None:
        """העשרת פרטי ספק ולקוח מ-DB לפי ח.פ/ע.מ."""
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

        # העשרת לקוח
        if data.customer and data.customer.tax_id:
            match = companies_db.find_by_tax_id(data.customer.tax_id, "customer")
            if not match:
                match = companies_db.find_by_tax_id(data.customer.tax_id)
            if match:
                logger.info("לקוח נמצא ב-DB: %s (קוד %s)", match["name"], match["priority_code"])
                data.customer.name = match["name"]
                data.customer.priority_customer_code = match["priority_code"]
                data.customer.priority_match_found = True
                data.customer.tax_id = match["tax_id"] or data.customer.tax_id
                if match.get("tax_id_type"):
                    data.customer.tax_id_type = match["tax_id_type"]
                if match.get("address"):
                    data.customer.address = match["address"]
                # חיפוש סניף
                branch = companies_db.find_branch_by_tax_id(data.customer.tax_id)
                if branch:
                    data.customer.branch = branch["branch_code"]
                    logger.info("סניף נמצא: %s (%s)", branch["branch_code"], branch["name"])

    def start_background_processing(self, invoice_id: str) -> None:
        """מתחיל עיבוד ברקע — לשימוש מה-API."""
        asyncio.create_task(self.process_invoice(invoice_id))
