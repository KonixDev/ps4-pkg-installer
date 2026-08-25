#!/usr/bin/env python3
"""
Lectura del formato PKG de PS4.

Un .pkg trae adentro su propia presentación: el título real del juego y su
carátula. Sin esto la lista muestra nombres como
"[DLPSGAME.COM]-EP0082-CUSA02369_00-HITMANGAME000001-A0137-V0100.pkg";
con esto muestra "HITMAN™ - Episode 3: Marrakesh".

Todo sale de la parte NO cifrada del paquete, que es justamente la que RPI se
baja primero cuando se le manda un install. No hace falta la consola.

Sin Flet a propósito: es parseo binario y se testea sin ventana.
"""

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

PKG_MAGIC = b"\x7fCNT"
SFO_MAGIC = b"\x00PSF"

ENTRY_PARAM_SFO = 0x1000
ENTRY_ICON0_PNG = 0x1200

# CATEGORY del param.sfo. Es también el orden correcto de instalación.
CATEGORIES = {"gd": "Juego", "gp": "Actualización", "ac": "Contenido adicional"}
CATEGORY_ORDER = {"gd": 0, "gp": 1, "ac": 2}

CACHE_DIR = Path.home() / ".ps4_pkg_installer_icons"


@dataclass
class PkgInfo:
    title: str = ""
    title_id: str = ""
    category: str = ""
    version: str = ""
    icon_offset: int = 0
    icon_size: int = 0


def _entries(f):
    """{entry_id: (offset, size)} de la tabla de entradas del PKG."""
    f.seek(0)
    head = f.read(0x100)
    if len(head) < 0x100 or head[:4] != PKG_MAGIC:
        return None

    count = struct.unpack(">I", head[0x10:0x14])[0]
    table = struct.unpack(">I", head[0x18:0x1C])[0]
    if count == 0 or count > 20000:
        return {}

    f.seek(table)
    raw = f.read(count * 32)
    out = {}
    for i in range(min(count, len(raw) // 32)):
        eid, _, _, _, off, size = struct.unpack(">IIIIII", raw[i * 32:i * 32 + 24])
        out[eid] = (off, size)
    return out


def _parse_sfo(blob):
    """
    param.sfo → dict.

    Formato propio de Sony: cabecera, tabla de entradas, tabla de claves y
    tabla de datos, todo little-endian — al revés que el PKG que lo contiene,
    que es big-endian.
    """
    if len(blob) < 0x14 or blob[:4] != SFO_MAGIC:
        return {}

    key_table, data_table, count = struct.unpack("<III", blob[0x08:0x14])
    out = {}
    for i in range(count):
        base = 0x14 + i * 16
        if base + 16 > len(blob):
            break
        k_off, fmt, length, _max, d_off = struct.unpack("<HHIII", blob[base:base + 16])
        key = blob[key_table + k_off:].split(b"\x00")[0].decode("utf8", "replace")
        data = blob[data_table + d_off:data_table + d_off + length]
        if fmt == 0x0404:                       # entero de 32 bits
            out[key] = struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0
        else:                                   # utf8 terminado en cero
            out[key] = data.split(b"\x00")[0].decode("utf8", "replace")
    return out


def read_pkg(path):
    """
    Metadata de un PKG, o None si el archivo no es uno.

    Nunca levanta por metadata faltante: un paquete sin param.sfo devuelve un
    PkgInfo vacío y la UI cae al nombre de archivo. De 16 paquetes reales, uno
    no traía ícono.
    """
    try:
        with open(path, "rb") as f:
            ents = _entries(f)
            if ents is None:
                return None

            info = PkgInfo()
            if ENTRY_ICON0_PNG in ents:
                info.icon_offset, info.icon_size = ents[ENTRY_ICON0_PNG]

            if ENTRY_PARAM_SFO in ents:
                off, size = ents[ENTRY_PARAM_SFO]
                if 0 < size <= 1 << 20:
                    f.seek(off)
                    sfo = _parse_sfo(f.read(size))
                    info.title = str(sfo.get("TITLE", "") or "")
                    info.title_id = str(sfo.get("TITLE_ID", "") or "")
                    info.category = str(sfo.get("CATEGORY", "") or "")
                    info.version = str(sfo.get("APP_VER") or sfo.get("VERSION") or "")
            return info
    except (OSError, struct.error):
        return None


def icon_path(pkg_path, info, cache_dir=None):
    """
    Vuelca el icon0.png a un caché en disco y devuelve su ruta.

    La clave incluye tamaño y mtime del PKG: si el archivo cambia, cambia la
    clave y se vuelve a extraer sola. Flet dibuja imágenes desde archivo, así
    que el caché evita releer un paquete de 16 GB en cada escaneo.
    """
    if not info or info.icon_size <= 0:
        return None

    directory = Path(cache_dir) if cache_dir else CACHE_DIR
    try:
        st = os.stat(pkg_path)
        clave = f"{os.path.abspath(pkg_path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return None

    dest = directory / (hashlib.sha1(clave.encode()).hexdigest() + ".png")
    if dest.exists():
        return str(dest)

    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(pkg_path, "rb") as f:
            f.seek(info.icon_offset)
            data = f.read(info.icon_size)
        if not data:
            return None
        # Escritura atómica: un PNG a medias haría que Flet dibuje un roto.
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return str(dest)
    except OSError:
        return None
