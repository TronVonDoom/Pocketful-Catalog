@echo off
rem Double-click to open the card editor in its own window.
rem
rem For a Start menu and desktop icon that opens without this console flashing up first,
rem run install-shortcut.ps1 once instead.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0server.py" --app
) else (
    start "" python "%~dp0server.py" --app
)
