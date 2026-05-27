@echo off
echo === מתקין תלויות ===
pip install fastapi uvicorn[standard] httpx anthropic python-dotenv python-multipart watchdog itsdangerous

echo.
echo === מפעיל שרת מקומי ===
echo פתחי בדפדפן: http://localhost:8000
echo.
python main.py
pause
