"""
Genera documentación.pdf a partir de docs/documentacion.html.
Requiere playwright (incluido en requirements.txt).

Uso:
    python src/generar_docs.py

Cuando hagas cambios en el código:
  1. Actualiza docs/documentacion.html con los nuevos detalles.
  2. Actualiza README.md de forma consistente.
  3. Ejecuta este script para regenerar documentación.pdf.
"""

import os
import sys
from datetime import datetime
from playwright.sync_api import sync_playwright

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUENTE_HTML = os.path.join(BASE_DIR, "docs", "documentacion.html")
DESTINO_PDF = os.path.join(BASE_DIR, "docs", "documentación.pdf")

MESES = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def main():
    if not os.path.exists(FUENTE_HTML):
        print(f"❌  No se encuentra: {FUENTE_HTML}")
        sys.exit(1)

    with open(FUENTE_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    hoy = datetime.now()
    fecha = f"{hoy.day} de {MESES[hoy.month]} de {hoy.year}"
    html = html.replace("{{FECHA}}", fecha)

    print("⏳  Generando documentación.pdf ...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=DESTINO_PDF,
            format="A4",
            print_background=True,
            margin={
                "top":    "0mm",
                "bottom": "0mm",
                "left":   "0mm",
                "right":  "0mm",
            },
        )
        browser.close()

    kb = os.path.getsize(DESTINO_PDF) // 1024
    print(f"✅  documentación.pdf  ({kb} KB)")
    print(f"    → {DESTINO_PDF}")


if __name__ == "__main__":
    main()
