"""
InvoiceSubmitter — קליטת חשבונית מאושרת בפריורטי
"""
import logging
from datetime import datetime

from agents.models import Invoice, InvoiceData, InvoiceStatus
from priority.priority_client import PriorityClient
from tools.invoice_store import InvoiceStore

logger = logging.getLogger("פריורטי.קליטה")


def _extract_journal_fields(data: InvoiceData) -> tuple[str, list[dict]]:
    """מחלץ קוד ספק ופריטים מפקודת היומן הנערכת.
    מחזיר (supplier_code, items) כאשר items הם PINVOICEITEMS_SUBFORM.
    קוד הספק נלקח תמיד מנתוני הפענוח (priority_supplier_code), לא מפקודת היומן.

    מדיניות: השדות שנשלחים ל-Priority חייבים להגיע מהמשתמש כמו שהם —
    בלי fallback שקט. חסר תיאור/סכום → ValueError עם הודעה מפורשת."""
    jl = getattr(data, 'journal_lines', None) or []
    debit_rows = [l for l in jl if l.get('type') == 'debit']

    if not debit_rows:
        raise ValueError("אין שורות חיוב בפקודת היומן — יש להזין לפחות שורה אחת לפני הקליטה")

    supplier_code = data.supplier.priority_supplier_code

    items = []
    for i, ln in enumerate(debit_rows, 1):
        desc = (ln.get('description') or '').strip()
        if not desc:
            raise ValueError(f"שורת חיוב {i} ללא תיאור — יש להזין תיאור לפני הקליטה")
        debit_val = ln.get('debit')
        if debit_val in (None, '', 0):
            raise ValueError(f"שורת חיוב {i} ללא סכום — יש להזין סכום לפני הקליטה")
        items.append({
            "PARTNAME": "000",
            "PDES": desc[:100],
            "TQUANT": 1,
            "PRICE": float(debit_val),
        })

    return supplier_code, items


def _build_priority_payload(data: InvoiceData) -> dict:
    """בונה את ה-payload לקליטה ב-PINVOICES.
    כל שדה שעובר ל-Priority מגיע ישירות מהמשתמש (פקודת יומן או שדות הפענוח)
    בלי שינוי, transformation או fallback. חוסר נתון → ValueError בולט."""
    if not data.invoice_date:
        raise ValueError("תאריך חשבונית חסר — יש לערוך ולהזין תאריך לפני הקליטה")
    if not data.customer.branch:
        raise ValueError("סניף חסר — יש לערוך ולהזין סניף לפני הקליטה")

    supplier_code, items = _extract_journal_fields(data)

    payload = {
        "DEBIT": "D",
        "BOOKNUM": data.invoice_number,
        "IVDATE": data.invoice_date,
        "SUPNAME": supplier_code,
        "DETAILS": items[0]["PDES"],
        "BRANCHNAME": data.customer.branch,
        "PINVOICEITEMS_SUBFORM": items,
    }

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
            err_detail = result.get("error", "") or result.get("stderr", "")
            # בפריורטי, CLOSEPRINTPIV יוצר רשומה חדשה עם IVNUM סופי — ה-T-number הישן נשאר.
            # לכן תמיד מחפשים לפי BOOKNUM+SUPNAME, ללא קשר לסיבת הכישלון.
            if invoice.extracted_data and invoice.extracted_data.invoice_number:
                sup = invoice.extracted_data.supplier.priority_supplier_code or ""
                booknum = invoice.extracted_data.invoice_number
                logger.info("מחפש IVNUM סופי לפי BOOKNUM=%s SUPNAME=%s", booknum, sup)
                lookup = await priority_client._get(
                    "PINVOICES",
                    params={
                        "$filter": f"BOOKNUM eq '{booknum}' and SUPNAME eq '{sup}'",
                        "$select": "IVNUM,FNCNUM",
                        "$top": "1",
                    },
                )
                found_list = (lookup or {}).get("value", [])
                # מחפשים את הרשומה הסופית (לא T-number)
                for found in found_list:
                    found_ivnum = found.get("IVNUM", "")
                    found_fncnum = found.get("FNCNUM", "")
                    if found_ivnum and not _is_temp_ivnum(found_ivnum):
                        invoice.priority_invoice_id = found_ivnum
                        invoice.priority_journal_id = str(found_fncnum or "")
                        invoice.status = InvoiceStatus.PENDING_FILING
                        invoice.error_message = ""
                        logger.info("נמצא IVNUM סופי לפי BOOKNUM: %s, FNCNUM: %s", found_ivnum, found_fncnum)
                        return
            invoice.status        = InvoiceStatus.PENDING_SUBMISSION
            invoice.error_message = f"CLOSEPRINTPIV נכשל: {err_detail}" if err_detail else "CLOSEPRINTPIV לא הצליח — בדוק יומן שרת"
            logger.warning("CLOSEPRINTPIV לא הפיק IVNUM סופי עבור %s — %s", ivnum, err_detail)
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


async def finalize_invoice_background(
    invoice_id: str,
    priority_client: PriorityClient,
    store: InvoiceStore,
) -> None:
    """מריץ CLOSEPRINTPIV ברקע ומעדכן את הסטטוס. קורא לזה כ-BackgroundTask."""
    invoice = store.get(invoice_id)
    if not invoice or not invoice.priority_invoice_id:
        return
    if not _is_temp_ivnum(invoice.priority_invoice_id):
        return  # כבר סגור
    await _finalize_in_priority(invoice, priority_client, True)
    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)
    logger.info("finalize_background הסתיים — %s → IVNUM: %s", invoice_id[:8], invoice.priority_invoice_id)


async def submit_invoice_odata_only(
    invoice: Invoice,
    priority_client: PriorityClient,
    store: InvoiceStore,
) -> Invoice:
    """
    שלב 1 בלבד: שולח ל-OData ומקבל T-number.
    לא מריץ CLOSEPRINTPIV — הקוראים אחראים לתזמן finalize_invoice_background.
    """
    if not invoice.extracted_data:
        raise ValueError("אין נתונים מנותחים לחשבונית")
    if not invoice.extracted_data.invoice_number:
        raise ValueError("מספר חשבונית חסר — יש לערוך ולהזין מספר חשבונית לפני הקליטה")

    if not invoice.extracted_data.supplier.priority_supplier_code:
        raise ValueError("לא נמצא קוד ספק בפריורטי — לא ניתן לקלוט")

    logger.info("שלח OData בלבד — חשבונית %s ספק %s", invoice.id, invoice.extracted_data.supplier.priority_supplier_code)

    payload = _build_priority_payload(invoice.extracted_data)

    try:
        result = await priority_client.submit_invoice(payload)
        invoice.priority_invoice_id = result.get("IVNUM", "")
        invoice.error_message = ""
        logger.info("OData קלט חשבונית — IVNUM: %s", invoice.priority_invoice_id)
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
                invoice.error_message = ""
                logger.info("חשבונית כבר קיימת — IVNUM: %s", ivnum)
            else:
                invoice.status = InvoiceStatus.PENDING_SUBMISSION
                invoice.error_message = f"שגיאה בקליטה: {detail}"
        else:
            invoice.status = InvoiceStatus.PENDING_SUBMISSION
            invoice.error_message = f"שגיאה בקליטה בפריורטי: {detail}"
            logger.error("שגיאה בקליטה: %s", detail)

    invoice.updated_at = datetime.now().isoformat()
    store.save(invoice)
    return invoice


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
