@echo off
setlocal
set "VENV=D:\venvs\shopfloor"
if not exist "%VENV%\Scripts\python.exe" (
  echo Virtual environment not found at %VENV%.
  echo Create it with: python -m venv D:\venvs\shopfloor
  exit /b 1
)
"%VENV%\Scripts\python.exe" -m streamlit run "%~dp0app.py"
