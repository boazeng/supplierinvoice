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

    return payload


def _is_temp_ivnum(ivnum: str) -> bool:
    """מחזיר True אם ה-IVNUM הוא זמני (מתחיל ב-T)."""
    return ivnum.upper().startswith("T")


async def _finalize_in_priority(
    invoice: Invoice,
    priority_client: PriorityClient,
    is_new: bool,
) -> None:
    """
    אחרי קליטה ראשונית בפריורטי:
    - לחשבוניות חדשות עם T-number: מריץ CLOSEPRINTPIV + צירוף קובץ (WCF SDK)
      → מעדכן IVNUM סופי + FNCNUM, מעביר לסטטוס PENDING_FILING
      → אם נכשל — נשאר PENDING_SUBMISSION עם הודעת שגיאה
    - לחשבוניות כפולות שנמצאו (IVNUM סופי כבר): קורא FNCNUM ומעביר ל-PENDING_FILING
    """
    ivnum = invoice.priority_invoice_id
    if not ivnum:
        return

    if _is_temp_ivnum(ivnum):
        # מספר זמני (T) — צריך CLOSEPRINTPIV בין אם חדש ובין אם כפול
        result = await priority_client.finalize_invoice(
            ivnum, invoice.file_path if invoice.file_path else ""
        )
        final_ivnum = result.get("ivnum", "")
        fncnum      = result.get("fncnum", "")

        if final_ivnum and not _is_temp_ivnum(final_ivnum):
            invoice.priority_invoice_id  = final_ivnum
            invoice.priority_journal_id  = fncnum
            invoice.status               = InvoiceStatus.PENDING_FILING
            invoice.error_message        = ""
            logger.info("CLOSEPRINTPIV הצליח — IVNUM: %s, FNCNUM: %s", final_ivnum, fncnum)
        else:
            invoice.status        = InvoiceStatus.PENDING_SUBMISSION
            invoice.error_message = "CLOSEPRINTPIV לא הצליח — החשבונית ממתינה לאישור ידני בפריורטי"
            logger.warning("CLOSEPRINTPIV לא הפיק IVNUM סופי עבור %s", ivnum)
    else:
        # חשבונית כבר קיימת עם IVNUM סופי — קרא FNCNUM וסמן כמוכן לתיוק
        result = await priority_client._get(
            "PINVOICES",
            params={
                "$filter": f"IVNUM eq '{ivnum}'",
                "$select": "IVNUM,FNCNUM",
                "$top": "1",
            },
        )
        fncnum = (result or {}).get("value", [{}])[0].get("FNCNUM", "") or ""
        if fncnum:
            invoice.priority_journal_id = str(fncnum)
        invoice.status = InvoiceStatus.PENDING_FILING


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

    payload = _build_priority_payload(invoice.extracted_data)

    is_new = False
    try:
        result = await priority_client.submit_invoice(payload)
        invoice.priority_invoice_id = result.get("IVNUM", "")
        invoice.error_message = ""
        is_new = True
        logger.info("חשבונית נקלטה בפריורטי — IVNUM: %s", invoice.priority_invoice_id)
    except Exception as e:
        import httpx as _httpx
        import json as _json
        detail = str(e)
        if isinstance(e, _httpx.HTTPStatusError):
            raw = e.response.text
            try:
                parsed = _json.loads(raw)
                detail = (
                    parsed.get("FORM", {}).get("InterfaceErrors", {}).get("text")
                    or parsed.get("error", {}).get("message")
                    or raw
                )
            except Exception:
                detail = raw

        # אם פריורטי דוחה בגלל מספר חשבונית כפול — החשבונית כבר קיימת שם
        is_duplicate = "כבר קיימת" in detail or "already exists" in detail.lower()
        if is_duplicate:
            existing = await priority_client._get(
                "PINVOICES",
                params={
                    "$filter": f"BOOKNUM eq '{invoice.extracted_data.invoice_number}' and SUPNAME eq '{invoice.extracted_data.supplier.priority_supplier_code}'",
                    "$select": "IVNUM,BOOKNUM,SUPNAME",
                    "$top": "1",
                },
            )
            ivnum = (existing or {}).get("value", [{}])[0].get("IVNUM", "") if existing else ""
            if ivnum:
                invoice.priority_invoice_id = ivnum
                invoice.status = InvoiceStatus.PENDING_FILING
                invoice.error_message = ""
                logger.info("חשבונית כבר קיימת בפריורטי — IVNUM: %s", ivnum)
            else:
                invoice.status = InvoiceStatus.PENDING_SUBMISSION
                invoice.error_message = f"שגיאה בקליטה בפריורטי: {detail}"
                logger.error("שגיאה בקליטה: %s", detail)
        else:
            invoice.status = InvoiceStatus.PENDING_SUBMISSION
            invoice.error_message = f"שגיאה בקליטה בפריורטי: {detail}"
            logger.error("שגיאה בקליטה: %s", detail)

    # הפעל finalize אם יש IVNUM — בין אם חדש (T-number) ובין אם כפול (קיים)
    if invoice.priority_invoice_id:
        await _finalize_in_priority(invoice, priority_client, is_new)

    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)

    return invoice
