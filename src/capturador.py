"""
Capturador de Páginas Web
Lee una lista de URLs y guarda cada página en los formatos configurados.
Formatos disponibles: imagen (PNG), pdf, html, texto

Uso:
    python src/capturador.py
    python src/capturador.py --urls otra_lista.txt
    python src/capturador.py --formatos imagen,pdf
    python src/capturador.py --forzar
    python src/capturador.py --sesion cookies.json
    python src/capturador.py --guardar-sesion cookies.json
    python src/capturador.py --verificar
    python src/capturador.py --solo-analizar
"""

import os
import sys
import json
import signal
import hashlib
import argparse
import zipfile
import asyncio
import platform
import subprocess
import warnings
from datetime import datetime
from urllib.parse import urlparse

# Módulo de análisis de contenido (mismo directorio src/)
sys.path.insert(0, os.path.dirname(__file__))
import extractor as _ext

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, BarColumn,
    TextColumn, TimeElapsedColumn, MofNCompleteColumn,
)
from rich.panel import Panel
from rich.table import Table
from rich import box

console = Console()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────
#  CONFIGURACIÓN  (edita aquí)
# ─────────────────────────────────────────
ARCHIVO_URLS     = os.path.join(BASE_DIR, "config", "urls.txt")
CARPETA_SALIDA   = os.path.join(BASE_DIR, "salida", "capturas")
CARPETA_BACKUPS  = os.path.join(BASE_DIR, "salida", "backups")
CARPETA_SESIONES = os.path.join(BASE_DIR, "sesiones")
ARCHIVO_HASHES   = os.path.join(BASE_DIR, "config", "historial_hashes.json")
ARCHIVO_LOG      = os.path.join(BASE_DIR, "salida", "logs", "errores.log")

FORMATOS_DEFAULT = {
    "imagen": True,   # screenshot.png  (captura completa)
    "pdf":    True,   # documento.pdf
    "html":   True,   # pagina.html     (código fuente renderizado)
    "texto":  True,   # texto.txt       (solo texto visible)
}

VIEWPORT          = {"width": 1920, "height": 1080}
TIMEOUT_MS        = 60_000        # tiempo máximo de carga por página (ms)
REINTENTOS        = 2             # intentos extra con backoff exponencial
WAIT_UNTIL_RAPIDO = "domcontentloaded"  # evento principal — rápido y fiable en cualquier sitio
NETWORKIDLE_MS    = 5_000         # espera adicional best-effort para SPAs
CONTENIDO_MIN_CH  = 100          # caracteres mínimos de body para no capturar spinners vacíos

MAX_CONCURRENT   = 3             # URLs procesadas simultáneamente
DOMAIN_DELAY_S   = 2.0           # segundos mínimos entre requests al mismo dominio

SCROLL_ANTES_CAPTURAR = True
SCROLL_PASO_PX        = 800
SCROLL_PAUSA_MS       = 300
SCROLL_MAX_ALTO_PX    = 30_000

GENERAR_REPORTE  = True
MAX_BACKUPS      = 0
NOTIFICACIONES   = True

ANALIZAR_CONTENIDO  = True   # extrae emails, teléfonos, nombres, cargos y metadatos
REGION_TELEFONOS    = "MX"   # región para parsear números sin código de país (MX, ES, US…)
CARPETA_EXTRACCION  = os.path.join(BASE_DIR, "salida", "extraccion")
TABLERO_SALIDA      = os.path.join(BASE_DIR, "tablero.html")
# ─────────────────────────────────────────

FMT_LEGIBLE = "%Y-%m-%d %H:%M:%S"
FMT_ARCHIVO = "%Y%m%d_%H%M%S"

_historial_global: dict = {}

_ARCHIVOS_FORMATO = {
    "imagen": ("screenshot.png", "imagen"),
    "pdf":    ("documento.pdf",  "pdf"),
    "html":   ("pagina.html",    "html"),
    "texto":  ("texto.txt",      "texto"),
}

# Primitivas async — inicializadas al inicio de procesar_urls_async
_sem:             asyncio.Semaphore                 # limita concurrencia global
_historial_lock:  asyncio.Lock                      # protege lecturas/escrituras de _historial_global
_domain_locks:    dict[str, asyncio.Lock] = {}      # un Lock por dominio para rate limiting


def _get_domain_lock(dominio: str) -> asyncio.Lock:
    """Retorna (o crea) el Lock asociado al dominio dado.
    Seguro sin mutex adicional porque asyncio es cooperativo (no hay preemción entre awaits).
    """
    if dominio not in _domain_locks:
        _domain_locks[dominio] = asyncio.Lock()
    return _domain_locks[dominio]


# ── Utilidades ────────────────────────────────────────────────────

def calcular_hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8", errors="replace")).hexdigest()


def cargar_historial() -> dict:
    """Lee el historial de hashes.
    Si el archivo principal está corrupto (escritura interrumpida), cae al .tmp como fallback.
    """
    for ruta in (ARCHIVO_HASHES, ARCHIVO_HASHES + ".tmp"):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}


def guardar_historial(historial: dict) -> None:
    """Escritura atómica vía rename (patrón Redis AOF / Git object store).

    Escribe a un .tmp, fuerza los bytes a disco con fsync y luego hace
    os.replace() — operación atómica en POSIX y Windows 10+.
    Nunca deja historial_hashes.json en estado corrupto, ni siquiera ante SIGKILL.
    """
    tmp = ARCHIVO_HASHES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(historial, f, indent=4, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())          # garantiza que los bytes lleguen al disco físico
    os.replace(tmp, ARCHIVO_HASHES)  # atómico: el archivo nunca está a medias


def registrar_error(url: str, mensaje: str) -> None:
    with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime(FMT_LEGIBLE)}] {url}\n  {mensaje}\n\n")


def validar_url(url: str) -> tuple:
    if not url.startswith(("http://", "https://")):
        return False, f"falta protocolo http/https — ¿quisiste escribir 'https://{url}'?"
    if not urlparse(url).netloc:
        return False, "formato de URL no reconocido"
    return True, ""


def nombre_carpeta(url: str) -> str:
    netloc = urlparse(url).netloc
    dominio_limpio = "".join(c if c.isalnum() or c in "-_." else "_" for c in netloc)[:40]
    return f"{dominio_limpio}_{datetime.now().strftime(FMT_ARCHIVO)}"


def _ruta_sesion(archivo: str) -> str:
    if os.path.isabs(archivo):
        return archivo
    return os.path.join(CARPETA_SESIONES, archivo)


# ── Notificaciones ────────────────────────────────────────────────

def notificar(titulo: str, mensaje: str) -> None:
    """Notificación de escritorio multiplataforma. Falla en silencio si no está disponible.

    - Linux  : notify-send (stdlib, sin dependencias Python)
    - Windows/macOS : plyer (nativo del SO)
    """
    if not NOTIFICACIONES:
        return
    if platform.system() == "Linux":
        try:
            subprocess.run(
                ["notify-send", "--app-name=Capturador Web", "--expire-time=6000", titulo, mensaje],
                check=False, capture_output=True, timeout=3,
            )
            return
        except (FileNotFoundError, Exception):
            pass  # notify-send no disponible — cae al fallback
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # suprime UserWarning de dbus
            from plyer import notification
            notification.notify(title=titulo, message=mensaje, app_name="Capturador Web", timeout=6)
    except Exception:
        pass


# ── Sesiones / Cookies (async) ────────────────────────────────────

async def cargar_cookies(context, archivo: str) -> None:
    ruta = _ruta_sesion(archivo)
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)
        console.print(f"  [cyan]🍪 Sesión cargada:[/cyan] {os.path.basename(ruta)}")
    except FileNotFoundError:
        console.print(f"  [yellow]⚠️  Sesión no encontrada:[/yellow] {ruta}")


async def guardar_cookies(context, archivo: str) -> None:
    ruta = _ruta_sesion(archivo)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    cookies = await context.cookies()
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    console.print(f"  [cyan]💾 Sesión guardada:[/cyan] {os.path.basename(ruta)}")


# ── Navegación con espera por capas (async) ───────────────────────

async def navegar_con_fallback(page, url: str) -> None:
    """Estrategia de espera en 3 capas (estándar ChangeDetection.io / Apify):

    1. domcontentloaded — rápido y fiable en cualquier sitio
    2. networkidle      — best-effort 5s; se omite en SPAs con polling continuo,
                          WebSockets o ads, que nunca llegan a networkidle
    3. contenido mínimo — guard contra capturas de spinners de carga vacíos
    """
    await page.goto(url, wait_until=WAIT_UNTIL_RAPIDO, timeout=TIMEOUT_MS)

    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_MS)
    except Exception:
        pass  # SPAs / dashboards / sites con ads — continuar sin bloquear

    try:
        await page.wait_for_function(
            f"() => document.body.innerText.trim().length > {CONTENIDO_MIN_CH}",
            timeout=10_000,
        )
    except Exception:
        pass  # Páginas legítimamente cortas (login, 404) — capturar igualmente


# ── Scroll (async) ────────────────────────────────────────────────

async def hacer_scroll(page) -> None:
    """Scroll completo para disparar lazy loading, luego vuelve al inicio."""
    posicion = 0
    while posicion < SCROLL_MAX_ALTO_PX:
        posicion += SCROLL_PASO_PX
        await page.evaluate(f"window.scrollTo(0, {posicion})")
        await page.wait_for_timeout(SCROLL_PAUSA_MS)
        if posicion >= await page.evaluate("document.body.scrollHeight"):
            break
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(500)


# ── Integridad de archivos capturados (patrón ArchiveBox) ─────────

def _hash_archivo(ruta: str) -> str:
    """SHA-256 del archivo físico — lectura en bloques para no cargar todo en RAM."""
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65_536), b""):
            h.update(bloque)
    return h.hexdigest()


def guardar_manifiesto(ruta_carpeta: str, url: str, titulo: str) -> None:
    """Genera manifest.json con SHA-256 y tamaño de cada archivo guardado.

    Permite detectar modificaciones o corrupción posterior a la captura,
    de forma equivalente a `archivebox check` o la verificación de integridad
    del Wayback Machine.
    """
    manifiesto = {
        "url":      url,
        "titulo":   titulo,
        "fecha":    datetime.now().strftime(FMT_LEGIBLE),
        "archivos": {},
    }
    for archivo in sorted(os.listdir(ruta_carpeta)):
        if archivo == "manifest.json":
            continue
        ruta_archivo = os.path.join(ruta_carpeta, archivo)
        if os.path.isfile(ruta_archivo):
            manifiesto["archivos"][archivo] = {
                "sha256": _hash_archivo(ruta_archivo),
                "bytes":  os.path.getsize(ruta_archivo),
            }
    with open(os.path.join(ruta_carpeta, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifiesto, f, indent=2, ensure_ascii=False)


def verificar_integridad(ruta_carpeta: str) -> list:
    """Retorna lista de problemas encontrados (vacía = todo OK)."""
    try:
        with open(os.path.join(ruta_carpeta, "manifest.json"), "r", encoding="utf-8") as f:
            manifiesto = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return ["manifest.json no encontrado o corrupto"]

    problemas = []
    for archivo, meta in manifiesto["archivos"].items():
        ruta = os.path.join(ruta_carpeta, archivo)
        try:
            if _hash_archivo(ruta) != meta["sha256"]:
                problemas.append(f"{archivo}: hash no coincide (posible modificación)")
        except FileNotFoundError:
            problemas.append(f"{archivo}: archivo eliminado")
    return problemas


def verificar_todas_las_capturas() -> None:
    """Verifica la integridad de todas las carpetas de capturas con manifest.json."""
    console.print("\n[bold]🔍 Verificando integridad de todas las capturas...[/bold]\n")
    total = ok = con_problemas = sin_manifiesto = 0

    for nombre in sorted(os.listdir(CARPETA_SALIDA)):
        ruta_abs = os.path.join(CARPETA_SALIDA, nombre)
        if not os.path.isdir(ruta_abs):
            continue
        if not os.path.exists(os.path.join(ruta_abs, "manifest.json")):
            sin_manifiesto += 1
            continue
        total += 1
        problemas = verificar_integridad(ruta_abs)
        if problemas:
            con_problemas += 1
            console.print(f"  [red]❌ {nombre}[/red]")
            for p in problemas:
                console.print(f"     [dim]→ {p}[/dim]")
        else:
            ok += 1
            console.print(f"  [green]✅ {nombre}[/green]")

    tabla = Table(title="Resultado de verificación", box=box.ROUNDED, show_header=False)
    tabla.add_column(style="bold", no_wrap=True)
    tabla.add_column(justify="right")
    tabla.add_row("[green]Sin problemas[/green]",          str(ok))
    tabla.add_row("[red]Con problemas[/red]",              str(con_problemas))
    tabla.add_row("[dim]Sin manifiesto (captura antigua)[/dim]", str(sin_manifiesto))
    tabla.add_row("[cyan]Total verificadas[/cyan]",        str(total))
    console.print(tabla)


# ── Captura (async) ───────────────────────────────────────────────

async def guardar_capturas(page, url: str, ruta: str, formatos: dict, titulo: str = "") -> tuple:
    """Guarda todos los formatos configurados.
    Retorna (html_fuente, texto_visible) para el pipeline de análisis.
    """
    os.makedirs(ruta, exist_ok=True)

    if SCROLL_ANTES_CAPTURAR:
        console.print("      [dim]🔄 Cargando contenido (scroll)...[/dim]")
        await hacer_scroll(page)

    if formatos.get("imagen"):
        await page.screenshot(path=os.path.join(ruta, "screenshot.png"), full_page=True)
        console.print("      [green]✅ Imagen guardada[/green]")

    if formatos.get("pdf"):
        await page.pdf(path=os.path.join(ruta, "documento.pdf"), format="A4", print_background=True)
        console.print("      [green]✅ PDF guardado[/green]")

    html_fuente   = ""
    texto_visible = ""

    # Obtener HTML si algún formato lo necesita O si se va a analizar contenido
    if formatos.get("html") or formatos.get("texto") or ANALIZAR_CONTENIDO:
        html_fuente = await page.content()

        if formatos.get("html"):
            with open(os.path.join(ruta, "pagina.html"), "w", encoding="utf-8") as f:
                f.write(html_fuente)
            console.print("      [green]✅ HTML guardado[/green]")

        # Parsear BS4 una sola vez — reutilizar para texto.txt y análisis
        soup = BeautifulSoup(html_fuente, "html.parser")
        texto_visible = soup.get_text(separator="\n", strip=True)

        if formatos.get("texto"):
            with open(os.path.join(ruta, "texto.txt"), "w", encoding="utf-8") as f:
                f.write(texto_visible)
            console.print("      [green]✅ Texto guardado[/green]")

    with open(os.path.join(ruta, "info.txt"), "w", encoding="utf-8") as f:
        f.write(
            f"URL:      {url}\n"
            f"Titulo:   {titulo}\n"
            f"Fecha:    {datetime.now().strftime(FMT_LEGIBLE)}\n"
            f"Formatos: {', '.join(k for k, v in formatos.items() if v)}\n"
        )

    guardar_manifiesto(ruta, url, titulo)
    console.print("      [cyan]🔏 Manifiesto de integridad generado[/cyan]")

    return html_fuente, texto_visible


def _buscar_ultima_captura_sin_analisis(url: str):
    """Retorna la ruta de la carpeta más reciente para 'url' que no tiene datos.json. None si no hay."""
    mejor_ruta  = None
    mejor_fecha = ""
    for nombre in os.listdir(CARPETA_SALIDA):
        ruta_abs = os.path.join(CARPETA_SALIDA, nombre)
        if not os.path.isdir(ruta_abs):
            continue
        info = _leer_info(ruta_abs)
        if info.get("URL") != url:
            continue
        if os.path.exists(os.path.join(ruta_abs, "datos.json")):
            continue
        fecha = info.get("Fecha", "")
        if fecha > mejor_fecha:
            mejor_fecha = fecha
            mejor_ruta  = ruta_abs
    return mejor_ruta


def _analizar_existente(ruta_abs: str, url: str) -> dict:
    """Lee pagina.html (o texto.txt) de una captura y retorna el dict de análisis. Síncrono."""
    ruta_html  = os.path.join(ruta_abs, "pagina.html")
    ruta_texto = os.path.join(ruta_abs, "texto.txt")
    html_fuente = ""
    texto_visible = ""
    try:
        with open(ruta_html, "r", encoding="utf-8") as f:
            html_fuente = f.read()
        texto_visible = BeautifulSoup(html_fuente, "html.parser").get_text(separator="\n", strip=True)
    except FileNotFoundError:
        try:
            with open(ruta_texto, "r", encoding="utf-8") as f:
                texto_visible = f.read()
        except FileNotFoundError:
            return {}
    datos = _ext.analizar(url, html_fuente, texto_visible, REGION_TELEFONOS)
    return datos


# ── Reporte HTML acumulativo ──────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; background: #f0f2f5; color: #333; padding: 2rem; }
h1 { font-size: 1.4rem; margin-bottom: .3rem; }
.meta { color: #888; font-size: .82rem; margin-bottom: 1.5rem; }
.stats { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
.stat { background: #fff; border-radius: 8px; padding: .8rem 1.4rem; box-shadow: 0 1px 4px rgba(0,0,0,.08); text-align: center; }
.stat .num { font-size: 1.8rem; font-weight: 700; display: block; }
.stat .lbl { font-size: .78rem; color: #888; }
.cards { display: flex; flex-direction: column; gap: .8rem; }
.card { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); display: flex; overflow: hidden; }
.card-thumb { width: 180px; min-height: 110px; flex-shrink: 0; background: #eee; position: relative; }
.card-thumb img { width: 100%; height: 100%; object-fit: cover; object-position: top; display: block; }
.placeholder { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #bbb; font-size: .78rem; }
.card-body { padding: .9rem 1.1rem; flex: 1; display: flex; flex-direction: column; gap: .3rem; }
.card-head { display: flex; align-items: center; gap: .6rem; }
.badge { display: inline-block; padding: .15rem .55rem; border-radius: 4px; font-size: .72rem; font-weight: 600; }
.nueva  { background: #cce5ff; color: #004085; }
.previa { background: #e2e3e5; color: #383d41; }
.fecha { font-size: .78rem; color: #999; }
.card-url { font-size: .82rem; word-break: break-all; }
.card-url a { color: #0066cc; text-decoration: none; }
.card-url a:hover { text-decoration: underline; }
.card-title { font-weight: 600; font-size: .88rem; }
.card-links { display: flex; gap: .4rem; flex-wrap: wrap; margin-top: .3rem; }
.card-links a { font-size: .74rem; background: #f0f0f0; padding: .2rem .55rem; border-radius: 4px; text-decoration: none; color: #444; }
.card-links a:hover { background: #ddd; }
.sep { border: none; border-top: 2px dashed #e0e0e0; margin: .4rem 0; }
"""

_HTML_PAGE = """\
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Historial de Capturas</title>
  <style>{css}</style>
</head>
<body>
  <h1>Historial de Capturas</h1>
  <p class="meta">Actualizado: {actualizado} &nbsp;|&nbsp; Carpeta: capturas/</p>
  <div class="stats">
    <div class="stat"><span class="num">{total}</span><span class="lbl">Capturas totales</span></div>
    <div class="stat"><span class="num">{sitios}</span><span class="lbl">Sitios únicos</span></div>
    <div class="stat"><span class="num">{sesiones}</span><span class="lbl">Sesiones</span></div>
  </div>
  <div class="cards">
{filas}
  </div>
</body>
</html>
"""


def _leer_info(carpeta_abs: str) -> dict:
    info = {}
    try:
        with open(os.path.join(carpeta_abs, "info.txt"), "r", encoding="utf-8") as f:
            for linea in f:
                if ": " in linea:
                    clave, _, valor = linea.partition(": ")
                    info[clave.strip()] = valor.strip()
    except FileNotFoundError:
        pass
    return info


def generar_reporte(carpetas_nuevas: set) -> None:
    """Escanea capturas/ y genera un reporte HTML acumulativo con todas las sesiones."""
    entradas = []

    for nombre in os.listdir(CARPETA_SALIDA):
        ruta_abs = os.path.join(CARPETA_SALIDA, nombre)
        if not os.path.isdir(ruta_abs):
            continue
        info = _leer_info(ruta_abs)
        if not info.get("URL"):
            continue

        archivos = set(os.listdir(ruta_abs))
        formatos_presentes = [
            fmt for fmt, (archivo, _) in _ARCHIVOS_FORMATO.items()
            if archivo in archivos
        ]
        entradas.append({
            "carpeta":  nombre,
            "url":      info.get("URL", ""),
            "titulo":   info.get("Titulo", ""),
            "fecha":    info.get("Fecha", ""),
            "formatos": formatos_presentes,
            "es_nueva": nombre in carpetas_nuevas,
        })

    entradas.sort(key=lambda x: x["fecha"], reverse=True)

    filas = []
    url_anterior = None

    for e in entradas:
        if url_anterior and e["url"] != url_anterior:
            filas.append('    <hr class="sep">')
        url_anterior = e["url"]

        thumb = (
            f'<img src="{e["carpeta"]}/screenshot.png" loading="lazy" alt="">'
            if "imagen" in e["formatos"]
            else '<div class="placeholder">sin imagen</div>'
        )
        links = " ".join(
            f'<a href="{e["carpeta"]}/{archivo}">{etiqueta}</a>'
            for fmt, (archivo, etiqueta) in _ARCHIVOS_FORMATO.items()
            if fmt in e["formatos"]
        )
        badge = (
            '<span class="badge nueva">🆕 Esta sesión</span>'
            if e["es_nueva"] else
            '<span class="badge previa">📁 Sesión anterior</span>'
        )
        filas.append(f"""\
    <div class="card">
      <div class="card-thumb">{thumb}</div>
      <div class="card-body">
        <div class="card-head">{badge}<span class="fecha">{e["fecha"]}</span></div>
        <div class="card-url"><a href="{e["url"]}" target="_blank">{e["url"]}</a></div>
        <div class="card-title">{e["titulo"]}</div>
        <div class="card-links">{links}</div>
      </div>
    </div>""")

    urls_unicas = len({e["url"] for e in entradas})
    sesiones    = len({e["fecha"][:10] for e in entradas if e["fecha"]})

    html = _HTML_PAGE.format(
        css=_CSS,
        actualizado=datetime.now().strftime(FMT_LEGIBLE),
        total=len(entradas),
        sitios=urls_unicas,
        sesiones=sesiones,
        filas="\n".join(filas),
    )

    with open(os.path.join(CARPETA_SALIDA, "reporte.html"), "w", encoding="utf-8") as f:
        f.write(html)
    console.print(f"  [cyan]📄 Reporte:[/cyan] capturas/reporte.html  ({len(entradas)} capturas, {urls_unicas} sitios)")


# ── Tablero HTML unificado ────────────────────────────────────────

_TABLERO_HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tablero \u2014 Capturas Web</title>
<style>
:root{--bg:#f0f4f8;--card:#fff;--acc:#2b6cb0;--acc-l:#ebf4ff;--txt:#2d3748;--dim:#718096;--bdr:#e2e8f0;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--txt);}
header{background:#1a365d;color:#fff;padding:.85rem 1.5rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem;}
.hl h1{font-size:1.2rem;font-weight:700;}.hl .upd{font-size:.72rem;color:rgba(255,255,255,.5);margin-top:.1rem;}
#q{padding:.38rem .85rem;border:none;border-radius:20px;font-size:.85rem;width:240px;background:rgba(255,255,255,.15);color:#fff;outline:none;}
#q::placeholder{color:rgba(255,255,255,.45);}#q:focus{background:rgba(255,255,255,.22);}
.stats{display:flex;gap:.65rem;padding:.8rem 1.5rem;background:#fff;border-bottom:1px solid var(--bdr);flex-wrap:wrap;align-items:center;}
.stat{text-align:center;}.stat .num{font-size:1.45rem;font-weight:800;display:block;line-height:1.1;}.stat .lbl{font-size:.68rem;color:var(--dim);}
.sdiv{border-left:1px solid var(--bdr);margin:0 .15rem;height:1.8rem;}
.tabs{display:flex;background:#fff;border-bottom:2px solid var(--bdr);padding:0 1.5rem;overflow-x:auto;}
.tab{background:none;border:none;border-bottom:3px solid transparent;padding:.6rem 1rem;font-size:.87rem;cursor:pointer;color:var(--dim);margin-bottom:-2px;white-space:nowrap;display:flex;align-items:center;gap:.3rem;}
.tab:hover{color:var(--acc);}.tab.active{color:var(--acc);border-bottom-color:var(--acc);font-weight:600;}
.bdg{background:var(--acc-l);color:var(--acc);border-radius:10px;padding:.07rem .4rem;font-size:.68rem;font-weight:700;}
.panel{padding:1rem 1.5rem;}
/* tarjetas de sitio */
.scards{display:flex;flex-direction:column;gap:1rem;}
.scard{background:#fff;border-radius:10px;box-shadow:0 1px 4px rgba(0,0,0,.09);overflow:hidden;}
.shead{display:flex;}
.sthumb{width:150px;min-height:90px;flex-shrink:0;background:#eee;position:relative;}
.sthumb img{width:100%;height:100%;object-fit:cover;object-position:top;display:block;}
.no-img{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.68rem;color:#bbb;}
.sinfo{padding:.7rem 1rem;flex:1;min-width:0;display:flex;flex-direction:column;gap:.22rem;}
.stitle{font-weight:700;font-size:.92rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.surl-a{font-size:.75rem;color:var(--acc);text-decoration:none;word-break:break-all;}
.surl-a:hover{text-decoration:underline;}
.smrow{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-top:.1rem;}
.sfecha{font-size:.7rem;color:var(--dim);}
.sp{font-size:.7rem;background:#f7fafc;border:1px solid var(--bdr);border-radius:4px;padding:.05rem .3rem;color:var(--dim);}
.sp.on{background:var(--acc-l);border-color:#bee3f8;color:var(--acc);}
.slks a{font-size:.68rem;background:#f0f0f0;padding:.1rem .35rem;border-radius:3px;text-decoration:none;color:#444;margin-right:.18rem;}
.slks a:hover{background:#ddd;}
/* cuerpo del sitio */
.sbody{border-top:1px solid var(--bdr);padding:.65rem 1rem .8rem;}
.pgrid{display:flex;flex-direction:column;gap:.45rem;}
/* tarjeta de persona */
.pcard{background:#f7fafc;border:1px solid var(--bdr);border-radius:7px;padding:.5rem .75rem;}
.pcard:hover{background:var(--acc-l);border-color:#bee3f8;}
.pname{font-weight:700;font-size:.88rem;}
.pcargo{font-size:.75rem;color:var(--dim);margin-bottom:.2rem;}
.pcont{display:flex;flex-wrap:wrap;gap:.28rem;margin-top:.2rem;}
/* chips de contacto */
.ci{font-size:.75rem;text-decoration:none;border-radius:4px;padding:.1rem .38rem;display:inline-flex;align-items:center;gap:.18rem;border:1px solid var(--bdr);background:#fff;}
.ci:hover{opacity:.85;}
.ci.em{color:#2c7a7b;border-color:#b2f5ea;background:#e6fffa;}
.ci.tel{color:#553c9a;border-color:#d6bcfa;background:#faf5ff;}
/* sin asignar */
.unass{margin-top:.5rem;padding:.38rem .55rem;background:#fffbeb;border:1px solid #f6e05e;border-radius:6px;display:flex;flex-wrap:wrap;gap:.28rem;align-items:center;}
.ualbl{font-size:.7rem;font-weight:700;color:#744210;margin-right:.2rem;}
/* metadatos fallback */
.metasec{margin-top:.4rem;}
.mslbl{font-size:.72rem;font-weight:600;color:var(--dim);margin-bottom:.28rem;}
.mitem{font-size:.75rem;margin-bottom:.16rem;}
.mkey{font-weight:600;color:var(--dim);margin-right:.22rem;}
/* tablas */
.twrap{overflow-x:auto;}
.empty{text-align:center;padding:2.5rem;color:var(--dim);font-size:.9rem;}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:.83rem;}
th{background:#f7fafc;color:var(--dim);font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:.52rem 1rem;text-align:left;border-bottom:2px solid var(--bdr);white-space:nowrap;cursor:pointer;user-select:none;}
th:hover{background:#edf2f7;}
td{padding:.52rem 1rem;border-bottom:1px solid var(--bdr);vertical-align:top;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:#f7fafc;}
.turl{font-size:.75rem;color:var(--dim);max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.turl a{color:var(--acc);text-decoration:none;}.turl a:hover{text-decoration:underline;}
.sa{opacity:.45;margin-left:.2rem;}.sact .sa{opacity:1;}
.pnsm{font-weight:600;font-size:.84rem;}.pcsm{font-size:.73rem;color:var(--dim);}
</style>
</head>
<body>
<header>
  <div class="hl"><h1>&#127760; Tablero de Capturas</h1><div class="upd">Actualizado: FECHA_PH</div></div>
  <input type="search" id="q" placeholder="Buscar en todo..." oninput="filtrar()" autocomplete="off">
</header>
<div class="stats" id="sbar"></div>
<nav class="tabs">
  <button class="tab active" id="btn-sitios"    onclick="tab('sitios')"   >&#127760; Sitios     <span class="bdg" id="b-sit">0</span></button>
  <button class="tab"        id="btn-personas"  onclick="tab('personas')" >&#128101; Personas   <span class="bdg" id="b-per">0</span></button>
  <button class="tab"        id="btn-emails"    onclick="tab('emails')"   >&#128231; Emails     <span class="bdg" id="b-ema">0</span></button>
  <button class="tab"        id="btn-telefonos" onclick="tab('telefonos')">&#128222; Tel&eacute;fonos <span class="bdg" id="b-tel">0</span></button>
</nav>
<div class="panel" id="p-sitios"></div>
<div class="panel" id="p-personas"  hidden></div>
<div class="panel" id="p-emails"    hidden></div>
<div class="panel" id="p-telefonos" hidden></div>
<script>
const D=DATA_PH;
let CT='sitios';
const SS={per:{c:0,a:1},ema:{c:0,a:1},tel:{c:0,a:1}};
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function gq(){return document.getElementById('q').value.toLowerCase();}
function hostOf(u){try{return new URL(u).hostname;}catch(e){return u;}}

/* agrupa por URL; queda solo la captura m\u00e1s reciente de cada p\u00e1gina */
function sites(){
  const m={};
  D.forEach(d=>{if(!m[d.url]||d.fecha>m[d.url].fecha)m[d.url]=d;});
  return Object.values(m).sort((a,b)=>{
    const sc=x=>(x.personas||[]).length*10+(x.emails||[]).length+(x.telefonos||[]).length;
    return sc(b)-sc(a)||b.fecha.localeCompare(a.fecha);
  });
}
/* emails/tels no asignados a ning\u00fan persona (compatibilidad con formato antiguo) */
function elibres(d){
  return(d.personas||[]).some(p=>(p.emails||[]).length>0)
    ?(d.emails_sin_asignar||[]):(d.emails||[]);
}
function tlibres(d){
  return(d.personas||[]).some(p=>(p.telefonos||[]).length>0)
    ?(d.telefonos_sin_asignar||[]):(d.telefonos||[]);
}

/* --- stats --- */
function renderStats(){
  const ss=sites();
  const np=ss.reduce((s,d)=>s+(d.personas||[]).length,0);
  const ne=ss.reduce((s,d)=>s+(d.emails||[]).length,0);
  const nt=ss.reduce((s,d)=>s+(d.telefonos||[]).length,0);
  document.getElementById('sbar').innerHTML=[
    {n:ss.length,l:'Sitios'},{n:np,l:'Personas'},{n:ne,l:'Emails'},{n:nt,l:'Tel\u00e9fonos'},{n:D.length,l:'Capturas totales'}
  ].map((s,i)=>(i?'<div class="sdiv"></div>':'')+`<div class="stat"><span class="num">${s.n}</span><span class="lbl">${s.l}</span></div>`).join('');
  document.getElementById('b-sit').textContent=ss.length;
  document.getElementById('b-per').textContent=np;
  document.getElementById('b-ema').textContent=ne;
  document.getElementById('b-tel').textContent=nt;
}
/* --- tabs --- */
function tab(t){
  CT=t;
  ['sitios','personas','emails','telefonos'].forEach(x=>{
    document.getElementById('p-'+x).hidden=(x!==t);
    document.getElementById('btn-'+x).classList.toggle('active',x===t);
  });
  filtrar();
}
function filtrar(){
  if(CT==='sitios')        rSit(gq());
  else if(CT==='personas') rPer(gq());
  else if(CT==='emails')   rEma(gq());
  else                     rTel(gq());
}

/* ============ SITIOS ============ */
function rSit(sq){
  const el=document.getElementById('p-sitios');
  const list=sites().filter(d=>!sq||[
    d.url,d.titulo,
    ...(d.personas||[]).flatMap(p=>[p.nombre,p.cargo||'',...(p.emails||[]),...(p.telefonos||[])]),
    ...(d.emails||[]),...(d.telefonos||[])
  ].join(' ').toLowerCase().includes(sq));
  if(!list.length){el.innerHTML='<p class="empty">Sin resultados.</p>';return;}
  el.innerHTML='<div class="scards">'+list.map(cSite).join('')+'</div>';
}
function cSite(d){
  const np=(d.personas||[]).length,ne=(d.emails||[]).length,nt=(d.telefonos||[]).length;
  const thumb=d.tiene_imagen
    ?`<img src="capturas/${esc(d.carpeta)}/screenshot.png" loading="lazy" alt="">`
    :'<div class="no-img">sin imagen</div>';
  const fmap={imagen:'screenshot.png',pdf:'documento.pdf',html:'pagina.html',texto:'texto.txt'};
  const links=(d.formatos||[]).filter(f=>fmap[f]).map(f=>
    `<a href="capturas/${esc(d.carpeta)}/${fmap[f]}" target="_blank">${f}</a>`).join('');
  const pills=[
    np?`<span class="sp on">&#128101; ${np} persona${np!==1?'s':''}</span>`:'',
    ne?`<span class="sp on">&#128231; ${ne} email${ne!==1?'s':''}</span>`:'',
    nt?`<span class="sp on">&#128222; ${nt} tel${nt!==1?'s':''}</span>`:'',
  ].filter(Boolean).join(' ');

  /* personas con sus contactos asociados */
  const pHTML=(d.personas||[]).map(p=>{
    const pe=(p.emails||[]).map(e=>`<a class="ci em" href="mailto:${esc(e)}" title="${esc(e)}">&#128231; ${esc(e)}</a>`).join('');
    const pt=(p.telefonos||[]).map(t=>`<a class="ci tel" href="tel:${esc(t)}">&#128222; ${esc(t)}</a>`).join('');
    return `<div class="pcard">
      <div class="pname">${esc(p.nombre)}</div>
      ${p.cargo?`<div class="pcargo">${esc(p.cargo)}</div>`:''}
      ${pe||pt?`<div class="pcont">${pe}${pt}</div>`:''}
    </div>`;
  }).join('');

  /* emails/tels sin persona asignada */
  const el_=elibres(d),tl_=tlibres(d);
  const unHTML=(el_.length||tl_.length)?`<div class="unass">
    ${np?`<span class="ualbl">Sin asignar:</span>`:''}
    ${el_.map(e=>`<a class="ci em" href="mailto:${esc(e)}">&#128231; ${esc(e)}</a>`).join('')}
    ${tl_.map(t=>`<a class="ci tel" href="tel:${esc(t)}">&#128222; ${esc(t)}</a>`).join('')}
  </div>`:'';

  /* metadatos cuando no hay nada m\u00e1s */
  let metaHTML='';
  if(!np&&!el_.length&&!tl_.length){
    const m=d.metadatos||{};
    const KEYS=['description','og:description','keywords','author','og:title','og:site_name'];
    const items=KEYS.filter(k=>m[k]).map(k=>
      `<div class="mitem"><span class="mkey">${esc(k)}:</span>${esc(String(m[k]).substring(0,220))}</div>`).join('');
    if(items)metaHTML=`<div class="metasec"><div class="mslbl">&#128203; Metadatos del sitio</div>${items}</div>`;
  }

  const body=pHTML+unHTML+metaHTML;
  return `<div class="scard">
    <div class="shead">
      <div class="sthumb">${thumb}</div>
      <div class="sinfo">
        <div class="stitle">${esc(d.titulo||d.url)}</div>
        <a class="surl-a" href="${esc(d.url)}" target="_blank">${esc(d.url)}</a>
        <div class="smrow">
          <span class="sfecha">${esc(d.fecha.substring(0,10))}</span>
          ${pills}
          <span class="slks">${links}</span>
        </div>
      </div>
    </div>
    ${body?`<div class="sbody"><div class="pgrid">${body}</div></div>`:''}
  </div>`;
}

/* ============ PERSONAS (tabla) ============ */
function allPer(){
  const seen=new Set(),rows=[];
  sites().forEach(d=>(d.personas||[]).forEach(p=>{
    const k=p.nombre+'|'+d.url;
    if(!seen.has(k)){seen.add(k);rows.push({...p,url:d.url,fecha:d.fecha});}
  }));
  return rows;
}
function rPer(sq){
  const el=document.getElementById('p-personas');
  const rows=allPer().filter(r=>!sq||
    [r.nombre,r.cargo||'',r.url,...(r.emails||[]),...(r.telefonos||[])].join(' ').toLowerCase().includes(sq));
  if(!rows.length){el.innerHTML='<p class="empty">Sin personas encontradas.</p>';return;}
  const s=SS.per;
  const keys=['nombre','cargo','url','fecha'];
  const sr=[...rows].sort((a,b)=>{
    const va=String(a[keys[s.c]]||''),vb=String(b[keys[s.c]]||'');
    return s.a?va.localeCompare(vb):vb.localeCompare(va);
  });
  const th=(i,l)=>`<th onclick="srt('per',${i})" class="${s.c===i?'sact':''}">${l}<span class="sa">${s.c===i?(s.a?'\u2191':'\u2193'):'\u2195'}</span></th>`;
  el.innerHTML=`<div class="twrap"><table><thead><tr>
    ${th(0,'Nombre / Cargo')}${th(1,'Emails')}${th(2,'Tel\u00e9fonos')}${th(3,'Sitio')}${th(4,'Fecha')}
  </tr></thead><tbody>`+sr.map(r=>`<tr>
    <td><div class="pnsm">${esc(r.nombre)}</div>${r.cargo?`<div class="pcsm">${esc(r.cargo)}</div>`:''}</td>
    <td>${(r.emails||[]).map(e=>`<a class="ci em" href="mailto:${esc(e)}" style="margin:.07rem;display:inline-flex">&#128231; ${esc(e)}</a>`).join('')}</td>
    <td>${(r.telefonos||[]).map(t=>`<a class="ci tel" href="tel:${esc(t)}" style="margin:.07rem;display:inline-flex">&#128222; ${esc(t)}</a>`).join('')}</td>
    <td class="turl"><a href="${esc(r.url)}" target="_blank" title="${esc(r.url)}">${esc(hostOf(r.url))}</a></td>
    <td>${esc(r.fecha.substring(0,10))}</td>
  </tr>`).join('')+'</tbody></table></div>';
}

/* ============ EMAILS ============ */
function allEma(){
  const seen=new Set(),rows=[];
  sites().forEach(d=>{
    (d.personas||[]).forEach(p=>(p.emails||[]).forEach(e=>{
      const k=e+'|'+d.url;
      if(!seen.has(k)){seen.add(k);rows.push({email:e,persona:p.nombre,cargo:p.cargo||'',url:d.url,fecha:d.fecha});}
    }));
    elibres(d).forEach(e=>{
      const k=e+'|'+d.url;
      if(!seen.has(k)){seen.add(k);rows.push({email:e,persona:'',cargo:'',url:d.url,fecha:d.fecha});}
    });
  });
  return rows;
}
function rEma(sq){
  const el=document.getElementById('p-emails');
  const rows=allEma().filter(r=>!sq||[r.email,r.persona,r.url].join(' ').toLowerCase().includes(sq));
  if(!rows.length){el.innerHTML='<p class="empty">Sin emails encontrados.</p>';return;}
  const s=SS.ema;const keys=['email','persona','url','fecha'];
  const sr=[...rows].sort((a,b)=>{const va=String(a[keys[s.c]]||''),vb=String(b[keys[s.c]]||'');return s.a?va.localeCompare(vb):vb.localeCompare(va);});
  const th=(i,l)=>`<th onclick="srt('ema',${i})" class="${s.c===i?'sact':''}">${l}<span class="sa">${s.c===i?(s.a?'\u2191':'\u2193'):'\u2195'}</span></th>`;
  el.innerHTML=`<div class="twrap"><table><thead><tr>${th(0,'Email')}${th(1,'Pertenece a')}${th(2,'Sitio')}${th(3,'Fecha')}</tr></thead><tbody>`+
    sr.map(r=>`<tr>
      <td><a class="ci em" href="mailto:${esc(r.email)}">&#128231; ${esc(r.email)}</a></td>
      <td>${r.persona?`<div class="pnsm">${esc(r.persona)}</div>${r.cargo?`<div class="pcsm">${esc(r.cargo)}</div>`:''}`:'<span style="color:var(--dim);font-size:.75rem">sin asignar</span>'}</td>
      <td class="turl"><a href="${esc(r.url)}" target="_blank" title="${esc(r.url)}">${esc(hostOf(r.url))}</a></td>
      <td>${esc(r.fecha.substring(0,10))}</td>
    </tr>`).join('')+'</tbody></table></div>';
}

/* ============ TELÉFONOS ============ */
function allTel(){
  const seen=new Set(),rows=[];
  sites().forEach(d=>{
    (d.personas||[]).forEach(p=>(p.telefonos||[]).forEach(t=>{
      const k=t+'|'+d.url;
      if(!seen.has(k)){seen.add(k);rows.push({telefono:t,persona:p.nombre,cargo:p.cargo||'',url:d.url,fecha:d.fecha});}
    }));
    tlibres(d).forEach(t=>{
      const k=t+'|'+d.url;
      if(!seen.has(k)){seen.add(k);rows.push({telefono:t,persona:'',cargo:'',url:d.url,fecha:d.fecha});}
    });
  });
  return rows;
}
function rTel(sq){
  const el=document.getElementById('p-telefonos');
  const rows=allTel().filter(r=>!sq||[r.telefono,r.persona,r.url].join(' ').toLowerCase().includes(sq));
  if(!rows.length){el.innerHTML='<p class="empty">Sin tel\u00e9fonos encontrados.</p>';return;}
  const s=SS.tel;const keys=['telefono','persona','url','fecha'];
  const sr=[...rows].sort((a,b)=>{const va=String(a[keys[s.c]]||''),vb=String(b[keys[s.c]]||'');return s.a?va.localeCompare(vb):vb.localeCompare(va);});
  const th=(i,l)=>`<th onclick="srt('tel',${i})" class="${s.c===i?'sact':''}">${l}<span class="sa">${s.c===i?(s.a?'\u2191':'\u2193'):'\u2195'}</span></th>`;
  el.innerHTML=`<div class="twrap"><table><thead><tr>${th(0,'Tel\u00e9fono')}${th(1,'Pertenece a')}${th(2,'Sitio')}${th(3,'Fecha')}</tr></thead><tbody>`+
    sr.map(r=>`<tr>
      <td><a class="ci tel" href="tel:${esc(r.telefono)}">&#128222; ${esc(r.telefono)}</a></td>
      <td>${r.persona?`<div class="pnsm">${esc(r.persona)}</div>${r.cargo?`<div class="pcsm">${esc(r.cargo)}</div>`:''}`:'<span style="color:var(--dim);font-size:.75rem">sin asignar</span>'}</td>
      <td class="turl"><a href="${esc(r.url)}" target="_blank" title="${esc(r.url)}">${esc(hostOf(r.url))}</a></td>
      <td>${esc(r.fecha.substring(0,10))}</td>
    </tr>`).join('')+'</tbody></table></div>';
}

function srt(t,c){const s=SS[t];if(s.c===c)s.a=!s.a;else{s.c=c;s.a=true;}filtrar();}
renderStats();rSit('');
</script>
</body>
</html>
"""


def generar_tablero() -> None:
    """Genera tablero.html con visualización completa: capturas + análisis extraído."""
    capturas = []
    for nombre in sorted(os.listdir(CARPETA_SALIDA)):
        ruta_abs = os.path.join(CARPETA_SALIDA, nombre)
        if not os.path.isdir(ruta_abs):
            continue
        info = _leer_info(ruta_abs)
        if not info.get("URL"):
            continue

        datos_json = {}
        try:
            with open(os.path.join(ruta_abs, "datos.json"), "r", encoding="utf-8") as f:
                datos_json = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        archivos = set(os.listdir(ruta_abs))
        formatos = [fmt for fmt, (archivo, _) in _ARCHIVOS_FORMATO.items() if archivo in archivos]

        capturas.append({
            "carpeta":               nombre,
            "url":                   info.get("URL", ""),
            "titulo":                info.get("Titulo", ""),
            "fecha":                 info.get("Fecha", ""),
            "formatos":              formatos,
            "tiene_imagen":          "screenshot.png" in archivos,
            "emails":                datos_json.get("emails",                []),
            "telefonos":             datos_json.get("telefonos",             []),
            "personas":              datos_json.get("personas",              []),
            "emails_sin_asignar":    datos_json.get("emails_sin_asignar",    []),
            "telefonos_sin_asignar": datos_json.get("telefonos_sin_asignar", []),
            "metadatos":             datos_json.get("metadatos",             {}),
        })

    capturas.sort(key=lambda x: x["fecha"], reverse=True)
    data_json = json.dumps(capturas, ensure_ascii=False)

    html = (_TABLERO_HTML
            .replace("DATA_PH",  data_json)
            .replace("FECHA_PH", datetime.now().strftime(FMT_LEGIBLE)))

    with open(TABLERO_SALIDA, "w", encoding="utf-8") as f:
        f.write(html)
    console.print(f"  [cyan]📊 Tablero:[/cyan] tablero.html  ({len(capturas)} capturas)")


# ── Backup automático ─────────────────────────────────────────────

def comprimir_sesion(carpetas_nuevas: set) -> None:
    """ZIP incremental en backups/ con solo las carpetas nuevas de esta sesión."""
    if not carpetas_nuevas:
        console.print("  [dim]ℹ️  Sin capturas nuevas — no se genera backup.[/dim]")
        return

    os.makedirs(CARPETA_BACKUPS, exist_ok=True)
    n           = len(carpetas_nuevas)
    nombre_zip  = f"backup_{n}sitios_{datetime.now().strftime(FMT_ARCHIVO)}.zip"
    destino_zip = os.path.join(CARPETA_BACKUPS, nombre_zip)

    with zipfile.ZipFile(destino_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for carpeta_nombre in sorted(carpetas_nuevas):
            carpeta_abs = os.path.join(CARPETA_SALIDA, carpeta_nombre)
            for raiz, _, archivos in os.walk(carpeta_abs):
                for archivo in archivos:
                    ruta_abs = os.path.join(raiz, archivo)
                    zf.write(ruta_abs, os.path.relpath(ruta_abs, BASE_DIR))
        reporte = os.path.join(CARPETA_SALIDA, "reporte.html")
        try:
            zf.write(reporte, os.path.relpath(reporte, BASE_DIR))
        except FileNotFoundError:
            pass
        # extraccion/ (CSVs, JSONL) y tablero
        for raiz, _, archivos in os.walk(CARPETA_EXTRACCION):
            for archivo in archivos:
                ruta_abs = os.path.join(raiz, archivo)
                zf.write(ruta_abs, os.path.relpath(ruta_abs, BASE_DIR))
        try:
            zf.write(TABLERO_SALIDA, os.path.relpath(TABLERO_SALIDA, BASE_DIR))
        except FileNotFoundError:
            pass

    mb = os.path.getsize(destino_zip) / (1024 * 1024)
    console.print(f"  [cyan]🗜️  Backup:[/cyan] backups/{nombre_zip}  ({mb:.1f} MB, {n} sitio{'s' if n != 1 else ''})")
    _limpiar_backups_antiguos()


def _limpiar_backups_antiguos() -> None:
    if MAX_BACKUPS <= 0:
        return
    zips = sorted(f for f in os.listdir(CARPETA_BACKUPS) if f.endswith(".zip"))
    while len(zips) > MAX_BACKUPS:
        a_eliminar = zips.pop(0)
        os.remove(os.path.join(CARPETA_BACKUPS, a_eliminar))
        console.print(f"  [dim]🗑️  Backup antiguo eliminado: {a_eliminar}  (límite: {MAX_BACKUPS})[/dim]")


# ── CLI ───────────────────────────────────────────────────────────

def parsear_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capturador de páginas web — genera imagen, PDF, HTML y texto",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--urls",           default=ARCHIVO_URLS, metavar="ARCHIVO",
                        help="Archivo con las URLs (default: urls.txt)")
    parser.add_argument("--formatos",       default=None,         metavar="LISTA",
                        help="Formatos: imagen,pdf,html,texto  (default: todos)")
    parser.add_argument("--forzar",         action="store_true",
                        help="Ignorar historial y re-capturar todo")
    parser.add_argument("--sesion",         default=None,         metavar="ARCHIVO",
                        help="Cargar cookies desde sesiones/ARCHIVO (login persistente)")
    parser.add_argument("--guardar-sesion", default=None,         metavar="ARCHIVO",
                        help="Guardar cookies en sesiones/ARCHIVO al finalizar")
    parser.add_argument("--verificar",      action="store_true",
                        help="Verificar integridad de todas las capturas guardadas")
    parser.add_argument("--solo-analizar",  action="store_true",
                        help="Re-analizar capturas ya guardadas sin volver a capturar")
    return parser.parse_args()


# ── Re-análisis de capturas existentes ───────────────────────────

def solo_analizar_capturas() -> None:
    """Re-analiza todas las carpetas de capturas que ya tienen pagina.html o texto.txt.
    Útil para actualizar datos.json si se mejoró el extractor sin volver a capturar.
    """
    console.print("\n[bold]🔎 Re-analizando capturas existentes...[/bold]\n")
    total = procesadas = sin_html = 0

    for nombre in sorted(os.listdir(CARPETA_SALIDA)):
        ruta_abs = os.path.join(CARPETA_SALIDA, nombre)
        if not os.path.isdir(ruta_abs):
            continue

        info      = _leer_info(ruta_abs)
        url       = info.get("URL", "")
        titulo    = info.get("Titulo", "")
        if not url:
            continue
        total += 1

        # Preferir HTML; si no existe usar texto.txt
        ruta_html  = os.path.join(ruta_abs, "pagina.html")
        ruta_texto = os.path.join(ruta_abs, "texto.txt")

        html_fuente   = ""
        texto_visible = ""

        try:
            with open(ruta_html, "r", encoding="utf-8") as f:
                html_fuente = f.read()
            soup = BeautifulSoup(html_fuente, "html.parser")
            texto_visible = soup.get_text(separator="\n", strip=True)
        except FileNotFoundError:
            try:
                with open(ruta_texto, "r", encoding="utf-8") as f:
                    texto_visible = f.read()
            except FileNotFoundError:
                sin_html += 1
                continue

        datos = _ext.analizar(url, html_fuente, texto_visible, REGION_TELEFONOS)
        _ext.guardar_datos(ruta_abs, datos)
        _ext.consolidar(CARPETA_EXTRACCION, datos)

        n_p = len(datos.get("personas",  []))
        n_e = len(datos.get("emails",    []))
        n_t = len(datos.get("telefonos", []))
        console.print(
            f"  [cyan]{nombre}[/cyan] — "
            f"[magenta]{n_p}p · {n_e}e · {n_t}t[/magenta]"
        )
        procesadas += 1

    console.print(f"\n[bold]Listo:[/bold] {procesadas} analizadas, {sin_html} sin HTML/texto, {total} total")
    console.print(f"[dim]Resultados en extraccion/[/dim]")
    generar_tablero()


# ── Señales ───────────────────────────────────────────────────────

def manejador_interrupcion(sig, frame):
    console.print("\n\n[yellow]⚠️  Interrumpido. Guardando historial parcial...[/yellow]")
    guardar_historial(_historial_global)
    console.print("[green]✅  Historial guardado. Saliendo.[/green]")
    sys.exit(0)


# ── Procesador de URL individual (async) ─────────────────────────

async def _procesar_una_url(
    context,
    url: str,
    formatos: dict,
    forzar: bool,
    resultados: list,
    carpetas_nuevas: set,
    contadores: dict,
) -> None:
    global _historial_global

    dominio = urlparse(url).netloc
    titulo  = ""

    async with _sem:
        async with _get_domain_lock(dominio):
            for intento in range(1 + REINTENTOS):
                page = None
                try:
                    page = await context.new_page()
                    await navegar_con_fallback(page, url)

                    data       = await page.evaluate("() => ({ titulo: document.title, texto: document.body.innerText })")
                    titulo     = data["titulo"]
                    hash_nuevo = calcular_hash(data["texto"])

                    async with _historial_lock:
                        sin_cambios = not forzar and _historial_global.get(url) == hash_nuevo

                    if sin_cambios:
                        console.print(f"  [dim]⏭️  Sin cambios — {dominio}[/dim]")
                        contadores["saltadas"] += 1
                        resultados.append({"url": url, "titulo": titulo, "estado": "saltada"})
                        # Analizar si la captura anterior nunca tuvo análisis de contenido
                        if ANALIZAR_CONTENIDO:
                            ruta_previa = _buscar_ultima_captura_sin_analisis(url)
                            if ruta_previa:
                                console.print(f"    [magenta]🔎 Analizando captura previa ({os.path.basename(ruta_previa)})...[/magenta]")
                                try:
                                    datos = await asyncio.to_thread(_analizar_existente, ruta_previa, url)
                                    if datos:
                                        _ext.guardar_datos(ruta_previa, datos)
                                        _ext.consolidar(CARPETA_EXTRACCION, datos)
                                        n_p = len(datos.get("personas",  []))
                                        n_e = len(datos.get("emails",    []))
                                        n_t = len(datos.get("telefonos", []))
                                        contadores["personas"]  = contadores.get("personas",  0) + n_p
                                        contadores["emails"]    = contadores.get("emails",    0) + n_e
                                        contadores["telefonos"] = contadores.get("telefonos", 0) + n_t
                                        console.print(f"      [magenta]{n_p} persona(s) · {n_e} email(s) · {n_t} tel(s)[/magenta]")
                                except Exception as ex:
                                    console.print(f"      [yellow]⚠️  Análisis previo fallido: {ex}[/yellow]")
                    else:
                        ruta = os.path.join(CARPETA_SALIDA, nombre_carpeta(url))
                        html_fuente, texto_visible = await guardar_capturas(
                            page, url, ruta, formatos, titulo
                        )

                        async with _historial_lock:
                            _historial_global[url] = hash_nuevo
                            guardar_historial(_historial_global)

                        carpeta_nombre = os.path.basename(ruta)
                        carpetas_nuevas.add(carpeta_nombre)
                        contadores["capturadas"] += 1
                        resultados.append({
                            "url":      url,
                            "titulo":   titulo,
                            "estado":   "capturada",
                            "carpeta":  carpeta_nombre,
                            "formatos": [k for k, v in formatos.items() if v],
                        })
                        console.print(f"  [green]📁 capturas/{carpeta_nombre}/[/green]")

                        # ── Análisis de contenido ──────────────────────────
                        if ANALIZAR_CONTENIDO:
                            try:
                                # spaCy es CPU-bound — corre en hilo separado
                                # para no bloquear el event loop async
                                datos = await asyncio.to_thread(
                                    _ext.analizar,
                                    url, html_fuente, texto_visible, REGION_TELEFONOS,
                                )
                                _ext.guardar_datos(ruta, datos)
                                _ext.consolidar(CARPETA_EXTRACCION, datos)

                                n_p = len(datos.get("personas",  []))
                                n_e = len(datos.get("emails",    []))
                                n_t = len(datos.get("telefonos", []))
                                contadores["personas"]  = contadores.get("personas",  0) + n_p
                                contadores["emails"]    = contadores.get("emails",    0) + n_e
                                contadores["telefonos"] = contadores.get("telefonos", 0) + n_t
                                console.print(
                                    f"      [magenta]🔎 Análisis: "
                                    f"{n_p} persona{'s' if n_p != 1 else ''} · "
                                    f"{n_e} email{'s' if n_e != 1 else ''} · "
                                    f"{n_t} teléfono{'s' if n_t != 1 else ''}[/magenta]"
                                )
                            except Exception as ex:
                                console.print(f"      [yellow]⚠️  Análisis fallido: {ex}[/yellow]")

                    await asyncio.sleep(DOMAIN_DELAY_S)
                    break

                except Exception as e:
                    if intento < REINTENTOS:
                        espera = 3 * (intento + 1)
                        console.print(f"  [yellow]⚠️  {dominio} — intento {intento + 1} fallido, reintentando en {espera}s...[/yellow]")
                        await asyncio.sleep(espera)
                    else:
                        msg = str(e)
                        console.print(f"  [red]❌ {dominio} — error tras {1 + REINTENTOS} intentos: {msg[:120]}[/red]")
                        registrar_error(url, msg)
                        contadores["errores"] += 1
                        resultados.append({"url": url, "titulo": titulo, "estado": "error", "error_msg": msg})

                finally:
                    if page and not page.is_closed():
                        await page.close()


# ── Proceso principal (async) ─────────────────────────────────────

async def procesar_urls_async(
    archivo_urls,
    formatos,
    forzar,
    archivo_sesion=None,
    guardar_sesion=None,
):
    global _historial_global, _sem, _historial_lock, _domain_locks

    try:
        with open(archivo_urls, "r", encoding="utf-8") as f:
            lineas = [s for l in f if (s := l.strip()) and not s.startswith("#")]
    except FileNotFoundError:
        console.print(f"[red]❌  No se encuentra el archivo '{archivo_urls}'[/red]")
        return []

    if not lineas:
        console.print(f"[yellow]⚠️  El archivo '{archivo_urls}' está vacío.[/yellow]")
        return []

    urls_validas = []
    for url in lineas:
        ok, msg = validar_url(url)
        if ok:
            urls_validas.append(url)
        else:
            console.print(f"[yellow]⚠️  URL ignorada — {msg}[/yellow]\n   → {url}")
            registrar_error(url, f"URL inválida: {msg}")

    if not urls_validas:
        console.print("[red]❌  No hay URLs válidas para procesar.[/red]")
        return []

    os.makedirs(CARPETA_SALIDA, exist_ok=True)
    _historial_global = {} if forzar else cargar_historial()

    # Inicializar primitivas async para este run
    _sem            = asyncio.Semaphore(MAX_CONCURRENT)
    _historial_lock = asyncio.Lock()
    _domain_locks.clear()

    formatos_activos = [k for k, v in formatos.items() if v]

    cfg = Table.grid(padding=(0, 2))
    cfg.add_column(style="bold cyan", no_wrap=True)
    cfg.add_column()
    cfg.add_row("Formatos",     ", ".join(formatos_activos))
    cfg.add_row("URLs",         str(len(urls_validas)))
    cfg.add_row("Concurrencia", f"{MAX_CONCURRENT} simultáneas · {DOMAIN_DELAY_S}s/dominio")
    cfg.add_row("Capturas →",   "capturas/")
    cfg.add_row("Backups  →",   "backups/")
    if forzar:
        cfg.add_row("Modo", "[bold red]FORZAR (ignora historial)[/bold red]")
    if archivo_sesion:
        cfg.add_row("Sesión", os.path.basename(_ruta_sesion(archivo_sesion)))
    console.print(Panel(cfg, title="[bold]Capturador de Páginas Web[/bold]", expand=False))

    resultados      = []
    carpetas_nuevas = set()
    contadores      = {"capturadas": 0, "saltadas": 0, "errores": 0}
    activas         = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport=VIEWPORT)

        if archivo_sesion:
            await cargar_cookies(context, archivo_sesion)

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            tarea = progress.add_task("Iniciando...", total=len(urls_validas))

            async def _wrapper(url: str) -> None:
                nonlocal activas
                activas += 1
                dominio = urlparse(url).netloc
                progress.update(
                    tarea,
                    description=(
                        f"[cyan]{dominio}[/cyan] "
                        f"[dim]({activas} activa{'s' if activas != 1 else ''})[/dim]"
                    ),
                )
                try:
                    await _procesar_una_url(
                        context, url, formatos, forzar,
                        resultados, carpetas_nuevas, contadores,
                    )
                finally:
                    activas -= 1
                    progress.advance(tarea)

            await asyncio.gather(*[_wrapper(url) for url in urls_validas])

        if guardar_sesion:
            await guardar_cookies(context, guardar_sesion)

        await browser.close()

    # Tabla resumen
    resumen = Table(title="Resumen de sesión", box=box.ROUNDED, show_header=False)
    resumen.add_column(style="bold", no_wrap=True)
    resumen.add_column(justify="right")
    resumen.add_row("[green]Capturadas[/green]",              str(contadores["capturadas"]))
    resumen.add_row("[yellow]Saltadas (sin cambios)[/yellow]", str(contadores["saltadas"]))
    resumen.add_row("[red]Errores[/red]",                     str(contadores["errores"]))
    if contadores["errores"]:
        resumen.add_row("[dim]Ver detalles en[/dim]", "errores.log")
    if ANALIZAR_CONTENIDO and contadores.get("capturadas", 0):
        resumen.add_row("", "")
        resumen.add_row("[magenta]Personas encontradas[/magenta]",  str(contadores.get("personas",  0)))
        resumen.add_row("[magenta]Emails encontrados[/magenta]",    str(contadores.get("emails",    0)))
        resumen.add_row("[magenta]Teléfonos encontrados[/magenta]", str(contadores.get("telefonos", 0)))
        resumen.add_row("[dim]Datos en[/dim]", "extraccion/")
    console.print(resumen)

    if GENERAR_REPORTE:
        generar_reporte(carpetas_nuevas)

    generar_tablero()
    comprimir_sesion(carpetas_nuevas)

    cap = contadores["capturadas"]
    err = contadores["errores"]
    sal = contadores["saltadas"]
    if cap:
        sufijo_err = f", {err} error{'es' if err != 1 else ''}" if err else ""
        notificar("Capturador Web — Listo",
                  f"{cap} página{'s' if cap != 1 else ''} capturada{'s' if cap != 1 else ''}{sufijo_err}")
    elif not err:
        notificar("Capturador Web — Sin cambios",
                  f"{sal} sitio{'s' if sal != 1 else ''} revisado{'s' if sal != 1 else ''}, ninguno cambió")
    else:
        notificar("Capturador Web — Errores",
                  f"{err} error{'es' if err != 1 else ''} al procesar las URLs")

    return resultados


# ── Entrada ───────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, manejador_interrupcion)
    try:
        signal.signal(signal.SIGTERM, manejador_interrupcion)
    except (OSError, AttributeError):
        pass

    args = parsear_args()

    if args.verificar:
        verificar_todas_las_capturas()
        sys.exit(0)

    if args.solo_analizar:
        solo_analizar_capturas()
        sys.exit(0)

    if args.formatos:
        nombres   = [f.strip().lower() for f in args.formatos.split(",")]
        invalidos = [n for n in nombres if n not in FORMATOS_DEFAULT]
        if invalidos:
            console.print(f"[yellow]⚠️  Formatos no reconocidos: {', '.join(invalidos)}[/yellow]")
            console.print(f"   Válidos: {', '.join(FORMATOS_DEFAULT.keys())}")
        formatos = {k: (k in nombres) for k in FORMATOS_DEFAULT}
    else:
        formatos = FORMATOS_DEFAULT

    asyncio.run(
        procesar_urls_async(
            archivo_urls=args.urls,
            formatos=formatos,
            forzar=args.forzar,
            archivo_sesion=args.sesion,
            guardar_sesion=args.guardar_sesion,
        )
    )
