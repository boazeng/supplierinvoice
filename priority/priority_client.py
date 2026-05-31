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

    async def attach_file(self, ivnum: str, file_path: str) -> bool:
        """מצרף קובץ לנספחים של חשבונית קיימת ב-Priority."""
        import base64
        from pathlib import Path
        p = Path(file_path)
        if not p.exists():
            logger.warning("קובץ לא נמצא לצירוף: %s", file_path)
            return False
        suffix = p.suffix.lstrip(".").lower()
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        entity = f"PINVOICES('{ivnum}')/EXTFILES_SUBFORM"
        try:
            await self._post(entity, {
                "EXTFILEDES": p.name,
                "SUFFIX": suffix,
                "EXTFILENAME": b64,
            })
            logger.info("קובץ צורף בהצלחה ל-IVNUM %s", ivnum)
            return True
        except Exception as e:
            logger.warning("לא ניתן לצרף קובץ ל-%s: %s", ivnum, e)
            return False

    async def close_invoice(self, ivnum: str) -> bool:
        """מבצע CLOSEPRINTPIV על חשבונית קיימת (אישור וסגירה)."""
        client = await self._get_client()
        url = f"{self.base_url}/PINVOICES('{ivnum}')/CLOSEPRINTPIV"
        logger.info("שולח CLOSEPRINTPIV ל-IVNUM %s", ivnum)
        try:
            response = await client.post(url, json={})
            response.raise_for_status()
            logger.info("CLOSEPRINTPIV הצליח ל-IVNUM %s", ivnum)
            return True
        except Exception as e:
            logger.warning("CLOSEPRINTPIV נכשל ל-%s: %s", ivnum, e)
            return False

    # --- בדיקת חיבור ---

    async def health_check(self) -> bool:
        """בודק תקינות החיבור ל-Priority."""
        try:
            result = await self._get("SUPPLIERS", params={"$top": "1", "$select": "SUPNAME"})
            return result is not None
        except Exception:
            return False
