"""
SupplierInvoice — נקודת כניסה ראשית
מפעיל את שרת FastAPI עם uvicorn
"""
import uvicorn

from config.logging_config import setup_logging
from config.settings import HOST, PORT
from tools.invoice_store import InvoiceStore
from priority.priority_client import PriorityClient
from agents.orchestrator import Orchestrator
from website.server import app, init_dependencies
from database import db


def main() -> None:
    """הפעלת המערכת."""
    # הגדרת לוגים
    setup_logging()

    import logging
    logger = logging.getLogger("ראשי")
    logger.info("מערכת קליטת חשבוניות ספק — מתחיל...")

    # אתחול בסיס נתונים
    db.init_db()
    logger.info("בסיס נתונים אותחל")

    # אתחול רכיבים
    store = InvoiceStore()
    priority_client = PriorityClient()
    orchestrator = Orchestrator(store=store, priority_client=priority_client)

    # חיבור ל-FastAPI
    init_dependencies(store, priority_client, orchestrator)

    logger.info("השרת עולה על %s:%s", HOST, PORT)

    # הפעלת uvicorn
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
