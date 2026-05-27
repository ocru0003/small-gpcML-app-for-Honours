@echo off
echo Starting gpcML Validator...

cd /d "%~dp0"

if not exist venv (
    echo Virtual environment not found. Creating one...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo Installing dependencies (if needed)...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo Starting Uvicorn server...
python -m uvicorn main:app --reload

pause
