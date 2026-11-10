@echo off
rem StreamSplitter for Windows: install and set up with one command. Double-click it, or run it in a terminal:
rem     run.bat                     first time: install, then the guided setup; later: a menu (start, status, stop,
rem                                 change settings, listen for Slack commands)
rem     run.bat --section slack     one setup step: event, tba, stream, slack, youtube, notify, listen, test
rem     run.bat status              any streamsplitter command: start, status, stop, listen, live ..., record ...
rem     run.bat --install-only      only install and download (no questions)
rem It installs uv (astral.sh's official installer) into .tools\uv if uv is not on PATH, Python 3.12 and the light
rem packages into .venv, then starts the setup, which downloads the rest (detection packages, ffmpeg, Deno) in the
rem background. Everything stays inside this folder: Windows' settings and PATH are not changed. A second run skips
rem what is done. Set STREAMSPLITTER_NO_PAUSE=1 to skip the "press a key" at the end of a double-click.
setlocal EnableExtensions
cd /d "%~dp0"
set "ROOT=%~dp0"
set "TOOLS=%ROOT%.tools"
set "VENV=%ROOT%.venv"
set "PY=%ROOT%.venv\Scripts\python.exe"
rem uv keeps its Python and download cache here too; copy (not link) files, which works on every drive
set "UV_PYTHON_INSTALL_DIR=%TOOLS%\python"
set "UV_CACHE_DIR=%TOOLS%\uv-cache"
set "UV_LINK_MODE=copy"

rem Started by a double-click (cmd /c): keep the window open at the end so the messages can be read.
set "DBLCLICK="
setlocal EnableDelayedExpansion
set "CMDLINE=!cmdcmdline!"
if /i not "!CMDLINE:/c =!"=="!CMDLINE!" (endlocal & set "DBLCLICK=1") else endlocal
if defined STREAMSPLITTER_NO_PAUSE set "DBLCLICK="

set "UV="
for /f "delims=" %%U in ('where uv 2^>nul') do if not defined UV set "UV=%%U"
if not defined UV if exist "%TOOLS%\uv\uv.exe" set "UV=%TOOLS%\uv\uv.exe"

rem Up to date: the venv works and was installed from this very pyproject.toml.
if not exist "%PY%" goto :install
if not exist "%VENV%\installed-pyproject.toml" goto :install
fc /b "%ROOT%pyproject.toml" "%VENV%\installed-pyproject.toml" >nul 2>&1 || goto :install
"%PY%" -c "import streamsplitter, tomli_w, yt_dlp" >nul 2>&1 || goto :install
goto :run

:install
echo StreamSplitter: installing (the first time takes a minute or two)
if defined UV goto :have_uv
echo.
echo [1/3] Getting uv, which installs Python and the packages (official installer from astral.sh) ...
set "UV_UNMANAGED_INSTALL=%TOOLS%\uv"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12; irm https://astral.sh/uv/install.ps1 | iex"
set "UV_UNMANAGED_INSTALL="
if not exist "%TOOLS%\uv\uv.exe" goto :fail_uv
set "UV=%TOOLS%\uv\uv.exe"
:have_uv
echo.
echo [2/3] Python 3.12 in .venv (uv's own copy, the same on every computer) ...
if exist "%PY%" goto :have_venv
"%UV%" venv --python 3.12 --managed-python "%VENV%"
if errorlevel 1 goto :fail_venv
:have_venv
echo.
echo [3/3] The setup's own packages (a few seconds) ...
"%UV%" pip install --python "%PY%" -e .
if errorlevel 1 goto :fail_pip
copy /y "%ROOT%pyproject.toml" "%VENV%\installed-pyproject.toml" >nul

:run
if defined UV set "STREAMSPLITTER_UV=%UV%"
set "FIRST=%~1"
if not defined FIRST goto :setup
if "%FIRST:~0,1%"=="-" goto :setup
"%PY%" -m streamsplitter.cli %*
goto :end
:setup
"%PY%" -m streamsplitter.cli setup %*
goto :end

:fail_uv
echo.
echo Could not get uv from astral.sh. Check the internet connection (a school or company network may block
echo downloads; a phone hotspot works) and run run.bat again. Or install uv yourself (https://docs.astral.sh/uv/)
echo and run run.bat again.
goto :failed
:fail_venv
echo.
echo Could not make the Python 3.12 environment. Check the internet connection and run run.bat again. If it
echo keeps failing, delete the .venv and .tools folders here and run run.bat again.
goto :failed
:fail_pip
echo.
echo Could not install StreamSplitter's packages. Check the internet connection and run run.bat again (it
echo continues where it stopped).
goto :failed

:failed
if defined DBLCLICK pause
exit /b 1

:end
set "RC=%ERRORLEVEL%"
if defined DBLCLICK (
    echo.
    pause
)
exit /b %RC%
