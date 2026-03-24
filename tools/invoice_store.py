"""
InvoiceStore — שמירת חשבוניות ב-JSON (אחסון מקומי)
"""
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from agents.models import Invoice, InvoiceData, InvoiceLine, InvoiceStatus, InvoiceSource, SupplierInfo, CustomerInfo
from config.settings import DATA_DIR

logger = logging.getLogger("כלים.אחסון")

STORE_FILE = DATA_DIR / "invoices_store.json"


class InvoiceStore:
    """אחסון חשבוניות בקובץ JSON."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """טוען את ה-store מהדיסק."""
        if STORE_FILE.exists():
            with open(STORE_FILE, "r", encoding="utf-8") as f:
                self._store = json.load(f)
            logger.info("נטענו %d חשבוניות מהאחסון", len(self._store))
        else:
            self._store = {}

    def _persist(self) -> None:
        """שומר את ה-store לדיסק."""
        STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(self._store, f, ensure_ascii=False, indent=2)

    def save(self, invoice: Invoice) -> None:
        """שומר/מעדכן חשבונית."""
        self._store[invoice.id] = asdict(invoice)
        self._persist()
        logger.debug("חשבונית %s נשמרה", invoice.id)

    def get(self, invoice_id: str) -> Optional[Invoice]:
        """מחזיר חשבונית לפי ID."""
        raw = self._store.get(invoice_id)
        if not raw:
            return None
        return self._dict_to_invoice(raw)

    def get_all(self, status: Optional[str] = None) -> list[Invoice]:
        """מחזיר את כל החשבוניות, עם אפשרות סינון לפי סטטוס."""
        invoices = []
        for raw in self._store.values():
            if status and raw.get("status") != status:
                continue
            invoices.append(self._dict_to_invoice(raw))
        # מיון לפי תאריך יצירה (חדשות קודם)
        invoices.sort(key=lambda x: x.created_at, reverse=True)
        return invoices

    def delete(self, invoice_id: str) -> bool:
        """מוחק חשבונית."""
        if invoice_id in self._store:
            del self._store[invoice_id]
            self._persist()
            logger.info("חשבונית %s נמחקה", invoice_id)
            return True
        return False

    @staticmethod
    def _dict_to_invoice(raw: dict) -> Invoice:
        """ממיר dict חזרה ל-Invoice."""
        extracted = raw.get("extracted_data")
        extracted_data = None
        if extracted:
            supplier_raw = extracted.get("supplier", {})
            supplier = SupplierInfo(**{k: v for k, v in supplier_raw.items() if k in SupplierInfo.__dataclass_fields__}) if supplier_raw else SupplierInfo()
            customer_raw = extracted.get("customer", {})
            customer = CustomerInfo(**{k: v for k, v in customer_raw.items() if k in CustomerInfo.__dataclass_fields__}) if customer_raw else CustomerInfo()
            lines = [InvoiceLine(**ln) for ln in extracted.get("lines", [])]
            extracted_data = InvoiceData(
                invoice_number=extracted.get("invoice_number", ""),
                invoice_date=extracted.get("invoice_date", ""),
                allocation_number=extracted.get("allocation_number", ""),
                supplier=supplier,
                customer=customer,
                lines=lines,
                subtotal=extracted.get("subtotal", 0),
                vat_amount=extracted.get("vat_amount", 0),
                total_amount=extracted.get("total_amount", 0),
                currency=extracted.get("currency", "ILS"),
                confidence_score=extracted.get("confidence_score", 0),
                extraction_warnings=extracted.get("extraction_warnings", []),
            )

        return Invoice(
            id=raw["id"],
            status=InvoiceStatus(raw.get("status", "pending")),
            source=InvoiceSource(raw.get("source", "upload")),
            file_path=raw.get("file_path", ""),
            file_type=raw.get("file_type", ""),
            extracted_data=extracted_data,
            priority_validation=raw.get("priority_validation", {}),
            priority_invoice_id=raw.get("priority_invoice_id", ""),
            user_notes=raw.get("user_notes", ""),
            error_message=raw.get("error_message", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
        )
