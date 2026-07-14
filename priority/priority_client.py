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

    async def attach_extfile(self, parent_entity: str, parent_key: str, file_path: str) -> bool:
        """מצרף קובץ ל-EXTFILES של רשומה דרך OData (POST ל-subform).
        parent_entity: שם הישות, למשל 'PINVOICES'
        parent_key: המפתח המורכב, למשל "IVNUM='12345',IVTYPE='P',DEBIT='D'"

        סכמת EXTFILES_SUBFORM (נבדק על PINVOICES של ebyael):
          EXTFILEDES   — שם/תיאור הקובץ (string)
          EXTFILENAME  — data URL מלא: "data:<mime>;base64,<base64>"
          SUFFIX       — סיומת הקובץ באותיות קטנות (pdf/jpeg/...)
        """
        import base64
        from pathlib import Path
        p = Path(file_path)
        if not p.exists():
            logger.warning("קובץ לא נמצא לצירוף ל-EXTFILES: %s", file_path)
            return False
        content_b64 = base64.b64encode(p.read_bytes()).decode()
        ext = (p.suffix.lstrip('.') or 'pdf').lower()
        mime = "application/pdf" if ext == "pdf" else f"image/{ext}"
        data_url = f"data:{mime};base64,{content_b64}"
        endpoint = f"{parent_entity}({parent_key})/EXTFILES_SUBFORM"
        try:
            await self._post(endpoint, {
                "EXTFILEDES":  p.name,
                "EXTFILENAME": data_url,
                "SUFFIX":      ext,
            })
            logger.info("קובץ צורף ל-EXTFILES של %s(%s)", parent_entity, parent_key)
            return True
        except Exception as e:
            logger.warning("שגיאה בצירוף קובץ ל-EXTFILES של %s: %s", parent_entity, e)
            return False

    # --- ספקים ---

    async def create_supplier(self, supplier_data: dict) -> Optional[dict]:
        """יצירת ספק חדש ב-Priority (POST ל-SUPPLIERS)."""
        logger.info("יוצר ספק חדש בפריורטי: %s", supplier_data.get("SUPDES", ""))
        return await self._post("SUPPLIERS", supplier_data)

    async def create_accounts_payable(self, sup_name: str, branch_name: str) -> Optional[dict]:
        """פתיחת חשבון ספק בתת-חברה (POST ל-ACCOUNTS_PAYABLE)."""
        logger.info("פותח חשבון ספק %s בסניף %s", sup_name, branch_name)
        return await self._post("ACCOUNTS_PAYABLE", {"SUPNAME": sup_name, "BRANCHNAME": branch_name})

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
        """קליטת חשבונית ספק ב-Priority (POST ל-PINVOICES עם PINVOICEITEMS_SUBFORM)."""
        logger.info("שולח חשבונית לפריורטי: BOOKNUM=%s, שורות=%d",
                    invoice_data.get("BOOKNUM", ""),
                    len(invoice_data.get("PINVOICEITEMS_SUBFORM", [])))
        return await self._post("PINVOICES", invoice_data)

    def _pinvoice_key(self, ivnum: str, ivtype: str = "P", debit: str = "D") -> str:
        """מחזיר את המפתח המורכב של PINVOICES לפי OData."""
        return f"PINVOICES(IVNUM='{ivnum}',IVTYPE='{ivtype}',DEBIT='{debit}')"

    async def finalize_invoice(self, ivnum: str, file_path: str = "") -> dict:
        """
        מצרף קובץ ומבצע CLOSEPRINTPIV על חשבונית דרך WCF SDK.
        מחזיר {"ivnum": ..., "fncnum": ...} בהצלחה או {} בכישלון.
        """
        import asyncio
        import json as _json
        from pathlib import Path

        priority_dir = Path(__file__).parent
        js_script = priority_dir / "finalize_invoice.js"
        if not js_script.exists():
            logger.warning("finalize_invoice.js לא נמצא")
            return {}

        # בדיקה ש-node מותקן — Ubuntu לפעמים מתקין בשם "nodejs"
        node_executable = None
        for candidate in ["node", "nodejs"]:
            try:
                node_check = await asyncio.create_subprocess_exec(
                    candidate, "--version",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                node_out, _ = await asyncio.wait_for(node_check.communicate(), timeout=5)
                if node_check.returncode == 0:
                    node_executable = candidate
                    logger.info("Node.js (%s) version: %s", candidate, node_out.decode().strip())
                    break
            except Exception:
                continue
        if not node_executable:
            logger.error("Node.js לא מותקן — CLOSEPRINTPIV לא יפעל. הרץ deploy להתקנה.")
            return {"error": "node_not_installed"}

        # אם node_modules חסר — הרץ npm install לפני ההרצה
        node_modules = priority_dir / "node_modules"
        if not node_modules.exists():
            logger.info("node_modules חסר — מריץ npm install...")
            try:
                install = await asyncio.create_subprocess_exec(
                    "npm", "install", "--production",
                    cwd=str(priority_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, install_err = await asyncio.wait_for(install.communicate(), timeout=180)
                if install_err:
                    logger.info("npm install stderr: %s", install_err.decode(errors="replace")[:500])
                logger.info("npm install הושלם")
            except Exception as e:
                logger.error("npm install נכשל: %s", e)
                return {}

        args = [node_executable, str(js_script), ivnum]
        if file_path and Path(file_path).exists():
            args.append(file_path)
            logger.info("מצרף קובץ לפריורטי: %s", file_path)
        elif file_path:
            logger.warning("קובץ לא נמצא בנתיב: %s — יוחלף ללא צירוף", file_path)

        logger.info("מריץ finalize_invoice.js עבור IVNUM %s", ivnum)
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(priority_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if stderr:
                logger.info("finalize_invoice stderr: %s", stderr.decode(errors="replace")[:1000])
            # dotenv עלול להדפיס שורות לוג ל-stdout לפני ה-JSON — לוקחים את השורה האחרונה שמתחילה ב-{
            raw_full = stdout.decode(errors="replace").strip()
            raw = ""
            for line in reversed(raw_full.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    raw = line
                    break
            if not raw:
                logger.warning("finalize_invoice לא החזיר פלט JSON. stdout: %s", raw_full[:300])
                return {}
            result = _json.loads(raw)
            stderr_text = stderr.decode(errors="replace")[:800] if stderr else ""
            if result.get("ok"):
                fncnum = result.get("fncnum", "")
                ivnum  = result.get("ivnum", "")
                logger.info("finalize_invoice הצליח — IVNUM: %s, FNCNUM: %s", ivnum, fncnum)
                return {"ivnum": ivnum, "fncnum": fncnum}
            else:
                err = result.get("error", "unknown")
                logger.warning("finalize_invoice נכשל: %s | stderr: %s", err, stderr_text)
                return {"error": err, "stderr": stderr_text}
        except Exception as e:
            logger.warning("שגיאה ב-finalize_invoice: %s", e)
            return {"error": str(e)}

    # --- תעודות קבלה מספק ---

    async def get_supplier_receipt_documents(self, sup_name: str) -> list[dict]:
        """מחזיר את תעודות הקבלה של הספק ממסך 'קבלות סחורה מספק' (DOCUMENTS_P)."""
        logger.info("מביא תעודות קבלה לספק %s מפריורטי", sup_name)
        result = await self._get(
            "DOCUMENTS_P",
            params={
                "$filter": f"SUPNAME eq '{sup_name}' and TYPE eq 'P' and STATDES eq 'סופית'",
                "$select": "DOC,DOCNO,CURDATE,BOOKNUM,ORDNAME,STATDES,IVALL,TOTQUANT,DISPRICE,TOTPRICE",
                "$orderby": "CURDATE desc",
                "$top": "200",
            },
        )
        return result.get("value", []) if result else []

    # --- בדיקת חיבור ---

    async def health_check(self) -> bool:
        """בודק תקינות החיבור ל-Priority."""
        try:
            result = await self._get("SUPPLIERS", params={"$top": "1", "$select": "SUPNAME"})
            return result is not None
        except Exception:
            return False
