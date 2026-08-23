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
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# En Windows la app corre --windowed: sin esto, cada subproceso abre una
# consola negra arriba de la ventana.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# .part1.rar / .part01.rar  →  RAR5 multivolumen
_RE_RAR_PART = re.compile(r"^(?P<stem>.+)\.part(?P<num>\d+)\.rar$", re.I)
# .7z.001 / .zip.001        →  volúmenes numerados de 7-Zip
_RE_NUM_VOL = re.compile(r"^(?P<stem>.+\.(?:7z|zip|rar))\.(?P<num>\d{3})$", re.I)

_SINGLE_EXT = (".rar", ".zip", ".7z")

# Una .app abierta desde Finder hereda un PATH mínimo que no incluye los
# directorios de Homebrew, así que which() sola no alcanza.
_EXTRA_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def _find_binary(names):
    """Primero el bundleado, después el PATH, después las rutas conocidas."""
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

    for directory in _EXTRA_DIRS:
        for name in names:
            cand = os.path.join(directory, name)
            if os.path.exists(cand):
                return cand

    return None


class SevenZipMissing(Exception):
    """No hay binario de 7-Zip disponible ni bundleado ni en el sistema."""


@dataclass
class Archive:
    """Un comprimido a extraer. `path` es SIEMPRE el primer volumen."""
    path: str
    name: str
    parts: list = field(default_factory=list)
    total_size: int = 0
    missing_parts: list = field(default_factory=list)


def seven_zip_path():
    """
    Devuelve la ruta al CLI de 7-Zip, o None si no hay ninguno.

    Prioridad: el bundleado dentro del ejecutable (el usuario final no tiene
    nada instalado), después el del sistema (útil corriendo desde fuente).
    """
    names = ["7za.exe", "7z.exe"] if os.name == "nt" else ["7zz", "7za", "7z"]
    return _find_binary(names)


def _classify(filename):
    """(stem, numero) si el archivo es un volumen; (None, None) si no."""
    m = _RE_RAR_PART.match(filename)
    if m:
        return m.group("stem"), int(m.group("num"))
    m = _RE_NUM_VOL.match(filename)
    if m:
        return m.group("stem"), int(m.group("num"))
    return None, None


def _volume_name(first, n):
    """Nombre del volumen n a partir del nombre del primero."""
    m = _RE_RAR_PART.match(first)
    if m:
        width = len(m.group("num"))
        return f"{m.group('stem')}.part{n:0{width}d}.rar"
    m = _RE_NUM_VOL.match(first)
    return f"{m.group('stem')}.{n:03d}"


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
            base = os.path.splitext(stem)[0] if stem.lower().endswith(_SINGLE_EXT) else stem
            if _already_extracted(dirpath, base):
                continue
            nums = sorted(vols)
            first = vols[nums[0]]
            parts = [os.path.join(dirpath, vols[n]) for n in nums]
            result.append(Archive(
                path=os.path.join(dirpath, first),
                name=base,
                parts=parts,
                total_size=sum(os.path.getsize(p) for p in parts),
                missing_parts=[
                    _volume_name(first, n)
                    for n in range(nums[0], nums[-1] + 1) if n not in vols
                ],
            ))

        for fn in sorted(singles):
            base = os.path.splitext(fn)[0]
            if _already_extracted(dirpath, base):
                continue
            full = os.path.join(dirpath, fn)
            result.append(Archive(
                path=full, name=base, parts=[full],
                total_size=os.path.getsize(full),
            ))

    return result


class WrongPassword(Exception):
    """7-Zip rechazó la contraseña (o hacía falta una y no se dio)."""


class UnsupportedMethod(Exception):
    """
    El extractor lee el índice pero no implementa el codec del archivo.

    Pasa con RAR5 comprimido (-m3 y parientes): 7-Zip inventaria el contenido
    perfecto y muere recién al descomprimir. Los releases de PS4 suelen venir
    en -m0 (store), donde el RAR es apenas un contenedor y no se nota — hasta
    que aparece uno comprimido de verdad.
    """


@dataclass
class ArchiveInfo:
    unpacked_size: int = 0
    encrypted: bool = False
    entries: list = field(default_factory=list)


def _is_password_error(output):
    low = output.lower()
    return "wrong password" in low or "cannot open encrypted archive" in low


def _is_unsupported_method(output):
    low = output.lower()
    return "unsupported method" in low or "unsupported compression" in low


def fallback_extractor_path():
    """
    Extractor de respaldo para lo que 7-Zip no puede abrir.

    unrar es el de RARLAB y soporta todo formato RAR; unar (The Unarchiver)
    es la alternativa libre y alcanza para los mismos casos. Cualquiera de los
    dos sirve, se usa el primero que aparezca.
    """
    names = ["unrar.exe", "unar.exe"] if os.name == "nt" else ["unrar", "unar"]
    return _find_binary(names)


def _fallback_cmd(exe, archive_path, dest, password):
    """Comando del extractor de respaldo. Los dos tienen CLIs distintas."""
    base = os.path.basename(exe).lower()
    if base.startswith("unar"):
        # -D evita que cree otra carpeta contenedora, -f pisa sin preguntar.
        return [exe, "-p", password or "", "-D", "-f", "-o", dest, archive_path]
    # unrar de RARLAB: "x" extrae con rutas. -p- es "no hay contraseña": sin
    # eso se queda esperando teclado y el proceso nunca termina.
    return [exe, "x", f"-p{password}" if password else "-p-", "-y",
            archive_path, dest + os.sep]


def _run_7z(args, password):
    """
    Corre 7-Zip y devuelve (returncode, salida).

    La contraseña va en -p. Queda visible en la lista de procesos de la
    máquina; para una app local que instala PKGs es aceptable, y 7-Zip no
    ofrece una vía por stdin que ande pareja en las tres plataformas.
    -y responde que sí a todo: sin eso, una contraseña vacía deja el proceso
    colgado esperando teclado.
    """
    exe = seven_zip_path()
    if not exe:
        raise SevenZipMissing("No se encontró el binario de 7-Zip")

    proc = subprocess.run(
        [exe] + args + ["-y", f"-p{password or ''}"],
        capture_output=True, text=True, errors="replace",
        creationflags=_NO_WINDOW,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def inspect_archive(archive, password=""):
    """Lee el contenido sin extraer: tamaño real, si está cifrado, qué trae."""
    code, out = _run_7z(["l", "-slt", archive.path], password)

    if _is_password_error(out):
        raise WrongPassword(archive.name)
    if code != 0:
        raise RuntimeError(f"7-Zip falló al leer {archive.name}: {out.strip()[:400]}")

    info = ArchiveInfo(encrypted="Encrypted = +" in out)

    # -slt describe primero el archivo contenedor y después cada entrada; solo
    # nos interesan las que vienen después de la línea "----------".
    _, _, cuerpo = out.partition("\n----------\n")
    cur_path, cur_size, is_dir = None, 0, False

    def cerrar():
        if cur_path and not is_dir:
            info.entries.append(cur_path)
            info.unpacked_size += cur_size

    for line in cuerpo.splitlines():
        if line.startswith("Path = "):
            cerrar()
            cur_path, cur_size, is_dir = line[7:].strip(), 0, False
        elif line.startswith("Size = "):
            try:
                cur_size = int(line[7:].strip())
            except ValueError:
                cur_size = 0
        elif line.startswith("Attributes = ") and line[13:].strip().startswith("D"):
            is_dir = True
    cerrar()

    return info


# Con -bsp1, 7-Zip escribe el avance a stdout como " 37% 12 - nombre".
_RE_PCT = re.compile(r"(\d{1,3})%")


def _extract_with_7z(archive, dest, password, on_progress):
    """
    Extrae con 7-Zip. Se le pasa SOLO el primer volumen: encuentra el resto
    por su cuenta. `-bsp1` manda el porcentaje a stdout, de donde sale la barra.
    """
    exe = seven_zip_path()
    if not exe:
        raise SevenZipMissing("No se encontró el binario de 7-Zip")

    os.makedirs(dest, exist_ok=True)
    proc = subprocess.Popen(
        [exe, "x", archive.path, f"-o{dest}", "-y", "-bsp1", "-bso0",
         f"-p{password or ''}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
        creationflags=_NO_WINDOW,
    )

    tail, last = [], -1.0
    # 7-Zip pisa la misma línea con \r, así que iterar por líneas no sirve.
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
    if _is_unsupported_method(out):
        raise UnsupportedMethod(archive.name)
    if proc.returncode != 0:
        raise RuntimeError(f"7-Zip falló extrayendo {archive.name}: {out.strip()[:400]}")

    if on_progress and last < 1.0:
        on_progress(1.0)

    return dest


def _extract_with_fallback(archive, dest, password, on_progress):
    """
    Extrae con unrar o unar, para lo que 7-Zip no puede.

    unrar reporta porcentaje y se parsea igual que 7-Zip. unar solo imprime una
    línea por archivo terminado, así que ahí la barra se queda indeterminada
    hasta el final: es el precio de que el archivo se pueda abrir.
    """
    exe = fallback_extractor_path()
    if not exe:
        raise UnsupportedMethod(archive.name)

    os.makedirs(dest, exist_ok=True)
    proc = subprocess.Popen(
        _fallback_cmd(exe, archive.path, dest, password),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1,
        creationflags=_NO_WINDOW,
    )

    tail, last = [], -1.0
    for chunk in iter(lambda: proc.stdout.read(256), ""):
        tail.append(chunk)
        if len(tail) > 40:
            del tail[0]
        if on_progress:
            for m in _RE_PCT.finditer(chunk):
                pct = min(100, int(m.group(1))) / 100.0
                if pct > last:
                    last = pct
                    on_progress(pct)

    proc.wait()
    out = "".join(tail)

    if _is_password_error(out) or "password" in out.lower() and "incorrect" in out.lower():
        raise WrongPassword(archive.name)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{os.path.basename(exe)} falló extrayendo {archive.name}: {out.strip()[:400]}"
        )

    if on_progress and last < 1.0:
        on_progress(1.0)

    return dest


def extract(archive, dest, password="", on_progress=None):
    """
    Extrae `archive` en `dest` informando avance.

    Intenta primero con 7-Zip, que es el que viaja bundleado. Si el archivo usa
    un codec que no implementa —RAR5 comprimido, típicamente— reintenta con
    unrar o unar. Una contraseña incorrecta no se reintenta: cambiar de
    extractor no la arregla.
    """
    os.makedirs(dest, exist_ok=True)
    try:
        return _extract_with_7z(archive, dest, password, on_progress)
    except UnsupportedMethod:
        if not fallback_extractor_path():
            raise UnsupportedMethod(
                f"{archive.name} usa un codec de compresión que 7-Zip no soporta. "
                f"Hace falta unrar (o unar) instalado para abrirlo."
            )
        return _extract_with_fallback(archive, dest, password, on_progress)


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
