"""
Tests de la ruta con la que se publica cada PKG.

Contexto medido contra una PS4 real (RPI 12800): la consola decodifica el
percent-encoding de la URL y usa el resultado CRUDO para armar el request
HTTP. Un solo espacio en la ruta le parte el request line y falla en 21 ms
sin abrir un socket: "Unable to set up prerequisites for package". Servido
el mismo archivo bajo una ruta plana, la misma consola contesta
{"status": "success", "task_id": 85}. Ninguna codificación evita el bug
—%20 es correcto y falla igual— así que la URL no puede llevar el nombre
real del archivo.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps4_pkg_installer as app_mod
from ps4_pkg_installer import App, PkgHandler


FEO = "GAME/Mafia - Definitive Edition [CUSA18097] FIXED 7.50+/MAFIAGAME.pkg"


def _app():
    app = App.__new__(App)
    app._alias_seq = 0
    app.pkgs = []
    app.logs = []
    app.log = lambda texto, kind="info": app.logs.append((kind, texto))
    app.refresh_rows = lambda: None
    return app


def _pkg(**over):
    pkg = {
        "path": "/carpeta/" + FEO, "name": "MAFIAGAME.pkg", "rel": FEO,
        "state": "sending", "size": 100, "served_pos": 0, "last_log": 0,
        "transferred": 0, "length": 0,
    }
    pkg.update(over)
    return pkg


def test_la_ruta_publicada_no_tiene_caracteres_que_rompan_rpi():
    app = _app()
    alias = app._publish(_pkg())
    assert not set(alias) & set(" []+%&?#"), alias
    assert alias.startswith("/")


def test_el_handler_traduce_el_alias_al_archivo_real():
    app = _app()
    pkg = _pkg()
    alias = app._publish(pkg)

    h = PkgHandler.__new__(PkgHandler)
    h.directory = "/otra/carpeta"
    assert h.translate_path(alias) == pkg["path"]
    # Con query string (la PS4 le cuelga downloadId, threadId, etc.)
    assert h.translate_path(alias + "?downloadId=00000055&r=3") == pkg["path"]


def test_dos_paquetes_no_comparten_alias():
    app = _app()
    a, b = _pkg(name="A.pkg", path="/x/A.pkg"), _pkg(name="B.pkg", path="/x/B.pkg")
    assert app._publish(a) != app._publish(b)


def test_reenviar_el_mismo_paquete_no_le_cambia_la_ruta():
    """Si el alias se reasignara por posición, un reenvío podría pisar el
    alias de otro paquete que todavía está descargando."""
    app = _app()
    pkg = _pkg()
    assert app._publish(pkg) == app._publish(pkg)


def test_el_progreso_reconoce_al_paquete_por_su_alias():
    app = _app()
    pkg = _pkg()
    app.pkgs = [pkg]
    alias = app._publish(pkg)

    app._on_download(alias + "?downloadId=00000055", "bytes=18874368-")
    assert pkg["served_pos"] == 18874368


def test_el_log_nombra_el_archivo_real_y_no_el_alias():
    app = _app()
    pkg = _pkg()
    app.pkgs = [pkg]
    alias = app._publish(pkg)

    app._on_download(alias, None)
    assert any("MAFIAGAME.pkg" in t for _, t in app.logs)
    assert not any(alias.lstrip("/") in t for _, t in app.logs)
