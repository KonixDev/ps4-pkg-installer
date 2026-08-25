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


# ---------------------------------------------------------- motor de respaldo
# 7-Zip lee los headers de cualquier RAR5 pero no implementa todos los codecs
# de compresión: con un RAR "-m3" lista bien el contenido y muere al extraer
# con "Unsupported Method". Verificado contra un DLC pack real de HITMAN.
# Como no existe compresor RAR libre, no se puede generar un fixture así en el
# test: lo que se verifica acá es la lógica de conmutación y los comandos.

def test_fallback_path_encuentra_un_extractor():
    # unar (The Unarchiver) o unrar (RARLAB); en esta máquina hay unar.
    p = archives.fallback_extractor_path()
    assert p is not None
    assert os.path.exists(p)


def test_comando_de_unar_bien_armado():
    cmd = archives._fallback_cmd("/usr/bin/unar", "/x/a.part1.rar", "/dest", "hako")

    assert cmd[0] == "/usr/bin/unar"
    assert "-D" in cmd            # no crear carpeta contenedora extra
    assert "-f" in cmd            # pisar sin preguntar
    assert cmd[cmd.index("-o") + 1] == "/dest"
    assert cmd[-1] == "/x/a.part1.rar"
    assert "hako" in cmd


def test_comando_de_unrar_bien_armado():
    cmd = archives._fallback_cmd("/usr/bin/unrar", "/x/a.part1.rar", "/dest", "hako")

    assert cmd[:2] == ["/usr/bin/unrar", "x"]
    assert "-phako" in cmd
    assert "-y" in cmd


def test_comando_de_unrar_sin_password():
    # -p- le dice a unrar que no hay contraseña; sin eso se queda esperando.
    cmd = archives._fallback_cmd("/usr/bin/unrar", "/x/a.rar", "/dest", "")

    assert "-p-" in cmd


def test_detecta_el_codec_no_soportado():
    salida = "ERROR: Unsupported Method : Hitman.DLCs/HITMAN.Bonus.pkg\n  0% - Hitman"

    assert archives._is_unsupported_method(salida) is True
    assert archives._is_unsupported_method("ERROR: Wrong password") is False


def test_extract_cae_al_fallback_cuando_7zip_no_puede(tmp_path, monkeypatch):
    """El caso HITMAN: 7-Zip se rinde y el respaldo termina el trabajo."""
    arc = archives.Archive(path=str(tmp_path / "a.rar"), name="a")
    llamados = []

    def fake_7z(archive, dest, password, on_progress):
        llamados.append("7z")
        raise archives.UnsupportedMethod("m3")

    def fake_fb(archive, dest, password, on_progress):
        llamados.append("fallback")
        return dest

    monkeypatch.setattr(archives, "_extract_with_7z", fake_7z)
    monkeypatch.setattr(archives, "_extract_with_fallback", fake_fb)

    archives.extract(arc, str(tmp_path / "out"))

    assert llamados == ["7z", "fallback"]


def test_extract_no_usa_el_fallback_si_7zip_pudo(tmp_path, monkeypatch):
    arc = archives.Archive(path=str(tmp_path / "a.7z"), name="a")
    llamados = []

    monkeypatch.setattr(archives, "_extract_with_7z",
                        lambda a, d, p, o: llamados.append("7z") or d)
    monkeypatch.setattr(archives, "_extract_with_fallback",
                        lambda a, d, p, o: llamados.append("fallback") or d)

    archives.extract(arc, str(tmp_path / "out"))

    assert llamados == ["7z"]


def test_sin_fallback_disponible_el_error_es_claro(tmp_path, monkeypatch):
    arc = archives.Archive(path=str(tmp_path / "a.rar"), name="a")
    monkeypatch.setattr(archives, "_extract_with_7z",
                        lambda a, d, p, o: (_ for _ in ()).throw(archives.UnsupportedMethod("m3")))
    monkeypatch.setattr(archives, "fallback_extractor_path", lambda: None)

    with pytest.raises(archives.UnsupportedMethod) as e:
        archives.extract(arc, str(tmp_path / "out"))

    assert "unrar" in str(e.value).lower()


def test_password_incorrecta_no_dispara_el_fallback(tmp_path, monkeypatch):
    """Una contraseña mala no se arregla cambiando de extractor."""
    arc = archives.Archive(path=str(tmp_path / "a.rar"), name="a")
    llamados = []

    def fake_7z(a, d, p, o):
        llamados.append("7z")
        raise archives.WrongPassword("a")

    monkeypatch.setattr(archives, "_extract_with_7z", fake_7z)
    monkeypatch.setattr(archives, "_extract_with_fallback",
                        lambda a, d, p, o: llamados.append("fallback") or d)

    with pytest.raises(archives.WrongPassword):
        archives.extract(arc, str(tmp_path / "out"))

    assert llamados == ["7z"]


def test_busca_en_rutas_conocidas_sin_PATH(monkeypatch):
    """
    Una .app abierta desde Finder hereda un PATH mínimo, sin /opt/homebrew/bin.
    Sin esto, la app compilada no encontraría nada aunque esté instalado.
    """
    monkeypatch.setattr(archives.shutil, "which", lambda n: None)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    # En esta máquina 7zz está en Homebrew; con which() anulado, tiene que
    # encontrarlo igual por ruta conocida.
    assert archives.seven_zip_path() is not None
