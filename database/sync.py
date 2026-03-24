"""
סנכרון ספקים ולקוחות מפריורטי ל-SQLite.
"""
import logging
from typing import Optional

from priority.priority_client import PriorityClient
from database import db

logger = logging.getLogger("בסיס.נתונים.סנכרון")

# שדות ספקים בפריורטי
SUP_FIELDS = "SUPNAME,SUPDES,VATNUM,COMPNUM,ADDRESS,PHONE,EMAIL,STATDES"
# שדות לקוחות בפריורטי
CUST_FIELDS = "CUSTNAME,CUSTDES,WTAXNUM,ADDRESS,PHONE,EMAIL,STATDES"

BATCH_SIZE = 500


def _determine_tax_id_type(vatnum: Optional[str], compnum: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """קובע סוג מספר עוסק — ע.מ או ח.פ."""
    tax_id = vatnum or compnum
    if not tax_id:
        return None, None
    # אם VATNUM ו-COMPNUM זהים — סביר שזה ח.פ
    # אם שונים — VATNUM הוא ע.מ, COMPNUM הוא ח.פ
    if vatnum and compnum and vatnum != compnum:
        return vatnum, "ע.מ"
    return tax_id, "ח.פ"


async def sync_suppliers(client: PriorityClient) -> int:
    """מסנכרן את כל הספקים מפריורטי ל-DB."""
    logger.info("מתחיל סנכרון ספקים...")
    total = 0
    skip = 0

    while True:
        result = await client._get("SUPPLIERS", params={
            "$select": SUP_FIELDS,
            "$top": str(BATCH_SIZE),
            "$skip": str(skip),
        })
        if not result or not result.get("value"):
            break

        records = []
        for sup in result["value"]:
            tax_id, tax_type = _determine_tax_id_type(sup.get("VATNUM"), sup.get("COMPNUM"))
            records.append({
                "priority_code": sup["SUPNAME"],
                "name": sup.get("SUPDES", ""),
                "type": "supplier",
                "tax_id": tax_id,
                "tax_id_type": tax_type,
                "address": sup.get("ADDRESS"),
                "phone": sup.get("PHONE"),
                "email": sup.get("EMAIL"),
                "status": "active" if sup.get("STATDES") == "פעיל" else "inactive",
            })

        count = db.bulk_upsert(records)
        total += count
        logger.info("סונכרנו %d ספקים (batch %d)", count, skip // BATCH_SIZE + 1)

        if len(result["value"]) < BATCH_SIZE:
            break
        skip += BATCH_SIZE

    logger.info("סנכרון ספקים הושלם — סה\"כ %d", total)
    return total


async def sync_customers(client: PriorityClient) -> int:
    """מסנכרן את כל הלקוחות מפריורטי ל-DB."""
    logger.info("מתחיל סנכרון לקוחות...")
    total = 0
    skip = 0

    while True:
        result = await client._get("CUSTOMERS", params={
            "$select": CUST_FIELDS,
            "$top": str(BATCH_SIZE),
            "$skip": str(skip),
        })
        if not result or not result.get("value"):
            break

        records = []
        for cust in result["value"]:
            tax_id = cust.get("WTAXNUM")
            records.append({
                "priority_code": cust["CUSTNAME"],
                "name": cust.get("CUSTDES", ""),
                "type": "customer",
                "tax_id": tax_id,
                "tax_id_type": "ח.פ" if tax_id else None,
                "address": cust.get("ADDRESS"),
                "phone": cust.get("PHONE"),
                "email": cust.get("EMAIL"),
                "status": "active" if cust.get("STATDES") == "פעיל" else "inactive",
            })

        count = db.bulk_upsert(records)
        total += count
        logger.info("סונכרנו %d לקוחות (batch %d)", count, skip // BATCH_SIZE + 1)

        if len(result["value"]) < BATCH_SIZE:
            break
        skip += BATCH_SIZE

    logger.info("סנכרון לקוחות הושלם — סה\"כ %d", total)
    return total


async def sync_branches(client: PriorityClient) -> int:
    """מסנכרן תתי חברות מפריורטי ל-DB."""
    logger.info("מתחיל סנכרון תתי חברות (COMPANIES)...")
    result = await client._get("COMPANIES", params={
        "$select": "COMPANYNAME,COMPANYDES,WTAXNUM,ADDRESS,PHONE,EMAIL",
    })
    if not result or not result.get("value"):
        logger.warning("COMPANIES לא זמין או ריק — דילוג")
        return 0

    records = []
    for comp in result["value"]:
        records.append({
            "branch_code": comp.get("COMPANYNAME", ""),
            "name": comp.get("COMPANYDES", ""),
            "tax_id": comp.get("WTAXNUM"),
            "address": comp.get("ADDRESS"),
            "phone": comp.get("PHONE"),
            "email": comp.get("EMAIL"),
        })

    count = db.bulk_upsert_branches(records)
    logger.info("סנכרון תתי חברות הושלם — סה\"כ %d", count)
    return count


async def sync_all() -> dict:
    """סנכרון מלא — ספקים, לקוחות, תתי חברות."""
    client = PriorityClient()
    try:
        suppliers = await sync_suppliers(client)
        customers = await sync_customers(client)
        branches = await sync_branches(client)
        db.update_sync_status(suppliers, customers, branches)
        stats = db.get_stats()
        logger.info("סנכרון מלא הושלם: %s", stats)
        return {"suppliers_synced": suppliers, "customers_synced": customers,
                "branches_synced": branches, **stats}
    finally:
        await client.close()
