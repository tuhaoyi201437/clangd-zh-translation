@echo off
setlocal
set "HERE=%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%HERE%clangd_zh_proxy.py" %*
) else (
    python "%HERE%clangd_zh_proxy.py" %*
)
exit /b
