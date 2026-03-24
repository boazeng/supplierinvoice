"""
FolderWatcher — מעקב אחר תיקייה לקבלת חשבוניות חדשות (אופציונלי)
"""
import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent

from config.settings import INVOICES_DIR

logger = logging.getLogger("כלים.תיקייה")

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif"}


class _InvoiceFileHandler(FileSystemEventHandler):
    """מטפל באירועי יצירת קבצים חדשים."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self.callback = callback

    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        ext = Path(event.src_path).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            logger.info("קובץ חדש זוהה: %s", event.src_path)
            self.callback(event.src_path)


class FolderWatcher:
    """מעקב אחר תיקייה — מזהה חשבוניות חדשות."""

    def __init__(self, watch_dir: Optional[Path] = None) -> None:
        self.watch_dir = watch_dir or INVOICES_DIR
        self._observer: Optional[Observer] = None
        self._callback: Optional[Callable] = None

    def start(self, on_new_file: Callable[[str], None]) -> None:
        """מתחיל מעקב אחר התיקייה."""
        self._callback = on_new_file
        self._observer = Observer()
        handler = _InvoiceFileHandler(on_new_file)
        self._observer.schedule(handler, str(self.watch_dir), recursive=False)
        self._observer.start()
        logger.info("מעקב אחר תיקייה פעיל: %s", self.watch_dir)

    def stop(self) -> None:
        """עוצר את המעקב."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            logger.info("מעקב אחר תיקייה הופסק")
