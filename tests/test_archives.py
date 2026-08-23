"""Tests de archives.py — detección y extracción de comprimidos."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import archives


# ------------------------------------------------------------------ helpers

def _touch(p, size=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")


# ------------------------------------------------------- ubicar el binario

def test_seven_zip_path_encuentra_el_binario_del_sistema():
    p = archives.seven_zip_path()
    assert p is not None
    assert os.path.exists(p)


def test_seven_zip_path_prefiere_el_bundleado(tmp_path, monkeypatch):
    # PyInstaller onefile expone sus datos en sys._MEIPASS. Si hay un binario
    # ahí, gana sobre el del sistema: el usuario final no tiene 7zz instalado.
    fake = tmp_path / ("7za.exe" if os.name == "nt" else "7zz")
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert archives.seven_zip_path() == str(fake)


# ------------------------------------------------------ detectar y agrupar

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


def test_encuentra_en_subcarpetas(tmp_path):
    _touch(tmp_path / "EA26" / "UPDATE" / "u.rar", 10)

    found = archives.find_archives(str(tmp_path))

    assert len(found) == 1
    assert found[0].name == "u"


# --------------------------------------------------- inspeccionar y extraer
# No se puede *crear* un RAR (el compresor es propietario), así que los tests
# usan .7z generados por el mismo 7zz: ejercitan el mismo camino de código.

def _make_7z(tmp_path, name="t.7z", password=None, payload=b"x" * 5000, volumes=None):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "juego.pkg").write_bytes(payload)
    out = tmp_path / name
    cmd = [archives.seven_zip_path(), "a", str(out), str(src / "juego.pkg")]
    if password:
        cmd += [f"-p{password}", "-mhe=on"]
    if volumes:
        cmd += [f"-v{volumes}"]
    subprocess.run(cmd, capture_output=True, check=True)
    shutil_rm = tmp_path / "src"
    for f in shutil_rm.iterdir():
        f.unlink()
    shutil_rm.rmdir()
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


def test_extract_deja_el_pkg_en_destino(tmp_path):
    _make_7z(tmp_path)
    arc = archives.find_archives(str(tmp_path))[0]
    dest = tmp_path / "out"

    archives.extract(arc, str(dest))

    assert (dest / "juego.pkg").exists()
    assert (dest / "juego.pkg").stat().st_size == 5000


def test_extract_reporta_progreso_monotono(tmp_path):
    # Datos aleatorios (no comprimen) para que 7-Zip tarde y emita porcentajes.
    _make_7z(tmp_path, payload=os.urandom(40 * 1024 * 1024))
    arc = archives.find_archives(str(tmp_path))[0]
    seen = []

    archives.extract(arc, str(tmp_path / "out"), on_progress=seen.append)

    assert seen, "no se reportó ningún progreso"
    assert seen == sorted(seen), "el progreso retrocedió"
    assert 0.0 <= seen[0] and seen[-1] <= 1.0


def test_extract_con_password_correcta(tmp_path):
    _make_7z(tmp_path, password="DLPSGAME.COM")
    arc = archives.find_archives(str(tmp_path))[0]

    archives.extract(arc, str(tmp_path / "out"), password="DLPSGAME.COM")

    assert (tmp_path / "out" / "juego.pkg").exists()


def test_extract_con_password_incorrecta_levanta_WrongPassword(tmp_path):
    _make_7z(tmp_path, password="secreta")
    arc = archives.find_archives(str(tmp_path))[0]

    with pytest.raises(archives.WrongPassword):
        archives.extract(arc, str(tmp_path / "out"), password="equivocada")


def test_extract_de_multivolumen_desde_el_primero(tmp_path):
    """El caso de los releases: 7-Zip encuentra el resto de los volúmenes solo."""
    _make_7z(tmp_path, payload=os.urandom(3 * 1024 * 1024), volumes="1m")
    found = archives.find_archives(str(tmp_path))

    assert len(found) == 1, f"debería agrupar los volúmenes: {found}"
    archives.extract(found[0], str(tmp_path / "out"))

    assert (tmp_path / "out" / "juego.pkg").exists()


# ------------------------------------------------------------------ espacio

def test_check_space_falla_cuando_no_alcanza(tmp_path):
    ok, free = archives.check_space(str(tmp_path), needed=10 ** 18)

    assert ok is False
    assert free > 0


def test_check_space_pasa_con_un_archivo_chico(tmp_path):
    ok, _ = archives.check_space(str(tmp_path), needed=1024, margin=0)

    assert ok is True
