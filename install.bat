@echo off
REM Installer entry point for Windows. Prefers Abaqus' own Python so the
REM environment matches, falls back to whatever python is on PATH.
REM
REM The engine directory is named after the Python minor version, and that
REM changes between releases (2024 ships python3.10), so it is globbed instead
REM of hardcoded -- a hardcoded list silently stops matching the moment a new
REM release lands. Newest release first. Delayed expansion is deliberately NOT
REM enabled: it would corrupt any path containing "!".
setlocal
set HERE=%~dp0

set ABQPY=
for %%V in (2026 2025 2024) do (
  if not defined ABQPY (
    for /d %%P in ("C:\SIMULIA\EstProducts\%%V\win_b64\tools\SMApy\python3*") do (
      if not defined ABQPY if exist "%%~P\python.exe" set ABQPY=%%~P\python.exe
    )
  )
)

if defined ABQPY (
  echo Using Abaqus Python: %ABQPY%
  "%ABQPY%" "%HERE%install.py" %*
) else (
  echo Abaqus Python not found, using python from PATH.
  python "%HERE%install.py" %*
)
endlocal
