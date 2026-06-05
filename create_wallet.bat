@echo off
cd /d "%~dp0"
"C:\Users\local_ybjuj27\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" create_wallet.py
echo.
echo ============================================================
echo  Copy the two lines (PUMPPORTAL_API_KEY / TRADE_STREAM_ENABLED)
echo  into your .env file, fund the wallet address with SOL,
echo  then tell the assistant: bolson
echo ============================================================
echo.
pause
