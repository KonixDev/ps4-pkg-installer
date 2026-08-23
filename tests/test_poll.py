"""
Tests de la política de reintento de poll_task.

Contexto: RPI (el instalador de la PS4) sirve la API y descarga el PKG con el
mismo servidor HTTP de un solo hilo. Mientras baja un paquete grande, /api deja
de contestar por completo — medido: 12 timeouts de 10s seguidos, cero
respuestas. Que la consola no conteste es lo NORMAL durante una transferencia,
no una condición terminal.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps4_pkg_installer as app_mod
from ps4_pkg_installer import App


def _app(respuestas):
    """
    App sin __init__ (no hace falta ventana) con rpi_call scripteado.

    `respuestas` es una lista; cada llamada consume una. Cuando se agota,
    sigue devolviendo la última.
    """
    app = App.__new__(App)
    app.stopping = False
    app.logs = []
    app.log = lambda texto, kind="info": app.logs.append((kind, texto))
    app.refresh_rows = lambda: None

    seq = list(respuestas)
    def rpi_call(endpoint, payload, timeout=15):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    app.rpi_call = rpi_call
    return app


def _pkg(**over):
    pkg = {
        "task_id": 7, "name": "juego.pkg", "polling": True, "state": "downloading",
        "size": 1000, "transferred": 400, "length": 1000, "rest_sec": 60,
    }
    pkg.update(over)
    return pkg


def _cortar_a(pkg, n, monkeypatch):
    """Deja correr n sleeps y después simula que el usuario cancela."""
    contador = {"n": 0, "esperas": []}
    def fake_sleep(s):
        contador["n"] += 1
        contador["esperas"].append(s)
        if contador["n"] >= n:
            pkg["polling"] = False
    monkeypatch.setattr(app_mod.time, "sleep", fake_sleep)
    return contador


TIMEOUT = {"status": "fail", "_exc": "timeout: timed out"}


def test_no_se_rinde_mientras_la_consola_no_contesta(monkeypatch):
    """El bug: a los 3 timeouts apagaba el polling y no volvía nunca."""
    pkg = _pkg()
    app = _app([TIMEOUT])
    c = _cortar_a(pkg, 10, monkeypatch)

    app.poll_task(pkg)

    assert c["n"] >= 10, f"el polling se rindió tras {c['n']} intentos"
    assert pkg["state"] != "unknown", "dio la tarea por perdida"


def test_conserva_el_ultimo_progreso_conocido(monkeypatch):
    """La fila no puede volver a cero: la PS4 sigue bajando."""
    pkg = _pkg(transferred=400, length=1000)
    app = _app([TIMEOUT])
    _cortar_a(pkg, 5, monkeypatch)

    app.poll_task(pkg)

    assert pkg["transferred"] == 400
    assert pkg["length"] == 1000


def test_marca_stale_para_que_la_ui_lo_muestre(monkeypatch):
    """Sin datos frescos la UI tiene que decirlo, no fingir que todo va bien."""
    pkg = _pkg()
    app = _app([TIMEOUT])
    _cortar_a(pkg, 5, monkeypatch)

    app.poll_task(pkg)

    assert pkg.get("stale") is True


def test_hace_backoff_en_vez_de_martillar(monkeypatch):
    """Reintentar cada 2s contra una consola saturada no ayuda a nadie."""
    pkg = _pkg()
    app = _app([TIMEOUT])
    c = _cortar_a(pkg, 8, monkeypatch)

    app.poll_task(pkg)

    assert max(c["esperas"]) > 2, f"no hubo backoff: {c['esperas']}"
    assert max(c["esperas"]) <= 30, "el backoff se fue de mambo"


def test_se_recupera_cuando_la_consola_vuelve(monkeypatch):
    """Lo importante: al volver la respuesta, el progreso se actualiza solo."""
    pkg = _pkg(transferred=400)
    ok = {"status": "success", "transferred_total": 900,
          "length_total": 1000, "rest_sec_total": 10, "local_copy_percent": 0}
    app = _app([TIMEOUT, TIMEOUT, TIMEOUT, TIMEOUT, ok])
    _cortar_a(pkg, 12, monkeypatch)

    app.poll_task(pkg)

    assert pkg["transferred"] == 900, "no retomó al volver la consola"
    assert pkg["state"] == "downloading"
    assert pkg.get("stale") is False


def test_termina_cuando_la_tarea_se_completa(monkeypatch):
    """El loop tiene que cortar solo cuando de verdad terminó."""
    pkg = _pkg()
    done = {"status": "success", "transferred_total": 1000,
            "length_total": 1000, "local_copy_percent": 100}
    app = _app([done])
    _cortar_a(pkg, 50, monkeypatch)

    app.poll_task(pkg)

    assert pkg["state"] == "done"
    assert pkg["polling"] is False


# --------------------------------------------------------------- progreso local
# El servidor HTTP propio ve cada Range que pide la consola. Es la única fuente
# que no se cae mientras hay transferencia, así que de ahí sale la barra.


def _app_con_pkgs(pkgs):
    app = App.__new__(App)
    app.logs = []
    app.log = lambda texto, kind="info": app.logs.append((kind, texto))
    app.refresh_rows = lambda: None
    app.pkgs = pkgs
    return app


def test_on_download_deriva_la_posicion_del_range():
    pkg = _pkg(name="juego.pkg", size=1000, transferred=0)
    app = _app_con_pkgs([pkg])

    app._on_download("/juego.pkg", "bytes=400-900")

    assert pkg["served_pos"] == 400


def test_on_download_ignora_el_query_string():
    """RPI agrega ?downloadId=...&r=... al pedir; el nombre está antes del '?'."""
    pkg = _pkg(name="juego.pkg", size=1000)
    app = _app_con_pkgs([pkg])

    app._on_download(
        "/juego.pkg?downloadId=0000001f&du=00&serverIpAddr=192.168.1.73&r=0b",
        "bytes=700-800",
    )

    assert pkg["served_pos"] == 700


def test_on_download_desescapa_el_nombre():
    """Los releases traen corchetes: _[5.05]_ viaja como _%5B5.05%5D_."""
    pkg = _pkg(name="TW3_[5.05]_OPOISSO893.pkg", size=1000)
    app = _app_con_pkgs([pkg])

    app._on_download("/TW3_%5B5.05%5D_OPOISSO893.pkg", "bytes=500-600")

    assert pkg["served_pos"] == 500


def test_on_download_no_retrocede():
    """La PS4 pide rangos fuera de orden (header, sfo, icono). Solo subir."""
    pkg = _pkg(name="juego.pkg", size=1000)
    app = _app_con_pkgs([pkg])

    app._on_download("/juego.pkg", "bytes=800-900")
    app._on_download("/juego.pkg", "bytes=10-20")

    assert pkg["served_pos"] == 800


def test_paint_usa_el_progreso_local_cuando_no_hay_datos_frescos():
    """El caso real: la API muda 23 minutos y la barra tiene que moverse igual."""
    pkg = _pkg(name="juego.pkg", size=1000, transferred=0, length=1000,
               state="downloading", stale=True, served_pos=850)
    app = App.__new__(App)

    assert app._progress_of(pkg) == (850, 1000)


def test_paint_prefiere_la_api_cuando_los_datos_son_frescos():
    pkg = _pkg(name="juego.pkg", size=1000, transferred=900, length=1000,
               state="downloading", stale=False, served_pos=100)
    app = App.__new__(App)

    assert app._progress_of(pkg) == (900, 1000)
