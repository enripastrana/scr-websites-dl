"""
Extractor de datos estructurados desde páginas web capturadas.

Extrae:
  - Metadatos HTML (<meta>, <title>, Open Graph, JSON-LD)
  - Emails (regex validado)
  - Teléfonos (phonenumbers de Google — valida formato MX/internacional)
  - Nombres y cargos (spaCy NER es_core_news_sm + patrones de contexto)

Salida por captura : datos.json
Salida consolidada : extraccion/consolidado.jsonl
                     extraccion/personas.csv
                     extraccion/emails.csv
                     extraccion/telefonos.csv
"""

import re
import os
import csv
import json
from datetime import datetime
from bs4 import BeautifulSoup
from rich.console import Console

console = Console()

# ── Carga diferida de dependencias opcionales ─────────────────────

try:
    import phonenumbers
    from phonenumbers import PhoneNumberMatcher, PhoneNumberFormat, is_valid_number, format_number
    _PHONENUMBERS_OK = True
except ImportError:
    _PHONENUMBERS_OK = False

_nlp = None          # instancia de spaCy — carga diferida (tarda ~1s)
_SPACY_OK = None     # None = no intentado, True = listo, False = no disponible


def _cargar_spacy():
    global _nlp, _SPACY_OK
    if _SPACY_OK is not None:
        return _SPACY_OK
    try:
        import spacy
        _nlp = spacy.load("es_core_news_sm")
        _SPACY_OK = True
    except OSError:
        console.print(
            "  [yellow]⚠️  Modelo spaCy no encontrado — "
            "ejecuta: python -m spacy download es_core_news_sm[/yellow]"
        )
        _SPACY_OK = False
    except ImportError:
        _SPACY_OK = False
    return _SPACY_OK


# ── Catálogo de cargos (español — Mexico, España, LATAM) ──────────

TITULOS_CARGO = [
    # Dirección
    "Director General", "Director Ejecutivo", "Director Comercial",
    "Director de Operaciones", "Director de Finanzas", "Director Académico",
    "Director", "Directora", "Subdirector", "Subdirectora",
    # Gerencia
    "Gerente General", "Gerente Comercial", "Gerente de Proyectos",
    "Gerente", "Gerenta", "Subgerente",
    # Coordinación / Jefatura
    "Coordinador General", "Coordinador", "Coordinadora",
    "Jefe de Departamento", "Jefe de Área", "Jefe", "Jefa",
    "Subjefe", "Subjefa",
    # Gobierno / Institucional
    "Presidente", "Presidenta", "Vicepresidente", "Vicepresidenta",
    "Secretario de Estado", "Secretario General", "Secretario", "Secretaria",
    "Subsecretario", "Subsecretaria",
    "Tesorero", "Tesorera", "Contralor", "Contralora",
    "Delegado", "Delegada", "Regidor", "Regidora",
    "Alcalde", "Alcaldesa", "Gobernador", "Gobernadora",
    "Diputado", "Diputada", "Senador", "Senadora",
    # Academia
    "Rector", "Rectora", "Vicerrector", "Vicerrectora",
    "Decano", "Decana", "Catedrático", "Catedrática",
    "Profesor Investigador", "Profesor", "Profesora",
    # Profesiones (prefijos)
    "Licenciado", "Licenciada", "Lic.",
    "Ingeniero", "Ingeniera", "Ing.",
    "Doctor en", "Doctor", "Doctora", "Dr.", "Dra.",
    "Arquitecto", "Arquitecta", "Arq.",
    "Contador Público", "Contador", "Contadora", "C.P.",
    "Maestro", "Maestra", "M.C.", "Mtro.", "Mtra.",
    "Abogado", "Abogada",
]

# Ordenados de mayor a menor longitud para que el match greedy coincida primero
TITULOS_CARGO.sort(key=len, reverse=True)

# ── Regex de email ────────────────────────────────────────────────

_RE_EMAIL = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)

# Extensiones de archivo que generan falsos positivos en emails
_EXTS_FALSE_POSITIVE = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".css", ".js", ".php", ".html", ".htm",
    ".pdf", ".doc", ".docx",
}


# ══════════════════════════════════════════════════════════════════
#  EXTRACTORES
# ══════════════════════════════════════════════════════════════════

def extraer_metadatos(html: str) -> dict:
    """Extrae metadatos de <meta>, <title>, Open Graph y JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}

    # <title>
    if soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()

    # <meta name|property|http-equiv content>
    for tag in soup.find_all("meta"):
        clave = (
            tag.get("name")
            or tag.get("property")
            or tag.get("http-equiv", "")
        )
        contenido = tag.get("content", "")
        if clave and contenido:
            meta[clave.lower().strip()] = contenido.strip()

    # Schema.org JSON-LD (primer bloque encontrado)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            datos = json.loads(script.string or "")
            if isinstance(datos, dict):
                meta["jsonld"] = datos
                break
            if isinstance(datos, list) and datos:
                meta["jsonld"] = datos[0]
                break
        except (json.JSONDecodeError, TypeError):
            pass

    return meta


def extraer_emails(texto: str) -> list:
    """Extrae emails únicos y filtra extensiones de archivo comunes."""
    encontrados = _RE_EMAIL.findall(texto)
    return sorted({
        e.lower() for e in encontrados
        if not any(e.lower().endswith(ext) for ext in _EXTS_FALSE_POSITIVE)
    })


def extraer_telefonos(texto: str, region: str = "MX") -> list:
    """Extrae teléfonos válidos usando phonenumbers (Google libphonenumber).
    Fallback a regex si la librería no está disponible.
    """
    if _PHONENUMBERS_OK:
        return _telefonos_phonenumbers(texto, region)
    return _telefonos_regex(texto)


def _telefonos_phonenumbers(texto: str, region: str) -> list:
    encontrados = set()
    try:
        for match in PhoneNumberMatcher(texto, region):
            if is_valid_number(match.number):
                encontrados.add(
                    format_number(match.number, PhoneNumberFormat.INTERNATIONAL)
                )
    except Exception:
        pass
    return sorted(encontrados)


def _telefonos_regex(texto: str) -> list:
    """Regex conservador para números de 10 dígitos tipo México."""
    patron = re.compile(
        r'(?<!\d)'
        r'(?:\+?52[\s\-.]?)?'          # código de país opcional
        r'(?:\(?[2-9]\d{2}\)?[\s\-.])'  # área (3 dígitos)
        r'\d{3}[\s\-.]\d{4}'            # local (7 dígitos)
        r'(?!\d)'
    )
    return sorted({m.group().strip() for m in patron.finditer(texto)})


def extraer_personas(texto: str) -> list:
    """Extrae personas y cargos via spaCy NER + búsqueda de contexto.
    Fallback a regex heurístico si spaCy no está disponible.
    """
    if _cargar_spacy():
        return _personas_spacy(texto)
    return _personas_regex(texto)


def _personas_spacy(texto: str) -> list:
    # spaCy procesa hasta nlp.max_length caracteres; bloques más largos se truncan
    limite = _nlp.max_length - 1
    doc = _nlp(texto[:limite])

    vistas = set()
    resultado = []

    for ent in doc.ents:
        if ent.label_ != "PER":
            continue
        nombre = _limpiar_nombre(ent.text)
        if not nombre or nombre in vistas:
            continue
        vistas.add(nombre)
        cargo = _cargo_en_contexto(doc, ent)
        resultado.append({
            "nombre": nombre,
            "cargo":  cargo,
            "metodo": "spacy",
        })

    return resultado


def _limpiar_nombre(texto: str) -> str:
    nombre = " ".join(texto.split())   # normaliza espacios internos
    if len(nombre) < 4:
        return ""
    if nombre.isupper():               # probable encabezado de tabla
        return ""
    if re.search(r'[|<>{}/\\@\d]', nombre):
        return ""
    return nombre


def _cargo_en_contexto(doc, entidad, ventana_tokens: int = 15) -> str:
    """Busca el cargo más específico en los tokens adyacentes a la entidad."""
    inicio = max(0, entidad.start - ventana_tokens)
    fin    = min(len(doc), entidad.end + ventana_tokens)
    contexto = doc[inicio:fin].text

    for titulo in TITULOS_CARGO:          # ya ordenados de mayor a menor longitud
        if re.search(re.escape(titulo), contexto, re.IGNORECASE):
            return titulo
    return ""


def _personas_regex(texto: str) -> list:
    """Detecta patrones 'Cargo: Nombre Apellido' cuando spaCy no está disponible."""
    titulos_re = "|".join(re.escape(t) for t in TITULOS_CARGO)
    patron = re.compile(
        rf'({titulos_re})[:\s]+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
        rf'(?:\s+(?:de\s+)?[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+){{1,3}})',
        re.UNICODE,
    )
    vistos = set()
    resultado = []
    for m in patron.finditer(texto):
        cargo  = m.group(1).strip()
        nombre = m.group(2).strip()
        clave  = f"{nombre}|{cargo}"
        if clave not in vistos and len(nombre) >= 4:
            vistos.add(clave)
            resultado.append({"nombre": nombre, "cargo": cargo, "metodo": "regex"})
    return resultado


# ══════════════════════════════════════════════════════════════════
#  RELACIONES PERSONA ↔ CONTACTO
# ══════════════════════════════════════════════════════════════════

_VENTANA_RELACION = 400   # caracteres alrededor del nombre para buscar su email/teléfono


def _relacionar(texto: str, personas: list, emails: list, telefonos: list) -> tuple:
    """Asocia emails y teléfonos a la persona más cercana en el texto por proximidad.

    En páginas de directorio los datos de una persona (cargo, email, teléfono)
    suelen aparecer en ±1-2 filas o en la misma celda de tabla, lo que equivale
    a unos pocos cientos de caracteres alrededor del nombre.

    Retorna (personas_enriquecidas, emails_sin_asignar, telefonos_sin_asignar).
    Añade campos 'emails' y 'telefonos' a cada persona.
    """
    asig_emails = set()
    asig_tels   = set()
    texto_lower = texto.lower()

    for persona in personas:
        nom_lower = persona["nombre"].lower()
        posiciones = [m.start() for m in re.finditer(re.escape(nom_lower), texto_lower)]

        p_emails: list = []
        p_tels:   list = []

        for pos in posiciones:
            ini = max(0, pos - _VENTANA_RELACION)
            fin = min(len(texto), pos + len(persona["nombre"]) + _VENTANA_RELACION)
            ventana_l = texto[ini:fin].lower()
            ventana   = texto[ini:fin]

            for email in emails:
                if email not in asig_emails and email.lower() in ventana_l:
                    p_emails.append(email)
                    asig_emails.add(email)

            for tel in telefonos:
                if tel not in asig_tels and tel in ventana:
                    p_tels.append(tel)
                    asig_tels.add(tel)

        persona["emails"]    = list(dict.fromkeys(p_emails))
        persona["telefonos"] = list(dict.fromkeys(p_tels))

    emails_lib = [e for e in emails if e not in asig_emails]
    tels_lib   = [t for t in telefonos if t not in asig_tels]
    return personas, emails_lib, tels_lib


# ══════════════════════════════════════════════════════════════════
#  PIPELINE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def analizar(url: str, html: str, texto_visible: str, region: str = "MX") -> dict:
    """Extrae todos los datos, relaciona contactos con personas y retorna el consolidado."""
    metadatos     = extraer_metadatos(html)
    emails_todos  = extraer_emails(texto_visible)
    tels_todos    = extraer_telefonos(texto_visible, region)
    personas_base = extraer_personas(texto_visible)

    personas, emails_lib, tels_lib = _relacionar(
        texto_visible, personas_base, emails_todos, tels_todos
    )

    return {
        "url":                   url,
        "fecha":                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metadatos":             metadatos,
        "personas":              personas,
        "emails_sin_asignar":    emails_lib,
        "telefonos_sin_asignar": tels_lib,
        # Listas planas — para CSV y compatibilidad con capturas anteriores
        "emails":                emails_todos,
        "telefonos":             tels_todos,
    }


# ══════════════════════════════════════════════════════════════════
#  PERSISTENCIA
# ══════════════════════════════════════════════════════════════════

def guardar_datos(ruta_carpeta: str, datos: dict) -> None:
    """Guarda datos.json en la carpeta de la captura."""
    with open(os.path.join(ruta_carpeta, "datos.json"), "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)


def consolidar(carpeta_extraccion: str, datos: dict) -> None:
    """Agrega los datos al consolidado JSONL y actualiza los CSVs."""
    os.makedirs(carpeta_extraccion, exist_ok=True)

    # ── JSONL: una línea por captura (append-only, eficiente) ──────
    with open(os.path.join(carpeta_extraccion, "consolidado.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(datos, ensure_ascii=False) + "\n")

    # ── CSV personas ───────────────────────────────────────────────
    _csv_append(
        ruta=os.path.join(carpeta_extraccion, "personas.csv"),
        campos=["fecha", "url", "nombre", "cargo", "emails", "telefonos", "metodo"],
        filas=[
            {
                "fecha":     datos["fecha"],
                "url":       datos["url"],
                "nombre":    p.get("nombre", ""),
                "cargo":     p.get("cargo", ""),
                "emails":    "|".join(p.get("emails",    [])),
                "telefonos": "|".join(p.get("telefonos", [])),
                "metodo":    p.get("metodo", ""),
            }
            for p in datos.get("personas", [])
        ],
    )

    # ── CSV emails ─────────────────────────────────────────────────
    _csv_append(
        ruta=os.path.join(carpeta_extraccion, "emails.csv"),
        campos=["fecha", "url", "email"],
        filas=[
            {"fecha": datos["fecha"], "url": datos["url"], "email": e}
            for e in datos.get("emails", [])
        ],
    )

    # ── CSV teléfonos ──────────────────────────────────────────────
    _csv_append(
        ruta=os.path.join(carpeta_extraccion, "telefonos.csv"),
        campos=["fecha", "url", "telefono"],
        filas=[
            {"fecha": datos["fecha"], "url": datos["url"], "telefono": t}
            for t in datos.get("telefonos", [])
        ],
    )


def _csv_append(ruta: str, campos: list, filas: list) -> None:
    """Agrega filas a un CSV, escribiendo la cabecera solo si el archivo es nuevo."""
    if not filas:
        return
    nuevo = not os.path.exists(ruta)
    with open(ruta, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        if nuevo:
            writer.writeheader()
        writer.writerows(filas)
