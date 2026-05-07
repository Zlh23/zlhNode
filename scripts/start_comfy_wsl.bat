@echo off
REM Do NOT pushd UNC -> Z: ; WSL fails to translate Z:\ and may drop argv (your "0" became empty -> 1,0).
REM Jump to C:\Windows before PowerShell/WSL so interop cwd is valid.

cd /d "%SystemRoot%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_comfy_wsl.ps1"
exit /b %ERRORLEVEL%
