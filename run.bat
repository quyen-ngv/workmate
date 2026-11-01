@echo off
rem Double-click to start Copilot. Everything else is set up on the web page that opens.
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Can cai uv truoc: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  pause
  exit /b 1
)
uv sync --quiet
if errorlevel 1 (
  pause
  exit /b 1
)
uv run actoroffice serve %*
pause
