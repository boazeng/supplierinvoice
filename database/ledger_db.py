"""ספרי הנהלת חשבונות — חברות, ספרים שנתיים, חוצצים ומסמכים.

DB נפרד (ledger.db) — נפרד מטבלת ה-companies של Priority.
"""
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "ledger.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """יצירת הטבלאות אם אינן קיימות."""
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ledger_companies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            tax_id      TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger_books (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  INTEGER NOT NULL REFERENCES ledger_companies(id),
            year        INTEGER NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(company_id, year)
        );
        CREATE TABLE IF NOT EXISTS ledger_dividers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id     INTEGER NOT NULL REFERENCES ledger_books(id),
            name        TEXT NOT NULL,
            sort_order  INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ledger_documents (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id           INTEGER NOT NULL REFERENCES ledger_books(id),
            divider_id        INTEGER REFERENCES ledger_dividers(id),
            title             TEXT DEFAULT '',
            file_path         TEXT NOT NULL,
            original_filename TEXT DEFAULT '',
            file_type         TEXT DEFAULT '',
            document_date     TEXT,            -- YYYY-MM-DD — התאריך שעל המסמך
            scan_date         TEXT,            -- YYYY-MM-DD — תאריך הסריקה/העלאה
            date_source       TEXT DEFAULT 'scan',  -- 'document' | 'scan'
            category1         TEXT DEFAULT '',
            category2         TEXT DEFAULT '',
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_docs_book
            ON ledger_documents(book_id, document_date);
    """)
    # מיגרציה — עמודת invoice_id (מקשרת חזרה לחשבונית המקורית)
    try:
        conn.execute("ALTER TABLE ledger_documents ADD COLUMN invoice_id TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # עמודה כבר קיימת
    conn.close()


# ===================== חברות =====================

def list_companies() -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM ledger_companies ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_company(name: str, tax_id: str = "") -> int:
    conn = _conn()
    cur = conn.execute("INSERT INTO ledger_companies (name, tax_id) VALUES (?, ?)",
                       (name.strip(), tax_id.strip()))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def find_or_create_company(name: str) -> int:
    """מחזיר id של חברה לפי שם — יוצר אם לא קיימת."""
    conn = _conn()
    row = conn.execute("SELECT id FROM ledger_companies WHERE name = ?", (name.strip(),)).fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute("INSERT INTO ledger_companies (name) VALUES (?)", (name.strip(),))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return cid


def get_company(company_id: int) -> Optional[dict]:
    conn = _conn()
    row = conn.execute("SELECT * FROM ledger_companies WHERE id = ?", (company_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ===================== ספרים (שנה לכל חברה) =====================

def list_books(company_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM ledger_books WHERE company_id = ? ORDER BY year DESC",
        (company_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_book(company_id: int, year: int) -> int:
    conn = _conn()
    cur = conn.execute("INSERT INTO ledger_books (company_id, year) VALUES (?, ?)",
                       (company_id, year))
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    return bid


def find_or_create_book(company_id: int, year: int) -> int:
    """מחזיר id של ספר לפי חברה+שנה — יוצר אם לא קיים."""
    conn = _conn()
    row = conn.execute(
        "SELECT id FROM ledger_books WHERE company_id = ? AND year = ?",
        (company_id, year),
    ).fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute("INSERT INTO ledger_books (company_id, year) VALUES (?, ?)",
                       (company_id, year))
    conn.commit()
    bid = cur.lastrowid
    conn.close()
    return bid


def get_book(book_id: int) -> Optional[dict]:
    """ספר עם שם החברה."""
    conn = _conn()
    row = conn.execute("""
        SELECT b.*, c.name AS company_name, c.tax_id AS company_tax_id
        FROM ledger_books b JOIN ledger_companies c ON c.id = b.company_id
        WHERE b.id = ?
    """, (book_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ===================== חוצצים =====================

def list_dividers(book_id: int) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM ledger_dividers WHERE book_id = ? ORDER BY sort_order, name",
        (book_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_divider(book_id: int, name: str) -> int:
    conn = _conn()
    cur = conn.execute("INSERT INTO ledger_dividers (book_id, name) VALUES (?, ?)",
                       (book_id, name.strip()))
    conn.commit()
    did = cur.lastrowid
    conn.close()
    return did


def find_or_create_divider(book_id: int, name: str) -> int:
    """מחזיר id של חוצץ (ספק) בספר — יוצר אם לא קיים."""
    conn = _conn()
    row = conn.execute(
        "SELECT id FROM ledger_dividers WHERE book_id = ? AND name = ?",
        (book_id, name.strip()),
    ).fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO ledger_dividers (book_id, name) VALUES (?, ?)",
        (book_id, name.strip()),
    )
    conn.commit()
    did = cur.lastrowid
    conn.close()
    return did


def delete_divider(divider_id: int) -> None:
    conn = _conn()
    # מסמכים שהיו בחוצץ — מתנתקים ממנו (לא נמחקים)
    conn.execute("UPDATE ledger_documents SET divider_id = NULL WHERE divider_id = ?",
                 (divider_id,))
    conn.execute("DELETE FROM ledger_dividers WHERE id = ?", (divider_id,))
    conn.commit()
    conn.close()


# ===================== מסמכים =====================

def get_document_by_invoice_id(invoice_id: str) -> Optional[dict]:
    """מחזיר מסמך לדג'ר לפי invoice_id."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM ledger_documents WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def create_document(book_id: int, file_path: str, original_filename: str,
                    file_type: str, document_date: Optional[str], scan_date: str,
                    date_source: str, title: str = "",
                    divider_id: Optional[int] = None,
                    invoice_id: str = "") -> int:
    conn = _conn()
    cur = conn.execute("""
        INSERT INTO ledger_documents
            (book_id, divider_id, title, file_path, original_filename, file_type,
             document_date, scan_date, date_source, invoice_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (book_id, divider_id, title, file_path, original_filename, file_type,
          document_date, scan_date, date_source, invoice_id))
    conn.commit()
    doc_id = cur.lastrowid
    conn.close()
    return doc_id


def get_document(doc_id: int) -> Optional[dict]:
    conn = _conn()
    row = conn.execute("SELECT * FROM ledger_documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_documents(book_id: int, q: str = "", date_from: str = "", date_to: str = "",
                   divider_id: Optional[int] = None, category: str = "") -> list[dict]:
    """מסמכים בספר, ממויינים לפי תאריך המסמך (לפי החודשים והימים בסדר)."""
    sql = """
        SELECT d.*, v.name AS divider_name
        FROM ledger_documents d
        LEFT JOIN ledger_dividers v ON v.id = d.divider_id
        WHERE d.book_id = ?
    """
    params: list = [book_id]
    if q:
        sql += " AND (d.title LIKE ? OR d.original_filename LIKE ? OR d.category1 LIKE ? OR d.category2 LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like]
    if date_from:
        sql += " AND COALESCE(d.document_date, d.scan_date) >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND COALESCE(d.document_date, d.scan_date) <= ?"
        params.append(date_to)
    if divider_id is not None:
        sql += " AND d.divider_id = ?"
        params.append(divider_id)
    if category:
        sql += " AND (d.category1 = ? OR d.category2 = ?)"
        params += [category, category]
    # מיון לפי התאריך האפקטיבי (תאריך המסמך, ובהיעדרו תאריך הסריקה)
    sql += " ORDER BY COALESCE(d.document_date, d.scan_date) DESC, d.id DESC"
    conn = _conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_document(doc_id: int, **fields) -> None:
    """עדכון שדות מותרים של מסמך."""
    allowed = {"title", "document_date", "date_source", "category1",
               "category2", "divider_id"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn = _conn()
    conn.execute(f"UPDATE ledger_documents SET {cols} WHERE id = ?",
                 (*sets.values(), doc_id))
    conn.commit()
    conn.close()


def delete_document(doc_id: int) -> Optional[dict]:
    """מוחק מסמך ומחזיר את הרשומה (כדי שהקורא ימחק את הקובץ)."""
    doc = get_document(doc_id)
    if doc:
        conn = _conn()
        conn.execute("DELETE FROM ledger_documents WHERE id = ?", (doc_id,))
        conn.commit()
        conn.close()
    return doc


def list_categories(book_id: int) -> list[str]:
    """ערכי קטגוריה ייחודיים שכבר בשימוש בספר — להשלמה אוטומטית."""
    conn = _conn()
    rows = conn.execute("""
        SELECT category1 AS c FROM ledger_documents WHERE book_id = ? AND category1 <> ''
        UNION
        SELECT category2 AS c FROM ledger_documents WHERE book_id = ? AND category2 <> ''
        ORDER BY c
    """, (book_id, book_id)).fetchall()
    conn.close()
    return [r["c"] for r in rows]
