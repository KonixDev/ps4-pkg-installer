@echo off
chcp 65001 >nul
REM ===========================================================================
REM  PS4 PKG Installer — compilar el .exe en Windows.
REM
REM  Doble clic y listo: deja "dist\PS4 PKG Installer.exe", un único archivo
REM  que se abre en cualquier Windows sin instalar nada. Lo único que hace
REM  falta en esta máquina es Python (python.org, tildando "Add to PATH").
REM
REM  Hace lo mismo que el workflow de release del repo, para que lo que sale
REM  de acá sea igual a lo que se publica en GitHub.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "SEVENZIP_VER=25.01"
set "SEVENZIP_PKG=7z2501-x64.exe"

echo.
echo === [1/5] Buscando Python ===
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo ERROR: no encontré Python en el PATH.
  echo Instalalo desde https://www.python.org/downloads/windows/ y tildá
  echo "Add python.exe to PATH" en la primera pantalla del instalador.
  goto :fallo
)
%PY% --version

echo.
echo === [2/5] Entorno virtual y dependencias ===
REM Un venv aparte para que el build no ensucie el Python del sistema.
if not exist ".venv-build\Scripts\python.exe" (
  %PY% -m venv .venv-build || goto :fallo
)
call ".venv-build\Scripts\activate.bat"
python -m pip install --upgrade pip >nul || goto :fallo
pip install -r requirements.txt pyinstaller || goto :fallo
REM flet_desktop trae el runtime de Flutter (~45 MB). Sin él, PyInstaller no
REM se queja pero el .exe muere al arrancar con "No module named flet_desktop".
python -c "import flet_desktop" || goto :fallo

echo.
echo === [3/5] 7-Zip (viaja adentro del .exe) ===
if not exist vendor mkdir vendor
if not exist "vendor\7z.exe" (
  REM Hace falta el 7-Zip COMPLETO —7z.exe junto a su 7z.dll—, no el "7za.exe"
  REM del paquete Extra: ese es la versión reducida y no lee RAR, que es como
  REM viene casi todo release de PS4.
  REM 7zr.exe es un extractor mínimo y autónomo; su único trabajo acá es abrir
  REM el instalador de 7-Zip, que adentro trae el 7z.exe que sí queremos.
  echo   bajando 7zr.exe ...
  curl -fL --retry 3 -o "vendor\7zr.exe" "https://github.com/ip7z/7zip/releases/download/%SEVENZIP_VER%/7zr.exe" || goto :fallo
  echo   bajando 7-Zip %SEVENZIP_VER% ...
  curl -fL --retry 3 -o "vendor\7zsetup.exe" "https://github.com/ip7z/7zip/releases/download/%SEVENZIP_VER%/%SEVENZIP_PKG%" || goto :fallo
  "vendor\7zr.exe" x "vendor\7zsetup.exe" -ovendor -y 7z.exe 7z.dll >nul || goto :fallo
  del "vendor\7zr.exe" "vendor\7zsetup.exe" >nul 2>&1
)
if not exist "vendor\7z.dll" (
  echo ERROR: falta vendor\7z.dll — 7z.exe no funciona sin ella.
  goto :fallo
)
echo   ok: vendor\7z.exe

echo.
echo === [4/5] UnRAR de respaldo (opcional) ===
REM 7-Zip lee los headers de cualquier RAR5 pero no implementa todos los
REM codecs: con algunos comprimidos lista bien el contenido y muere al extraer
REM con "Unsupported Method". Ahí entra UnRAR. Si este paso falla el build
REM sigue igual: solo se pierde el respaldo.
if not exist "vendor\unrar.exe" (
  curl -fL --retry 3 -o "%TEMP%\unrarw64.exe" "https://www.rarlab.com/rar/unrarw64.exe" >nul 2>&1
  if exist "%TEMP%\unrarw64.exe" (
    REM RARLAB no publica el unrar de Windows suelto: viene en un instalador
    REM autoextraíble. "-s" lo corre sin ventanas y "-d" elige dónde lo deja.
    "%TEMP%\unrarw64.exe" -s -d"%CD%\vendor\_unrar" >nul 2>&1
    if exist "%CD%\vendor\_unrar\UnRAR.exe" move /y "%CD%\vendor\_unrar\UnRAR.exe" "vendor\unrar.exe" >nul
    rmdir /s /q "%CD%\vendor\_unrar" >nul 2>&1
    del "%TEMP%\unrarw64.exe" >nul 2>&1
  )
)
set EXTRA=
if exist "vendor\unrar.exe" (
  set EXTRA=--add-binary "vendor\unrar.exe;."
  echo   ok: vendor\unrar.exe
) else (
  echo   aviso: sin UnRAR de respaldo. Algún RAR muy comprimido puede no abrirse.
)

echo.
echo === [5/5] Compilando — esto tarda unos minutos ===
if not exist build-win mkdir build-win
REM --specpath manda el .spec generado a build-win para no pisar el
REM "PS4 PKG Installer.spec" del repo, que es el de macOS.
pyinstaller --noconfirm --onefile --windowed ^
  --name "PS4 PKG Installer" ^
  --icon "assets\icon.ico" ^
  --collect-all flet ^
  --collect-all flet_desktop ^
  --add-binary "vendor\7z.exe;." ^
  --add-binary "vendor\7z.dll;." ^
  %EXTRA% ^
  --workpath "build-win" ^
  --specpath "build-win" ^
  ps4_pkg_installer.py || goto :fallo

set "EXE=dist\PS4 PKG Installer.exe"
if not exist "%EXE%" (
  echo ERROR: PyInstaller terminó pero no dejó "%EXE%".
  goto :fallo
)
REM Un .exe sano ronda los 50-70 MB. Si sale en ~10 MB, el runtime de Flutter
REM quedó afuera y la ventana no abre.
for %%F in ("%EXE%") do set "TAM=%%~zF"
set /a TAM_MB=%TAM% / 1048576
echo   tamaño: %TAM_MB% MB
if %TAM% LSS 35000000 (
  echo ERROR: pesa menos de 35 MB, el runtime de Flet no quedó embebido.
  goto :fallo
)

echo.
echo  ======================================================
echo   LISTO:  %CD%\%EXE%
echo   Doble clic para abrirlo. Para pasárselo a alguien,
echo   alcanza con ese único archivo.
echo  ======================================================
echo.
pause
exit /b 0

:fallo
echo.
echo  *** El build falló. El motivo es el último mensaje de arriba. ***
echo.
pause
exit /b 1
