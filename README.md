# 🌐 Capturador de Páginas Web

Herramienta de línea de comandos para capturar páginas web de forma automática.
Lee una lista de URLs desde un archivo `.txt` y guarda cada página en **imagen (PNG), PDF, HTML y texto plano**.

Detecta cambios mediante SHA-256: si una página no cambió desde la última captura, la omite.
Genera un reporte HTML acumulativo y backups ZIP automáticos por sesión.
Extrae y consolida **nombres, cargos, teléfonos, emails y metadatos** de cada página capturada.

---

## ¿Qué hace?

| Función | Descripción |
|---|---|
| 📸 Captura múltiple | Screenshot PNG completo, PDF, HTML renderizado y texto extraído |
| 🔍 Detección de cambios | Compara SHA-256 del cuerpo — omite páginas sin modificaciones |
| 🔄 Scroll automático | Desplaza la página para disparar lazy loading antes de capturar |
| 📜 Reporte HTML | `capturas/reporte.html` con historial visual de todas las sesiones |
| 🗜️ Backup automático | ZIP incremental por sesión guardado en `backups/` |
| 🍪 Sesiones / Login | Soporte de cookies para sitios con autenticación |
| 🔔 Notificaciones | Alerta de escritorio al finalizar cada sesión |
| 🔁 Reintentos | Backoff exponencial automático ante fallos de red |
| 🧠 Análisis de contenido | Extrae nombres, cargos, emails, teléfonos y metadatos (Open Graph, JSON-LD) |
| 📊 Consolidado CSV/JSONL | `extraccion/personas.csv`, `emails.csv`, `telefonos.csv`, `consolidado.jsonl` |

---

## Instalación

### Requisitos previos

- **Python 3.9 o superior**
  - Linux/macOS: disponible en la mayoría de distribuciones. Verifica con `python3 --version`.
  - Windows: descarga desde [python.org](https://www.python.org/downloads/). Marca la opción **"Add Python to PATH"** durante la instalación.

---

### Linux / macOS

**Opción A — iniciador automático (recomendado):**

```bash
cd capturaPaginasWebs
chmod +x iniciar_linux.sh
./iniciar_linux.sh
```

El script crea el entorno virtual, instala dependencias, instala Chromium y ejecuta la captura.

**Opción B — manual:**

```bash
cd capturaPaginasWebs

# Crear entorno virtual
python3 -m venv venv

# Activar entorno
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Instalar navegador Chromium
playwright install chromium --with-deps

# Ejecutar
python src/capturador.py
```

---

### Windows

**Opción A — iniciador automático (recomendado):**

Haz doble clic en `iniciar_windows.bat` o ejecuta desde CMD:

```bat
cd capturaPaginasWebs
iniciar_windows.bat
```

**Opción B — manual (PowerShell o CMD):**

```bat
cd capturaPaginasWebs

:: Crear entorno virtual
python -m venv venv

:: Activar entorno
venv\Scripts\activate.bat

:: Instalar dependencias
pip install -r requirements.txt

:: Instalar navegador Chromium
playwright install chromium --with-deps

:: Ejecutar
python src\capturador.py
```

> **Nota Windows:** Si al activar el venv aparece un error de política de ejecución en PowerShell, ejecuta primero:
> `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`

---

## Uso

### Ejecución básica

Con el entorno virtual activo:

```bash
# Linux / macOS
python src/capturador.py

# Windows
python src\capturador.py
```

### Opciones de línea de comandos

| Opción | Valor | Descripción |
|---|---|---|
| `--urls` | `ARCHIVO` | Archivo con las URLs (default: `urls.txt`) |
| `--formatos` | `LISTA` | Formatos: `imagen,pdf,html,texto` (default: todos) |
| `--forzar` | — | Re-captura aunque no haya cambios detectados |
| `--sesion` | `ARCHIVO` | Carga cookies desde `sesiones/ARCHIVO` |
| `--guardar-sesion` | `ARCHIVO` | Guarda cookies en `sesiones/ARCHIVO` al finalizar |
| `--verificar` | — | Verifica integridad SHA-256 de todas las capturas guardadas |
| `--solo-analizar` | — | Re-analiza capturas ya guardadas sin volver a navegar |

**Ejemplos:**

```bash
# Solo imagen y PDF
python src/capturador.py --formatos imagen,pdf

# Re-capturar todo ignorando el historial
python src/capturador.py --forzar

# Usar otra lista de URLs
python src/capturador.py --urls mis_urls.txt

# Capturar con sesión autenticada
python src/capturador.py --sesion mi_cuenta.json

# Primera captura: guardar cookies de sesión para reutilizar
python src/capturador.py --guardar-sesion mi_cuenta.json

# Re-analizar capturas anteriores (sin re-descargar)
python src/capturador.py --solo-analizar
```

### Archivo `urls.txt`

Una URL por línea. Líneas que empiezan con `#` se ignoran:

```
https://example.com
https://otro-sitio.org

# Esta URL está desactivada:
# https://url-pausada.com
```

### Sesiones / Login

Para sitios que requieren autenticación:

1. Instala la extensión **Cookie Editor** en Chrome o Firefox.
2. Inicia sesión en el sitio web manualmente.
3. Abre Cookie Editor → **Export** → "Export as JSON".
4. Guarda el archivo en `sesiones/nombre.json`.
5. Ejecuta:
   ```bash
   python src/capturador.py --sesion nombre.json
   ```

---

## Configuración

Edita las variables al inicio de `src/capturador.py` (sección `CONFIGURACIÓN`):

| Variable | Por defecto | Descripción |
|---|---|---|
| `FORMATOS_DEFAULT` | todos `True` | Activar/desactivar cada formato |
| `TIMEOUT_MS` | `60000` | Tiempo máximo de carga por página (ms) |
| `REINTENTOS` | `2` | Número de reintentos extra ante fallos |
| `WAIT_UNTIL` | `"networkidle"` | Evento de espera: `networkidle`, `load`, `domcontentloaded` |
| `SCROLL_ANTES_CAPTURAR` | `True` | Scroll automático para lazy loading |
| `SCROLL_MAX_ALTO_PX` | `30000` | Altura máxima de scroll (evita bucles infinitos) |
| `MAX_BACKUPS` | `0` | Límite de ZIPs en `backups/` — `0` = ilimitado |
| `NOTIFICACIONES` | `True` | Notificación de escritorio al finalizar |
| `MAX_CONCURRENT` | `3` | URLs procesadas simultáneamente (async) |
| `DOMAIN_DELAY_S` | `2.0` | Segundos mínimos entre requests al mismo dominio |
| `NETWORKIDLE_MS` | `5000` | Espera adicional best-effort tras `domcontentloaded` |
| `ANALIZAR_CONTENIDO` | `True` | Extrae nombres, emails, teléfonos y metadatos de cada página |
| `REGION_TELEFONOS` | `"MX"` | Región de validación de teléfonos (ISO 3166-1 alpha-2) |

---

## Estructura de archivos generados

```
capturas/
├── reporte.html                         ← reporte visual acumulativo (todas las sesiones)
├── ejemplo.com_20260325_143022/
│   ├── screenshot.png                   ← captura completa de pantalla
│   ├── documento.pdf                    ← versión PDF
│   ├── pagina.html                      ← HTML renderizado
│   ├── texto.txt                        ← texto visible extraído
│   ├── datos.json                       ← análisis: emails, teléfonos, nombres, metadatos
│   ├── info.txt                         ← URL, título, fecha, formatos usados
│   └── manifest.json                    ← SHA-256 + bytes de cada archivo (integridad)
└── ejemplo.com_20260326_091500/         ← captura posterior (solo si la página cambió)
    └── ...

extraccion/
├── consolidado.jsonl                    ← una línea JSON por captura (append-only)
├── personas.csv                         ← nombres y cargos de todas las páginas
├── emails.csv                           ← emails de todas las páginas
└── telefonos.csv                        ← teléfonos de todas las páginas

tablero.html                             ← tablero unificado: capturas + análisis (abre en el navegador)

backups/
└── backup_3sitios_20260325_143100.zip   ← ZIP incremental de esa sesión

sesiones/
└── mi_cuenta.json                       ← cookies de sesión para login

historial_hashes.json                    ← auto-generado: hashes SHA-256 por URL
errores.log                              ← auto-generado: registro de fallos
```

---

## ¿Por qué está construido así?

**Playwright async + Semaphore por dominio:**
Las URLs se procesan en paralelo (`MAX_CONCURRENT=3` por defecto) usando la API async de Playwright con `asyncio.Semaphore` global y un `asyncio.Lock` por dominio. Esto evita sobrecargar un mismo servidor mientras maximiza el uso del ancho de banda.

**Espera por capas en lugar de `networkidle`:**
`networkidle` bloquea indefinidamente en SPAs con polling, WebSockets o anuncios. La estrategia actual usa `domcontentloaded` como base fiable, `networkidle` como best-effort de 5s y un guard de contenido mínimo para no capturar spinners vacíos.

**Escritura atómica del historial (patrón Redis AOF):**
`historial_hashes.json` se escribe a un `.tmp`, se sincroniza al disco con `fsync` y luego `os.replace()` hace el rename atómico. Un SIGKILL durante la escritura nunca deja el JSON corrupto. `cargar_historial()` también atrapa `json.JSONDecodeError` y cae al `.tmp` como fallback.

**Manifiesto de integridad SHA-256 por captura (patrón ArchiveBox):**
Cada carpeta genera `manifest.json` con el hash SHA-256 y el tamaño en bytes de cada archivo guardado. `python src/capturador.py --verificar` detecta modificaciones o corrupción posterior a la captura.

**SHA-256 del texto visible para detectar cambios web:**
Comparar el hash de `document.body.innerText` es más semánticamente correcto que comparar el HTML completo, que cambia por anuncios, timestamps y IDs dinámicos.

**Un contexto, múltiples páginas:**
Se abre un único contexto de Playwright para toda la sesión. Las cookies de sesión se comparten entre URLs del mismo dominio sin reautenticarse.

**Backups incrementales por sesión:**
Solo se comprime lo nuevo de cada sesión, no todo `capturas/`. Los ZIPs permanecen pequeños y rápidos aunque haya cientos de capturas históricas acumuladas.

**Reporte acumulativo escaneando el filesystem:**
En lugar de depender de variables en memoria, el reporte escanea `capturas/` en cada ejecución. Esto permite reconstruir el historial completo aunque el programa se interrumpa a mitad.

**Historial guardado tras cada captura:**
`historial_hashes.json` se escribe a disco después de cada URL procesada (no al final). Si el proceso se interrumpe con Ctrl+C, el progreso no se pierde.

**Análisis de contenido con spaCy + phonenumbers:**
`src/extractor.py` usa el modelo `es_core_news_sm` de spaCy para NER (reconocimiento de entidades `PER`) combinado con un catálogo de 60+ cargos en español para asociar nombre ↔ cargo. Los teléfonos se validan con `phonenumbers` (Google libphonenumber). Si alguna dependencia opcional falla, hay un fallback a regex conservador para no bloquear la captura.

**CPU-bound en thread pool:**
El análisis NER con spaCy es CPU-intensivo. Se ejecuta con `asyncio.to_thread()` para no bloquear el event loop mientras otras páginas siguen descargándose en paralelo.

**Consolidado JSONL + CSV:**
Cada ejecución hace `append` a `extraccion/consolidado.jsonl` (un JSON por línea, eficiente para análisis posterior) y a los tres CSVs. El formato JSONL permite procesar millones de registros con `jq`, pandas o cualquier herramienta de streaming sin cargar todo en memoria.

---

## ¿Qué hacer después?

Ideas de mejora ordenadas por impacto:

1. **Ejecución programada** — configurar `cron` (Linux/macOS) o Tarea Programada (Windows) para ejecutar automáticamente cada día o semana sin intervención manual.

2. **Alertas por email o Telegram** — enviar el reporte o un resumen de cambios cuando se detecten modificaciones en páginas monitoreadas.

3. **Modo paralelo** — procesar múltiples URLs simultáneamente usando la API async de Playwright con `asyncio`, reduciendo significativamente el tiempo total.

4. **Diff visual entre sesiones** — comparar screenshots con `Pillow` para detectar cambios visuales además de los textuales, generando imágenes de diferencias.

5. **Dashboard web** — servir `reporte.html` con Flask o FastAPI para consultar el historial desde cualquier dispositivo en la red local.

6. **Base de datos** — guardar metadatos en SQLite para búsquedas y filtros por fecha, URL, contenido o estado.

---

## Generar la documentación PDF

```bash
# Activar venv primero
source venv/bin/activate      # Linux/macOS
venv\Scripts\activate.bat     # Windows

# Generar documentación.pdf
python generar_docs.py
```

---

## Dependencias

| Paquete | Uso |
|---|---|
| `playwright` | Navegador headless (capturas, PDF, scroll) |
| `beautifulsoup4` | Extracción de texto desde HTML y metadatos |
| `rich` | Interfaz de terminal con barra de progreso y colores |
| `plyer` | Notificaciones de escritorio multiplataforma |
| `spacy` + `es_core_news_sm` | NER en español: extracción de personas y cargos |
| `phonenumbers` | Validación de teléfonos (Google libphonenumber) |
