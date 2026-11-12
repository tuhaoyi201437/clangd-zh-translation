@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%HERE%install.py" %*
    goto done
)
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
    python "%HERE%install.py" %*
    goto done
)
echo Python 3 not found. Install Python 3.8+ with "Add python.exe to PATH",
echo or set CLANGD_PY to the full path of python.exe and run this again.
:done
echo.
pause
exit /b
