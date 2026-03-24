"""
SQLite DB — טבלת companies לספקים ולקוחות מפריורטי.
"""
import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("בסיס.נתונים")

DB_PATH = Path(__file__).resolve().parent / "companies.db"


def get_connection() -> sqlite3.Connection:
    """מחזיר חיבור ל-SQLite."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """יצירת הטבלאות אם לא קיימות."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            priority_code TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('supplier', 'customer')),
            tax_id TEXT,
            tax_id_type TEXT,
            address TEXT,
            phone TEXT,
            email TEXT,
            status TEXT DEFAULT 'active',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(priority_code, type)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_companies_tax_id ON companies(tax_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_companies_type ON companies(type)
    """)
    # טבלת תתי חברות (סניפים)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            tax_id TEXT,
            address TEXT,
            phone TEXT,
            email TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_branches_tax_id ON branches(tax_id)
    """)
    # טבלת מצב סנכרון
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_status (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            last_sync_at TIMESTAMP,
            suppliers_count INTEGER DEFAULT 0,
            customers_count INTEGER DEFAULT 0,
            branches_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("INSERT OR IGNORE INTO sync_status (id) VALUES (1)")
    conn.commit()
    conn.close()
    logger.info("בסיס הנתונים אותחל: %s", DB_PATH)


# --- חיפוש ---

def find_by_tax_id(tax_id: str, company_type: Optional[str] = None) -> Optional[dict]:
    """חיפוש חברה לפי ח.פ / ע.מ."""
    conn = get_connection()
    clean = tax_id.strip().lstrip("0")
    if company_type:
        row = conn.execute(
            "SELECT * FROM companies WHERE REPLACE(LTRIM(tax_id, '0'), '-', '') = ? AND type = ?",
            (clean, company_type),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM companies WHERE REPLACE(LTRIM(tax_id, '0'), '-', '') = ?",
            (clean,),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_by_name(name: str, company_type: Optional[str] = None) -> list[dict]:
    """חיפוש חברה לפי שם (חלקי)."""
    conn = get_connection()
    if company_type:
        rows = conn.execute(
            "SELECT * FROM companies WHERE name LIKE ? AND type = ? LIMIT 20",
            (f"%{name}%", company_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM companies WHERE name LIKE ? LIMIT 20",
            (f"%{name}%",),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_by_priority_code(code: str, company_type: str) -> Optional[dict]:
    """חיפוש לפי קוד פריורטי."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM companies WHERE priority_code = ? AND type = ?",
        (code, company_type),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all(company_type: Optional[str] = None) -> list[dict]:
    """מחזיר את כל החברות."""
    conn = get_connection()
    if company_type:
        rows = conn.execute(
            "SELECT * FROM companies WHERE type = ? ORDER BY name", (company_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM companies ORDER BY type, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """מחזיר סטטיסטיקות."""
    conn = get_connection()
    suppliers = conn.execute("SELECT COUNT(*) FROM companies WHERE type='supplier'").fetchone()[0]
    customers = conn.execute("SELECT COUNT(*) FROM companies WHERE type='customer'").fetchone()[0]
    branches = conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0]
    sync_row = conn.execute("SELECT last_sync_at FROM sync_status WHERE id=1").fetchone()
    last_sync = sync_row["last_sync_at"] if sync_row else None
    conn.close()
    return {
        "suppliers": suppliers, "customers": customers, "branches": branches,
        "total": suppliers + customers, "last_sync_at": last_sync,
    }


# --- עדכון ---

def upsert_company(priority_code: str, name: str, company_type: str,
                   tax_id: str = None, tax_id_type: str = None,
                   address: str = None, phone: str = None, email: str = None,
                   status: str = "active") -> None:
    """הוספה או עדכון חברה."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO companies (priority_code, name, type, tax_id, tax_id_type, address, phone, email, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(priority_code, type) DO UPDATE SET
            name = excluded.name,
            tax_id = COALESCE(excluded.tax_id, tax_id),
            tax_id_type = COALESCE(excluded.tax_id_type, tax_id_type),
            address = COALESCE(excluded.address, address),
            phone = COALESCE(excluded.phone, phone),
            email = COALESCE(excluded.email, email),
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
    """, (priority_code, name, company_type, tax_id, tax_id_type, address, phone, email, status))
    conn.commit()
    conn.close()


def bulk_upsert(records: list[dict]) -> int:
    """עדכון מרובה — מקבל רשימת dicts עם אותם שדות כמו upsert_company."""
    conn = get_connection()
    count = 0
    for rec in records:
        conn.execute("""
            INSERT INTO companies (priority_code, name, type, tax_id, tax_id_type, address, phone, email, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(priority_code, type) DO UPDATE SET
                name = excluded.name,
                tax_id = COALESCE(excluded.tax_id, tax_id),
                tax_id_type = COALESCE(excluded.tax_id_type, tax_id_type),
                address = COALESCE(excluded.address, address),
                phone = COALESCE(excluded.phone, phone),
                email = COALESCE(excluded.email, email),
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
        """, (
            rec["priority_code"], rec["name"], rec["type"],
            rec.get("tax_id"), rec.get("tax_id_type"),
            rec.get("address"), rec.get("phone"), rec.get("email"),
            rec.get("status", "active"),
        ))
        count += 1
    conn.commit()
    conn.close()
    return count


# --- סניפים (תתי חברות) ---

def upsert_branch(branch_code: str, name: str, tax_id: str = None,
                  address: str = None, phone: str = None, email: str = None) -> None:
    """הוספה/עדכון סניף."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO branches (branch_code, name, tax_id, address, phone, email, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(branch_code) DO UPDATE SET
            name = excluded.name,
            tax_id = COALESCE(excluded.tax_id, tax_id),
            address = COALESCE(excluded.address, address),
            phone = COALESCE(excluded.phone, phone),
            email = COALESCE(excluded.email, email),
            updated_at = CURRENT_TIMESTAMP
    """, (branch_code, name, tax_id, address, phone, email))
    conn.commit()
    conn.close()


def bulk_upsert_branches(records: list[dict]) -> int:
    """עדכון מרובה של סניפים."""
    conn = get_connection()
    count = 0
    for rec in records:
        conn.execute("""
            INSERT INTO branches (branch_code, name, tax_id, address, phone, email, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(branch_code) DO UPDATE SET
                name = excluded.name,
                tax_id = COALESCE(excluded.tax_id, tax_id),
                address = COALESCE(excluded.address, address),
                phone = COALESCE(excluded.phone, phone),
                email = COALESCE(excluded.email, email),
                updated_at = CURRENT_TIMESTAMP
        """, (rec["branch_code"], rec["name"], rec.get("tax_id"),
              rec.get("address"), rec.get("phone"), rec.get("email")))
        count += 1
    conn.commit()
    conn.close()
    return count


def find_branch_by_tax_id(tax_id: str) -> Optional[dict]:
    """חיפוש סניף לפי ח.פ/ע.מ."""
    conn = get_connection()
    clean = tax_id.strip().lstrip("0")
    row = conn.execute(
        "SELECT * FROM branches WHERE REPLACE(LTRIM(tax_id, '0'), '-', '') = ?",
        (clean,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_branch_by_name(name: str) -> list[dict]:
    """חיפוש סניף לפי שם."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM branches WHERE name LIKE ? LIMIT 20", (f"%{name}%",)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_branches() -> list[dict]:
    """מחזיר כל הסניפים."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM branches ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- סטטוס סנכרון ---

def update_sync_status(suppliers: int, customers: int, branches: int) -> None:
    """עדכון מועד סנכרון אחרון."""
    conn = get_connection()
    conn.execute("""
        UPDATE sync_status SET
            last_sync_at = CURRENT_TIMESTAMP,
            suppliers_count = ?,
            customers_count = ?,
            branches_count = ?
        WHERE id = 1
    """, (suppliers, customers, branches))
    conn.commit()
    conn.close()
