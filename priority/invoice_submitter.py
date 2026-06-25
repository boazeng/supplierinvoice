"""
InvoiceSubmitter — קליטת חשבונית מאושרת בפריורטי
"""
import logging
from datetime import datetime

from agents.models import Invoice, InvoiceData, InvoiceStatus
from priority.priority_client import PriorityClient
from tools.invoice_store import InvoiceStore

logger = logging.getLogger("פריורטי.קליטה")


def _debit_type(data: InvoiceData) -> str:
    """מחזיר 'C' לחשבונית זיכוי, 'D' לחשבונית רגילה."""
    if getattr(data, 'is_credit', False):
        return "C"
    total = float(getattr(data, 'total_amount', 0) or 0)
    if total < 0:
        return "C"
    inv_num = (getattr(data, 'invoice_number', '') or '').strip()
    if 'זיכוי' in inv_num:
        return "C"
    return "D"


async def _attach_invoice_file(
    priority_client,
    ivnum: str,
    file_path: str,
    debit: str = "D",
) -> None:
    """מצרף את קובץ ה-PDF של החשבונית ל-EXTFILES של PINVOICES ב-Priority.
    אם הקובץ (לפי EXTFILEDES = שם הקובץ) כבר מצורף — מדלגים, כדי למנוע כפילויות
    כשמאמצים IVNUM קיים בעקבות דופליקציה."""
    from pathlib import Path
    pinvoice_key = f"IVNUM='{ivnum}',IVTYPE='P',DEBIT='{debit}'"
    file_name = Path(file_path).name
    try:
        existing = await priority_client._get(
            f"PINVOICES({pinvoice_key})",
            params={"$expand": "EXTFILES_SUBFORM($select=EXTFILEDES)"},
        )
        already = {row.get("EXTFILEDES") for row in (existing or {}).get("EXTFILES_SUBFORM", [])}
        if file_name in already:
            logger.info("הקובץ %s כבר מצורף ל-%s — מדלגים על תיוק חוזר", file_name, ivnum)
            return
    except Exception as e:
        # לא מצליח לבדוק → נמשיך ונצרף בכל זאת
        logger.debug("בדיקת EXTFILES קיים נכשלה (ממשיכים לצירוף): %s", e)
    ok = await priority_client.attach_extfile("PINVOICES", pinvoice_key, file_path)
    if not ok:
        logger.warning("תיוק קובץ ב-EXTFILES נכשל — IVNUM: %s", ivnum)


def _extract_journal_fields(data: InvoiceData) -> tuple[str, list[dict]]:
    """מחלץ קוד ספק ופריטים מפקודת היומן הנערכת לטובת PINVOICEITEMS_SUBFORM.

    אנחנו תמיד שולחים רק את שורות החיוב (לא את שורת המע"מ) ובלי VATFLAG —
    Priority מחשב את ה-18% מע"מ אוטומטית מתוך ה-PRICE של כל פריט.

    במצב 2/3 (רכב): שורת חיוב 1 בממשק מציגה את ה-1/3 הלא-מנוכה (subtotal +
    vatNd). את ה-vatNd צריך לקזז לפני שליחה ל-Priority — אחרת Priority יחשב
    18% על סכום מנופח ויקבל סה"כ שגוי. אז אנחנו מורידים אותו מהשורה הראשונה
    כך ש-PRICE של כל הפריטים יסתכם ב-subtotal הנטו. Priority יחזיר 18% מע"מ
    מלא — שמתאים לתיק החשבונאי (הספק חייב את העסק בכלל המע"מ). אם הספק
    מוגדר ב-Priority עם FNCPATNAME=2/3, יומן ה-FNCTRANS יפצל אוטומטית 2/3
    למע"מ ו-1/3 להוצאה."""
    jl = getattr(data, 'journal_lines', None) or []
    vat_type = getattr(data, 'vat_type', 'full') or 'full'

    # רק שורות חיוב (לא vat — Priority יחשב אוטומטית)
    debit_rows = [l for l in jl if l.get('type') == 'debit']
    if not debit_rows:
        raise ValueError("אין שורות חיוב בפקודת היומן — יש להזין לפחות שורה אחת לפני הקליטה")

    supplier_code = data.supplier.priority_supplier_code

    # ב-2/3 — לחשב כמה לקזז משורת חיוב 1 (=ה-vatNd שהוסף ע"י הרצת ה-rebalance בממשק)
    first_debit_offset = 0.0
    if vat_type == 'two_thirds':
        vat_full = float(getattr(data, 'vat_amount', 0) or 0)
        vat_ded  = round(vat_full * 2 / 3, 2)
        first_debit_offset = round(vat_full - vat_ded, 2)  # החלק הלא-מנוכה

    items = []
    for i, ln in enumerate(debit_rows, 1):
        desc = (ln.get('description') or '').strip()
        if not desc:
            raise ValueError(f"שורת חיוב {i} ללא תיאור — יש להזין תיאור לפני הקליטה")
        debit_val = ln.get('debit')
        if debit_val in (None, '', 0):
            raise ValueError(f"שורת חיוב {i} ללא סכום — יש להזין סכום לפני הקליטה")
        account = (ln.get('account') or '').strip()
        if not account:
            raise ValueError(f"שורת חיוב {i} ללא חשבון הוצאות — יש להזין חשבון לפני הקליטה")
        price = float(debit_val)
        if i == 1 and first_debit_offset > 0:
            # מקזזים את ה-1/3 הלא-מנוכה מהשורה הראשונה כדי שה-PRICE שיישלח
            # יהיה נטו אמיתי. סך כל ה-PRICEs יהיה subtotal; Priority יחשב VAT
            # מלא ויקבל סה"כ נכון.
            price = max(0.0, round(price - first_debit_offset, 2))
        item = {
            "PARTNAME": "000",
            "PDES": desc[:100],
            "TQUANT": 1,
            "PRICE": price,
            "ACCNAME": account,
        }
        if vat_type == 'exempt':
            item["VATFLAGA"] = "N"
        items.append(item)

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
        "DEBIT": _debit_type(data),
        "BOOKNUM": data.invoice_number,
        "IVDATE": data.invoice_date,
        "SUPNAME": supplier_code,
        "DETAILS": items[0]["PDES"],
        "BRANCHNAME": data.customer.branch,
        "PINVOICEITEMS_SUBFORM": items,
    }

    if data.allocation_number:
        payload["SDINUMIT"] = data.allocation_number

    # הערה: FNCPATNAME (תבנית כספית — קובע 2/3 או חסמ) לא קיים כשדה על
    # PINVOICES בהתקנה הזו (Priority ידחה עם 'property does not exist').
    # Priority מסיק את הדפוס מהגדרת הספק (FNCSUP.FNCPATNAME). אם בעתיד
    # נצטרך לדרוס לכל חשבונית — נחפש שדה ייחודי או נשנה את הגדרת הספק לפני
    # הקליטה ולא נשלח אותו ב-payload של PINVOICES.

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

    debit = _debit_type(invoice.extracted_data) if invoice.extracted_data else "D"

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
            if invoice.file_path:
                await _attach_invoice_file(priority_client, final_ivnum, invoice.file_path, debit)
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
                        if invoice.file_path:
                            await _attach_invoice_file(priority_client, found_ivnum, invoice.file_path, debit)
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
        if invoice.file_path:
            await _attach_invoice_file(priority_client, ivnum, invoice.file_path, debit)


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
        # מצרפים את ה-PDF ל-EXTFILES של PINVOICES כבר בשלב הטיוטה
        # (השדות זמינים על T-number — לא צריך להמתין ל-CLOSEPRINTPIV)
        if invoice.priority_invoice_id and invoice.file_path:
            await _attach_invoice_file(priority_client, invoice.priority_invoice_id, invoice.file_path,
                                       _debit_type(invoice.extracted_data))
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

        # זיהוי דופליקציה: Priority מחזיר 'קיימת כבר' / 'כבר קיימת' / 'already exists' —
        # מספיק לחפש 'קיימת' או 'exists' כדי לתפוס את כל הוריאציות.
        is_duplicate = "קיימת" in detail or "exists" in detail.lower()
        if is_duplicate:
            existing = await priority_client._get(
                "PINVOICES",
                params={
                    "$filter": f"BOOKNUM eq '{invoice.extracted_data.invoice_number}' and SUPNAME eq '{invoice.extracted_data.supplier.priority_supplier_code}'",
                    "$select": "IVNUM,BOOKNUM,SUPNAME,STATDES,STORNOFLAG",
                    "$top": "10",
                },
            )
            rows = (existing or {}).get("value", []) if existing else []
            # מסננים החוצה רשומות מבוטלות (STORNOFLAG='Y' או STATDES='מבוטלת')
            active = [r for r in rows
                      if r.get("STORNOFLAG") != "Y"
                      and (r.get("STATDES") or "") not in ("מבוטלת", "מבוטל")]
            chosen = active[0] if active else None
            if chosen:
                ivnum = chosen.get("IVNUM", "")
                invoice.priority_invoice_id = ivnum
                invoice.error_message = ""
                logger.info(
                    "אומץ IVNUM קיים לאחר דופליקציה — %s (סטטוס: %s, סה\"כ רשומות: %d, מבוטלות: %d)",
                    ivnum, chosen.get("STATDES"), len(rows), len(rows) - len(active),
                )
                # מצרפים את הקובץ לרשומה הקיימת
                if invoice.file_path:
                    await _attach_invoice_file(priority_client, ivnum, invoice.file_path,
                                               _debit_type(invoice.extracted_data))
            elif rows:
                # כל הרשומות מבוטלות — Priority עדיין חוסם את המספר הזה.
                # יש לבטל את הביטול ב-Priority, או להשתמש במספר חשבונית אחר.
                cancelled_ivnums = ", ".join(r.get("IVNUM", "") for r in rows[:5])
                invoice.status = InvoiceStatus.PENDING_SUBMISSION
                invoice.error_message = (
                    f"מספר החשבונית {invoice.extracted_data.invoice_number} "
                    f"של ספק {invoice.extracted_data.supplier.priority_supplier_code} "
                    f"קיים ב-Priority אך כל הרשומות מבוטלות ({cancelled_ivnums}). "
                    f"יש לבטל את הביטול ב-Priority, או להשתמש במספר חשבונית שונה."
                )
                logger.warning("דופליקציה — כל הרשומות מבוטלות: %s", cancelled_ivnums)
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
