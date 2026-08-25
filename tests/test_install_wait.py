"""
Tests de la espera de turno antes de cada envío.

RPI atiende de a un pedido: mientras descarga un PKG no contesta /api. Si se
le manda el siguiente install igual, el POST vence a los 150 s pero la consola
igual procesa el request y encola la tarea — con un task_id que nunca llegó al
cliente. Esa tarea queda huérfana: no se puede seguir, ni pausar, ni cancelar.
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps4_pkg_installer as app_mod
from ps4_pkg_installer import App


def _app(estados):
    """App sin ventana; `estados` es la secuencia que devuelve ps4_state."""
    app = App.__new__(App)
    app.stopping = False
    app.logs = []
    app.log = lambda t, kind="info": app.logs.append((kind, t))
    app.refresh_rows = lambda: None
    seq = list(estados)
    app.consultas = []
    def ps4_state(ip, timeout=8):
        app.consultas.append(ip)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    app.ps4_state = ps4_state
    return app


def _pkg(**over):
    p = {"name": "juego.pkg", "path": "/carpeta/juego.pkg", "state": "idle", "size": 1000}
    p.update(over)
    return p


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(app_mod.time, "sleep", lambda s: None)


def test_no_espera_si_la_consola_ya_atiende():
    app = _app(["ok"])
    pkg = _pkg()

    assert app._wait_for_console("192.168.1.35", pkg) is True
    assert len(app.consultas) == 1
    assert pkg["state"] == "idle"       # no la toca si no hubo espera


def test_espera_mientras_la_consola_esta_ocupada():
    app = _app(["busy", "busy", "busy", "ok"])
    pkg = _pkg()

    assert app._wait_for_console("192.168.1.35", pkg) is True
    assert len(app.consultas) == 4


def test_marca_el_paquete_como_esperando():
    """La fila tiene que decir que está esperando turno, no parecer colgada."""
    vistos = []
    app = _app(["busy", "ok"])
    pkg = _pkg()
    app.refresh_rows = lambda: vistos.append(pkg["state"])

    app._wait_for_console("192.168.1.35", pkg)

    assert "waiting" in vistos


def test_cancelar_corta_la_espera():
    app = _app(["busy"])
    pkg = _pkg()
    original = app.ps4_state
    def ps4_state(ip, timeout=8):
        app.stopping = True          # el usuario toca Cancelar todo
        return original(ip, timeout)
    app.ps4_state = ps4_state

    assert app._wait_for_console("192.168.1.35", pkg) is False


def test_se_rinde_si_la_consola_desaparece():
    """
    'busy' es una consola trabajando y se espera. 'down' es que no hay nadie
    en el puerto: apagada, dormida o cambió de IP. Ahí esperar no sirve.
    """
    app = _app(["down"])
    pkg = _pkg()

    assert app._wait_for_console("192.168.1.35", pkg) is False
    assert any(kind == "error" for kind, _ in app.logs)


def test_un_down_aislado_no_alcanza_para_rendirse():
    """Un pico de red no debería abortar un envío que iba bien."""
    app = _app(["down", "busy", "ok"])
    pkg = _pkg()

    assert app._wait_for_console("192.168.1.35", pkg) is True


def test_avisa_una_sola_vez_que_esta_esperando():
    app = _app(["busy", "busy", "busy", "busy", "ok"])
    pkg = _pkg()

    app._wait_for_console("192.168.1.35", pkg)

    esperas = [t for _, t in app.logs if "espero" in t.lower()]
    assert len(esperas) == 1, f"logueó {len(esperas)} veces: {esperas}"


def test_el_estado_de_no_confirmado_existe_y_no_es_error():
    """Un envío sin confirmar no es un fallo: la tarea probablemente se creó."""
    assert "queued" in App.STATES
    etiqueta, color, _ = App.STATES["queued"]
    assert color != app_mod.RED


# ------------------------------------------------- el timeout ya no es error

def _worker_app(monkeypatch, estados=("ok",), lanzar=None):
    """App lista para correr _install_worker con la red mockeada."""
    app = _app(estados)
    app.installing = False
    app.queue = []
    app.queue_lock = threading.Lock()
    app.pkgs = []
    boton = type("C", (), {"disabled": False})()
    boton.content = type("Row", (), {})()
    boton.content.controls = [type("C", (), {"name": None})(),
                              type("C", (), {"value": ""})()]
    app.btn_install = boton
    app.progress = type("C", (), {"visible": False, "value": 0})()
    app._safe_update = lambda: None
    app.set_chip = lambda *a, **k: None
    app.chip_ps4 = None
    app.f_local = type("C", (), {"value": "192.168.1.73"})()
    app.f_port = type("C", (), {"value": "8000"})()
    app.httpd = object()
    app._handle_install_reply = lambda pkg, data: None

    def fake_urlopen(req, timeout=None):
        raise lanzar
    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake_urlopen)
    return app


def test_timeout_marca_en_cola_sin_confirmar_no_error(monkeypatch):
    """El bug: marcaba error un paquete que la consola sí había encolado."""
    import socket
    app = _worker_app(monkeypatch, lanzar=socket.timeout())
    pkg = _pkg()

    app._enqueue([pkg])
    app._install_worker("192.168.1.35")

    assert pkg["state"] == "queued", f"quedó en {pkg['state']}"
    assert not any(kind == "error" for kind, _ in app.logs), app.logs


def test_no_aconseja_reiniciar_la_consola(monkeypatch):
    """Reiniciar RPI cortaría las descargas que están andando bien."""
    import socket
    app = _worker_app(monkeypatch, lanzar=socket.timeout())

    app._enqueue([_pkg()])
    app._install_worker("192.168.1.35")

    assert not any("reinicia" in t.lower() for _, t in app.logs), app.logs


def test_espera_turno_antes_de_cada_envio(monkeypatch):
    """Con la consola ocupada, el segundo envío no sale hasta que se libere."""
    import socket
    app = _worker_app(monkeypatch, estados=["ok", "busy", "busy", "ok"],
                      lanzar=socket.timeout())
    p1, p2 = _pkg(name="a.pkg"), _pkg(name="b.pkg")

    app._enqueue([p1, p2])
    app._install_worker("192.168.1.35")

    # 1 del chequeo inicial + 1 por p1 + 2 esperando + 1 al liberarse
    assert len(app.consultas) >= 4, app.consultas


# --------------------------------------------------------------- cancelación


def _app_cancelable(pkgs, installing):
    app = App.__new__(App)
    app.queue = []
    app.queue_lock = threading.Lock()
    app.stopping = False
    app.installing = installing
    app.pkgs = pkgs
    app.logs = []
    app.log = lambda t, kind="info": app.logs.append((kind, t))
    app.refresh_rows = lambda: None
    return app


def test_cancelar_corta_el_envio_aunque_todavia_no_haya_task_id():
    """
    Entre que se elige un paquete y la consola devuelve su task_id pasa un
    rato largo: el envío espera turno (_wait_for_console puede quedarse ahí
    horas mientras baja el anterior). Si en ese momento se cancela, no hay
    ninguna tarea con task_id para dar de baja — pero el envío SÍ está en
    curso y hay que cortarlo.
    """
    pkg = _pkg(state="waiting", task_id=None)
    app = _app_cancelable([pkg], installing=True)

    app.on_cancel_all(None)

    assert app.stopping is True, "el envío en curso siguió adelante"
    assert not any("no hay tareas activas" in t.lower() for _, t in app.logs)


def test_cancelar_sin_nada_en_curso_no_deja_la_app_trabada():
    """
    Al revés: sin envío ni tareas, cancelar no puede dejar `stopping` en True
    —el próximo envío abortaría solo, sin explicación.
    """
    app = _app_cancelable([_pkg(state="idle", task_id=None)], installing=False)

    app.on_cancel_all(None)

    assert app.stopping is False
    assert any("no hay tareas activas" in t.lower() for _, t in app.logs)
