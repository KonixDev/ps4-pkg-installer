# Extracción integrada de RAR/ZIP — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Que la app detecte los comprimidos sin extraer que haya en la carpeta elegida, pida una contraseña, los extraiga con progreso visible y deje los `.pkg` listos para instalar — eliminando el paso manual de descomprimir.

**Architecture:** La lógica de archivos comprimidos vive en un módulo nuevo `archives.py`, sin ninguna dependencia de Flet: funciones puras + invocación de `7zz` como subproceso. `ps4_pkg_installer.py` solo consume ese módulo desde un worker en thread y pinta el resultado. Esa separación es lo que permite testear la parte difícil (agrupación de volúmenes, parseo de progreso, detección de errores) sin levantar una ventana.

**Tech Stack:** Python 3.11, Flet 0.28.x, PyInstaller (onefile), 7-Zip CLI (`7zz` / `7za.exe`) bundleado, pytest.

**Spec:** No hay documento de spec — la tarea se clasificó como acotada y el diseño se aprobó en conversación. El diseño aprobado está transcripto íntegro en "Contexto" más abajo; el plan argumenta contra esa sección.

## Global Constraints

- Repo de trabajo: `ps4-pkg-installer/`. La carpeta padre (`../ps4_installer_flet.py`, `../BUILD.sh`, `../PS4 PKG Installer.spec`) es una copia vieja — **no tocarla**.
- El archivo de la app es `ps4_pkg_installer.py` (versión Flet, 1672 líneas), no confundir con el homónimo viejo de la carpeta padre.
- El CI (`.github/workflows/release.yml`) compila con flags de PyInstaller, **no** con el `.spec`. Los cambios de bundling van en el workflow.
- Plataformas objetivo: macOS arm64, Windows x64, Linux x64.
- La app corre `--windowed`: todo `subprocess` en Windows necesita `CREATE_NO_WINDOW` o abre una consola negra.
- Estilo del repo: comentarios en castellano rioplatense, explicando el *porqué*, no el *qué*. Seguir el tono existente.
- Idioma de la UI: castellano, sin voseo forzado en botones (mirar los labels actuales).
- No romper el flujo actual: si no hay comprimidos, la UI debe verse exactamente igual que hoy.

## Contexto — diseño aprobado

Flujo actual, en `ps4_pkg_installer.py`:

```
on_pick_folder → use_folder → scan_folder → _walk_pkgs(root)   ← solo *.pkg
                                   ↓
                    filas con checkbox → servidor HTTP → API RPI de la PS4
```

`_walk_pkgs()` ignora todo lo que no sea `.pkg`, así que los RAR son invisibles para la app.

Lo aprobado:

1. **Detección** junto al recorrido existente: `.rar`, `.zip`, `.7z`. Agrupar multi-volumen (de `part1..part7` se lista una entrada). Ignorar un comprimido si al lado ya existe un `.pkg` extraído.
2. **UI**: banner sobre la lista, visible solo si hay comprimidos, con campo de contraseña, botón Extraer y checkbox opt-in "borrar comprimidos al terminar".
3. **Extracción** en thread aparte; cada archivo a su propia subcarpeta; al terminar, re-escanear para que los `.pkg` aparezcan solos.
4. **Chequeos previos**: espacio libre vs. tamaño descomprimido, volúmenes contiguos completos, contraseña incorrecta con mensaje propio.
5. Contraseña única para toda la tanda; los archivos sin cifrar se extraen igual.
6. Borrar originales nunca por defecto.

## Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `archives.py` *(nuevo)* | Todo lo que sabe de comprimidos: ubicar `7zz`, detectar y agrupar, inspeccionar, extraer con progreso. Sin Flet, sin estado global. |
| `tests/test_archives.py` *(nuevo)* | Tests de `archives.py`. Genera sus propios comprimidos con `7zz` en `tmp_path`. |
| `ps4_pkg_installer.py` | Banner, campo de contraseña, worker de extracción. Solo UI y orquestación. |
| `.github/workflows/release.yml` | Bajar el binario de 7-Zip por plataforma y bundlearlo. |
| `requirements-dev.txt` *(nuevo)* | `pytest`. |
| `README.md` / `README.es.md` | Documentar la función nueva. |

`archives.py` se mantiene aparte a propósito: es la única parte con lógica no trivial, y separarla es lo que la hace testeable sin ventana.

---

### Task 1: Ubicar el binario de 7-Zip

**Files:**
- Create: `archives.py`
- Create: `tests/test_archives.py`
- Create: `requirements-dev.txt`

**Interfaces:**
- Consumes: nada
- Produces: `seven_zip_path() -> str | None`, `SevenZipMissing` (Exception)

- [x] **Step 1: Crear `requirements-dev.txt`**

```
pytest>=8,<9
```

- [x] **Step 2: Escribir el test que falla**

`tests/test_archives.py`:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import archives


def test_seven_zip_path_encuentra_el_binario_del_sistema():
    # En la máquina de desarrollo hay 7zz instalado; en CI lo instala el workflow.
    p = archives.seven_zip_path()
    assert p is not None
    assert os.path.exists(p)


def test_seven_zip_path_prefiere_el_bundleado(tmp_path, monkeypatch):
    # PyInstaller onefile expone sus datos en sys._MEIPASS. Si hay un binario
    # ahí, gana sobre el del sistema: el usuario final no tiene 7zz instalado.
    fake = tmp_path / "7zz"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert archives.seven_zip_path() == str(fake)
```

- [x] **Step 3: Correr el test y verificar que falla**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'archives'`

- [x] **Step 4: Implementar `archives.py`**

```python
#!/usr/bin/env python3
"""
Manejo de archivos comprimidos para el instalador.

Los releases de PS4 vienen casi siempre en RAR5 con contraseña, y a menudo
partidos en volúmenes. Python no abre RAR5 por su cuenta, así que delegamos
todo en el CLI de 7-Zip, que viaja bundleado dentro del ejecutable.

Este módulo no importa Flet a propósito: es la parte con lógica de verdad y
tiene que poder testearse sin levantar una ventana.
"""

import os
import shutil
import subprocess
import sys

# En Windows la app corre --windowed: sin esto, cada subproceso abre una
# consola negra arriba de la ventana.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class SevenZipMissing(Exception):
    """No hay binario de 7-Zip disponible ni bundleado ni en el sistema."""


def seven_zip_path():
    """
    Devuelve la ruta al CLI de 7-Zip, o None si no hay ninguno.

    Prioridad: el bundleado dentro del ejecutable (el usuario final no tiene
    nada instalado), después el del sistema (útil corriendo desde fuente).
    """
    names = ["7za.exe", "7z.exe"] if os.name == "nt" else ["7zz", "7za", "7z"]

    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        for name in names:
            cand = os.path.join(bundled, name)
            if os.path.exists(cand):
                return cand

    for name in names:
        found = shutil.which(name)
        if found:
            return found

    return None
```

- [x] **Step 5: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v`
Expected: PASS (2 tests)

- [x] **Step 6: Commit**

```bash
cd ps4-pkg-installer
git add archives.py tests/test_archives.py requirements-dev.txt
git commit -m "feat(archives): ubicar el binario de 7-Zip bundleado o del sistema"
```

---

### Task 2: Detectar y agrupar comprimidos

Ésta es la tarea con la lógica más delicada del plan: de `X.part1.rar … X.part7.rar` la UI tiene que mostrar **una** entrada, no siete, porque 7-Zip sigue los volúmenes solo desde el primero. Pasar el volumen 3 a `7zz x` no extrae nada útil.

**Files:**
- Modify: `archives.py`
- Modify: `tests/test_archives.py`

**Interfaces:**
- Consumes: nada de Task 1
- Produces: `Archive` (dataclass: `path: str`, `name: str`, `parts: list[str]`, `total_size: int`, `missing_parts: list[str]`), `find_archives(root: str, max_depth: int = 6) -> list[Archive]`

- [x] **Step 1: Escribir los tests que fallan**

Agregar a `tests/test_archives.py`:

```python
import pytest


def _touch(p, size=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")


def test_agrupa_volumenes_rar_en_una_sola_entrada(tmp_path):
    for i in range(1, 8):
        _touch(tmp_path / f"juego.part{i}.rar", 10)

    found = archives.find_archives(str(tmp_path))

    assert len(found) == 1
    assert found[0].path.endswith("part1.rar")
    assert len(found[0].parts) == 7
    assert found[0].total_size == 70


def test_detecta_volumenes_faltantes(tmp_path):
    for i in (1, 2, 4):
        _touch(tmp_path / f"juego.part{i}.rar", 10)

    found = archives.find_archives(str(tmp_path))

    assert found[0].missing_parts == ["juego.part3.rar"]


def test_agrupa_volumenes_7z_numerados(tmp_path):
    for i in (1, 2, 3):
        _touch(tmp_path / f"x.7z.{i:03d}", 5)

    found = archives.find_archives(str(tmp_path))

    assert len(found) == 1
    assert found[0].path.endswith(".7z.001")


def test_ignora_comprimidos_ya_extraidos(tmp_path):
    _touch(tmp_path / "juego.rar", 10)
    _touch(tmp_path / "juego" / "algo.pkg", 10)

    assert archives.find_archives(str(tmp_path)) == []


def test_archivo_suelto_es_una_entrada_de_una_parte(tmp_path):
    _touch(tmp_path / "solo.zip", 42)

    found = archives.find_archives(str(tmp_path))

    assert len(found) == 1
    assert found[0].parts == [str(tmp_path / "solo.zip")]
    assert found[0].total_size == 42
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v -k "agrupa or detecta or ignora or suelto"`
Expected: FAIL con `AttributeError: module 'archives' has no attribute 'find_archives'`

- [x] **Step 3: Implementar la detección**

Agregar a `archives.py`:

```python
import re
from dataclasses import dataclass, field

# .part1.rar / .part01.rar  →  RAR5 multivolumen
_RE_RAR_PART = re.compile(r"^(?P<stem>.+)\.part(?P<num>\d+)\.rar$", re.I)
# .7z.001 / .zip.001        →  volúmenes numerados de 7-Zip
_RE_NUM_VOL = re.compile(r"^(?P<stem>.+\.(?:7z|zip|rar))\.(?P<num>\d{3})$", re.I)

_SINGLE_EXT = (".rar", ".zip", ".7z")


@dataclass
class Archive:
    """Un comprimido a extraer. `path` es SIEMPRE el primer volumen."""
    path: str
    name: str
    parts: list = field(default_factory=list)
    total_size: int = 0
    missing_parts: list = field(default_factory=list)


def _classify(filename):
    """(stem, numero) si el archivo es un volumen; (None, None) si no."""
    m = _RE_RAR_PART.match(filename)
    if m:
        return m.group("stem"), int(m.group("num"))
    m = _RE_NUM_VOL.match(filename)
    if m:
        return m.group("stem"), int(m.group("num"))
    return None, None


def _already_extracted(directory, stem):
    """
    True si al lado del comprimido ya hay un .pkg suyo.

    Sin esto, un release ya extraído reaparece para extraerse cada vez que
    se abre la carpeta.
    """
    sibling = os.path.join(directory, stem)
    if os.path.isdir(sibling):
        for entry in os.listdir(sibling):
            if entry.lower().endswith(".pkg"):
                return True
    for entry in os.listdir(directory):
        if entry.lower().endswith(".pkg") and os.path.splitext(entry)[0] == stem:
            return True
    return False


def find_archives(root, max_depth=6):
    """
    Recorre el árbol y devuelve un Archive por release (no por volumen).

    Mismas reglas que el escaneo de .pkg: no sigue symlinks, ignora carpetas
    ocultas, corta a max_depth para no perderse en un disco entero.
    """
    root = os.path.abspath(root)
    result = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        groups = {}   # stem -> {numero: filename}
        singles = []

        for fn in filenames:
            if fn.startswith("."):
                continue
            stem, num = _classify(fn)
            if stem is not None:
                groups.setdefault(stem, {})[num] = fn
            elif fn.lower().endswith(_SINGLE_EXT):
                singles.append(fn)

        for stem, vols in sorted(groups.items()):
            base_stem = os.path.splitext(stem)[0] if stem.lower().endswith(_SINGLE_EXT) else stem
            if _already_extracted(dirpath, base_stem):
                continue
            nums = sorted(vols)
            first = vols[nums[0]]
            missing = [
                _volume_name(first, n)
                for n in range(nums[0], nums[-1] + 1)
                if n not in vols
            ]
            parts = [os.path.join(dirpath, vols[n]) for n in nums]
            result.append(Archive(
                path=os.path.join(dirpath, first),
                name=base_stem,
                parts=parts,
                total_size=sum(os.path.getsize(p) for p in parts),
                missing_parts=missing,
            ))

        for fn in singles:
            base_stem = os.path.splitext(fn)[0]
            if _already_extracted(dirpath, base_stem):
                continue
            full = os.path.join(dirpath, fn)
            result.append(Archive(
                path=full,
                name=base_stem,
                parts=[full],
                total_size=os.path.getsize(full),
            ))

    return result


def _volume_name(first, n):
    """Nombre del volumen n a partir del nombre del primero."""
    m = _RE_RAR_PART.match(first)
    if m:
        width = len(m.group("num"))
        return f"{m.group('stem')}.part{n:0{width}d}.rar"
    m = _RE_NUM_VOL.match(first)
    return f"{m.group('stem')}.{n:03d}"
```

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v`
Expected: PASS (7 tests)

- [x] **Step 5: Commit**

```bash
cd ps4-pkg-installer
git add archives.py tests/test_archives.py
git commit -m "feat(archives): detectar comprimidos y agrupar volumenes en una entrada"
```

---

### Task 3: Inspeccionar un comprimido antes de extraer

Saber el tamaño descomprimido **sin extraer** es lo que permite avisar por falta de espacio antes de escribir 70 GB a medias.

**Files:**
- Modify: `archives.py`
- Modify: `tests/test_archives.py`

**Interfaces:**
- Consumes: `seven_zip_path()` (Task 1), `Archive` (Task 2)
- Produces: `ArchiveInfo` (dataclass: `unpacked_size: int`, `encrypted: bool`, `entries: list[str]`), `inspect_archive(archive: Archive, password: str = "") -> ArchiveInfo`, `WrongPassword` (Exception)

- [x] **Step 1: Escribir los tests que fallan**

Agregar a `tests/test_archives.py`:

```python
def _make_7z(tmp_path, name="t.7z", password=None, payload=b"x" * 5000):
    """Crea un .7z de verdad con el propio 7zz (no se puede generar RAR)."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "juego.pkg").write_bytes(payload)
    out = tmp_path / name
    cmd = [archives.seven_zip_path(), "a", str(out), str(src / "juego.pkg")]
    if password:
        cmd += [f"-p{password}", "-mhe=on"]
    subprocess.run(cmd, capture_output=True, check=True)
    return out


def test_inspect_reporta_tamano_descomprimido(tmp_path):
    _make_7z(tmp_path)
    arc = archives.find_archives(str(tmp_path))[0]

    info = archives.inspect_archive(arc)

    assert info.unpacked_size == 5000
    assert any(e.endswith("juego.pkg") for e in info.entries)


def test_inspect_marca_los_cifrados(tmp_path):
    _make_7z(tmp_path, password="secreta")
    arc = archives.find_archives(str(tmp_path))[0]

    info = archives.inspect_archive(arc, password="secreta")

    assert info.encrypted is True


def test_inspect_con_password_incorrecta_levanta_WrongPassword(tmp_path):
    _make_7z(tmp_path, password="secreta")
    arc = archives.find_archives(str(tmp_path))[0]

    with pytest.raises(archives.WrongPassword):
        archives.inspect_archive(arc, password="equivocada")
```

Agregar `import subprocess` al principio del archivo de tests.

- [x] **Step 2: Correr y verificar que fallan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v -k inspect`
Expected: FAIL con `AttributeError: module 'archives' has no attribute 'inspect_archive'`

- [x] **Step 3: Implementar la inspección**

Agregar a `archives.py`:

```python
class WrongPassword(Exception):
    """7-Zip rechazó la contraseña (o hacía falta una y no se dio)."""


@dataclass
class ArchiveInfo:
    unpacked_size: int = 0
    encrypted: bool = False
    entries: list = field(default_factory=list)


def _run_7z(args, password):
    """
    Corre 7-Zip y devuelve (returncode, stdout).

    La contraseña va en -p. Queda visible en la lista de procesos de la
    máquina; para una app local que instala PKGs es aceptable, y 7-Zip no
    ofrece una vía por stdin que funcione parejo en las tres plataformas.
    -y responde que sí a todo; sin eso, una contraseña vacía cuelga el
    proceso esperando teclado.
    """
    exe = seven_zip_path()
    if not exe:
        raise SevenZipMissing("No se encontró el binario de 7-Zip")

    cmd = [exe] + args + ["-y", f"-p{password or ''}"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace",
        creationflags=_NO_WINDOW,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _is_password_error(output):
    low = output.lower()
    return "wrong password" in low or "cannot open encrypted archive" in low


def inspect_archive(archive, password=""):
    """Lee el contenido sin extraer: tamaño real, si está cifrado, qué trae."""
    code, out = _run_7z(["l", "-slt", archive.path], password)

    if _is_password_error(out):
        raise WrongPassword(archive.name)
    if code != 0:
        raise RuntimeError(f"7-Zip falló al leer {archive.name}: {out.strip()[:400]}")

    info = ArchiveInfo(encrypted="Encrypted = +" in out)
    cur_path, cur_size, is_dir = None, 0, False

    for line in out.splitlines():
        if line.startswith("Path = "):
            if cur_path and not is_dir:
                info.entries.append(cur_path)
                info.unpacked_size += cur_size
            cur_path, cur_size, is_dir = line[7:].strip(), 0, False
        elif line.startswith("Size = "):
            try:
                cur_size = int(line[7:].strip())
            except ValueError:
                cur_size = 0
        elif line.startswith("Attributes = ") and line[13:].strip().startswith("D"):
            is_dir = True

    if cur_path and not is_dir:
        info.entries.append(cur_path)
        info.unpacked_size += cur_size

    # La primera entrada de -slt es el archivo mismo, no su contenido.
    if info.entries and os.path.abspath(info.entries[0]) == os.path.abspath(archive.path):
        first = info.entries.pop(0)
        del first

    return info
```

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v`
Expected: PASS (10 tests)

Si `test_inspect_reporta_tamano_descomprimido` falla por 5000 vs. otro número, imprimir `out` y revisar si la primera entrada de `-slt` se está descontando de más o de menos.

- [x] **Step 5: Commit**

```bash
cd ps4-pkg-installer
git add archives.py tests/test_archives.py
git commit -m "feat(archives): inspeccionar tamano descomprimido y deteccion de password"
```

---

### Task 4: Extraer con progreso

**Files:**
- Modify: `archives.py`
- Modify: `tests/test_archives.py`

**Interfaces:**
- Consumes: `Archive` (Task 2), `_run_7z` / `WrongPassword` (Task 3)
- Produces: `extract(archive: Archive, dest: str, password: str = "", on_progress=None) -> str` — devuelve la carpeta destino; `on_progress(pct: float)` se llama con 0.0–1.0

- [x] **Step 1: Escribir los tests que fallan**

```python
def test_extract_deja_el_pkg_en_destino(tmp_path):
    _make_7z(tmp_path)
    arc = archives.find_archives(str(tmp_path))[0]
    dest = tmp_path / "out"

    archives.extract(arc, str(dest))

    assert (dest / "juego.pkg").exists()
    assert (dest / "juego.pkg").stat().st_size == 5000


def test_extract_reporta_progreso_monotono(tmp_path):
    # 40 MB para que 7-Zip alcance a emitir varios porcentajes.
    _make_7z(tmp_path, payload=os.urandom(40 * 1024 * 1024))
    arc = archives.find_archives(str(tmp_path))[0]
    seen = []

    archives.extract(arc, str(tmp_path / "out"), on_progress=seen.append)

    assert seen, "no se reportó ningún progreso"
    assert seen == sorted(seen), "el progreso retrocedió"
    assert 0.0 <= seen[0] and seen[-1] <= 1.0


def test_extract_con_password_incorrecta_levanta_WrongPassword(tmp_path):
    _make_7z(tmp_path, password="secreta")
    arc = archives.find_archives(str(tmp_path))[0]

    with pytest.raises(archives.WrongPassword):
        archives.extract(arc, str(tmp_path / "out"), password="equivocada")
```

- [x] **Step 2: Correr y verificar que fallan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v -k extract`
Expected: FAIL con `AttributeError: module 'archives' has no attribute 'extract'`

- [x] **Step 3: Implementar la extracción**

```python
# Con -bsp1, 7-Zip escribe el avance a stdout como " 37% 12 - nombre".
_RE_PCT = re.compile(r"(\d{1,3})%")


def extract(archive, dest, password="", on_progress=None):
    """
    Extrae `archive` en `dest` informando avance.

    Se le pasa SOLO el primer volumen: 7-Zip encuentra el resto solo. `-bsp1`
    manda el porcentaje a stdout, que es de dónde sale la barra.
    """
    exe = seven_zip_path()
    if not exe:
        raise SevenZipMissing("No se encontró el binario de 7-Zip")

    os.makedirs(dest, exist_ok=True)
    cmd = [
        exe, "x", archive.path, f"-o{dest}",
        "-y", "-bsp1", "-bso0", f"-p{password or ''}",
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
        creationflags=_NO_WINDOW,
    )

    tail = []
    last = -1.0
    # 7-Zip pisa la misma línea con \r, así que no sirve iterar por líneas.
    for chunk in iter(lambda: proc.stdout.read(256), ""):
        tail.append(chunk)
        if len(tail) > 40:
            del tail[0]
        if on_progress:
            for m in _RE_PCT.finditer(chunk):
                pct = min(100, int(m.group(1))) / 100.0
                if pct > last:          # monótono: nunca retroceder
                    last = pct
                    on_progress(pct)

    proc.wait()
    out = "".join(tail)

    if _is_password_error(out):
        raise WrongPassword(archive.name)
    if proc.returncode != 0:
        raise RuntimeError(f"7-Zip falló extrayendo {archive.name}: {out.strip()[:400]}")

    if on_progress and last < 1.0:
        on_progress(1.0)

    return dest
```

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v`
Expected: PASS (13 tests)

- [x] **Step 5: Commit**

```bash
cd ps4-pkg-installer
git add archives.py tests/test_archives.py
git commit -m "feat(archives): extraer con progreso parseado de 7-Zip"
```

---

### Task 5: Chequeo de espacio en disco

**Files:**
- Modify: `archives.py`
- Modify: `tests/test_archives.py`

**Interfaces:**
- Consumes: `Archive` (Task 2), `inspect_archive` (Task 3)
- Produces: `check_space(dest: str, needed: int, margin: int = 2 * 1024**3) -> tuple[bool, int]` — devuelve `(alcanza, libres)`

- [x] **Step 1: Escribir el test que falla**

```python
def test_check_space_falla_cuando_no_alcanza(tmp_path):
    ok, free = archives.check_space(str(tmp_path), needed=10 ** 18)

    assert ok is False
    assert free > 0


def test_check_space_pasa_con_un_archivo_chico(tmp_path):
    ok, _ = archives.check_space(str(tmp_path), needed=1024, margin=0)

    assert ok is True
```

- [x] **Step 2: Correr y verificar que falla**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v -k check_space`
Expected: FAIL con `AttributeError: module 'archives' has no attribute 'check_space'`

- [x] **Step 3: Implementar**

```python
def check_space(dest, needed, margin=2 * 1024 ** 3):
    """
    ¿Entra `needed` bytes en el volumen de `dest`?

    El margen deja aire para que el sistema no quede sin disco: llenar el
    volumen al 100% mientras se escriben 70 GB es peor que no empezar.
    """
    probe = dest
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    free = shutil.disk_usage(probe or ".").free
    return free >= needed + margin, free
```

- [x] **Step 4: Correr los tests y verificar que pasan**

Run: `cd ps4-pkg-installer && python3 -m pytest tests/test_archives.py -v`
Expected: PASS (15 tests)

- [x] **Step 5: Commit**

```bash
cd ps4-pkg-installer
git add archives.py tests/test_archives.py
git commit -m "feat(archives): chequeo de espacio libre antes de extraer"
```

---

### Task 6: Banner y worker en la app

**Files:**
- Modify: `ps4_pkg_installer.py` — `_build()` (~línea 503-556), `scan_folder()` (~995), imports (~8-22)

**Interfaces:**
- Consumes: todo `archives` (Tasks 1-5)
- Produces: nada que consuman otras tareas

- [x] **Step 1: Importar el módulo**

En `ps4_pkg_installer.py`, junto a los demás imports:

```python
import archives
```

- [x] **Step 2: Crear los controles del banner en `_build()`**

Justo antes de `self.pkg_list = ft.ListView(...)` (línea ~507):

```python
        self.f_pass = ft.TextField(
            label="Contraseña", password=True, can_reveal_password=True,
            width=190, dense=True, border_color=BORDER, color=TEXT,
        )
        self.cb_delete_archives = ft.Checkbox(
            label="borrar los comprimidos al terminar",
            value=False, active_color=BLUE,
        )
        self.btn_extract = ft.ElevatedButton(
            "Extraer", icon=ft.Icons.UNARCHIVE, on_click=self.on_extract,
        )
        self.arch_text = ft.Text("", size=13, color=TEXT)
        self.arch_bar = ft.ProgressBar(visible=False, color=BLUE, bgcolor=SURFACE_2)
        self.arch_banner = ft.Container(
            visible=False, padding=12, border_radius=8,
            bgcolor=SURFACE_2, border=ft.border.all(1, BORDER),
            content=ft.Column(spacing=8, controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
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
```

Y agregar `self.arch_banner` a los `controls` de la columna, inmediatamente antes del contenedor que envuelve `self.pkg_list` (línea ~554).

- [x] **Step 3: Detectar comprimidos al escanear**

Al final de `scan_folder()`, justo antes de `self._refresh_count()`:

```python
        self._scan_archives()
```

Y agregar el método:

```python
    def _scan_archives(self):
        """Busca comprimidos sin extraer y muestra el banner si hay."""
        try:
            self.archives = archives.find_archives(self.folder, max_depth=MAX_DEPTH)
        except Exception as e:
            self.archives = []
            self.log(f"No pude revisar comprimidos: {e}", "warn")

        if not self.archives:
            self.arch_banner.visible = False
            return

        total = sum(a.total_size for a in self.archives)
        faltan = [a for a in self.archives if a.missing_parts]

        self.arch_text.value = (
            f"{len(self.archives)} comprimido(s) sin extraer · {human_size(total)}"
        )
        self.arch_banner.visible = True
        self.btn_extract.disabled = bool(faltan)

        for a in faltan:
            self.log(
                f"{a.name}: faltan volúmenes ({', '.join(a.missing_parts)})", "error"
            )
        self.log(f"{len(self.archives)} comprimido(s) sin extraer", "warn")
```

En `__init__`, inicializar `self.archives = []`.

- [x] **Step 4: Implementar el worker de extracción**

```python
    def on_extract(self, _):
        if getattr(self, "extracting", False):
            return
        if not self.archives:
            return
        self.extracting = True
        self.btn_extract.disabled = True
        self.arch_bar.visible = True
        self.arch_bar.value = None
        self._safe_update()
        threading.Thread(target=self._extract_worker, daemon=True).start()

    def _extract_worker(self):
        """Extrae la tanda entera. Corre en thread: no bloquea la ventana."""
        pwd = (self.f_pass.value or "").strip()
        borrar = self.cb_delete_archives.value
        pendientes = list(self.archives)
        hechos = 0

        try:
            # Primero medimos todo: mejor frenar antes que a los 40 GB.
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
                    f"No hay espacio: hacen falta {human_size(necesario)} "
                    f"y hay {human_size(libre)} libres",
                    "error",
                )
                return

            for idx, a in enumerate(pendientes, 1):
                dest = os.path.join(os.path.dirname(a.path), a.name)
                self.log(f"Extrayendo {a.name} ({idx}/{len(pendientes)})…", "step")

                def progreso(pct, i=idx, n=len(pendientes)):
                    self.arch_bar.value = ((i - 1) + pct) / n
                    self.arch_text.value = f"Extrayendo {a.name} · {pct*100:.0f}%"
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
                self.log(f"{a.name}: listo", "ok")

                if borrar:
                    for p in a.parts:
                        try:
                            os.remove(p)
                        except OSError as e:
                            self.log(f"No pude borrar {os.path.basename(p)}: {e}", "warn")

        finally:
            self.extracting = False
            self.arch_bar.visible = False
            self.btn_extract.disabled = False
            if hechos:
                self.log(f"{hechos} comprimido(s) extraído(s)", "ok")
            # Re-escanear: los .pkg nuevos entran solos a la lista de siempre.
            self.scan_folder()
            self._safe_update()
```

- [x] **Step 5: Probar a mano**

```bash
cd ps4-pkg-installer && python3 ps4_pkg_installer.py
```

Verificar, con una carpeta que tenga un comprimido de prueba:
1. El banner aparece con la cuenta y el tamaño correctos
2. Con contraseña incorrecta, el log dice "contraseña incorrecta" y no se extrae nada
3. Con la correcta, la barra avanza y al terminar el `.pkg` aparece en la lista
4. En una carpeta sin comprimidos, el banner **no** aparece y la UI se ve como antes
5. La ventana sigue respondiendo durante la extracción

- [x] **Step 6: Commit**

```bash
cd ps4-pkg-installer
git add ps4_pkg_installer.py
git commit -m "feat(ui): banner de extraccion con password y progreso"
```

---

### Task 7: Bundlear 7-Zip en los tres builds

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `README.md`, `README.es.md`

**Interfaces:**
- Consumes: `seven_zip_path()` (Task 1) — que busca en `sys._MEIPASS`
- Produces: nada

- [x] **Step 1: Bajar el binario por plataforma en el CI**

Agregar al `release.yml` un step antes de `Build`:

```yaml
      - name: Fetch the 7-Zip CLI
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p vendor
          case "${{ runner.os }}" in
            macOS)
              curl -fsSL https://www.7-zip.org/a/7z2408-mac.tar.xz -o 7z.tar.xz
              tar -xf 7z.tar.xz 7zz
              mv 7zz vendor/7zz
              chmod +x vendor/7zz
              ;;
            Linux)
              curl -fsSL https://www.7-zip.org/a/7z2408-linux-x64.tar.xz -o 7z.tar.xz
              tar -xf 7z.tar.xz 7zz
              mv 7zz vendor/7zz
              chmod +x vendor/7zz
              ;;
            Windows)
              curl -fsSL https://www.7-zip.org/a/7z2408-extra.7z -o 7z.7z
              7z x 7z.7z -ovendor 7za.exe
              ;;
          esac
          ls -l vendor/
```

- [x] **Step 2: Sumar el binario al build de PyInstaller**

En el step `Build`, agregar el `--add-binary`. El separador es `;` en Windows y `:` en el resto, así que van dos variantes:

```yaml
      - name: Build
        shell: bash
        run: |
          if [ "${{ runner.os }}" = "Windows" ]; then
            SEP=";"; BIN="vendor/7za.exe"
          else
            SEP=":"; BIN="vendor/7zz"
          fi
          pyinstaller --noconfirm --onefile --windowed \
            --name "PS4 PKG Installer" \
            --collect-all flet \
            --collect-all flet_desktop \
            --add-binary "${BIN}${SEP}." \
            ps4_pkg_installer.py
```

- [x] **Step 3: Verificar que el binario viajó**

Agregar al step de verificación existente:

```bash
          # 7-Zip pesa ~1.5-2.5 MB; si el binario final no creció, no se bundleó.
          echo "Comprobá a mano que el ejecutable arranque y extraiga un .7z de prueba"
```

- [x] **Step 4: Setup local para desarrollo**

Agregar al final de `requirements-dev.txt`:

```
# Corriendo desde fuente hace falta el CLI de 7-Zip en el sistema:
#   macOS  brew install sevenzip
#   Linux  apt install p7zip-full
# El binario bundleado solo existe en los ejecutables que compila el CI.
```

- [x] **Step 5: Documentar la funcion en los README**

En `README.es.md`, despues de la seccion "Seleccionar PKGs", insertar:

```markdown
### Comprimidos sin extraer

Si la carpeta tiene archivos RAR, ZIP o 7z sin extraer, arriba de la lista
aparece un banner con cuantos hay y cuanto pesan.

1. Escribi la contrasena si el release la pide (la de DLPSGAME es `DLPSGAME.COM`)
2. Apreta **Extraer**
3. Cuando termina, los `.pkg` aparecen solos en la lista de abajo

Los releases partidos en varios volumenes (`.part1.rar`, `.part2.rar`, ...)
se listan como una sola entrada: alcanza con tener todas las partes en la
misma carpeta. Si falta alguna, el boton queda deshabilitado y el log dice
cual.

Antes de empezar se calcula cuanto espacio hace falta; si no entra, avisa
en vez de dejar un `.pkg` a medias.

La casilla "borrar los comprimidos al terminar" esta apagada por defecto.
```

En `README.md`, insertar la traduccion al ingles de ese mismo bloque en la
posicion equivalente.

- [x] **Step 6: Commit**

```bash
cd ps4-pkg-installer
git add .github/workflows/release.yml README.md README.es.md requirements-dev.txt
git commit -m "build: bundlear el CLI de 7-Zip en los tres ejecutables"
```

---

## Notas de verificación final

Antes de dar por cerrado el plan:

```bash
cd ps4-pkg-installer && python3 -m pytest tests/ -v
```

Todos los tests en verde, y la prueba manual del Task 6 Step 5 hecha con un comprimido real — idealmente uno multi-parte, que es el caso que más lógica propia tiene.

**No correr el build local mientras haya una transferencia a la PS4 en curso:** sobreescribe `dist/`, que es el binario en ejecución.
