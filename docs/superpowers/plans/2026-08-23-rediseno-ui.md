# Rediseño de la UI — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Que la app muestre juegos en vez de nombres de archivo — título e ícono leídos del propio PKG, agrupados por categoría — y que la lista pueda filtrarse y seleccionarse en conjunto, con el registro fuera de la pantalla principal.

**Architecture:** La lectura del formato PKG vive en `pkgmeta.py`, sin Flet, hermano de `archives.py`: parsea la tabla de entradas, lee `param.sfo` y vuelca `icon0.png` a un caché en disco. La UI consume esa metadata y se reorganiza en dos pestañas; la lógica de filtrado y selección se extrae a funciones puras para poder testearla sin ventana.

**Tech Stack:** Python 3.11, Flet 0.28.x, pytest. Sin dependencias nuevas: el parseo es `struct` de la biblioteca estándar.

**Spec:** `docs/superpowers/specs/2026-08-23-rediseno-ui.html` — maqueta interactiva aprobada, con las notas de diseño incluidas. Abrirla en un navegador antes de empezar: es la referencia visual exacta.

## Global Constraints

- Repo: `ps4-pkg-installer/`. La carpeta padre es una copia vieja, no tocarla.
- Paleta existente, sin cambios: `BG #0f1115`, `SURFACE #181b22`, `SURFACE_2 #212530`, `BORDER #2c313d`, `TEXT #e6e8eb`, `MUTED #8b929e`, `BLUE #4a9eff`, `GREEN #3ddc84`, `AMBER #ffb454`, `RED #ff5f56`.
- Comentarios en castellano rioplatense explicando el porqué, no el qué.
- No romper lo que ya anda: `poll_task`, `_wait_for_console`, `_extract_worker` y el servidor HTTP quedan intactos. Los 60 tests actuales deben seguir pasando.
- La vista por defecto es **lista**; cuadrícula a un clic.
- Leer metadata NO puede bloquear la ventana: va en thread, como el resto de los workers.
- Todo PKG sin `param.sfo` legible cae al nombre de archivo. Nunca romper por metadata ausente: de 16 paquetes reales, 15 tenían ícono y 1 no.

## Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `pkgmeta.py` *(nuevo)* | Formato PKG: tabla de entradas, `param.sfo`, extracción de `icon0.png` a caché. Sin Flet, sin estado global. |
| `tests/test_pkgmeta.py` *(nuevo)* | Parseo con PKG sintéticos construidos en el test. |
| `tests/test_ui_selection.py` *(nuevo)* | Filtro, selección en conjunto y agrupación, como funciones puras. |
| `ps4_pkg_installer.py` | Pestañas, barra de estado, toolbar, grupos, fila con ícono, tile, y el enganche de la metadata al escaneo. |

---

### Task 1: Leer la tabla de entradas y el param.sfo

**Files:**
- Create: `pkgmeta.py`
- Create: `tests/test_pkgmeta.py`

**Interfaces:**
- Consumes: nada
- Produces: `PkgInfo` (dataclass: `title: str`, `title_id: str`, `category: str`, `version: str`, `icon_offset: int`, `icon_size: int`), `read_pkg(path: str) -> PkgInfo | None` — None si no es un PKG legible

- [x] **Step 1: Escribir el test que falla**

`tests/test_pkgmeta.py`:

```python
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pkgmeta


def _sfo(pairs):
    """Arma un param.sfo mínimo con pares clave/valor de texto."""
    keys, data, entries = b"", b"", b""
    for k, v in pairs:
        vb = v.encode() + b"\x00"
        entries += struct.pack("<HHIII", len(keys), 0x0204, len(vb), len(vb), len(data))
        keys += k.encode() + b"\x00"
        data += vb
    key_table = 0x14 + len(entries)
    data_table = key_table + len(keys)
    head = b"\x00PSF" + struct.pack("<I", 0x0101) + struct.pack(
        "<III", key_table, data_table, len(pairs))
    return head + entries + keys + data


def _pkg(tmp_path, sfo_blob, icon=b"\x89PNG_fake", name="t.pkg"):
    """
    PKG mínimo: header con magic, cantidad de entradas y offset de la tabla,
    después la tabla y los blobs. Alcanza para ejercitar el parseo.
    """
    table_off = 0x100
    entry_count = 2
    sfo_off = table_off + entry_count * 32
    icon_off = sfo_off + len(sfo_blob)

    head = bytearray(b"\x00" * table_off)
    head[0:4] = b"\x7fCNT"
    head[0x10:0x14] = struct.pack(">I", entry_count)
    head[0x18:0x1C] = struct.pack(">I", table_off)

    table = struct.pack(">IIIIII", 0x1000, 0, 0, 0, sfo_off, len(sfo_blob)) + b"\x00" * 8
    table += struct.pack(">IIIIII", 0x1200, 0, 0, 0, icon_off, len(icon)) + b"\x00" * 8

    p = tmp_path / name
    p.write_bytes(bytes(head) + table + sfo_blob + icon)
    return str(p)


def test_lee_titulo_categoria_y_id(tmp_path):
    path = _pkg(tmp_path, _sfo([
        ("CATEGORY", "gd"), ("TITLE", "HITMAN™"), ("TITLE_ID", "CUSA02369"),
    ]))

    info = pkgmeta.read_pkg(path)

    assert info.title == "HITMAN™"
    assert info.title_id == "CUSA02369"
    assert info.category == "gd"


def test_ubica_el_icono_sin_leerlo(tmp_path):
    path = _pkg(tmp_path, _sfo([("TITLE", "X")]), icon=b"\x89PNG" + b"z" * 40)

    info = pkgmeta.read_pkg(path)

    assert info.icon_size == 44
    assert info.icon_offset > 0


def test_un_archivo_que_no_es_pkg_devuelve_None(tmp_path):
    p = tmp_path / "no.pkg"
    p.write_bytes(b"esto no es un paquete")

    assert pkgmeta.read_pkg(str(p)) is None


def test_pkg_sin_sfo_no_explota(tmp_path):
    head = bytearray(b"\x00" * 0x100)
    head[0:4] = b"\x7fCNT"
    head[0x10:0x14] = struct.pack(">I", 0)
    head[0x18:0x1C] = struct.pack(">I", 0x100)
    p = tmp_path / "vacio.pkg"
    p.write_bytes(bytes(head))

    info = pkgmeta.read_pkg(str(p))

    assert info is not None
    assert info.title == ""
    assert info.icon_size == 0
```

- [x] **Step 2: Correr y verificar que falla**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_pkgmeta.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'pkgmeta'`

- [x] **Step 3: Implementar el parseo**

```python
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

import os
import struct
from dataclasses import dataclass

PKG_MAGIC = b"\x7fCNT"
SFO_MAGIC = b"\x00PSF"

ENTRY_PARAM_SFO = 0x1000
ENTRY_ICON0_PNG = 0x1200

# CATEGORY del param.sfo. Es también el orden correcto de instalación.
CATEGORIES = {"gd": "Juego", "gp": "Actualización", "ac": "Contenido adicional"}
CATEGORY_ORDER = {"gd": 0, "gp": 1, "ac": 2}


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
    param.sfo → dict. Formato propio de Sony: cabecera, tabla de entradas,
    tabla de claves y tabla de datos, todo little-endian (al revés que el PKG
    que lo contiene, que es big-endian).
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
```

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_pkgmeta.py -v`
Expected: PASS (4 tests)

- [x] **Step 5: Verificar contra paquetes reales**

Si hay PKG en `~/Downloads/PS4`, correr a mano:

```bash
cd ps4-pkg-installer && python3 -c "
import glob, pkgmeta
for p in sorted(glob.glob('/Users/cellcaribe/Downloads/PS4/**/*.pkg', recursive=True))[:8]:
    i = pkgmeta.read_pkg(p)
    print(f'[{i.category or \"--\"}] {i.title or \"(sin título)\"}')
"
```

Esperado: títulos legibles y categorías `gd` / `gp` / `ac`. Si sale vacío, el parseo está mal aunque los tests pasen.

- [x] **Step 6: Commit**

```bash
cd ps4-pkg-installer
git add pkgmeta.py tests/test_pkgmeta.py
git commit -m "feat(pkgmeta): leer titulo, categoria e id del param.sfo"
```

---

### Task 2: Caché de íconos en disco

Flet dibuja imágenes desde un archivo. Volcar el `icon0.png` una vez y reusarlo evita releer un PKG de 16 GB en cada escaneo.

**Files:**
- Modify: `pkgmeta.py`
- Modify: `tests/test_pkgmeta.py`

**Interfaces:**
- Consumes: `read_pkg` / `PkgInfo` (Task 1)
- Produces: `icon_path(pkg_path: str, info: PkgInfo, cache_dir: str | None = None) -> str | None` — ruta al PNG en caché, o None si el paquete no trae ícono

- [x] **Step 1: Escribir los tests que fallan**

```python
def test_extrae_el_icono_al_cache(tmp_path):
    path = _pkg(tmp_path, _sfo([("TITLE", "X")]), icon=b"\x89PNG" + b"q" * 30)
    info = pkgmeta.read_pkg(path)
    cache = tmp_path / "cache"

    dest = pkgmeta.icon_path(path, info, str(cache))

    assert dest and os.path.exists(dest)
    assert open(dest, "rb").read() == b"\x89PNG" + b"q" * 30


def test_no_reextrae_si_ya_esta_en_cache(tmp_path):
    path = _pkg(tmp_path, _sfo([("TITLE", "X")]))
    info = pkgmeta.read_pkg(path)
    cache = tmp_path / "cache"

    primero = pkgmeta.icon_path(path, info, str(cache))
    os.utime(primero, (0, 0))
    mtime = os.path.getmtime(primero)
    segundo = pkgmeta.icon_path(path, info, str(cache))

    assert segundo == primero
    assert os.path.getmtime(segundo) == mtime, "lo volvió a escribir"


def test_sin_icono_devuelve_None(tmp_path):
    info = pkgmeta.PkgInfo(title="X")      # icon_size = 0

    assert pkgmeta.icon_path("/x/y.pkg", info, str(tmp_path)) is None
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_pkgmeta.py -v -k icono or cache`
Expected: FAIL con `AttributeError: module 'pkgmeta' has no attribute 'icon_path'`

- [x] **Step 3: Implementar el caché**

Agregar a `pkgmeta.py`:

```python
import hashlib
from pathlib import Path

CACHE_DIR = Path.home() / ".ps4_pkg_installer_icons"


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
```

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_pkgmeta.py -v`
Expected: PASS (7 tests)

- [x] **Step 5: Commit**

```bash
cd ps4-pkg-installer
git add pkgmeta.py tests/test_pkgmeta.py
git commit -m "feat(pkgmeta): cache de iconos en disco"
```

---

### Task 3: Filtro, selección en conjunto y agrupación

Lógica pura, fuera de la UI, para poder testear sin ventana. Es la parte con reglas que sorprenden si se hacen mal.

**Files:**
- Modify: `ps4_pkg_installer.py`
- Create: `tests/test_ui_selection.py`

**Interfaces:**
- Consumes: `CATEGORY_ORDER`, `CATEGORIES` (Task 1)
- Produces: en `ps4_pkg_installer.py`, funciones de módulo:
  - `matches(pkg: dict, query: str) -> bool`
  - `apply_bulk(pkgs: list[dict], accion: str, visibles: set[int]) -> None` — `accion` ∈ `"all" | "none" | "invert"`; muta `pkg["cb"].value`
  - `group_key(pkg: dict) -> tuple[int, str]` — para ordenar por categoría y después por título

- [x] **Step 1: Escribir los tests que fallan**

`tests/test_ui_selection.py`:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps4_pkg_installer as app


class _CB:
    """Sustituto del checkbox de Flet: solo value y disabled."""
    def __init__(self, value=True, disabled=False):
        self.value, self.disabled = value, disabled


def _pkg(name="a.pkg", title="", cat="ac", sub="", value=True, disabled=False):
    return {"name": name, "title": title, "category": cat, "sub": sub,
            "cb": _CB(value, disabled)}


def test_el_filtro_mira_titulo_nombre_y_subcarpeta():
    p = _pkg(name="X-CUSA02369.pkg", title="HITMAN™ - Episode 3: Marrakesh",
             sub="Hitman.DLCs")

    assert app.matches(p, "marrakesh") is True      # por título
    assert app.matches(p, "cusa02369") is True      # por nombre de archivo
    assert app.matches(p, "hitman.dlcs") is True    # por subcarpeta
    assert app.matches(p, "sapienza") is False


def test_el_filtro_vacio_deja_pasar_todo():
    assert app.matches(_pkg(), "") is True
    assert app.matches(_pkg(), "   ") is True


def test_todos_solo_toca_lo_visible():
    """La regla que no sorprende: lo que el filtro esconde conserva su tilde."""
    pkgs = [_pkg("a", value=False), _pkg("b", value=False), _pkg("c", value=False)]

    app.apply_bulk(pkgs, "all", visibles={0, 2})

    assert [p["cb"].value for p in pkgs] == [True, False, True]


def test_ninguno_solo_toca_lo_visible():
    pkgs = [_pkg("a"), _pkg("b"), _pkg("c")]

    app.apply_bulk(pkgs, "none", visibles={1})

    assert [p["cb"].value for p in pkgs] == [True, False, True]


def test_invertir_solo_toca_lo_visible():
    pkgs = [_pkg("a", value=True), _pkg("b", value=False)]

    app.apply_bulk(pkgs, "invert", visibles={0, 1})

    assert [p["cb"].value for p in pkgs] == [False, True]


def test_el_bulk_no_pisa_una_tarea_en_curso():
    """El checkbox de un paquete descargando está deshabilitado: se saltea."""
    pkgs = [_pkg("a", value=True, disabled=True), _pkg("b", value=True)]

    app.apply_bulk(pkgs, "none", visibles={0, 1})

    assert pkgs[0]["cb"].value is True, "pisó una tarea en curso"
    assert pkgs[1]["cb"].value is False


def test_orden_por_categoria_es_el_orden_de_instalacion():
    pkgs = [_pkg("d", cat="ac", title="DLC"), _pkg("b", cat="gd", title="Juego"),
            _pkg("u", cat="gp", title="Update")]

    assert [p["title"] for p in sorted(pkgs, key=app.group_key)] == \
    ["Juego", "Update", "DLC"]
```

Reemplazar `    ["Juego", "Update", "DLC"]` por: `["Juego", "Update", "DLC"]`

```python
def test_dentro_de_un_grupo_ordena_por_titulo():
    pkgs = [_pkg("z", cat="ac", title="Sapienza"), _pkg("a", cat="ac", title="Bangkok")]

    assert [p["title"] for p in sorted(pkgs, key=app.group_key)] == ["Bangkok", "Sapienza"]


def test_sin_categoria_cae_al_final():
    pkgs = [_pkg("x", cat="", title="Sin cat"), _pkg("g", cat="gd", title="Juego")]

    assert [p["title"] for p in sorted(pkgs, key=app.group_key)] == ["Juego", "Sin cat"]
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_ui_selection.py -v`
Expected: FAIL con `AttributeError: module 'ps4_pkg_installer' has no attribute 'matches'`

- [x] **Step 3: Implementar las tres funciones**

En `ps4_pkg_installer.py`, después de `human_eta` y antes de `choose_folder`:

```python
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
```

Agregar `import pkgmeta` junto a `import archives`.

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_ui_selection.py -v`
Expected: PASS (9 tests)

- [x] **Step 5: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py tests/test_ui_selection.py
git commit -m "feat(ui): filtro, seleccion en conjunto y orden por categoria"
```

---

### Task 4: Enganchar la metadata al escaneo

**Files:**
- Modify: `ps4_pkg_installer.py` — `scan_folder()` y el dict de paquete

**Interfaces:**
- Consumes: `read_pkg`, `icon_path` (Tasks 1-2), `group_key` (Task 3)
- Produces: cada dict de `self.pkgs` gana `title`, `category`, `version`, `icon`

- [x] **Step 1: Sumar los campos al dict de paquete**

En `scan_folder()`, donde se arma el dict nuevo, agregar a la definición:

```python
                    "title": "", "category": "", "version": "", "icon": None,
```

- [x] **Step 2: Leer la metadata en segundo plano**

Agregar el método y llamarlo al final de `scan_folder()`, junto a `self._scan_archives()`:

```python
    def _load_meta_async(self):
        """
        Lee título, categoría e ícono de cada PKG en un thread.

        Abrir 400 archivos y volcar sus carátulas no puede pasar en el hilo de
        la UI. Los paquetes ya se listaron con su nombre de archivo; cuando la
        metadata llega, las filas se repintan y se reordenan.
        """
        objetivo = list(self.pkgs)

        def worker():
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
            if not self.stopping:
                self.pkgs.sort(key=group_key)
                self.rebuild_list()

        threading.Thread(target=worker, daemon=True).start()
```

- [x] **Step 3: Probar a mano contra paquetes reales**

```bash
cd ps4-pkg-installer && python3 ps4_pkg_installer.py
```

Apuntar a una carpeta con PKG reales. Esperado: la lista aparece al instante con nombres de archivo y, un momento después, se reordena mostrando títulos e íconos. La ventana no se congela en ningún momento.

- [x] **Step 4: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py
git commit -m "feat(ui): leer la metadata de cada PKG en segundo plano"
```

---

### Task 5: Layout nuevo — pestañas, barra de estado y toolbar

**Files:**
- Modify: `ps4_pkg_installer.py` — `_build()` completo

**Interfaces:**
- Consumes: `matches`, `apply_bulk` (Task 3)
- Produces: `self.tabs`, `self.f_filter`, `self.view_mode` (`"rows"` | `"tiles"`), `self.list_container`, `self.rebuild_list()`

- [x] **Step 1: Reemplazar la estructura de `_build()`**

Sacar `_card()` y sus dos llamadas. La raíz pasa a ser:

```python
        self.view_mode = "rows"

        self.f_filter = ft.TextField(
            hint_text="Filtrar…", dense=True, height=38, expand=True,
            border_color=BORDER, color=TEXT, prefix_icon=ft.Icons.SEARCH,
            on_change=lambda _: self.rebuild_list(),
        )
        bulk = ft.Row(spacing=6, controls=[
            ft.TextButton("Todos", on_click=lambda _: self.on_bulk("all")),
            ft.TextButton("Ninguno", on_click=lambda _: self.on_bulk("none")),
            ft.TextButton("Invertir", on_click=lambda _: self.on_bulk("invert")),
        ])
        self.seg_view = ft.SegmentedButton(
            selected={"rows"},
            segments=[
                ft.Segment(value="rows", label=ft.Text("Lista")),
                ft.Segment(value="tiles", label=ft.Text("Cuadrícula")),
            ],
            on_change=self.on_view_change,
        )
        self.visible_label = ft.Text("", size=12, color=MUTED)

        toolbar = ft.Row(spacing=10, controls=[
            ft.Container(width=250, content=self.f_filter),
            bulk, self.seg_view, self.visible_label,
        ])

        self.list_container = ft.ListView(spacing=4, padding=6, expand=True)

        instalar = ft.Column(spacing=12, expand=True, controls=[
            folder_row, self.arch_banner, toolbar,
            ft.Container(
                expand=True, bgcolor=SURFACE, border_radius=10,
                border=ft.border.all(1, BORDER), content=self.list_container,
            ),
            ft.Row(controls=[self.count_label, ft.Container(expand=True),
                             self.btn_cancel, self.btn_install]),
        ])

        registro = ft.Column(spacing=10, expand=True, controls=[
            ft.Container(expand=True, bgcolor=SURFACE, border_radius=10,
                         border=ft.border.all(1, BORDER), content=self.log_view),
            ft.Row(controls=[
                ft.TextButton("Copiar", on_click=self.on_copy_log),
                ft.TextButton("Limpiar", on_click=lambda _: self.clear_log()),
            ]),
        ])

        self.tabs = ft.Tabs(
            selected_index=0, expand=True, indicator_color=BLUE,
            label_color=TEXT, unselected_label_color=MUTED,
            tabs=[ft.Tab(text="Instalar", content=ft.Container(padding=14, content=instalar)),
                  ft.Tab(text="Registro", content=ft.Container(padding=14, content=registro))],
        )
```

`folder_row` es la fila de carpeta que ya existe (label + botón Cambiar), extraída de la tarjeta vieja sin cambios.

- [x] **Step 2: Los handlers de la toolbar**

```python
    def on_bulk(self, accion):
        visibles = {i for i, p in enumerate(self.pkgs)
                    if matches(p, self.f_filter.value)}
        apply_bulk(self.pkgs, accion, visibles)
        self.rebuild_list()

    def on_view_change(self, e):
        self.view_mode = next(iter(e.control.selected), "rows")
        self.rebuild_list()
```

- [x] **Step 3: Probar que la ventana abre**

```bash
cd ps4-pkg-installer && python3 ps4_pkg_installer.py
```

Esperado: dos pestañas, la lista ocupando el alto, el filtro y los tres botones respondiendo. El registro en su pestaña.

- [x] **Step 4: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py
git commit -m "feat(ui): pestanas, toolbar de filtro y seleccion"
```

---

### Task 6: Lista con ícono, grupos y franja de estado

**Files:**
- Modify: `ps4_pkg_installer.py` — `_build_row()`, `refresh_rows()`, nuevo `rebuild_list()`

**Interfaces:**
- Consumes: `matches`, `group_key` (Task 3), `CATEGORIES` (Task 1)
- Produces: `rebuild_list()`, `_group_header(cat, n)`, `_tile(pkg)`

- [x] **Step 1: Colores de franja por estado**

```python
    # La franja izquierda repite el estado en forma además de en texto: con
    # dieciséis paquetes, encontrar el que necesita atención deja de ser lectura.
    STRIPE = {
        "downloading": BLUE, "preparing": BLUE, "sending": BLUE,
        "installing": AMBER, "paused": AMBER, "waiting": AMBER, "queued": AMBER,
        "done": GREEN, "error": RED, "cancelled": BORDER, "unknown": BORDER,
    }
```

- [x] **Step 2: Encabezado de grupo**

```python
    def _group_header(self, cat, n):
        return ft.Container(
            padding=ft.padding.only(8, 10, 8, 4),
            content=ft.Row(spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text((pkgmeta.CATEGORIES.get(cat) or "Otros").upper(),
                            size=11, weight=ft.FontWeight.W_600, color=MUTED),
                    ft.Container(expand=True, height=1, bgcolor=BORDER),
                    ft.Text(str(n), size=11, color="#69707d"),
                ]),
        )
```

- [x] **Step 3: Reconstruir la lista aplicando filtro y grupos**

```python
    def rebuild_list(self):
        """
        Rearma la lista visible: filtra, agrupa por categoría y pinta.

        Se rearma en vez de ocultar porque el cambio de vista cambia el tipo de
        control. El estado vivo de cada paquete vive en su dict, no en la fila,
        así que reconstruir no corta ninguna tarea en curso.
        """
        self.list_container.controls.clear()
        q = self.f_filter.value if hasattr(self, "f_filter") else ""
        visibles = [p for p in self.pkgs if matches(p, q)]

        cur = object()
        cajon = None
        for pkg in visibles:
            cat = pkg.get("category") or ""
            if cat != cur:
                cur = cat
                n = sum(1 for p in visibles if (p.get("category") or "") == cat)
                self.list_container.controls.append(self._group_header(cat, n))
                if self.view_mode == "tiles":
                    cajon = ft.Row(wrap=True, spacing=8, run_spacing=8)
                    self.list_container.controls.append(cajon)
                else:
                    cajon = None
            control = self._tile(pkg) if self.view_mode == "tiles" else self._build_row(pkg)
            (cajon.controls if cajon is not None else self.list_container.controls).append(control)

        oculto = len(self.pkgs) - len(visibles)
        self.visible_label.value = (
            f"{len(visibles)} a la vista · {oculto} ocultos" if oculto else ""
        )
        self.refresh_rows()
        self._refresh_count()
```

- [x] **Step 4: La fila con ícono y franja**

Reemplazar el `ft.Container` que devuelve `_build_row` por:

```python
        pkg["ui_stripe"] = ft.Container(width=3, border_radius=2, bgcolor="transparent")
        icono = (
            ft.Image(src=pkg["icon"], width=34, height=34, border_radius=6, fit=ft.ImageFit.COVER)
            if pkg.get("icon") else
            ft.Container(width=34, height=34, border_radius=6, bgcolor=SURFACE_2,
                         alignment=ft.alignment.center,
                         content=ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=16, color=MUTED))
        )
        titulo = pkg.get("title") or pkg["name"]

        row = ft.Container(
            padding=ft.padding.symmetric(6, 8), border_radius=8,
            content=ft.Row(spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    pkg["ui_stripe"], pkg["cb"], icono,
                    ft.Column(spacing=1, expand=True, controls=[
                        ft.Text(titulo, size=13, color=TEXT, no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(pkg["name"], size=10.5, color="#69707d", no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        pkg["ui_bar"],
                    ]),
                    ft.Text(human_size(pkg["size"]), size=11.5, color=MUTED, width=70,
                            text_align=ft.TextAlign.RIGHT),
                    pkg["ui_state"], pkg["ui_pause"], pkg["ui_resume"], pkg["ui_stop"],
                ]),
        )
```

- [x] **Step 5: Pintar la franja en `_paint_row`**

Agregar al principio de `_paint_row`, después de resolver `state`:

```python
        if "ui_stripe" in pkg:
            pkg["ui_stripe"].bgcolor = self.STRIPE.get(state) or "transparent"
```

- [x] **Step 6: Probar a mano**

```bash
cd ps4-pkg-installer && python3 ps4_pkg_installer.py
```

Verificar contra la maqueta: grupos con su encabezado, íconos, título arriba y nombre de archivo abajo en gris, franja de color a la izquierda. Filtrar por `outfit` deja tres. El paquete sin ícono muestra el marcador de respaldo, no un hueco.

- [x] **Step 7: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py
git commit -m "feat(ui): filas con icono, grupos por categoria y franja de estado"
```

---

### Task 7: Vista cuadrícula

**Files:**
- Modify: `ps4_pkg_installer.py` — nuevo `_tile()`

**Interfaces:**
- Consumes: `rebuild_list` (Task 6)
- Produces: `_tile(pkg) -> ft.Container`

- [x] **Step 1: Implementar el tile**

```python
    def _tile(self, pkg):
        """
        Tarjeta para la vista cuadrícula.

        Sirve para elegir: catorce carátulas se reconocen de un vistazo, catorce
        nombres de archivo no. Para mirar instalar sigue siendo mejor la lista,
        que tiene ancho para la barra y alinea los tamaños en columna.
        """
        seleccionado = pkg["cb"].value
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

        def alternar(_):
            if pkg["cb"].disabled:
                return
            pkg["cb"].value = not pkg["cb"].value
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
```

- [x] **Step 2: Probar a mano**

```bash
cd ps4-pkg-installer && python3 ps4_pkg_installer.py
```

Verificar: el botón *Cuadrícula* cambia la vista, los tiles seleccionados quedan con borde azul y fondo tenue, un clic alterna la selección, y un paquete en curso no se deja alternar. *Todos* y *Ninguno* siguen funcionando en esta vista.

- [x] **Step 3: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py
git commit -m "feat(ui): vista cuadricula con caratulas"
```

---

### Task 8: Registro legible

**Files:**
- Modify: `ps4_pkg_installer.py` — `_on_download()`

**Interfaces:**
- Consumes: `_progress_of` (ya existe)
- Produces: nada

- [x] **Step 1: Fundir las líneas de rango en una sola**

Hoy cada `Range` servido escribe su propia línea: docenas por paquete, todas casi iguales, y los mensajes que importan se ahogan. En `_on_download`, reemplazar el `self.log(f"La PS4 pide {rng} de {name}", "step")` por una línea que se emite como mucho una vez cada 15 segundos por paquete:

```python
        ahora = time.time()
        if ahora - pkg.get("last_log", 0) >= 15:
            pkg["last_log"] = ahora
            hechos, total = self._progress_of(pkg)
            self.log(
                f"{pkg.get('title') or name} · {human_size(hechos)} de "
                f"{human_size(total)} servidos", "step",
            )
```

Ese bloque va dentro del `for pkg in self.pkgs` que ya existe, después de actualizar `served_pos`. La línea suelta de antes se borra.

- [x] **Step 2: Verificar los tests que tocan `_on_download`**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_poll.py -v`
Expected: PASS. Los tests de `served_pos` no miran el log, así que deben seguir en verde sin cambios.

- [x] **Step 3: Correr toda la batería**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/ -q`
Expected: PASS, sin regresiones.

- [x] **Step 4: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py
git commit -m "fix(log): una linea de avance por paquete en vez de una por rango"
```

---

## Verificación final

```bash
cd ps4-pkg-installer && python3 -m pytest tests/ -q
```

Todo en verde, y la ventana abierta a mano contra una carpeta con PKG reales, comparada con `docs/superpowers/specs/2026-08-23-rediseno-ui.html`.

**No compilar mientras haya una transferencia en curso:** el build sobreescribe `dist/`, que es el binario en ejecución. Verificar con `pgrep -f "PS4 PKG Installer"` antes.
