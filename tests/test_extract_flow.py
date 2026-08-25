"""
Integración del flujo de extracción de la app, sin abrir ventana.

Ejercita _scan_archives + _extract_worker de punta a punta contra comprimidos
de verdad. No se puede levantar Flet en el test, así que los controles se
reemplazan por objetos bobos que solo guardan atributos — lo que se verifica
es la lógica, no el pintado.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import archives
from ps4_pkg_installer import App


class _Ctl:
    """Sustituto de un control de Flet: acepta cualquier atributo."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _app(folder):
    app = App.__new__(App)
    app.folder = str(folder)
    app.archives = []
    app.extracting = False
    app.logs = []
    app.log = lambda t, kind="info": app.logs.append((kind, t))
    app._safe_update = lambda: None
    app.scan_folder = lambda: None
    app.arch_banner = _Ctl(visible=False)
    app.arch_text = _Ctl(value="")
    app.arch_bar = _Ctl(visible=False, value=0)
    app.btn_extract = _Ctl(disabled=False)
    app.f_pass = _Ctl(value="")
    app.cb_delete_archives = _Ctl(value=False)
    return app


def _make_7z(folder, name, password=None, payload=b"P" * 20000, volumes=None):
    src = folder / "_src"
    src.mkdir(exist_ok=True)
    pkg = src / "juego.pkg"
    pkg.write_bytes(payload)
    cmd = [archives.seven_zip_path(), "a", str(folder / name), str(pkg)]
    if password:
        cmd += [f"-p{password}", "-mhe=on"]
    if volumes:
        cmd += [f"-v{volumes}"]
    subprocess.run(cmd, capture_output=True, check=True)
    pkg.unlink()
    src.rmdir()


def test_el_banner_aparece_con_el_tamano_correcto(tmp_path):
    _make_7z(tmp_path, "release.7z")
    app = _app(tmp_path)

    app._scan_archives()

    assert app.arch_banner.visible is True
    assert "1 comprimido" in app.arch_text.value
    assert app.btn_extract.disabled is False


def test_el_banner_no_aparece_si_no_hay_comprimidos(tmp_path):
    (tmp_path / "algo.pkg").write_bytes(b"x")
    app = _app(tmp_path)

    app._scan_archives()

    assert app.arch_banner.visible is False


def test_flujo_completo_deja_el_pkg_listo(tmp_path):
    _make_7z(tmp_path, "release.7z", password="DLPSGAME.COM")
    app = _app(tmp_path)
    app._scan_archives()
    app.f_pass.value = "DLPSGAME.COM"

    app._extract_worker()

    assert (tmp_path / "release" / "juego.pkg").exists()
    assert app.extracting is False
    assert any("extraído" in t for _, t in app.logs)


def test_password_incorrecta_no_extrae_nada_y_avisa(tmp_path):
    _make_7z(tmp_path, "release.7z", password="correcta")
    app = _app(tmp_path)
    app._scan_archives()
    app.f_pass.value = "incorrecta"

    app._extract_worker()

    assert not (tmp_path / "release" / "juego.pkg").exists()
    assert any(kind == "error" and "contraseña incorrecta" in t
               for kind, t in app.logs)


def test_multivolumen_se_extrae_de_una(tmp_path):
    _make_7z(tmp_path, "big.7z", payload=os.urandom(3 * 1024 * 1024), volumes="1m")
    app = _app(tmp_path)

    app._scan_archives()
    assert len(app.archives) == 1, "los volúmenes tienen que contar como uno"

    app._extract_worker()

    assert (tmp_path / "big" / "juego.pkg").exists()


def test_borrar_originales_solo_si_esta_tildado(tmp_path):
    _make_7z(tmp_path, "release.7z")
    app = _app(tmp_path)
    app._scan_archives()
    app.cb_delete_archives.value = True

    app._extract_worker()

    assert not (tmp_path / "release.7z").exists()
    assert (tmp_path / "release" / "juego.pkg").exists()


def test_por_defecto_no_borra_los_originales(tmp_path):
    _make_7z(tmp_path, "release.7z")
    app = _app(tmp_path)
    app._scan_archives()

    app._extract_worker()

    assert (tmp_path / "release.7z").exists()


def test_frena_si_no_entra_en_el_disco(tmp_path, monkeypatch):
    _make_7z(tmp_path, "release.7z")
    app = _app(tmp_path)
    app._scan_archives()
    monkeypatch.setattr(archives, "check_space", lambda *a, **k: (False, 1024))

    app._extract_worker()

    assert not (tmp_path / "release" / "juego.pkg").exists()
    assert any(kind == "error" and "espacio" in t for kind, t in app.logs)


def test_volumen_faltante_deshabilita_el_boton(tmp_path):
    for i in (1, 2, 4):
        (tmp_path / f"x.part{i}.rar").write_bytes(b"z" * 10)
    app = _app(tmp_path)

    app._scan_archives()

    assert app.btn_extract.disabled is True
    assert any(kind == "error" and "faltan volúmenes" in t for kind, t in app.logs)
