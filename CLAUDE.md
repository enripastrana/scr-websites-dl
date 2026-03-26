# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Herramienta para capturar páginas web (imagen, PDF, HTML y texto) leyendo una lista de URLs desde un archivo `.txt`. Usa Playwright con Chromium en modo headless y detecta cambios mediante SHA-256 para no re-capturar páginas sin modificaciones.

## Estructura del proyecto

```
capturaPaginasWebs/
├── src/
│   └── capturador.py      # Script principal
├── capturas/              # Salida: {dominio}_{timestamp}/{screenshot.png, documento.pdf, pagina.html, texto.txt, info.txt}
├── capturas/reporte.html  # Auto-generado: reporte HTML acumulativo de todas las sesiones
├── backups/               # ZIPs incrementales por sesión (backup_Nsitios_TIMESTAMP.zip)
├── sesiones/              # Cookies de sesión en JSON para sitios con login
├── urls.txt               # Lista de URLs a capturar (una por línea)
├── requirements.txt       # playwright + beautifulsoup4 + rich + plyer
├── iniciar_linux.sh       # Crea venv, instala deps y ejecuta (Linux/macOS)
├── iniciar_windows.bat    # Ídem para Windows
└── historial_hashes.json  # Auto-generado: hashes SHA-256 por URL para detectar cambios
```

## Uso rápido

**Linux / macOS** — primera vez o cualquier ejecución:
```bash
./iniciar_linux.sh
```

**Windows:**
```
iniciar_windows.bat
```

**Manual (si el venv ya está activo):**
```bash
python src/capturador.py
python src/capturador.py --formatos imagen,pdf
python src/capturador.py --forzar                        # re-captura aunque no haya cambios
python src/capturador.py --sesion mi_sitio.json          # carga cookies desde sesiones/
python src/capturador.py --guardar-sesion mi_sitio.json  # guarda cookies al finalizar
```

## Configuración

Todo se edita en la sección `CONFIGURACIÓN` al inicio de `capturador.py`:

| Variable | Por defecto | Descripción |
|---|---|---|
| `ARCHIVO_URLS` | `urls.txt` | Archivo de entrada |
| `CARPETA_SALIDA` | `capturas` | Carpeta raíz de salida |
| `FORMATOS_DEFAULT` | todos `True` | Activar/desactivar imagen, pdf, html, texto |
| `TIMEOUT_MS` | `60000` | Tiempo máximo por página (ms) |
| `NOTIFICACIONES` | `True` | Notificación de escritorio al finalizar (requiere `plyer`) |
| `MAX_BACKUPS` | `0` | Límite de ZIPs en backups/ (0 = ilimitado) |

Las líneas de `urls.txt` que comiencen con `#` se ignoran.

## Sesiones / Login

Para sitios que requieren autenticación:

1. Exporta cookies desde el navegador con la extensión **Cookie Editor** → "Export as JSON".
2. Guarda el archivo en `sesiones/nombre.json`.
3. Ejecuta: `python src/capturador.py --sesion nombre.json`

Alternativamente, captura las cookies automáticamente tras la primera navegación:
```bash
python src/capturador.py --guardar-sesion nombre.json
```

## Instalación manual del entorno

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate.bat
pip install -r requirements.txt
playwright install chromium --with-deps
```

## Arquitectura

`capturador.py` abre un único contexto de Playwright y procesa las URLs en secuencia (una página por URL, cerrada tras la captura). Antes de guardar verifica el hash SHA-256 del texto del `<body>` contra `historial_hashes.json` — si coincide, salta la URL. Los archivos de salida van a `capturas/{dominio}_{YYYYMMDD_HHMMSS}/`.
