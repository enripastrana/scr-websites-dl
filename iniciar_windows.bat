@echo off
chcp 65001 >nul
REM ─────────────────────────────────────────────────────────────
REM  Capturador de Páginas Web — Iniciador Windows
REM ─────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo.
echo ═══════════════════════════════════════
echo   Capturador de Páginas Web
echo ═══════════════════════════════════════

REM 1. Crear entorno virtual si no existe
if not exist "venv\" (
    echo.
    echo [1] Creando entorno virtual en .\venv ...
    python -m venv venv
    if errorlevel 1 (
        echo ERROR: No se encontro Python. Instala Python 3.9+ desde https://python.org
        pause
        exit /b 1
    )
)

REM 2. Activar entorno virtual
echo [2] Activando entorno virtual...
call venv\Scripts\activate.bat

REM 3. Instalar / actualizar dependencias
echo [3] Instalando dependencias...
pip install --upgrade pip --quiet
pip install -r config\requirements.txt --quiet

REM 4. Instalar navegador Chromium de Playwright
echo [4] Verificando navegador Chromium...
playwright install chromium --with-deps

REM 5. Instalar modelo de español para análisis de contenido (NER)
echo [5] Verificando modelo de idioma español (spaCy)...
python -m spacy download es_core_news_sm --quiet 2>nul

REM 6. Crear carpetas de salida si no existen
if not exist "salida\capturas\" mkdir salida\capturas
if not exist "salida\extraccion\" mkdir salida\extraccion
if not exist "salida\backups\" mkdir salida\backups
if not exist "salida\logs\" mkdir salida\logs
if not exist "config\" mkdir config
if not exist "sesiones\" mkdir sesiones

REM 7. Ejecutar
echo.
echo [6] Iniciando captura...
echo.
python src\capturador.py

echo.
pause
