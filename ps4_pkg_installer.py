#!/usr/bin/env python3
"""
PS4 PKG Remote Installer
Envía paquetes .pkg a una PS4 en modo debug via HTTP.
Flet 0.28.x - macOS / Windows / Linux
"""

import os
import json
import platform
import re
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import flet as ft

import archives
import pkgmeta

# ---------------------------------------------------------------- paleta

BG = "#0f1115"
SURFACE = "#181b22"
SURFACE_2 = "#212530"
BORDER = "#2c313d"
TEXT = "#e6e8eb"
MUTED = "#8b929e"
BLUE = "#4a9eff"
GREEN = "#3ddc84"
RED = "#ff5f56"
AMBER = "#ffb454"

PS4_PORT = 12800
INSTALL_TIMEOUT = 150   # RPI baja header+entry table+sfo+icono dentro del mismo POST
POLL_MAX_BACKOFF = 15   # tope de espera entre reintentos del poll
MAX_DEPTH = 6           # niveles de subcarpetas a recorrer
MAX_PKGS = 400          # tope para no colgarse si apuntan a un disco entero
PKG_MAGIC = b"\x7fCNT"  # firma de todo PKG de PS4
CONFIG = Path.home() / ".ps4_pkg_installer.json"


def load_config():
    try:
        with open(CONFIG) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(data):
    try:
        with open(CONFIG, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def parse_rpi_json(raw):
    """
    Las respuestas de RPI NO son JSON válido: escribe enteros en hexa con
    prefijo 0x (server.c, kick_error_json y handle_api_get_task_progress).
    json.loads() revienta con "Expecting ',' delimiter" en la 'x'.
    Convertimos 0xNN a decimal y recién ahí parseamos.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    fixed = re.sub(r"\b0[xX]([0-9a-fA-F]+)\b", lambda m: str(int(m.group(1), 16)), raw)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        # Último recurso: sacar los pares que podamos con regex.
        out = {}
        for k, v in re.findall(r'"(\w+)"\s*:\s*"([^"]*)"', raw):
            out[k] = v
        for k, v in re.findall(r'"(\w+)"\s*:\s*(0[xX][0-9a-fA-F]+|-?\d+)', raw):
            out[k] = int(v, 16) if v.lower().startswith("0x") else int(v)
        if not out:
            out["_raw"] = raw.strip()[:300]
        return out


# Códigos de bgft vistos en la práctica.
PS4_ERRORS = {
    0x80990085: "No hay espacio suficiente en el disco de la PS4.",
    0x80990088: "Ya existe una tarea para ese contenido. Cancelala en la consola.",
    0x8099000E: "El contenido ya está instalado.",
    0x80020016: "Parámetro inválido: el PKG puede no ser compatible.",
}


def describe_ps4_error(code):
    if not isinstance(code, int):
        return None
    return PS4_ERRORS.get(code)


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_eta(seconds):
    if not seconds or seconds <= 0:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def matches(pkg, query):
    """
    ¿Este paquete pasa el filtro?

    Busca en el título del juego, el nombre de archivo y la subcarpeta: con
    títulos legibles se filtra por "marrakesh", pero el nombre de archivo sigue
    siendo lo único que distingue dos volcados del mismo juego.
    """
    q = (query or "").strip().lower()
    if not q:
        return True
    campos = (pkg.get("title", ""), pkg.get("name", ""), pkg.get("sub", ""))
    return any(q in (c or "").lower() for c in campos)


def apply_bulk(pkgs, accion, visibles):
    """
    Aplica una acción de conjunto SOLO sobre los índices visibles.

    Lo que el filtro esconde conserva su tilde: es la única semántica que no
    sorprende. Y nunca se pisa un paquete cuyo checkbox está deshabilitado,
    que es como se marca una tarea ya en curso.
    """
    for i, pkg in enumerate(pkgs):
        if i not in visibles:
            continue
        cb = pkg.get("cb")
        if cb is None or cb.disabled:
            continue
        if accion == "all":
            cb.value = True
        elif accion == "none":
            cb.value = False
        else:
            cb.value = not cb.value


def group_key(pkg):
    """
    Orden: primero por categoría, después por título.

    El orden de CATEGORY_ORDER (juego, actualización, contenido) es también el
    orden correcto de instalación, así que la lista queda ordenada como hay que
    instalarla sin que nadie tenga que saberlo.
    """
    cat = pkg.get("category") or ""
    return (pkgmeta.CATEGORY_ORDER.get(cat, 9),
            (pkg.get("title") or pkg.get("name") or "").lower())


def choose_folder(initial=None):
    """
    Diálogo de carpetas del sistema operativo.
    Devuelve ("ok", ruta) | ("cancel", None) | ("unavailable", motivo).
    Bloquea: llamalo desde un hilo.
    """
    system = platform.system()
    start = initial if initial and os.path.isdir(initial) else None

    try:
        if system == "Darwin":
            loc = f' default location POSIX file "{start}"' if start else ""
            script = f'POSIX path of (choose folder with prompt "Carpeta con los .pkg"{loc})'
            r = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                # -128 es "cancelado por el usuario"; cualquier otra cosa es un fallo real.
                if "-128" in r.stderr:
                    return "cancel", None
                return "unavailable", r.stderr.strip() or f"osascript salió con {r.returncode}"
            path = r.stdout.strip().rstrip("/")
            return ("ok", path) if path else ("cancel", None)

        if system == "Windows":
            sel = f"$d.SelectedPath = '{start}';" if start else ""
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"{sel}"
                "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){$d.SelectedPath}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=600,
            )
            path = r.stdout.strip()
            return ("ok", path) if path else ("cancel", None)

        for cmd in (
            ["zenity", "--file-selection", "--directory", "--title=Carpeta con los .pkg"],
            ["kdialog", "--getexistingdirectory", start or os.path.expanduser("~")],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except FileNotFoundError:
                continue
            if r.returncode == 0 and r.stdout.strip():
                return "ok", r.stdout.strip()
            return "cancel", None
        return "unavailable", "no encontré zenity ni kdialog"

    except subprocess.TimeoutExpired:
        return "cancel", None
    except Exception as ex:
        return "unavailable", f"{type(ex).__name__}: {ex}"


class _Limited:
    """Envuelve un archivo para entregar solo `length` bytes desde la posición actual."""

    def __init__(self, f, length):
        self.f = f
        self.remaining = length

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        if n is None or n < 0 or n > self.remaining:
            n = self.remaining
        data = self.f.read(n)
        self.remaining -= len(data)
        return data

    def close(self):
        self.f.close()


class PkgHandler(SimpleHTTPRequestHandler):
    """
    Sirve la carpeta activa (`root` cambiable en caliente) CON soporte de
    HTTP Range. El descargador de la PS4 pide rangos para leer el header del
    PKG; SimpleHTTPRequestHandler los ignora y devuelve el archivo entero.
    """

    root = "."
    notify = None
    protocol_version = "HTTP/1.1"

    # {"/p1.pkg": "/ruta/real/con espacios/JUEGO.pkg"}. Ver App._publish.
    aliases = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PkgHandler.root, **kwargs)

    def translate_path(self, path):
        """
        El alias manda: si la ruta pedida es una publicada, devolvemos el
        archivo real esté donde esté, sin pasar por `root`.
        """
        clean = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        real = PkgHandler.aliases.get(clean)
        return real if real else super().translate_path(path)

    def log_message(self, fmt, *args):
        # Un request malformado se loguea ANTES de estar parseado, así que ni
        # `path` ni `headers` existen todavía. Pasaba de verdad: la consola
        # corta conexiones a medias y cada una dejaba un AttributeError.
        if PkgHandler.notify:
            headers = getattr(self, "headers", None)
            rng = headers.get("Range") if headers else None
            PkgHandler.notify(getattr(self, "path", ""), rng)


    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()

        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        try:
            size = os.fstat(f.fileno()).st_size
            m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng.strip())
            if not m:
                f.close()
                self.send_error(400, "Malformed Range header")
                return None

            first, last = m.group(1), m.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            elif last:                      # sufijo: bytes=-N → últimos N bytes
                start = max(0, size - int(last))
                end = size - 1
            else:
                f.close()
                self.send_error(400, "Malformed Range header")
                return None

            if start >= size or start > end:
                f.close()
                self.send_response(416, "Requested Range Not Satisfiable")
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None

            end = min(end, size - 1)
            length = end - start + 1
            f.seek(start)

            self.send_response(206, "Partial Content")
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header(
                "Last-Modified", self.date_time_string(os.fstat(f.fileno()).st_mtime)
            )
            self.end_headers()
            return _Limited(f, length)

        except Exception:
            f.close()
            raise


# ---------------------------------------------------------------- app


class App:
    # Un paquete en cualquiera de estos ya está en juego: volver a encolarlo
    # sería mandarlo dos veces.
    EN_JUEGO = frozenset({
        "pending", "sending", "waiting", "queued",
        "preparing", "downloading", "installing", "paused",
    })

    def __init__(self, page: ft.Page):
        self.page = page
        self.httpd = None
        self.port = None
        self.pkgs = []          # [{path, name, size, cb}]
        self.queue = []         # paquetes esperando su turno de envío
        self.queue_lock = threading.Lock()
        self.archives = []      # comprimidos sin extraer en la carpeta
        self.installing = False
        self.extracting = False
        self.scanning = False

        self.cfg = load_config()
        saved = self.cfg.get("folder")
        self.folder = saved if saved and os.path.isdir(saved) else str(Path.home() / "Downloads")
        if saved and not os.path.isdir(saved):
            self.cfg.pop("folder", None)

        page.title = "PS4 PKG Installer"
        page.bgcolor = BG
        page.padding = 0
        page.window.width = 880
        page.window.height = 940
        page.window.min_width = 720
        page.window.min_height = 640
        page.theme_mode = ft.ThemeMode.DARK
        page.fonts = {}

        self.picking = False
        self.stopping = False
        self.log_lines = []
        self._prev_pkgs = []

        self._build()
        page.add(self.root)
        page.update()

        PkgHandler.notify = self._on_download
        page.window.on_event = self._on_window_event

        if self.cfg.get("folder"):
            self.log(f"Retomo la última carpeta usada", "info")
        self.detect_local_ip()
        self.scan_folder()
        self.start_server()

    # ------------------------------------------------------------ helpers UI

    def _card(self, step, title, subtitle, body, expand=None):
        """Tarjeta numerada con encabezado."""
        return ft.Container(
            bgcolor=SURFACE,
            border=ft.border.all(1, BORDER),
            border_radius=14,
            padding=ft.padding.symmetric(15, 20),
            expand=expand,
            content=ft.Column(
                spacing=13,
                expand=bool(expand),
                controls=[
                    ft.Row(
                        spacing=13,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=([
                            ft.Container(
                                width=27,
                                height=27,
                                bgcolor=SURFACE_2,
                                border=ft.border.all(1, BORDER),
                                border_radius=14,
                                alignment=ft.alignment.center,
                                content=ft.Text(
                                    str(step), size=13.5, weight=ft.FontWeight.BOLD, color=BLUE
                                ),
                            ),
                        ] if step else []) + [
                            ft.Column(
                                spacing=1,
                                controls=[
                                    ft.Text(title, size=15.5, weight=ft.FontWeight.W_600, color=TEXT),
                                    ft.Text(subtitle, size=12, color=MUTED),
                                ],
                            ),
                        ],
                    ),
                    body,
                ],
            ),
        )

    def _field(self, label, value, read_only=False, width=None, hint=None):
        return ft.TextField(
            label=label,
            value=value,
            hint_text=hint,
            read_only=read_only,
            width=width,
            expand=width is None,
            text_size=15,
            height=54,
            content_padding=ft.padding.symmetric(12, 14),
            border_color=BORDER,
            focused_border_color=BLUE,
            border_radius=10,
            bgcolor=SURFACE_2,
            color=TEXT,
            label_style=ft.TextStyle(size=13, color=MUTED),
            cursor_color=BLUE,
        )

    def _chip(self, text, color, icon):
        return ft.Container(
            bgcolor=SURFACE_2,
            border=ft.border.all(1, BORDER),
            border_radius=20,
            padding=ft.padding.symmetric(6, 12),
            content=ft.Row(
                spacing=7,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, size=13, color=color),
                    ft.Text(text, size=12.5, color=TEXT, weight=ft.FontWeight.W_500),
                ],
            ),
        )

    # ------------------------------------------------------------ construcción

    def _build(self):
        # --- header -------------------------------------------------
        self.chip_server = self._chip("Servidor detenido", MUTED, ft.Icons.CIRCLE)
        self.chip_ps4 = self._chip("PS4 sin verificar", MUTED, ft.Icons.CIRCLE)

        header = ft.Container(
            padding=ft.padding.only(24, 18, 24, 16),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=13,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(ft.Icons.SPORTS_ESPORTS_OUTLINED, size=30, color=BLUE),
                            ft.Column(
                                spacing=1,
                                controls=[
                                    ft.Text(
                                        "PS4 PKG Installer",
                                        size=20,
                                        weight=ft.FontWeight.BOLD,
                                        color=TEXT,
                                    ),
                                    ft.Text(
                                        "Instalá paquetes .pkg por red local",
                                        size=12.5,
                                        color=MUTED,
                                    ),
                                ],
                            ),
                        ],
                    ),
                    ft.Row(spacing=8, controls=[
                        self.chip_server, self.chip_ps4,
                        ft.IconButton(ft.Icons.SETTINGS_OUTLINED, icon_size=19,
                                      icon_color=MUTED, tooltip="Conexión y servidor",
                                      on_click=self.open_settings),
                    ]),
                ],
            ),
        )

        # --- paso 1: conexión ---------------------------------------
        self.f_ps4 = self._field(
            "IP de la PS4", self.cfg.get("ps4_ip", ""), hint="192.168.1.x  ·  o usá el radar"
        )
        self.f_ps4.on_blur = self.on_ps4_ip_change
        self.f_local = self._field("Tu IP (automática)", "…", read_only=True, width=210)
        self.f_port = self._field("Puerto", str(self.cfg.get("port", 8000)), width=110)
        self.f_port.on_submit = self.on_port_change
        self.f_port.on_blur = self.on_port_change

        card1 = self._card(
            None,
            "Conexión",
            f"La PS4 debe estar en modo debug, escuchando en el puerto {PS4_PORT}",
            ft.Row(
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    self.f_ps4,
                    self.f_local,
                    self.f_port,
                    ft.OutlinedButton(
                        content=ft.Row(
                            tight=True,
                            spacing=8,
                            controls=[
                                ft.Icon(ft.Icons.WIFI_TETHERING, size=17),
                                ft.Text("Probar", size=14),
                            ],
                        ),
                        on_click=self.on_test,
                        height=48,
                        style=ft.ButtonStyle(
                            color=TEXT,
                            side=ft.BorderSide(1, BORDER),
                            shape=ft.RoundedRectangleBorder(radius=10),
                        ),
                    ),
                    ft.IconButton(
                        ft.Icons.RADAR,
                        icon_size=21,
                        icon_color=BLUE,
                        tooltip="Buscar la PS4 en la red",
                        on_click=self.on_scan,
                    ),
                ],
            ),
        )

        # --- paso 2: archivos ---------------------------------------
        self.folder_label = ft.Text(
            self.folder, size=13.5, color=TEXT, no_wrap=True, expand=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        self.f_pass = ft.TextField(
            label="Contraseña", password=True, can_reveal_password=True,
            width=190, dense=True, border_color=BORDER, color=TEXT,
        )
        self.cb_delete_archives = ft.Checkbox(
            label="borrar los comprimidos al terminar", value=False,
            active_color=BLUE, label_style=ft.TextStyle(size=12, color=MUTED),
        )
        self.btn_extract = ft.ElevatedButton(
            "Extraer", icon=ft.Icons.UNARCHIVE_ROUNDED, on_click=self.on_extract,
        )
        self.arch_text = ft.Text("", size=13, color=TEXT)
        self.arch_bar = ft.ProgressBar(
            value=0, color=BLUE, bgcolor=BORDER, height=4, border_radius=2, visible=False
        )
        self.arch_banner = ft.Container(
            visible=False, padding=12, border_radius=10,
            bgcolor=SURFACE_2, border=ft.border.all(1, BORDER),
            content=ft.Column(spacing=8, controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Row(spacing=8, controls=[
                            ft.Icon(ft.Icons.FOLDER_ZIP_OUTLINED, color=AMBER, size=18),
                            self.arch_text,
                        ]),
                        ft.Row(spacing=8, controls=[self.f_pass, self.btn_extract]),
                    ],
                ),
                self.cb_delete_archives,
                self.arch_bar,
            ]),
        )

        self.pkg_list = ft.ListView(spacing=6, padding=6, expand=True)
        self.count_label = ft.Text("", size=12.5, color=MUTED)

        # Respaldo por si el diálogo nativo no abre.
        self.manual_path = self._field("Pegá la ruta acá y dale Enter", "")
        self.manual_path.visible = False
        self.manual_path.on_submit = self.on_manual_path

        folder_row = ft.Container(
            bgcolor=SURFACE_2,
            border=ft.border.all(1, BORDER),
            border_radius=10,
            padding=ft.padding.only(14, 4, 6, 4),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.FOLDER_OUTLINED, size=18, color=MUTED),
                    self.folder_label,
                    ft.TextButton(
                        content=ft.Text("Cambiar", size=13.5, color=BLUE),
                        on_click=self.on_pick_folder,
                    ),
                    ft.IconButton(
                        ft.Icons.REFRESH, icon_size=18, icon_color=MUTED,
                        tooltip="Volver a escanear",
                        on_click=lambda _: self.scan_folder(),
                    ),
                ],
            ),
        )

        # --- toolbar: filtro, acciones de conjunto y modo de vista ---
        self.view_mode = "rows"
        self.f_filter = ft.TextField(
            hint_text="Filtrar…", dense=True, height=40, expand=True,
            border_color=BORDER, color=TEXT, text_size=13,
            prefix_icon=ft.Icons.SEARCH, on_change=lambda _: self.rebuild_list(),
        )
        self.visible_label = ft.Text("", size=12, color=MUTED)
        self.seg_view = ft.SegmentedButton(
            selected={"rows"},
            show_selected_icon=False,
            segments=[
                ft.Segment(value="rows", label=ft.Text("Lista", size=12.5),
                           icon=ft.Icon(ft.Icons.VIEW_LIST_ROUNDED, size=16)),
                ft.Segment(value="tiles", label=ft.Text("Cuadrícula", size=12.5),
                           icon=ft.Icon(ft.Icons.GRID_VIEW_ROUNDED, size=16)),
            ],
            on_change=self.on_view_change,
        )
        toolbar = ft.Row(
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(width=230, content=self.f_filter),
                ft.TextButton(content=ft.Text("Todos", size=12.5, color=BLUE),
                              on_click=lambda _: self.on_bulk("all")),
                ft.TextButton(content=ft.Text("Ninguno", size=12.5, color=BLUE),
                              on_click=lambda _: self.on_bulk("none")),
                ft.TextButton(content=ft.Text("Invertir", size=12.5, color=BLUE),
                              on_click=lambda _: self.on_bulk("invert")),
                ft.Container(expand=True),
                self.seg_view,
                self.visible_label,
            ],
        )

        card2 = ft.Column(
                spacing=11,
                expand=True,
                controls=[
                    folder_row,
                    self.manual_path,
                    self.arch_banner,
                    toolbar,
                    ft.Container(
                        expand=True,
                        bgcolor=SURFACE_2,
                        border=ft.border.all(1, BORDER),
                        border_radius=10,
                        content=self.pkg_list,
                    ),
                    self.count_label,
                ],
        )

        # --- paso 3: instalar ---------------------------------------
        self.btn_install = ft.ElevatedButton(
            content=ft.Row(
                tight=True,
                spacing=9,
                alignment=ft.MainAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, size=20),
                    ft.Text("Instalar en la PS4", size=15, weight=ft.FontWeight.W_600),
                ],
            ),
            on_click=self.on_install,
            bgcolor=BLUE,
            color="#04121f",
            height=52,
            expand=True,
            disabled=True,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=11), elevation=0
            ),
        )

        self.progress = ft.ProgressBar(
            value=0, color=BLUE, bgcolor=SURFACE_2, height=5, border_radius=3, visible=False
        )
        self.overall = ft.Text("", size=12, color=MUTED)
        self.btn_cancel_all = ft.OutlinedButton(
            content=ft.Row(
                tight=True, spacing=7,
                controls=[
                    ft.Icon(ft.Icons.STOP_CIRCLE_OUTLINED, size=17),
                    ft.Text("Cancelar todo", size=14),
                ],
            ),
            on_click=self.on_cancel_all,
            height=52,
            visible=False,
            style=ft.ButtonStyle(
                color=RED,
                side=ft.BorderSide(1, RED),
                shape=ft.RoundedRectangleBorder(radius=11),
            ),
        )

        action_bar = ft.Container(
            bgcolor=SURFACE,
            border=ft.border.all(1, BORDER),
            border_radius=14,
            padding=ft.padding.symmetric(13, 16),
            content=ft.Column(
                spacing=9,
                controls=[
                    ft.Row(spacing=12, controls=[self.btn_install, self.btn_cancel_all]),
                    self.progress,
                    self.overall,
                ],
            ),
        )

        # --- consola ------------------------------------------------
        self.log_view = ft.ListView(spacing=3, padding=14, auto_scroll=True, expand=True)
        self.btn_copy = ft.TextButton(
            content=ft.Row(
                tight=True,
                spacing=6,
                controls=[
                    ft.Icon(ft.Icons.CONTENT_COPY_ROUNDED, size=14, color=MUTED),
                    ft.Text("Copiar", size=12.5, color=MUTED),
                ],
            ),
            tooltip="Copiar todo el log al portapapeles",
            on_click=self.on_copy_log,
        )

        console = ft.Container(
            bgcolor=SURFACE,
            border=ft.border.all(1, BORDER),
            border_radius=14,
            expand=True,
            content=ft.Column(
                spacing=0,
                expand=True,
                controls=[
                    ft.Container(
                        padding=ft.padding.only(18, 8, 8, 8),
                        border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
                        content=ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Row(
                                    spacing=9,
                                    controls=[
                                        ft.Icon(ft.Icons.TERMINAL_ROUNDED, size=16, color=MUTED),
                                        ft.Text(
                                            "Consola",
                                            size=13.5,
                                            weight=ft.FontWeight.W_600,
                                            color=TEXT,
                                        ),
                                    ],
                                ),
                                ft.Row(
                                    spacing=2,
                                    controls=[
                                        self.btn_copy,
                                        ft.TextButton(
                                            content=ft.Text("Limpiar", size=12.5, color=MUTED),
                                            on_click=lambda _: self.clear_log(),
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ),
                    ft.Container(expand=True, content=self.log_view),
                ],
            ),
        )

        # --- conexión: deja de ser un paso y pasa a ser configuración ---
        # IP local y puerto se tocan una vez en la vida; ocupaban una tarjeta
        # entera arriba de todo. Se van al engranaje y el encabezado se queda
        # con lo único que importa a diario: si la consola responde.
        self.settings_dlg = ft.AlertDialog(
            bgcolor=SURFACE,
            content=ft.Container(width=560, content=card1),
            actions=[ft.TextButton("Listo", on_click=lambda _: self.close_settings())],
        )

        # --- raíz: dos pestañas, la lista se queda con la ventana ---
        # Hechas a mano y no con ft.Tabs: en Flet 0.28.3, Tab.before_update
        # hace isinstance(icon, IconValue) y IconValue es un Union, cosa que
        # Python 3.9 —el que trae macOS— no admite. Con ft.Tabs la ventana ni
        # llega a montarse. Dos botones y un par de "visible" hacen lo mismo.
        self.panel_install = ft.Container(
            expand=True,
            padding=ft.padding.only(20, 14, 20, 14),
            content=ft.Column(spacing=12, expand=True, controls=[card2, action_bar]),
        )
        self.panel_log = ft.Container(
            expand=True,
            visible=False,
            padding=ft.padding.only(20, 14, 20, 14),
            content=console,
        )
        self.tab_index = 0
        self.tab_buttons = [self._tab_button("Instalar", 0),
                            self._tab_button("Registro", 1)]

        tabbar = ft.Container(
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            padding=ft.padding.only(14, 0, 14, 0),
            content=ft.Row(spacing=2, controls=self.tab_buttons),
        )

        self.root = ft.Container(
            expand=True,
            bgcolor=BG,
            content=ft.Column(
                spacing=0,
                expand=True,
                controls=[header, tabbar, self.panel_install, self.panel_log],
            ),
        )

    def _tab_button(self, label, idx):
        """Pestaña: subrayado azul cuando está activa, gris cuando no."""
        activo = idx == 0
        return ft.Container(
            padding=ft.padding.only(15, 13, 15, 11),
            border=ft.border.only(
                bottom=ft.BorderSide(2, BLUE if activo else "transparent")),
            content=ft.Text(label, size=13.5, weight=ft.FontWeight.W_500,
                            color=TEXT if activo else MUTED),
            on_click=lambda _, i=idx: self.on_tab(i),
            ink=True,
        )

    def on_tab(self, idx):
        self.tab_index = idx
        for i, btn in enumerate(self.tab_buttons):
            activo = i == idx
            btn.content.color = TEXT if activo else MUTED
            btn.border = ft.border.only(
                bottom=ft.BorderSide(2, BLUE if activo else "transparent"))
        self.panel_install.visible = idx == 0
        self.panel_log.visible = idx == 1
        self._safe_update()

    # ------------------------------------------------------------ log

    def log(self, text, kind="info"):
        color, icon = {
            "ok": (GREEN, "✓"),
            "error": (RED, "✕"),
            "warn": (AMBER, "!"),
            "info": (MUTED, "·"),
            "step": (BLUE, "→"),
        }[kind]

        self.log_lines.append(f"{time.strftime('%H:%M:%S')} {icon} {text}")
        if len(self.log_lines) > 2000:
            del self.log_lines[:500]

        self.log_view.controls.append(
            ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Text(
                        time.strftime("%H:%M:%S"),
                        size=11.5,
                        color="#5a616e",
                        font_family="monospace",
                    ),
                    ft.Text(icon, size=12.5, color=color, width=12),
                    ft.Text(
                        text,
                        size=13,
                        color=TEXT if kind != "info" else MUTED,
                        selectable=True,
                        expand=True,
                    ),
                ],
            )
        )
        if len(self.log_view.controls) > 300:
            del self.log_view.controls[:100]
        self._safe_update()

    def clear_log(self):
        self.log_view.controls.clear()
        self.log_lines.clear()
        self.page.update()

    def on_copy_log(self, _):
        """Copia el log con una cabecera de contexto, para pegar en cualquier lado."""
        if not self.log_lines:
            self.log("No hay nada que copiar todavía", "info")
            return

        head = [
            "--- PS4 PKG Installer ---",
            f"SO:        {platform.system()} {platform.release()}",
            f"Python:    {platform.python_version()}  |  Flet: {getattr(ft.version, 'version', '?')}",
            f"IP local:  {self.f_local.value}   Puerto: {self.port}",
            f"IP PS4:    {self.f_ps4.value}",
            f"Carpeta:   {self.folder}",
            f"Paquetes:  {len(self.pkgs)} en carpeta, "
            f"{sum(1 for p in self.pkgs if p['cb'].value)} tildados",
            "-" * 25,
        ]
        text = "\n".join(head + self.log_lines)

        try:
            self.page.set_clipboard(text)
        except Exception as ex:
            self.log(f"No pude copiar: {type(ex).__name__}: {ex}", "error")
            return

        label = self.btn_copy.content.controls[1]
        icon = self.btn_copy.content.controls[0]
        label.value, label.color = "Copiado", GREEN
        icon.name, icon.color = ft.Icons.CHECK_ROUNDED, GREEN
        self._safe_update()

        def restore():
            time.sleep(1.6)
            label.value, label.color = "Copiar", MUTED
            icon.name, icon.color = ft.Icons.CONTENT_COPY_ROUNDED, MUTED
            self._safe_update()

        threading.Thread(target=restore, daemon=True).start()

    def _safe_update(self):
        try:
            self.page.update()
        except Exception:
            pass

    def set_chip(self, chip, text, color, icon=ft.Icons.CIRCLE):
        row = chip.content
        row.controls[0].name = icon
        row.controls[0].color = color
        row.controls[1].value = text
        self._safe_update()

    # ------------------------------------------------------------ red

    def detect_local_ip(self):
        ip = "127.0.0.1"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        self.f_local.value = ip
        self.log(f"IP local detectada: {ip}", "info")
        self._safe_update()

    def ps4_reachable(self, ip, timeout=3):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            ok = s.connect_ex((ip, PS4_PORT)) == 0
            s.close()
            return ok
        except Exception:
            return False

    def on_scan(self, _):
        if self.scanning:
            return
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        """Recorre la subred buscando un host con el puerto 12800 abierto."""
        self.scanning = True
        try:
            local = (self.f_local.value or "").strip()
            parts = local.split(".")
            if len(parts) != 4 or local.startswith("127."):
                self.log("No pude determinar tu subred. Escribí la IP a mano.", "error")
                return

            prefix = ".".join(parts[:3])
            mine = int(parts[3])
            # La .35 suele ser la consola: la probamos primero.
            order = [35] + [h for h in range(1, 255) if h not in (35, mine)]

            self.log(f"Buscando la PS4 en {prefix}.1-254 (puerto {PS4_PORT})…", "step")
            self.set_chip(self.chip_ps4, "Buscando…", AMBER, ft.Icons.RADAR)

            # Camino rápido: si la .35 responde, listo.
            fast = f"{prefix}.35"
            if self.ps4_reachable(fast, timeout=0.6):
                self._scan_found(fast)
                return

            pool = ThreadPoolExecutor(max_workers=80)
            futures = [
                pool.submit(self._probe, f"{prefix}.{h}") for h in order if h != 35
            ]
            found = None
            try:
                for fut in as_completed(futures):
                    hit = fut.result()
                    if hit:
                        found = hit
                        break
            finally:
                for f in futures:
                    f.cancel()
                pool.shutdown(wait=False)

            if found:
                self._scan_found(found)
            else:
                self.log(
                    f"Ningún equipo de {prefix}.x responde en el puerto {PS4_PORT}.", "error"
                )
                self.log(
                    "Abrí el Package Installer en la consola y fijate que esté en la misma red.",
                    "info",
                )
                self.set_chip(self.chip_ps4, "PS4 no encontrada", RED, ft.Icons.ERROR)
        finally:
            self.scanning = False
            self._safe_update()

    def _probe(self, ip):
        return ip if self.ps4_reachable(ip, timeout=0.6) else None

    def _scan_found(self, ip):
        self.f_ps4.value = ip
        self.cfg["ps4_ip"] = ip
        save_config(self.cfg)
        self.log(f"PS4 encontrada en {ip}", "ok")
        self.set_chip(self.chip_ps4, "PS4 conectada", GREEN, ft.Icons.CHECK_CIRCLE)
        self._safe_update()

    def on_test(self, _):
        ip = (self.f_ps4.value or "").strip()
        if not ip:
            self.log("Escribí la IP de la PS4 primero", "warn")
            return
        self.log(f"Probando {ip}:{PS4_PORT}…", "step")

        def work():
            state = self.ps4_state(ip)
            if state == "ok":
                self.log("La PS4 responde y está atendiendo pedidos.", "ok")
                self.set_chip(self.chip_ps4, "PS4 lista", GREEN, ft.Icons.CHECK_CIRCLE)
            elif state == "busy":
                self.log("El puerto está abierto pero la app no contesta.", "error")
                self.log(
                    "Suele quedar trabada por un intento anterior: sandbird atiende "
                    "de a un pedido. Cerrá la app en la consola y volvé a abrirla.",
                    "info",
                )
                self.set_chip(self.chip_ps4, "PS4 trabada", AMBER, ft.Icons.WARNING_ROUNDED)
            else:
                self.log(f"Sin respuesta en {ip}:{PS4_PORT}", "error")
                self.log("Revisá: IP, consola encendida, y Package Installer abierto y en foco", "info")
                self.set_chip(self.chip_ps4, "PS4 no responde", RED, ft.Icons.ERROR)

        threading.Thread(target=work, daemon=True).start()

    def ps4_state(self, ip, timeout=8):
        """
        Chequeo a nivel HTTP. Un connect() TCP solo prueba que el puerto está
        bindeado: el kernel completa el handshake aunque la app esté colgada
        procesando otro pedido. Pegarle a /api/is_exists prueba que atiende.
        Devuelve "ok" | "busy" | "down".
        """
        if not self.ps4_reachable(ip, timeout=3):
            return "down"
        try:
            req = urllib.request.Request(
                f"http://{ip}:{PS4_PORT}/api/is_exists",
                data=json.dumps({"title_id": "CUSA00000"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout):
                return "ok"
        except urllib.error.HTTPError:
            return "ok"        # contestó, aunque sea un error: está viva
        except Exception:
            return "busy"      # puerto abierto pero sin respuesta

    # ------------------------------------------------------------ archivos

    def on_pick_folder(self, _):
        if self.picking:
            return
        threading.Thread(target=self._pick_worker, daemon=True).start()

    def _pick_worker(self):
        """El diálogo del SO bloquea, así que va en su propio hilo."""
        self.picking = True
        try:
            status, value = choose_folder(self.folder)
            if status == "ok" and os.path.isdir(value):
                self.use_folder(value)
            elif status == "ok":
                self.log(f"Esa ruta no es una carpeta: {value}", "error")
            elif status == "cancel":
                self.log("Selección cancelada", "info")
            else:
                self.log(f"No pude abrir el selector: {value}", "error")
                self.log("Pegá la ruta en el campo de abajo y dale Enter.", "info")
                self.manual_path.visible = True
                self._safe_update()
        finally:
            self.picking = False

    def on_manual_path(self, _):
        path = (self.manual_path.value or "").strip()
        if not path:
            return
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            self.use_folder(path)
            self.manual_path.value = ""
        else:
            self.log(f"Esa ruta no es una carpeta: {path}", "error")
        self._safe_update()

    def use_folder(self, path):
        self.folder = path
        self.folder_label.value = path
        PkgHandler.root = path        # el servidor sigue la carpeta, sin reiniciar
        self.cfg["folder"] = path
        save_config(self.cfg)
        self.log(f"Carpeta: {path}", "ok")
        self.scan_folder()

    def scan_folder(self):
        self.pkgs.clear()
        self.pkg_list.controls.clear()

        if not os.path.isdir(self.folder):
            self.count_label.value = ""
            self.log(f"La carpeta no existe: {self.folder}", "error")
            self._safe_update()
            return

        found, skipped = self._walk_pkgs(self.folder)
        if skipped:
            self.log(f"Corté el escaneo en {MAX_PKGS} paquetes ({skipped} sin listar)", "warn")

        # La clave es la ruta relativa: puede haber dos .pkg con el mismo
        # nombre en subcarpetas distintas.
        keep = {p.get("rel", p["name"]): p for p in getattr(self, "_prev_pkgs", [])}

        for rel, path, size in found:
            name = os.path.basename(rel)
            sub = os.path.dirname(rel)

            # Si el paquete ya estaba en curso, conservamos su estado.
            old = keep.get(rel)
            if old and old.get("state") not in (None, "idle"):
                pkg = old
                pkg["path"], pkg["size"] = path, size
            else:
                pkg = {
                    "path": path, "name": name, "rel": rel, "sub": sub, "size": size,
                    "state": "idle", "task_id": None, "polling": False,
                    "served": 0, "transferred": 0, "length": 0, "rest_sec": 0,
                    "served_pos": 0, "stale": False,
                    "title": "", "category": "", "version": "", "icon": None,
                }
            pkg["cb"] = ft.Checkbox(
                value=True, active_color=BLUE, on_change=lambda _: self._refresh_count()
            )
            self.pkgs.append(pkg)

        self._prev_pkgs = list(self.pkgs)

        if not found:
            self.log("No se encontraron .pkg ni en la carpeta ni en sus subcarpetas", "warn")
        else:
            subs = len({p["sub"] for p in self.pkgs if p.get("sub")})
            extra = f" en {subs} subcarpeta(s)" if subs else ""
            self.log(f"{len(found)} paquete(s) encontrado(s){extra}", "ok")

        self.rebuild_list()
        self._scan_archives()
        self._load_meta_async()

    def _load_meta_async(self):
        """
        Lee título, categoría e ícono de cada PKG en un thread.

        Abrir cientos de archivos y volcar sus carátulas no puede pasar en el
        hilo de la UI. Los paquetes ya se listaron con su nombre de archivo;
        cuando la metadata llega, la lista se reordena y se repinta sola.
        """
        objetivo = list(self.pkgs)

        def worker():
            hubo = False
            for pkg in objetivo:
                if self.stopping or pkg not in self.pkgs:
                    return
                info = pkgmeta.read_pkg(pkg["path"])
                if not info:
                    continue
                pkg["title"] = info.title
                pkg["category"] = info.category
                pkg["version"] = info.version
                pkg["icon"] = pkgmeta.icon_path(pkg["path"], info)
                hubo = hubo or bool(info.title)
            if self.stopping:
                return
            # El orden por categoría es también el orden de instalación.
            self.pkgs.sort(key=group_key)
            self.rebuild_list()
            if hubo:
                grupos = {}
                for pkg in self.pkgs:
                    grupos[pkg.get("category") or ""] = grupos.get(pkg.get("category") or "", 0) + 1
                detalle = ", ".join(
                    f"{n} {(pkgmeta.CATEGORIES.get(c) or 'sin clasificar').lower()}"
                    for c, n in sorted(grupos.items(), key=lambda kv: pkgmeta.CATEGORY_ORDER.get(kv[0], 9))
                )
                self.log(f"Metadata leída · {detalle}", "ok")

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------ vista de la lista

    def open_settings(self, _=None):
        self.page.open(self.settings_dlg)

    def close_settings(self, _=None):
        self.page.close(self.settings_dlg)

    def on_bulk(self, accion):
        """Todos / Ninguno / Invertir, siempre sobre lo que dejó el filtro."""
        visibles = {i for i, p in enumerate(self.pkgs)
                    if matches(p, self.f_filter.value)}
        apply_bulk(self.pkgs, accion, visibles)
        self.rebuild_list()

    def on_view_change(self, e):
        self.view_mode = next(iter(e.control.selected), "rows")
        self.rebuild_list()

    def _group_header(self, cat, n):
        """Encabezado de grupo: qué es y cuántos hay."""
        return ft.Container(
            padding=ft.padding.only(8, 12, 8, 4),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text((pkgmeta.CATEGORIES.get(cat) or "Otros").upper(),
                            size=10.5, weight=ft.FontWeight.W_600, color=MUTED),
                    ft.Container(expand=True, height=1, bgcolor=BORDER),
                    ft.Text(str(n), size=10.5, color="#69707d"),
                ],
            ),
        )

    def _tile(self, pkg):
        """
        Tarjeta para la vista cuadrícula.

        Sirve para elegir: catorce carátulas se reconocen de un vistazo,
        catorce nombres de archivo no. Para mirar instalar sigue siendo mejor
        la lista, que tiene ancho para la barra y alinea los tamaños.
        """
        seleccionado = bool(pkg["cb"].value)
        estado = pkg.get("state", "idle")
        etiqueta, color, _ = self.STATES.get(estado, self.STATES["idle"])

        arte = (
            ft.Image(src=pkg["icon"], width=112, height=112, border_radius=7,
                     fit=ft.ImageFit.COVER)
            if pkg.get("icon") else
            ft.Container(width=112, height=112, border_radius=7, bgcolor=SURFACE_2,
                         alignment=ft.alignment.center,
                         content=ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=26, color=MUTED))
        )

        def alternar(_, p=pkg):
            if p["cb"].disabled:
                return
            p["cb"].value = not p["cb"].value
            self.rebuild_list()

        return ft.Container(
            width=130, padding=9, border_radius=9,
            bgcolor="#14273d" if seleccionado else BG,
            border=ft.border.all(1, BLUE if seleccionado else BORDER),
            on_click=alternar, ink=True,
            content=ft.Column(spacing=7, controls=[
                arte,
                ft.Text(pkg.get("title") or pkg["name"], size=11.5, color=TEXT,
                        max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, controls=[
                    ft.Text(human_size(pkg["size"]), size=10.5, color=MUTED),
                    ft.Text(etiqueta, size=10.5, color=color),
                ]),
            ]),
        )

    def rebuild_list(self):
        """
        Rearma la lista visible: filtra, agrupa por categoría y pinta.

        Se rearma en vez de ocultar porque el cambio de vista cambia el tipo de
        control. El estado vivo de cada paquete vive en su dict, no en la fila,
        así que reconstruir no corta ninguna tarea en curso.
        """
        if not hasattr(self, "pkg_list"):
            return
        self.pkg_list.controls.clear()
        q = self.f_filter.value if hasattr(self, "f_filter") else ""
        visibles = [p for p in self.pkgs if matches(p, q)]

        cur = object()
        cajon = None
        for pkg in visibles:
            cat = pkg.get("category") or ""
            if cat != cur:
                cur = cat
                n = sum(1 for p in visibles if (p.get("category") or "") == cat)
                self.pkg_list.controls.append(self._group_header(cat, n))
                cajon = None
                if self.view_mode == "tiles":
                    cajon = ft.Row(wrap=True, spacing=8, run_spacing=8)
                    self.pkg_list.controls.append(cajon)
            control = self._tile(pkg) if self.view_mode == "tiles" else self._build_row(pkg)
            (cajon.controls if cajon is not None else self.pkg_list.controls).append(control)

        if not visibles:
            self.pkg_list.controls.append(
                ft.Container(
                    padding=26, alignment=ft.alignment.center,
                    content=ft.Column(
                        spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(ft.Icons.FOLDER_OFF_OUTLINED, size=30, color="#4a505c"),
                            ft.Text("No hay paquetes que mostrar", size=13.5, color=MUTED),
                        ],
                    ),
                )
            )

        oculto = len(self.pkgs) - len(visibles)
        self.visible_label.value = (
            f"{len(visibles)} a la vista · {oculto} ocultos" if oculto else ""
        )
        if self.view_mode == "rows":
            self.refresh_rows()
        self._refresh_count()

    def _scan_archives(self):
        """Busca comprimidos sin extraer y muestra el banner si hay alguno."""
        if self.extracting:
            return
        try:
            self.archives = archives.find_archives(self.folder, max_depth=MAX_DEPTH)
        except Exception as e:
            self.archives = []
            self.log(f"No pude revisar comprimidos: {e}", "warn")

        if not self.archives:
            self.arch_banner.visible = False
            return

        if not archives.seven_zip_path():
            self.arch_banner.visible = False
            self.log(
                "Hay comprimidos sin extraer pero falta el binario de 7-Zip. "
                "Corriendo desde fuente: brew install sevenzip", "warn"
            )
            return

        total = sum(a.total_size for a in self.archives)
        incompletos = [a for a in self.archives if a.missing_parts]

        self.arch_text.value = (
            f"{len(self.archives)} comprimido(s) sin extraer  ·  {human_size(total)}"
        )
        self.arch_banner.visible = True
        self.btn_extract.disabled = bool(incompletos)

        for a in incompletos:
            self.log(
                f"{a.name}: faltan volúmenes ({', '.join(a.missing_parts)})", "error"
            )
        if not incompletos:
            self.log(f"{len(self.archives)} comprimido(s) sin extraer", "warn")

    def _walk_pkgs(self, root):
        """
        Recorre el árbol buscando .pkg. Devuelve ([(rel, path, size)], omitidos).
        No sigue symlinks (evita ciclos), ignora carpetas ocultas y corta a
        MAX_DEPTH niveles para no perderse en un disco entero.
        """
        found = []
        skipped = 0
        bogus = []
        root = os.path.abspath(root)

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            depth = dirpath[len(root):].count(os.sep)
            if depth >= MAX_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not d.startswith(".") and d not in ("__MACOSX", "node_modules")
                )

            for fn in sorted(filenames):
                if not fn.lower().endswith(".pkg") or fn.startswith("."):
                    continue
                full = os.path.join(dirpath, fn)
                if len(found) >= MAX_PKGS:
                    skipped += 1
                    continue
                try:
                    size = os.path.getsize(full)
                    with open(full, "rb") as fh:
                        magic = fh.read(4)
                except OSError:
                    continue

                # Todo PKG de PS4 arranca con 7F 43 4E 54 (".CNT"). Al buscar
                # recursivo se cuelan .pkg de otras cosas (instaladores de macOS,
                # artefactos de PyInstaller) que la consola rechazaría.
                if magic != PKG_MAGIC:
                    bogus.append(os.path.relpath(full, root))
                    continue

                found.append((os.path.relpath(full, root), full, size))

        # Primero los de la raíz, después por subcarpeta.
        found.sort(key=lambda t: (os.path.dirname(t[0]).lower(), os.path.basename(t[0]).lower()))

        for rel in bogus[:5]:
            self.log(f"Ignoro «{rel}»: no es un PKG de PS4", "info")
        if len(bogus) > 5:
            self.log(f"…y {len(bogus) - 5} archivo(s) más sin firma de PKG", "info")

        return found, skipped

    STATES = {
        "idle":        ("En espera",   MUTED,  ft.Icons.CIRCLE_OUTLINED),
        "sending":     ("Enviando",    BLUE,   ft.Icons.UPLOAD_ROUNDED),
        "preparing":   ("Preparando",  BLUE,   ft.Icons.HOURGLASS_TOP_ROUNDED),
        "downloading": ("Descargando", BLUE,   ft.Icons.DOWNLOADING_ROUNDED),
        "installing":  ("Instalando",  AMBER,  ft.Icons.SETTINGS_ROUNDED),
        "paused":      ("En pausa",    AMBER,  ft.Icons.PAUSE_CIRCLE_ROUNDED),
        "done":        ("Instalado",   GREEN,  ft.Icons.CHECK_CIRCLE_ROUNDED),
        "error":       ("Error",       RED,    ft.Icons.ERROR_ROUNDED),
        "cancelled":   ("Cancelado",   MUTED,  ft.Icons.CANCEL_ROUNDED),
        "unknown":     ("Sin datos",   MUTED,  ft.Icons.HELP_OUTLINE_ROUNDED),
        "waiting":     ("Esperando turno", MUTED, ft.Icons.HOURGLASS_EMPTY_ROUNDED),
        "queued":      ("En cola, sin confirmar", AMBER, ft.Icons.PENDING_ROUNDED),
        "pending":     ("En la cola",  MUTED,  ft.Icons.LIST_ALT_ROUNDED),
    }

    # La franja izquierda repite el estado en forma además de en texto: con
    # dieciséis paquetes, encontrar el que necesita atención deja de ser lectura.
    STRIPE = {
        "sending": BLUE, "preparing": BLUE, "downloading": BLUE,
        "installing": AMBER, "paused": AMBER, "waiting": AMBER, "queued": AMBER,
        "done": GREEN, "error": RED,
    }

    def _build_row(self, pkg):
        """Fila con estado, barra de progreso y controles de la tarea."""
        pkg["ui_state"] = ft.Text("", size=12, color=MUTED)
        pkg["ui_icon"] = ft.Icon(ft.Icons.CIRCLE_OUTLINED, size=16, color=MUTED)
        pkg["ui_detail"] = ft.Text("", size=11.5, color=MUTED)
        pkg["ui_bar"] = ft.ProgressBar(
            value=0, color=BLUE, bgcolor=BORDER, height=4, border_radius=2, visible=False
        )
        pkg["ui_pause"] = ft.IconButton(
            ft.Icons.PAUSE_ROUNDED, icon_size=17, icon_color=MUTED, visible=False,
            tooltip="Pausar", on_click=lambda _, p=pkg: self.task_action(p, "pause_task"),
        )
        pkg["ui_resume"] = ft.IconButton(
            ft.Icons.PLAY_ARROW_ROUNDED, icon_size=17, icon_color=GREEN, visible=False,
            tooltip="Reanudar", on_click=lambda _, p=pkg: self.task_action(p, "resume_task"),
        )
        pkg["ui_stop"] = ft.IconButton(
            ft.Icons.STOP_ROUNDED, icon_size=17, icon_color=RED, visible=False,
            tooltip="Cancelar", on_click=lambda _, p=pkg: self.task_action(p, "stop_task"),
        )

        pkg["ui_stripe"] = ft.Container(width=3, height=38, border_radius=2,
                                        bgcolor="transparent")
        # Un paquete sin carátula no deja un hueco: muestra el marcador. De 18
        # paquetes reales, uno no traía icon0.png.
        caratula = (
            ft.Image(src=pkg["icon"], width=34, height=34, border_radius=6,
                     fit=ft.ImageFit.COVER)
            if pkg.get("icon") else
            ft.Container(width=34, height=34, border_radius=6, bgcolor=SURFACE_2,
                         alignment=ft.alignment.center,
                         content=ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=16, color=MUTED))
        )
        titulo = pkg.get("title") or pkg["name"]

        row = ft.Container(
            padding=ft.padding.only(0, 6, 10, 6),
            border_radius=8,
            content=ft.Row(
                spacing=9,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    pkg["ui_stripe"],
                    pkg["cb"],
                    caratula,
                    ft.Column(
                        spacing=1,
                        expand=True,
                        controls=[
                            ft.Text(titulo, size=13, color=TEXT,
                                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                            # El nombre de archivo baja a segunda línea: sigue
                            # siendo lo único que distingue dos volcados del
                            # mismo juego, pero deja de ser lo primero que se lee.
                            ft.Text(pkg["name"], size=10.5, color="#69707d",
                                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
                            pkg["ui_bar"],
                            pkg["ui_detail"],
                        ],
                    ),
                    ft.Text(human_size(pkg["size"]), size=11.5, color=MUTED,
                            width=72, text_align=ft.TextAlign.RIGHT),
                    pkg["ui_state"],
                    pkg["ui_pause"], pkg["ui_resume"], pkg["ui_stop"],
                ],
            ),
        )
        self._paint_row(pkg)
        return row

    def _progress_of(self, pkg):
        """
        (bytes hechos, total) para pintar la barra.

        Con datos frescos manda lo que reporta la consola. Cuando está muda
        (stale), el servidor local es lo único que sabe algo — y sabe bastante:
        el Range trae el byte exacto.
        """
        total = pkg.get("length") or pkg["size"]
        done = pkg.get("transferred") or 0
        if pkg.get("stale"):
            done = max(done, pkg.get("served_pos", 0))
        return done, total

    def _paint_row(self, pkg):
        state = pkg.get("state", "idle")
        label, color, icon = self.STATES.get(state, self.STATES["idle"])

        pkg["ui_icon"].name, pkg["ui_icon"].color = icon, color
        if "ui_stripe" in pkg:
            pkg["ui_stripe"].bgcolor = self.STRIPE.get(state) or "transparent"
        pkg["ui_state"].value, pkg["ui_state"].color = label, color

        active = state in ("preparing", "downloading", "installing", "paused")
        pkg["cb"].disabled = state not in ("idle", "error", "cancelled", "done")
        pkg["ui_pause"].visible = state in ("downloading", "preparing")
        pkg["ui_resume"].visible = state == "paused"
        pkg["ui_stop"].visible = active

        done, total = self._progress_of(pkg)
        pct = (done / total) if total else 0

        if state == "done":
            pkg["ui_bar"].visible = True
            pkg["ui_bar"].value = 1
            pkg["ui_bar"].color = GREEN
            pkg["ui_detail"].value = "Completado"
        elif active:
            pkg["ui_bar"].visible = True
            pkg["ui_bar"].color = AMBER if state in ("paused", "installing") else BLUE
            pkg["ui_bar"].value = pct if total else None
            bits = [f"{pct*100:.1f}%"]
            if total:
                bits.append(f"{human_size(done)} de {human_size(total)}")
            if pkg.get("stale"):
                bits.append("avance medido en el servidor local")
            else:
                eta = human_eta(pkg.get("rest_sec"))
                if eta and state == "downloading":
                    bits.append(f"faltan {eta}")
            pkg["ui_detail"].value = "  ·  ".join(bits)
        elif state == "sending":
            pkg["ui_bar"].visible = True
            pkg["ui_bar"].value = None      # indeterminado
            pkg["ui_detail"].value = "Registrando la tarea en la consola…"
        else:
            pkg["ui_bar"].visible = False
            pkg["ui_detail"].value = pkg.get("error", "")

    def refresh_rows(self):
        if getattr(self, "view_mode", "rows") == "tiles":
            # Los tiles no tienen controles vivos que repintar: se rearman.
            self.rebuild_list()
            self._update_overall()
            self._safe_update()
            return
        for pkg in self.pkgs:
            if "ui_bar" in pkg:
                self._paint_row(pkg)
        self._update_overall()
        self._safe_update()

    def _update_overall(self):
        active = [p for p in self.pkgs
                  if p.get("state") in ("preparing", "downloading", "installing", "paused")]
        done = [p for p in self.pkgs if p.get("state") == "done"]
        if not active and not done:
            self.progress.visible = False
            self.overall.value = ""
            return

        self.progress.visible = True
        tot = sum((p.get("length") or p["size"]) for p in active + done)
        got = sum((p.get("length") or p["size"]) if p.get("state") == "done"
                  else (p.get("transferred") or 0) for p in active + done)
        self.progress.value = (got / tot) if tot else None
        self.overall.value = (
            f"{len(done)} de {len(active) + len(done)} listos  ·  "
            f"{human_size(got)} de {human_size(tot)}"
            + (f"  ·  {len(active)} en curso" if active else "")
        )
        self.btn_cancel_all.visible = bool(active)

    def _refresh_count(self):
        sel = [p for p in self.pkgs if p["cb"].value]
        if sel:
            total = sum(p["size"] for p in sel)
            self.count_label.value = f"{len(sel)} de {len(self.pkgs)} seleccionados · {human_size(total)}"
        else:
            self.count_label.value = f"0 de {len(self.pkgs)} seleccionados" if self.pkgs else ""
        self._safe_update()


    # ------------------------------------------------------------ servidor

    def _enqueue(self, pkgs):
        """
        Suma a la cola los que todavía no están en juego. Devuelve los que
        entraron de verdad.

        El dedup es por identidad del dict, no por nombre: dos volcados del
        mismo juego en subcarpetas distintas se llaman igual y son paquetes
        distintos.
        """
        agregados = []
        with self.queue_lock:
            for pkg in pkgs:
                if pkg.get("state") in self.EN_JUEGO:
                    continue
                if any(p is pkg for p in self.queue):
                    continue
                pkg["state"] = "pending"
                self.queue.append(pkg)
                agregados.append(pkg)
        return agregados

    def _dequeue(self):
        with self.queue_lock:
            return self.queue.pop(0) if self.queue else None

    def _claim_worker(self):
        """
        ¿Le toca a este hilo arrancar el worker? Si sí, queda reservado.

        `installing` sólo se toca bajo el lock de la cola. Es lo que garantiza
        que nunca haya dos workers: dos POST /api/install a la vez le dejan a
        RPI una tarea sin task_id, imposible de seguir o cancelar.
        """
        with self.queue_lock:
            if self.installing:
                return False
            self.installing = True
            return True

    def _drain_queue(self):
        """Vacía la cola y devuelve lo que había."""
        with self.queue_lock:
            resto, self.queue = self.queue, []
        return resto

    def set_install_button(self, en_cola):
        """Con un envío vivo el botón suma a la cola en vez de quedar gris."""
        icono, texto = self.btn_install.content.controls
        icono.name = ft.Icons.PLAYLIST_ADD_ROUNDED if en_cola else ft.Icons.DOWNLOAD_ROUNDED
        texto.value = "Agregar a la cola" if en_cola else "Instalar en la PS4"
        self.btn_install.disabled = self.httpd is None

    def _publish(self, pkg):
        """
        Deja el PKG accesible bajo una ruta plana y ASCII, y la devuelve.

        Medido contra la consola: RPI decodifica el percent-encoding de la URL
        y arma el request HTTP con el resultado crudo. Un solo espacio en la
        ruta le parte el request line y falla en 21 ms sin abrir un socket
        ("Unable to set up prerequisites"). El mismo archivo servido como
        /p1.pkg entra sin chistar. No es cuestión de codificar mejor: %20 es
        correcto y falla igual, porque el bug está del lado que decodifica.

        El alias se asigna una sola vez por paquete y nunca se reusa: si fuera
        por posición en la tanda, reenviar uno solo le robaría la ruta a otro
        que todavía está descargando.
        """
        if not pkg.get("alias"):
            self._alias_seq = getattr(self, "_alias_seq", 0) + 1
            pkg["alias"] = f"/p{self._alias_seq}.pkg"
        PkgHandler.aliases[pkg["alias"]] = pkg["path"]
        return pkg["alias"]

    def start_server(self):
        """Levanta el servidor solo. Si el puerto está ocupado, prueba los siguientes."""
        self.stop_server()
        PkgHandler.root = self.folder

        try:
            base = int(self.f_port.value)
        except (TypeError, ValueError):
            base = 8000

        for port in range(base, base + 12):
            try:
                httpd = ThreadingHTTPServer(("0.0.0.0", port), PkgHandler)
            except OSError:
                continue

            self.httpd = httpd
            self.port = port
            self.f_port.value = str(port)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

            if port != base:
                self.log(f"El puerto {base} estaba ocupado, uso el {port}", "warn")
            self.log(f"Sirviendo {self.folder} en el puerto {port}", "ok")
            self.set_chip(self.chip_server, f"Servidor :{port}", GREEN, ft.Icons.CHECK_CIRCLE)
            self.btn_install.disabled = False
            self._safe_update()
            return True

        self.log(f"No hay puertos libres entre {base} y {base + 11}", "error")
        self.set_chip(self.chip_server, "Servidor caído", RED, ft.Icons.ERROR)
        self.btn_install.disabled = True
        self._safe_update()
        return False

    def stop_server(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None

    def on_port_change(self, _):
        """Rebinding solo si el puerto realmente cambió."""
        try:
            wanted = int(self.f_port.value)
        except (TypeError, ValueError):
            self.f_port.value = str(self.port or 8000)
            self._safe_update()
            return
        if wanted != self.port:
            if self.start_server():
                self.cfg["port"] = self.port
                save_config(self.cfg)

    def on_ps4_ip_change(self, _):
        ip = (self.f_ps4.value or "").strip()
        if ip and ip != self.cfg.get("ps4_ip"):
            self.cfg["ps4_ip"] = ip
            save_config(self.cfg)

    def _on_window_event(self, e):
        if getattr(e, "data", None) == "close":
            self.stop_server()

    # ------------------------------------------------------------ tareas RPI

    def rpi_call(self, endpoint, payload, timeout=15):
        """POST a la API de RPI. Devuelve dict (tolerante al JSON roto) o None."""
        ip = (self.f_ps4.value or "").strip()
        if not ip:
            return None
        try:
            req = urllib.request.Request(
                f"http://{ip}:{PS4_PORT}/api/{endpoint}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return parse_rpi_json(r.read())
        except urllib.error.HTTPError as e:
            try:
                return parse_rpi_json(e.read())
            except Exception:
                return {"status": "fail", "_http": e.code}
        except Exception as e:
            return {"status": "fail", "_exc": f"{type(e).__name__}: {e}"}

    def task_action(self, pkg, action):
        """pause_task / resume_task / stop_task sobre un paquete con task_id."""
        tid = pkg.get("task_id")
        if tid is None:
            self.log(f"{pkg['name']}: todavía no tiene tarea asignada", "warn")
            return

        verb = {"pause_task": "Pausando", "resume_task": "Reanudando",
                "stop_task": "Cancelando"}[action]
        self.log(f"{verb} {pkg['name']} (tarea {tid})…", "step")

        def work():
            res = self.rpi_call(action, {"task_id": tid}) or {}
            if str(res.get("status", "")).lower() == "success":
                pkg["state"] = {"pause_task": "paused", "resume_task": "downloading",
                                "stop_task": "cancelled"}[action]
                if action == "stop_task":
                    pkg["polling"] = False
                self.log(f"{pkg['name']}: {verb.lower()[:-4]}ado", "ok")
            else:
                self._report_api_error(pkg["name"], res)
            self.refresh_rows()

        threading.Thread(target=work, daemon=True).start()

    def _report_api_error(self, name, res):
        code = res.get("error_code")
        hint = describe_ps4_error(code)
        if code is not None:
            self.log(f"{name}: la consola devolvió 0x{code:08X}", "error")
            if hint:
                self.log(hint, "info")
        elif res.get("_exc"):
            self.log(f"{name}: {res['_exc']}", "error")
        elif res.get("_raw"):
            self.log(f"{name}: respuesta inesperada -> {res['_raw']}", "error")
        else:
            self.log(f"{name}: falló sin detalle ({res})", "error")

    def poll_task(self, pkg):
        """
        Sigue una tarea en la consola hasta que termina o se cancela.

        RPI sirve la API y el PKG con el mismo hilo: mientras hay una descarga
        en curso, /api no contesta absolutamente nada (medido: 12 timeouts de
        10s seguidos, cero respuestas, con la transferencia avanzando sin
        problemas). Acá el silencio es lo NORMAL, no una tarea perdida.

        Antes se apagaba el polling a los 3 fallos y el paquete quedaba en
        "unknown": la fila se veía como si no se hubiera enviado nada, para
        siempre y sin un mensaje que lo explicara. Ahora reintenta con backoff
        y marca el paquete como "stale", así la UI puede seguir mostrando el
        avance que reporta el servidor HTTP local (ver _on_download).
        """
        tid = pkg["task_id"]
        stagnant = 0
        last_seen = -1
        aviso_dado = False

        while pkg.get("polling") and not self.stopping:
            res = self.rpi_call("get_task_progress", {"task_id": tid}, timeout=10) or {}

            if str(res.get("status", "")).lower() != "success":
                stagnant += 1
                pkg["stale"] = True
                if stagnant == 3 and not aviso_dado:
                    aviso_dado = True
                    self.log(
                        f"{pkg['name']}: la consola no contesta la API mientras "
                        f"descarga. Sigo el avance por el servidor local.", "warn"
                    )
                self.refresh_rows()
                # Martillar una consola saturada no la despierta antes.
                time.sleep(min(2 * stagnant, POLL_MAX_BACKOFF))
                continue

            if stagnant and aviso_dado:
                self.log(f"{pkg['name']}: la consola volvió a contestar", "ok")
            stagnant = 0
            pkg["stale"] = False

            total = res.get("length_total") or res.get("length") or pkg["size"]
            done = res.get("transferred_total") or res.get("transferred") or 0
            prep = res.get("preparing_percent", 0) or 0
            copy = res.get("local_copy_percent", 0) or 0

            pkg["transferred"] = done
            pkg["length"] = total
            pkg["rest_sec"] = res.get("rest_sec_total") or res.get("rest_sec") or 0

            if pkg["state"] != "paused":
                if done >= total > 0:
                    pkg["state"] = "installing" if copy < 100 else "done"
                elif prep and prep < 100 and done == 0:
                    pkg["state"] = "preparing"
                else:
                    pkg["state"] = "downloading"

            if total > 0 and done >= total and copy >= 100:
                pkg["state"] = "done"
                pkg["polling"] = False
                self.log(f"{pkg['name']}: instalado", "ok")

            if done == last_seen and pkg["state"] == "downloading":
                pkg["idle_ticks"] = pkg.get("idle_ticks", 0) + 1
            else:
                pkg["idle_ticks"] = 0
            last_seen = done

            self.refresh_rows()
            time.sleep(2)

    _RE_RANGE_START = re.compile(r"bytes=(\d+)-")

    def _on_download(self, path, rng=None):
        """
        Cada pedido que atiende el servidor local.

        Además de loguear, de acá sale el progreso de verdad: el header Range
        dice en qué byte va la consola. Es la única fuente que sigue viva
        durante la transferencia — la API de RPI queda muda mientras descarga,
        así que sin esto la barra no se movería nunca.
        """
        # El pedido viene por el alias (/p1.pkg), no por el nombre real, y la
        # consola le cuelga su propia query: ?downloadId=...&threadId=...
        clean = urllib.parse.unquote(path.split("?", 1)[0])
        pkg = next((p for p in self.pkgs if p.get("alias") == clean), None)
        name = pkg["name"] if pkg else (os.path.basename(clean) or path)

        if not rng:
            self.log(f"La PS4 está descargando {name}", "step")
            return

        m = self._RE_RANGE_START.match(rng.strip())
        if not m or not pkg:
            return
        start = int(m.group(1))

        # La consola pide rangos fuera de orden (header, sfo, icono):
        # nos quedamos con la marca más alta alcanzada, nunca retrocede.
        if start > pkg.get("served_pos", 0):
            pkg["served_pos"] = start
            self.refresh_rows()

        # Antes cada rango servido escribía su propia línea: docenas por
        # paquete, todas casi iguales, y los mensajes que importan se
        # ahogaban. Ahora es una línea de avance cada 15 segundos.
        ahora = time.time()
        if ahora - pkg.get("last_log", 0) >= 15:
            pkg["last_log"] = ahora
            hechos, total = self._progress_of(pkg)
            self.log(
                f"{pkg.get('title') or name} · {human_size(hechos)} de "
                f"{human_size(total)} servidos", "step",
            )

    # ------------------------------------------------------------ extracción

    def on_extract(self, _):
        if self.extracting or not self.archives:
            return
        self.extracting = True
        self.btn_extract.disabled = True
        self.arch_bar.visible = True
        self.arch_bar.value = None
        self._safe_update()
        threading.Thread(target=self._extract_worker, daemon=True).start()

    def _extract_worker(self):
        """
        Extrae la tanda entera. Corre en thread aparte: 70 GB de escritura no
        pueden bloquear la ventana ni el servidor que le sirve a la consola.
        """
        pwd = (self.f_pass.value or "").strip()
        borrar = self.cb_delete_archives.value
        pendientes = list(self.archives)
        hechos = 0

        try:
            # Medimos todo antes de escribir un byte: mejor frenar ahora que
            # quedarse sin disco a los 40 GB y dejar un .pkg corrupto.
            necesario = 0
            for a in pendientes:
                try:
                    necesario += archives.inspect_archive(a, pwd).unpacked_size
                except archives.WrongPassword:
                    self.log(f"{a.name}: contraseña incorrecta", "error")
                    return
                except Exception as e:
                    self.log(f"{a.name}: no pude leerlo ({e})", "error")
                    return

            alcanza, libre = archives.check_space(self.folder, necesario)
            if not alcanza:
                self.log(
                    f"No hay espacio: hacen falta {human_size(necesario)} y "
                    f"quedan {human_size(libre)} libres", "error",
                )
                return

            self.log(
                f"Extrayendo {len(pendientes)} comprimido(s) · "
                f"{human_size(necesario)} al terminar", "step",
            )

            for idx, a in enumerate(pendientes, 1):
                dest = os.path.join(os.path.dirname(a.path), a.name)

                def progreso(pct, nombre=a.name, i=idx, n=len(pendientes)):
                    self.arch_bar.value = ((i - 1) + pct) / n
                    self.arch_text.value = f"Extrayendo {nombre}  ·  {pct * 100:.0f}%  ({i}/{n})"
                    self._safe_update()

                try:
                    archives.extract(a, dest, pwd, on_progress=progreso)
                except archives.WrongPassword:
                    self.log(f"{a.name}: contraseña incorrecta", "error")
                    return
                except Exception as e:
                    self.log(f"{a.name}: falló la extracción ({e})", "error")
                    return

                hechos += 1
                self.log(f"{a.name}: extraído", "ok")

                if borrar:
                    for parte in a.parts:
                        try:
                            os.remove(parte)
                        except OSError as e:
                            self.log(
                                f"No pude borrar {os.path.basename(parte)}: {e}", "warn"
                            )

        finally:
            self.extracting = False
            self.arch_bar.visible = False
            self.btn_extract.disabled = False
            if hechos:
                self.log(f"{hechos} comprimido(s) extraído(s)", "ok")
            # Re-escanear: los .pkg nuevos entran solos a la lista de siempre.
            self.scan_folder()
            self._safe_update()

    # ------------------------------------------------------------ instalación

    def on_install(self, _):
        ip = (self.f_ps4.value or "").strip()
        if not ip:
            self.log("Falta la IP de la PS4", "error")
            return

        selected = [p for p in self.pkgs if p["cb"].value]
        if not selected:
            # Dos causas muy distintas bajo el mismo mensaje: se vio en un log
            # real "No seleccionaste ningún paquete" con la lista mostrando
            # todo tildado, y así no hay forma de saber cuál de las dos fue.
            if not self.pkgs:
                self.log("La lista está vacía: no hay paquetes para enviar", "warn")
            else:
                self.log(f"Ninguno de los {len(self.pkgs)} paquetes está tildado", "warn")
            return

        nuevos = self._enqueue(selected)
        if not nuevos:
            self.log("Todo lo tildado ya está en la cola o enviado", "warn")
            return

        if not self._claim_worker():
            # Hay un worker vivo: se lo lleva él. Arrancar un segundo sería
            # mandarle dos installs a la vez a RPI, que atiende de a uno.
            delante = len(self.queue) - len(nuevos)
            cuantos = f"{len(nuevos)} paquete(s)"
            self.log(
                f"{cuantos} a la cola" + (f" ({delante} por delante)" if delante else ""),
                "ok",
            )
            self.refresh_rows()
            return

        threading.Thread(target=self._install_worker, args=(ip,), daemon=True).start()

    def _wait_for_console(self, ip, pkg):
        """
        Espera a que la consola vuelva a atender la API antes del próximo envío.

        RPI sirve la API y los PKG con el mismo hilo: mientras descarga no
        contesta /api. Mandarle el install igual no lo rechaza — lo encola y la
        respuesta con el task_id nunca llega, así que queda una tarea que no se
        puede seguir ni cancelar. Esperar el turno no alarga la descarga (el
        ancho de banda es el mismo), solo conserva el control.

        Devuelve True si la consola está lista, False si hay que abortar.
        """
        if self.ps4_state(ip, timeout=8) == "ok":
            return True

        pkg["state"] = "waiting"
        self.refresh_rows()
        self.log(f"{pkg['name']}: espero a que la consola se libere…", "step")

        caidas = 0
        while not self.stopping:
            estado = self.ps4_state(ip, timeout=8)
            if estado == "ok":
                self.log(f"{pkg['name']}: la consola se liberó", "ok")
                return True
            if estado == "down":
                # "busy" es una consola trabajando y se espera. "down" es que no
                # hay nadie en el puerto; un pico aislado se tolera, tres no.
                caidas += 1
                if caidas >= 3:
                    self.log(
                        f"{pkg['name']}: la consola dejó de responder en {ip}. Corto el envío.",
                        "error",
                    )
                    return False
            else:
                caidas = 0
            time.sleep(POLL_MAX_BACKOFF)

        return False

    def _install_worker(self, ip):
        """
        Vacía la cola, de a un paquete y esperando turno.

        Uno solo de estos corre a la vez: dos POST /api/install simultáneos es
        justo lo que deja tareas huérfanas. Lo que sí puede pasar en cualquier
        momento es que le agreguen paquetes a la cola mientras trabaja — por
        eso la lee en cada vuelta en vez de recibir una lista cerrada.
        """
        self.installing = True
        self.set_install_button(en_cola=True)
        self.progress.visible = True
        self.progress.value = None      # indeterminado
        self._safe_update()

        try:
            self.log(f"Verificando la PS4 en {ip}…", "step")
            state = self.ps4_state(ip)
            if state == "down":
                self.log(f"No hay respuesta en {ip}:{PS4_PORT}. Cancelo.", "error")
                self.set_chip(self.chip_ps4, "PS4 no responde", RED, ft.Icons.ERROR)
                return
            if state == "busy":
                # NO se aborta: que la consola esté bajando algo es lo normal,
                # no un error. Antes moría acá con "Cancelo para no encolar" y
                # no había forma de sumar un paquete sin abrir otra instancia.
                self.log("La consola está ocupada; espero turno para cada envío.", "info")
                self.set_chip(self.chip_ps4, "PS4 ocupada", AMBER, ft.Icons.HOURGLASS_TOP_ROUNDED)
            else:
                self.set_chip(self.chip_ps4, "PS4 lista", GREEN, ft.Icons.CHECK_CIRCLE)

            local = self.f_local.value
            port = self.f_port.value
            i = 0
            ok_count = 0
            enviados = []

            while not self.stopping:
                pkg = self._dequeue()
                if pkg is None:
                    break
                i += 1
                total = i + len(self.queue)   # se mueve: pueden sumar más
                enviados.append(pkg)

                name = pkg["name"]

                # RPI no atiende /api mientras descarga. Mandar igual encola una
                # tarea cuyo id nunca llega: queda huérfana, sin forma de
                # seguirla ni cancelarla. Esperamos el turno.
                if not self._wait_for_console(ip, pkg):
                    self.log("Corto el envío. Los que faltan quedan sin enviar.", "warn")
                    break

                pkg["state"] = "sending"
                pkg["error"] = ""
                self.refresh_rows()

                # La URL no lleva el nombre real: RPI se atraganta con los
                # espacios (y todo lo demás) por más que vayan escapados.
                url = f"http://{local}:{port}{self._publish(pkg)}"
                self.log(f"[{i}/{total}] Enviando {name}", "step")

                body = json.dumps({"type": "direct", "packages": [url]}).encode()
                req = urllib.request.Request(
                    f"http://{ip}:{PS4_PORT}/api/install",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )

                try:
                    with urllib.request.urlopen(req, timeout=INSTALL_TIMEOUT) as resp:
                        data = parse_rpi_json(resp.read())
                    self._handle_install_reply(pkg, data)
                    if pkg["state"] != "error":
                        ok_count += 1

                except urllib.error.HTTPError as e:
                    self._handle_install_reply(pkg, parse_rpi_json(e.read()))
                except socket.timeout:
                    # No es un fallo: el pedido llegó y RPI lo procesa cuando
                    # puede, así que la tarea casi seguro existe. Lo que se
                    # perdió es el task_id, no el paquete.
                    pkg["state"] = "queued"
                    pkg["error"] = "la consola no confirmó; probablemente esté en cola"
                    self.log(f"{name}: sin confirmación en {INSTALL_TIMEOUT} s", "warn")
                    self.log(
                        "Lo más probable es que igual haya quedado en cola. No cierres "
                        "la app ni reinicies la consola: cortarías las descargas en curso.",
                        "info",
                    )
                except urllib.error.URLError as e:
                    pkg["state"] = "error"
                    pkg["error"] = str(e.reason)
                    self.log(f"{name}: no se pudo contactar la consola ({e.reason})", "error")
                except Exception as e:
                    pkg["state"] = "error"
                    pkg["error"] = str(e)
                    self.log(f"{name}: {e}", "error")

                self.refresh_rows()

                if self.queue and not self.stopping:
                    time.sleep(3)

            total = len(enviados)
            if not total:
                return
            sin_confirmar = sum(1 for p in enviados if p.get("state") == "queued")
            if ok_count == total:
                self.log(f"{ok_count} de {total} en cola. Seguí el progreso acá arriba.", "ok")
            elif ok_count or sin_confirmar:
                partes = []
                if ok_count:
                    partes.append(f"{ok_count} confirmado(s)")
                if sin_confirmar:
                    partes.append(f"{sin_confirmar} sin confirmar")
                self.log(f"{' y '.join(partes)} de {total}.", "warn")
            else:
                self.log("La consola rechazó todos los paquetes.", "error")

        finally:
            # El pedido de corte ya se cumplió: si queda arriba, el próximo
            # envío se abortaría solo y sin decir por qué.
            corte = self.stopping
            self.stopping = False

            # Alguien puede haber encolado entre la última vuelta del while y
            # este finally. Si quedó algo, `installing` NO baja —así nadie
            # arranca un segundo worker— y este deja un relevo.
            with self.queue_lock:
                relevo = bool(self.queue) and not corte
                if not relevo:
                    self.installing = False

            if relevo:
                threading.Thread(
                    target=self._install_worker, args=(ip,), daemon=True
                ).start()
                return

            # Lo que quedó sin mandar vuelve a "idle": si se quedara en
            # "pending" no se podría re-encolar nunca (EN_JUEGO lo filtra).
            for pkg in self._drain_queue():
                pkg["state"] = "idle"
            self.set_install_button(en_cola=False)
            self.refresh_rows()

    def on_cancel_all(self, _):
        """Corta el envío en curso y manda stop_task a todo lo que esté activo."""
        self.stopping = True

        # Lo que todavía no salió se descarta acá: no tiene task_id, así que
        # no hay nada que darle de baja a la consola.
        for pkg in self._drain_queue():
            pkg["state"] = "idle"

        active = [p for p in self.pkgs
                  if p.get("task_id") is not None
                  and p.get("state") in ("preparing", "downloading", "installing", "paused")]

        if not active:
            if self.installing:
                # Todavía no hay ningún task_id que dar de baja, pero el envío
                # está en curso (esperando turno, o entre paquete y paquete).
                # `stopping` queda arriba: lo baja _install_worker al cortar.
                self.log("Corto el envío en curso", "warn")
                return
            self.log("No hay tareas activas para cancelar", "info")
            self.stopping = False
            return

        self.log(f"Cancelando {len(active)} tarea(s)…", "warn")

        def work():
            for pkg in active:
                res = self.rpi_call("stop_task", {"task_id": pkg["task_id"]}) or {}
                pkg["polling"] = False
                if str(res.get("status", "")).lower() == "success":
                    pkg["state"] = "cancelled"
                    self.log(f"{pkg['name']}: cancelado", "ok")
                else:
                    self._report_api_error(pkg["name"], res)
                self.refresh_rows()
            self.stopping = False
            self.refresh_rows()

        threading.Thread(target=work, daemon=True).start()

    def _handle_install_reply(self, pkg, data):
        """Interpreta la respuesta de /api/install y arranca el seguimiento."""
        data = data or {}
        if str(data.get("status", "")).lower() == "success":
            pkg["task_id"] = data.get("task_id")
            pkg["state"] = "preparing"
            pkg["transferred"] = 0
            title = data.get("title") or pkg["name"]
            if pkg["task_id"] is not None:
                self.log(f"{pkg['name']}: en cola como «{title}» (tarea {pkg['task_id']})", "ok")
                pkg["polling"] = True
                threading.Thread(target=self.poll_task, args=(pkg,), daemon=True).start()
            else:
                self.log(f"{pkg['name']}: aceptado, pero sin task_id para seguirlo", "warn")
                pkg["state"] = "downloading"
        else:
            pkg["state"] = "error"
            code = data.get("error_code")
            pkg["error"] = (
                f"0x{code:08X}" if isinstance(code, int) else str(data.get("error", "falló"))
            )
            if "error" in data and not isinstance(code, int):
                self._explain(pkg["name"], data["error"])
            else:
                self._report_api_error(pkg["name"], data)

    def _explain(self, name, error):
        """
        Traduce los errores de la consola. Los mensajes salen de server.c del
        Remote PKG Installer; saber en qué línea nacen dice mucho más que el texto.
        """
        self.log(f"{name}: {error}", "error")
        low = str(error).lower()

        if "system file object" in low:
            # server.c:361 — falló sfo_load_from_file() sobre el param.sfo que
            # pkg.c bajó por Range desde un offset interno del PKG. No es firmware.
            self.log(
                "La consola bajó el param.sfo del PKG pero no pudo parsearlo.",
                "info",
            )
            self.log(
                "Suele ser que el servidor devolvió bytes del offset equivocado "
                "(Range mal soportado) o que el .pkg está incompleto o corrupto.",
                "info",
            )
        elif "set up prerequisites" in low:
            # server.c:340 — ni siquiera pudo bajar el header o la entry table.
            self.log(
                "No pudo bajar el header del PKG. Revisá que la URL sea alcanzable "
                "desde la consola y que el archivo esté entero.",
                "info",
            )
        elif "unsupported content type" in low:
            self.log(
                "El PKG no es de un tipo instalable (GD/AC/AL/DP). "
                "Puede ser un archivo de otra plataforma.",
                "info",
            )
        elif "0x80990085" in low or "space" in low:
            self.log("Falta espacio en el disco de la PS4.", "info")
        elif "already" in low or "exist" in low:
            self.log("El paquete ya está instalado o en cola.", "info")


def main(page: ft.Page):
    App(page)


if __name__ == "__main__":
    ft.app(target=main)
