"""
מודלי נתונים מרכזיים — חשבוניות, ספקים, שורות
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class InvoiceStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    REVIEW = "review"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    ERROR = "error"


class InvoiceSource(str, Enum):
    UPLOAD = "upload"
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    FOLDER = "folder"
    API = "api"


@dataclass
class SupplierInfo:
    """נתוני ספק שחולצו מהחשבונית."""
    name: str = ""
    tax_id: str = ""                    # ח.פ / ע.מ
    tax_id_type: str = ""               # "ח.פ" או "ע.מ"
    address: str = ""
    phone: str = ""
    # שדות שמתמלאים אחרי סנכרון עם Priority
    priority_supplier_code: str = ""    # SUPDES
    priority_match_found: bool = False


@dataclass
class CustomerInfo:
    """נתוני הלקוח (אנחנו) שמופיעים בחשבונית."""
    name: str = ""
    tax_id: str = ""                    # ח.פ / ע.מ
    tax_id_type: str = ""               # "ח.פ" או "ע.מ"
    address: str = ""
    branch: str = ""                    # סניף
    priority_customer_code: str = ""    # CUSTNAME — קוד פריורטי
    priority_match_found: bool = False


@dataclass
class InvoiceLine:
    """שורת חשבונית בודדת."""
    line_number: int = 0
    description: str = ""
    catalog_number: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    total_price: float = 0.0
    vat_amount: float = 0.0
    # שדות Priority
    priority_part_name: str = ""        # PARTNAME
    priority_match_found: bool = False


@dataclass
class InvoiceData:
    """נתוני חשבונית מנותחת — תוצאת חילוץ מ-Claude Vision."""
    invoice_number: str = ""
    invoice_date: str = ""              # YYYY-MM-DD
    allocation_number: str = ""         # מספר הקצאה — אישור ניכוי מס במקור
    supplier: SupplierInfo = field(default_factory=SupplierInfo)
    customer: CustomerInfo = field(default_factory=CustomerInfo)
    lines: list[InvoiceLine] = field(default_factory=list)
    subtotal: float = 0.0
    vat_amount: float = 0.0
    total_amount: float = 0.0
    currency: str = "ILS"
    confidence_score: float = 0.0       # 0.0 - 1.0
    extraction_warnings: list[str] = field(default_factory=list)


@dataclass
class Invoice:
    """הישות הראשית — חשבונית ספק."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: InvoiceStatus = InvoiceStatus.PENDING
    source: InvoiceSource = InvoiceSource.UPLOAD
    file_path: str = ""
    file_type: str = ""                 # pdf / image
    extracted_data: Optional[InvoiceData] = None
    priority_validation: dict = field(default_factory=dict)
    priority_invoice_id: str = ""       # IVNUM שהתקבל מ-Priority לאחר קליטה
    user_notes: str = ""
    error_message: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
