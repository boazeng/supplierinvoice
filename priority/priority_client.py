"""
PriorityClient — HTTP client לגישה ל-OData API של Priority ERP
"""
import logging
from typing import Optional

import httpx

from config.settings import PRIORITY_URL, PRIORITY_USER, PRIORITY_PASS

logger = logging.getLogger("פריורטי.לקוח")


class PriorityClient:
    """ממשק HTTP ל-Priority OData API."""

    def __init__(self) -> None:
        self.base_url = PRIORITY_URL.rstrip("/")
        self.auth = (PRIORITY_USER, PRIORITY_PASS) if PRIORITY_USER else None
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """מחזיר httpx client — יוצר אם לא קיים."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                auth=self.auth,
                verify=False,  # Priority לרוב עם self-signed certs
                timeout=30.0,
                headers={"Content-Type": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        """סוגר את ה-HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, entity: str, params: Optional[dict] = None) -> Optional[dict]:
        """שליחת GET request ל-Priority OData."""
        client = await self._get_client()
        url = f"{self.base_url}/{entity}"
        logger.debug("GET %s | params=%s", url, params)

        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("שגיאת HTTP מ-Priority: %s — %s", e.response.status_code, e.response.text)
            return None
        except httpx.RequestError as e:
            logger.error("שגיאת תקשורת עם Priority: %s", e)
            return None

    async def _post(self, entity: str, data: dict) -> Optional[dict]:
        """שליחת POST request ל-Priority OData."""
        client = await self._get_client()
        url = f"{self.base_url}/{entity}"
        logger.debug("POST %s | data=%s", url, data)

        try:
            response = await client.post(url, json=data)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("שגיאת HTTP מ-Priority: %s — %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("שגיאת תקשורת עם Priority: %s", e)
            raise

    # --- ספקים ---

    async def find_supplier_by_tax_id(self, tax_id: str) -> Optional[dict]:
        """חיפוש ספק לפי ח.פ / ע.מ (VATNUM)."""
        logger.info("מחפש ספק לפי ח.פ: %s", tax_id)
        result = await self._get(
            "SUPPLIERS",
            params={"$filter": f"VATNUM eq '{tax_id}'", "$select": "SUPNAME,SUPDES,VATNUM"},
        )
        if result and result.get("value"):
            return result["value"][0]
        return None

    async def get_all_suppliers(self) -> list[dict]:
        """מחזיר רשימת כל הספקים."""
        logger.info("מביא רשימת ספקים מפריורטי")
        result = await self._get(
            "SUPPLIERS",
            params={"$select": "SUPNAME,SUPDES,VATNUM"},
        )
        return result.get("value", []) if result else []

    # --- פריטים ---

    async def find_part(self, part_name: str) -> Optional[dict]:
        """חיפוש פריט לפי PARTNAME."""
        logger.info("מחפש פריט: %s", part_name)
        result = await self._get(
            "PART",
            params={"$filter": f"PARTNAME eq '{part_name}'", "$select": "PARTNAME,PARTDES"},
        )
        if result and result.get("value"):
            return result["value"][0]
        return None

    async def get_all_parts(self) -> list[dict]:
        """מחזיר רשימת כל הפריטים."""
        logger.info("מביא רשימת פריטים מפריורטי")
        result = await self._get(
            "PART",
            params={"$select": "PARTNAME,PARTDES"},
        )
        return result.get("value", []) if result else []

    # --- הזמנות רכש ---

    async def find_purchase_order(self, order_name: str) -> Optional[dict]:
        """חיפוש הזמנת רכש לפי ORDNAME."""
        logger.info("מחפש הזמנת רכש: %s", order_name)
        result = await self._get(
            "PORDERS",
            params={"$filter": f"ORDNAME eq '{order_name}'", "$select": "ORDNAME,SUPNAME,ORDSTATUSDES"},
        )
        if result and result.get("value"):
            return result["value"][0]
        return None

    # --- חשבוניות ---

    async def submit_invoice(self, invoice_data: dict) -> dict:
        """קליטת חשבונית ספק ב-Priority (POST ל-PINVOICES)."""
        logger.info("שולח חשבונית לפריורטי: %s", invoice_data.get("IVNUM", ""))
        return await self._post("PINVOICES", invoice_data)

    def _pinvoice_key(self, ivnum: str, ivtype: str = "P", debit: str = "D") -> str:
        """מחזיר את המפתח המורכב של PINVOICES לפי OData."""
        return f"PINVOICES(IVNUM='{ivnum}',IVTYPE='{ivtype}',DEBIT='{debit}')"

    async def finalize_invoice(self, ivnum: str, file_path: str = "") -> str:
        """
        מצרף קובץ ומבצע CLOSEPRINTPIV על חשבונית דרך WCF SDK.
        מחזיר את מספר תנועת היומן (FNCNUM) או מחרוזת ריקה בכישלון.
        """
        import asyncio
        import json as _json
        import subprocess
        from pathlib import Path

        js_script = Path(__file__).parent / "finalize_invoice.js"
        if not js_script.exists():
            logger.warning("finalize_invoice.js לא נמצא")
            return ""

        args = ["node", str(js_script), ivnum]
        if file_path and Path(file_path).exists():
            args.append(file_path)

        logger.info("מריץ finalize_invoice.js עבור IVNUM %s", ivnum)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(__file__).parent),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if stderr:
                logger.debug("finalize_invoice stderr: %s", stderr.decode(errors="replace"))
            result = _json.loads(stdout.decode().strip())
            if result.get("ok"):
                fncnum = result.get("fncnum", "")
                ivnum  = result.get("ivnum", "")
                logger.info("finalize_invoice הצליח — IVNUM: %s, FNCNUM: %s", ivnum, fncnum)
                return {"ivnum": ivnum, "fncnum": fncnum}
            else:
                logger.warning("finalize_invoice נכשל: %s", result.get("error"))
                return {}
        except Exception as e:
            logger.warning("שגיאה ב-finalize_invoice: %s", e)
            return {}

    # --- בדיקת חיבור ---

    async def health_check(self) -> bool:
        """בודק תקינות החיבור ל-Priority."""
        try:
            result = await self._get("SUPPLIERS", params={"$top": "1", "$select": "SUPNAME"})
            return result is not None
        except Exception:
            return False
