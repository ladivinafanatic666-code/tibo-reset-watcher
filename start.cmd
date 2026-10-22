@echo off
setlocal
cd /d "%~dp0"
where py.exe >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys,subprocess,pathlib; p=pathlib.Path(sys.executable).with_name('pythonw.exe'); subprocess.Popen([str(p), 'app.py'], cwd=pathlib.Path.cwd())"
  if not errorlevel 1 exit /b 0
)
where python.exe >nul 2>nul
if not errorlevel 1 (
  python -c "import sys,subprocess,pathlib; p=pathlib.Path(sys.executable).with_name('pythonw.exe'); subprocess.Popen([str(p), 'app.py'], cwd=pathlib.Path.cwd())"
  if not errorlevel 1 exit /b 0
)
echo Python 3.12 or newer with tkinter is required.
echo Download: https://www.python.org/downloads/windows/
echo Keep app.py, watcher.py and start.cmd in the same folder.
pause
exit /b 1
