#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Capturador de Páginas Web — Iniciador Linux / macOS
# ─────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "═══════════════════════════════════════"
echo "  Capturador de Páginas Web"
echo "═══════════════════════════════════════"

# 1. Crear entorno virtual si no existe
if [ ! -d "venv" ]; then
    echo ""
    echo "▶ Creando entorno virtual en ./venv ..."
    python3 -m venv venv
fi

# 2. Activar entorno virtual
echo "▶ Activando entorno virtual..."
source venv/bin/activate

# 3. Instalar / actualizar dependencias
echo "▶ Instalando dependencias..."
pip install --upgrade pip --quiet
pip install -r config/requirements.txt --quiet

# 4. Instalar navegador Chromium de Playwright (solo si no está)
echo "▶ Verificando navegador Chromium..."
playwright install chromium --with-deps

# 5. Instalar modelo de español para análisis de contenido (NER)
echo "▶ Verificando modelo de idioma español (spaCy)..."
python -m spacy download es_core_news_sm --quiet 2>/dev/null || true

# 6. Crear carpetas de salida si no existen
mkdir -p salida/capturas salida/extraccion salida/backups salida/logs config sesiones

# 6. Ejecutar
echo ""
echo "▶ Iniciando captura..."
echo ""
python src/capturador.py
