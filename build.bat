@echo off
cd /d "%~dp0"
set "PY=C:\Users\fhz11\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m pip install pyinstaller
for %%I in ("%PY%") do set "PYDIR=%%~dpI"
if exist "%PYDIR%abi3t-compat\python3t.dll" (
  "%PY%" -m PyInstaller --noconfirm --clean --windowed --name SSLTool --hidden-import keyring.backends.Windows --hidden-import win32ctypes.core --hidden-import win32ctypes.pywin32 --hidden-import win32ctypes.pywintypes --add-binary "%PYDIR%abi3t-compat\python3t.dll;." main.py
) else (
  "%PY%" -m PyInstaller --noconfirm --clean --windowed --name SSLTool --hidden-import keyring.backends.Windows --hidden-import win32ctypes.core --hidden-import win32ctypes.pywin32 --hidden-import win32ctypes.pywintypes main.py
)
if errorlevel 1 (echo Build failed, see log above. & pause) else (echo Done: dist\SSLTool\SSLTool.exe & pause)