@echo off
REM WinRate AI - launch Flask server and open frontend
cd /d %~dp0

echo Starting Flask API server...
start "WinRate AI - Flask" py -3.11 -m flask --app api_setup/flask_app.py run --port 5000

REM Wait a moment for Flask to finish starting up
timeout /t 3 /nobreak >nul

echo Opening frontend...
start "" "http://localhost:5000"

echo.
echo WinRate AI is running.
echo Flask server: http://localhost:5000
echo Close the Flask window to stop the server.

