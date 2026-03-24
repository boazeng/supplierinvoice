"""
DataValidatorAgent — סנכרון וולידציה של נתוני חשבונית מול Priority ERP
"""
import logging

from agents.models import InvoiceData
from priority.priority_client import PriorityClient

logger = logging.getLogger("סוכן.וולידציה")


async def validate_invoice_data(
    invoice_data: InvoiceData,
    priority_client: PriorityClient,
) -> dict:
    """
    בודק את נתוני החשבונית מול Priority:
    - מחפש ספק לפי ח.פ (דרך DB, לא API ישיר)
    - מחפש פריטים לפי מק"ט

    מחזיר dict עם תוצאות הוולידציה.
    """
    logger.info("מתחיל וולידציה מול Priority — ספק: %s", invoice_data.supplier.name)

    validation = {
        "supplier_found": invoice_data.supplier.priority_match_found,
        "supplier_code": invoice_data.supplier.priority_supplier_code,
        "lines_validation": [],
        "warnings": [],
    }

    if not invoice_data.supplier.tax_id:
        validation["warnings"].append("לא נמצא ח.פ/ע.מ בחשבונית")

    if not invoice_data.supplier.priority_match_found and invoice_data.supplier.tax_id:
        validation["warnings"].append(
            f"ספק עם ח.פ {invoice_data.supplier.tax_id} לא נמצא בפריורטי"
        )

    # --- חיפוש פריטים ---
    for line in invoice_data.lines:
        line_validation = {
            "line_number": line.line_number,
            "description": line.description,
            "catalog_number": line.catalog_number,
            "part_found": False,
            "priority_part_name": "",
        }

        if line.catalog_number:
            try:
                part = await priority_client.find_part(line.catalog_number)
                if part:
                    line_validation["part_found"] = True
                    line_validation["priority_part_name"] = part.get("PARTNAME", "")
                    line.priority_part_name = part.get("PARTNAME", "")
                    line.priority_match_found = True
            except Exception:
                pass

        validation["lines_validation"].append(line_validation)

    logger.info(
        "וולידציה הושלמה — ספק: %s, אזהרות: %d",
        "נמצא" if validation["supplier_found"] else "לא נמצא",
        len(validation["warnings"]),
    )

    return validation
