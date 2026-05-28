"""
InvoiceSubmitter — קליטת חשבונית מאושרת בפריורטי
"""
import base64
import logging
from datetime import datetime
from pathlib import Path

from agents.models import Invoice, InvoiceData, InvoiceStatus
from priority.priority_client import PriorityClient
from tools.invoice_store import InvoiceStore

logger = logging.getLogger("פריורטי.קליטה")


def _build_priority_payload(data: InvoiceData, file_path: str = "") -> dict:
    """בונה את ה-payload לקליטה ב-PINVOICES לפי מבנה OData של Priority."""
    if data.lines:
        pdes = "; ".join(ln.description for ln in data.lines if ln.description)[:100]
    else:
        pdes = data.supplier.name or "חשבונית ספק"

    item = {
        "PARTNAME": "000",
        "PDES": pdes,
        "TQUANT": 1,
        "PRICE": data.subtotal,
    }
    payload = {
        "DEBIT": "D",
        "BOOKNUM": data.invoice_number,
        "IVDATE": data.invoice_date,
        "SUPNAME": data.supplier.priority_supplier_code,
        "PINVOICEITEMS_SUBFORM": [item],
    }

    if data.customer.branch:
        payload["BRANCHNAME"] = data.customer.branch

    if data.allocation_number:
        payload["SDINUMIT"] = data.allocation_number

    if file_path:
        p = Path(file_path)
        if p.exists():
            suffix = p.suffix.lstrip(".").lower()
            b64 = base64.b64encode(p.read_bytes()).decode("ascii")
            payload["EXTFILES_SUBFORM"] = [{
                "EXTFILEDES": p.name,
                "SUFFIX": suffix,
                "EXTFILENAME": b64,
            }]
            logger.info("מצרף קובץ %s (%d bytes) לפריורטי", p.name, p.stat().st_size)
        else:
            logger.warning("קובץ חשבונית לא נמצא: %s", file_path)

    return payload


async def submit_approved_invoice(
    invoice: Invoice,
    priority_client: PriorityClient,
    store: InvoiceStore,
) -> Invoice:
    """
    שולח חשבונית מאושרת לפריורטי.
    מעדכן את הסטטוס ל-PENDING_FILING או משאיר ב-PENDING_SUBMISSION בשגיאה.
    """
    if not invoice.extracted_data:
        raise ValueError("אין נתונים מנותחים לחשבונית")

    if not invoice.extracted_data.supplier.priority_supplier_code:
        raise ValueError("לא נמצא קוד ספק בפריורטי — לא ניתן לקלוט")

    if not invoice.extracted_data.invoice_number:
        raise ValueError("מספר חשבונית חסר — יש לערוך ולהזין מספר חשבונית לפני הקליטה")

    logger.info(
        "שולח חשבונית %s לפריורטי — ספק: %s",
        invoice.id,
        invoice.extracted_data.supplier.priority_supplier_code,
    )

    payload = _build_priority_payload(invoice.extracted_data, invoice.file_path)

    try:
        result = await priority_client.submit_invoice(payload)
        invoice.priority_invoice_id = result.get("IVNUM", "")
        invoice.status = InvoiceStatus.PENDING_FILING
        invoice.error_message = ""
        logger.info("חשבונית נקלטה בפריורטי בהצלחה — IVNUM: %s", invoice.priority_invoice_id)
    except Exception as e:
        import httpx as _httpx
        detail = str(e)
        if isinstance(e, _httpx.HTTPStatusError):
            detail = e.response.text
        invoice.status = InvoiceStatus.PENDING_SUBMISSION
        invoice.error_message = f"שגיאה בקליטה בפריורטי: {detail}"
        logger.error("שגיאה בקליטה: %s", detail)

    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    return invoice
