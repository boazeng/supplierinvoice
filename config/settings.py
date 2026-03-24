"""
הגדרות מערכת — טעינת משתני סביבה מ-.env
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# טעינת .env מהתיקייה הראשית של הפרויקט
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)


# --- סביבה ---
ENV: str = os.getenv("ENV", "development")

# --- שרת ---
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# --- Priority ERP ---
# PRIORITY_URL_REAL = סביבה אמיתית, PRIORITY_URL_DEMO = סביבת דמו
PRIORITY_URL: str = os.getenv("PRIORITY_URL_REAL", os.getenv("PRIORITY_URL_DEMO", ""))
PRIORITY_USER: str = os.getenv("PRIORITY_USERNAME", "")
PRIORITY_PASS: str = os.getenv("PRIORITY_PASSWORD", "")
PRIORITY_COMPANY: str = os.getenv("PRIORITY_COMPANY", "")
PRIORITY_SYNC_INTERVAL_MINUTES: int = int(os.getenv("PRIORITY_SYNC_INTERVAL_MINUTES", "60"))

# --- Anthropic AI ---
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# --- אימייל (אופציונלי) ---
EMAIL_HOST: str = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT: int = int(os.getenv("EMAIL_PORT", "993"))
EMAIL_USER: str = os.getenv("GMAIL_USER", "")
EMAIL_PASS: str = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_FOLDER: str = os.getenv("EMAIL_FOLDER", "INBOX")

# --- נתיבים ---
DATA_DIR: Path = BASE_DIR / "data"
INVOICES_DIR: Path = DATA_DIR / "invoices"
CACHE_DIR: Path = DATA_DIR / "cache"
LOGS_DIR: Path = BASE_DIR / "logs"

# יצירת תיקיות אם לא קיימות
for d in [INVOICES_DIR, CACHE_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)
