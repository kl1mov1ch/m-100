@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if exist ".local\python\python.exe" (
  ".local\python\python.exe" start.py %*
  exit /b !errorlevel!
)
where py >nul 2>&1
if not errorlevel 1 (
  for %%V in (3.13 3.12 3.11) do (
    py -%%V -c "pass" >nul 2>&1
    if not errorlevel 1 (
      py -%%V start.py %*
      exit /b !errorlevel!
    )
  )
)
python start.py %*
exit /b %errorlevel%
