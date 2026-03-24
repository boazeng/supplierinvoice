"""
InvoiceSubmitter — קליטת חשבונית מאושרת בפריורטי
"""
import logging
from datetime import datetime

from agents.models import Invoice, InvoiceData, InvoiceStatus
from priority.priority_client import PriorityClient
from tools.invoice_store import InvoiceStore

logger = logging.getLogger("פריורטי.קליטה")


def _build_priority_payload(data: InvoiceData) -> dict:
    """בונה את ה-payload לקליטה ב-AINVOICES לפי מבנה OData של Priority."""
    # שורות חשבונית
    details = []
    for line in data.lines:
        detail = {
            "PARTNAME": line.priority_part_name or line.catalog_number,
            "TQUANT": line.quantity,
            "TPRICE": line.unit_price,
            "VATPRICE": line.vat_amount,
        }
        details.append(detail)

    payload = {
        "IVNUM": data.invoice_number,
        "IVDATE": data.invoice_date,
        "SUPNAME": data.supplier.priority_supplier_code,
        "DETAILS_SUBFORM": details,
    }

    # הוספת הזמנת רכש אם קיימת
    if data.purchase_order:
        payload["ORDNAME"] = data.purchase_order

    return payload


async def submit_approved_invoice(
    invoice: Invoice,
    priority_client: PriorityClient,
    store: InvoiceStore,
) -> Invoice:
    """
    שולח חשבונית מאושרת לפריורטי.
    מעדכן את הסטטוס ל-SUBMITTED או ERROR.
    """
    if not invoice.extracted_data:
        raise ValueError("אין נתונים מנותחים לחשבונית")

    if not invoice.extracted_data.supplier.priority_supplier_code:
        raise ValueError("לא נמצא קוד ספק בפריורטי — לא ניתן לקלוט")

    logger.info(
        "שולח חשבונית %s לפריורטי — ספק: %s",
        invoice.id,
        invoice.extracted_data.supplier.priority_supplier_code,
    )

    payload = _build_priority_payload(invoice.extracted_data)

    try:
        result = await priority_client.submit_invoice(payload)
        invoice.priority_invoice_id = result.get("IVNUM", "")
        invoice.status = InvoiceStatus.SUBMITTED
        logger.info(
            "חשבונית נקלטה בפריורטי בהצלחה — IVNUM: %s",
            invoice.priority_invoice_id,
        )
    except Exception as e:
        invoice.status = InvoiceStatus.ERROR
        invoice.error_message = f"שגיאה בקליטה בפריורטי: {e}"
        logger.error("שגיאה בקליטה: %s", e)

    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    return invoice
