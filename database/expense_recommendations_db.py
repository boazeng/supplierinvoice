"""מאגר המלצות לחשבון הוצאות לפי ספק.

המערכת לומדת מההקלדות של המשתמש: כל פעם שנשמרת חשבונית עם
(supplier_code, expense_account) — הצמד הזה נרשם ב-DB ומקבל times_used+1.
בעת פתיחת חשבונית של אותו ספק — אפשר לקבל את הצמד הנפוץ ביותר כהמלצה.

הסכמה כוללת גם branch כי לאותו ספק יכולים להיות חשבונות שונים בכל סניף.
מבנה דומה ל-Bank-discrepancies/database/receipts/recommendations_db.py.
"""
import sqlite3
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("בסיס.נתונים.המלצות-הוצאות")

DB_PATH = Path(__file__).resolve().parent / "expense_recommendations.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expense_recommendations (
            id              TEXT PRIMARY KEY,
            supplier_code   TEXT NOT NULL,
            expense_account TEXT NOT NULL,
            account_desc    TEXT DEFAULT '',
            branch          TEXT DEFAULT '',
            times_used      INTEGER DEFAULT 1,
            last_used       TEXT DEFAULT '',
            created_at      TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_recs_supplier ON expense_recommendations(supplier_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expense_recs_account  ON expense_recommendations(expense_account)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_expense_recs_unique "
        "ON expense_recommendations(supplier_code, expense_account, branch)"
    )
    conn.commit()
    conn.close()
    logger.info("מאגר המלצות-חשבונות אותחל: %s", DB_PATH)


# --- כתיבה ---

def record(
    supplier_code: str,
    expense_account: str,
    account_desc: str = "",
    branch: str = "",
) -> None:
    """רושם שימוש של (supplier, account, branch). אם הצמד קיים — מעלה times_used."""
    if not supplier_code or not expense_account:
        return
    supplier_code = supplier_code.strip()
    expense_account = expense_account.strip()
    branch = (branch or "").strip()
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, times_used FROM expense_recommendations "
            "WHERE supplier_code=? AND expense_account=? AND branch=?",
            (supplier_code, expense_account, branch),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE expense_recommendations "
                "SET times_used=times_used+1, last_used=?, account_desc=COALESCE(NULLIF(?, ''), account_desc) "
                "WHERE id=?",
                (now, account_desc or "", row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO expense_recommendations "
                "(id, supplier_code, expense_account, account_desc, branch, times_used, last_used, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (str(uuid.uuid4()), supplier_code, expense_account, account_desc or "", branch, now, now),
            )
        conn.commit()
    finally:
        conn.close()


# --- קריאה / דירוג ---

def match(supplier_code: str, branch: str = "", limit: int = 5) -> list[dict]:
    """מחזיר עד `limit` המלצות לחשבון הוצאות לספק, מדורגות לפי frequency.
    אם branch ניתן — מעדיף התאמה לאותו סניף, אך נופל גם להמלצות מסניפים אחרים."""
    if not supplier_code:
        return []
    supplier_code = supplier_code.strip()
    branch = (branch or "").strip()

    conn = get_connection()
    try:
        if branch:
            # התאמת branch מקבלת ניקוד גבוה יותר; אחרים נדורגים אחריה
            rows = conn.execute(
                """
                SELECT id, supplier_code, expense_account, account_desc, branch,
                       times_used, last_used,
                       CASE WHEN branch = ? THEN 1 ELSE 0 END AS branch_match
                FROM expense_recommendations
                WHERE supplier_code = ?
                ORDER BY branch_match DESC, times_used DESC, last_used DESC
                LIMIT ?
                """,
                (branch, supplier_code, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, supplier_code, expense_account, account_desc, branch,
                       times_used, last_used
                FROM expense_recommendations
                WHERE supplier_code = ?
                ORDER BY times_used DESC, last_used DESC
                LIMIT ?
                """,
                (supplier_code, limit),
            ).fetchall()

        # חישוב ביטחון יחסי — האחוז שמופע הצמד הזה מתוך השימושים של אותו ספק (+סניף אם הוגדר)
        if branch:
            total_row = conn.execute(
                "SELECT SUM(times_used) AS s FROM expense_recommendations "
                "WHERE supplier_code=? AND branch=?",
                (supplier_code, branch),
            ).fetchone()
        else:
            total_row = conn.execute(
                "SELECT SUM(times_used) AS s FROM expense_recommendations WHERE supplier_code=?",
                (supplier_code,),
            ).fetchone()
        total = (total_row["s"] or 0) if total_row else 0

        results = []
        for r in rows:
            rec = dict(r)
            rec.pop("branch_match", None)
            rec["confidence"] = round((rec["times_used"] / total) * 100) if total else 0
            results.append(rec)
        return results
    finally:
        conn.close()


def top(supplier_code: str, branch: str = "") -> Optional[dict]:
    """מחזיר את ההמלצה החזקה ביותר (או None)."""
    results = match(supplier_code, branch=branch, limit=1)
    return results[0] if results else None


# --- ניהול ---

def list_all(q: str = "", limit: int = 500) -> list[dict]:
    """רשימה לניהול — אופציונלית מסונן לפי q (חיפוש בקוד ספק / חשבון / תיאור)."""
    conn = get_connection()
    try:
        if q:
            like = f"%{q.strip()}%"
            rows = conn.execute(
                """
                SELECT * FROM expense_recommendations
                WHERE supplier_code LIKE ? OR expense_account LIKE ? OR account_desc LIKE ?
                ORDER BY times_used DESC, last_used DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM expense_recommendations "
                "ORDER BY times_used DESC, last_used DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM expense_recommendations").fetchone()[0]
    finally:
        conn.close()


def update(rec_id: str, expense_account: str = "", account_desc: str = "", branch: str = "") -> bool:
    """עדכון ידני של רשומה."""
    if not rec_id:
        return False
    sets = []
    params: list = []
    if expense_account:
        sets.append("expense_account=?")
        params.append(expense_account.strip())
    if account_desc:
        sets.append("account_desc=?")
        params.append(account_desc.strip())
    if branch is not None:
        sets.append("branch=?")
        params.append((branch or "").strip())
    if not sets:
        return False
    params.append(rec_id)
    conn = get_connection()
    try:
        cur = conn.execute(f"UPDATE expense_recommendations SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete(rec_id: str) -> bool:
    if not rec_id:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM expense_recommendations WHERE id=?", (rec_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- מיגרציה חד-פעמית מהמערכת הישנה ---

def seed_from_legacy(legacy_pairs: list[tuple[str, str]]) -> int:
    """אם supplier_expense_accounts (השמירה הישנה — חשבון יחיד לספק) הכיל ערכים,
    נטען אותם כהתחלה עם times_used=1."""
    n = 0
    for sup, acc in legacy_pairs:
        if sup and acc:
            record(sup, acc)
            n += 1
    return n
