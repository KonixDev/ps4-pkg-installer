"""
Tests de la cola de envío.

RPI atiende de a un pedido: mientras baja un PKG no contesta /api. Eso obliga
a mandar de a uno esperando turno, pero NO tiene por qué obligar al usuario a
esperar para elegir. Antes sí: con un envío en curso el botón quedaba gris, y
peor, arrancar un envío con la consola descargando abortaba todo con "Cancelo
para no encolar". La salida era abrir una segunda instancia de la app — que es
justo lo que fabrica tareas huérfanas, porque las dos le mandan a la vez.

Ahora hay una cola con un solo worker: encolar es libre, mandar sigue siendo
de a uno.
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps4_pkg_installer as app_mod
from ps4_pkg_installer import App


class _Campo:
    def __init__(self, v=""):
        self.value = v


class _Chip:
    disabled = False
    visible = False
    value = None


class _Boton:
    """Igual que el de verdad: un Row con [icono, texto] adentro."""

    def __init__(self):
        self.disabled = False
        self.content = type("Row", (), {})()
        self.content.controls = [_Chip(), _Chip()]

    @property
    def texto(self):
        return self.content.controls[1].value


class _Respuesta:
    """Lo que devuelve urlopen: un context manager con .read()."""

    def __init__(self, cuerpo):
        self.cuerpo = cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.cuerpo


def _app(estados=("ok",)):
    app = App.__new__(App)
    app.installing = False
    app.stopping = False
    app.pkgs = []
    app.queue = []
    app.queue_lock = threading.Lock()
    app._alias_seq = 0
    app.httpd = object()
    app.logs = []
    app.log = lambda t, kind="info": app.logs.append((kind, t))
    app.refresh_rows = lambda: None
    app._safe_update = lambda: None
    app.set_chip = lambda *a, **k: None
    app.poll_task = lambda pkg: None
    app.chip_ps4 = _Chip()
    app.btn_install = _Boton()
    app.progress = _Chip()
    app.f_ps4 = _Campo("192.168.1.35")
    app.f_local = _Campo("192.168.1.73")
    app.f_port = _Campo("8000")

    seq = list(estados)
    app.ps4_state = lambda ip, timeout=8: seq.pop(0) if len(seq) > 1 else seq[0]
    app._wait_for_console = lambda ip, pkg: True
    return app


def _pkg(nombre="juego.pkg", **over):
    p = {
        "name": nombre, "path": "/carpeta/" + nombre, "rel": nombre,
        "state": "idle", "size": 1000, "cb": _Chip(),
    }
    p["cb"].value = True
    p.update(over)
    return p


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(app_mod.time, "sleep", lambda s: None)


@pytest.fixture
def _consola_ok(monkeypatch):
    """urlopen que siempre acepta, y anota qué URLs le mandaron."""
    enviadas = []

    def fake(req, timeout=None):
        import json
        enviadas.append(json.loads(req.data.decode())["packages"][0])
        return _Respuesta(b'{"status": "success", "task_id": 1}')

    monkeypatch.setattr(app_mod.urllib.request, "urlopen", fake)
    return enviadas


# ------------------------------------------------------------------ encolar


def test_se_puede_encolar_con_un_envio_en_curso():
    app = _app()
    app.installing = True
    app.pkgs = [_pkg("nuevo.pkg")]

    app.on_install(None)

    assert [p["name"] for p in app.queue] == ["nuevo.pkg"]
    assert not any("no seleccionaste" in t.lower() for _, t in app.logs)


def test_no_se_encola_dos_veces_el_mismo_paquete():
    app = _app()
    app.installing = True
    pkg = _pkg()
    app.pkgs = [pkg]

    app.on_install(None)
    app.on_install(None)

    assert app.queue == [pkg]


def test_no_se_reenvia_algo_que_ya_esta_bajando():
    app = _app()
    app.installing = True
    app.pkgs = [_pkg("bajando.pkg", state="downloading"), _pkg("nuevo.pkg")]

    app.on_install(None)

    assert [p["name"] for p in app.queue] == ["nuevo.pkg"]


def test_cancelar_vacia_la_cola():
    app = _app()
    app.installing = True
    app.pkgs = [_pkg()]
    app.on_install(None)
    assert app.queue

    app.on_cancel_all(None)

    assert app.queue == []


# ------------------------------------------------------------------- worker


def test_la_consola_ocupada_ya_no_aborta_el_envio(_consola_ok):
    """
    Era el muro de verdad: con la PS4 descargando, ps4_state da "busy" y el
    envío entero moría en "Cancelo para no encolar" antes de mandar nada.
    Ahora espera turno, que es lo que _wait_for_console ya sabía hacer.
    """
    app = _app(estados=("busy",))
    app.pkgs = [_pkg()]
    app._enqueue(app.pkgs)

    app._install_worker("192.168.1.35")

    assert len(_consola_ok) == 1, "no mandó el paquete"
    assert not any("cancelo" in t.lower() for _, t in app.logs)


def test_la_consola_caida_si_aborta(_consola_ok):
    """"down" es que no hay nadie en el puerto: esperar no arregla nada."""
    app = _app(estados=("down",))
    app.pkgs = [_pkg()]
    app._enqueue(app.pkgs)

    app._install_worker("192.168.1.35")

    assert _consola_ok == []


def test_el_worker_toma_lo_que_se_agrega_mientras_corre(_consola_ok):
    """
    El corazón: encolar durante el envío tiene que sumarse a la tanda viva,
    no arrancar un segundo worker (dos POST /api/install a la vez es
    exactamente lo que deja tareas huérfanas).
    """
    app = _app()
    primero, segundo = _pkg("uno.pkg"), _pkg("dos.pkg")
    app.pkgs = [primero, segundo]
    app._enqueue([primero])

    # Cuando el worker está por mandar el primero, aparece el segundo.
    original = app._wait_for_console
    def espiar(ip, pkg):
        if pkg is primero:
            app._enqueue([segundo])
        return original(ip, pkg)
    app._wait_for_console = espiar

    app._install_worker("192.168.1.35")

    assert len(_consola_ok) == 2, "no tomó el que se agregó en el medio"
    assert app.queue == []


# ------------------------------------------------------- un solo worker vivo


def test_solo_un_hilo_se_queda_con_el_worker():
    """
    Dos clicks casi simultáneos no pueden dejar dos workers: RPI atiende de a
    uno y dos installs a la vez le dejan una tarea sin task_id.
    """
    app = _app()

    assert app._claim_worker() is True
    assert app._claim_worker() is False


def test_al_terminar_con_la_cola_vacia_suelta_el_worker(_consola_ok):
    app = _app()
    app.pkgs = [_pkg()]
    app._enqueue(app.pkgs)
    app._claim_worker()

    app._install_worker("192.168.1.35")

    assert app.installing is False, "quedó reservado y nadie podría enviar más"
    assert app._claim_worker() is True


def test_lo_que_se_encola_justo_al_final_no_queda_dormido(monkeypatch, _consola_ok):
    """
    La ventana fina: el while ya vio la cola vacía y todavía no bajó
    `installing`. Si en ese hueco entra un paquete, sin relevo se quedaría en
    la cola para siempre, sin nadie que lo mande.
    """
    app = _app()
    tarde = _pkg("tarde.pkg")
    app.pkgs = [_pkg("temprano.pkg"), tarde]
    app._enqueue([app.pkgs[0]])
    app._claim_worker()

    # El relevo se lanza en un Thread: acá lo corremos en línea para que el
    # test sea determinístico.
    class Espia(app_mod.threading.Thread):
        def start(self):
            self.run()
    monkeypatch.setattr(app_mod.threading, "Thread", Espia)

    # El hueco: el resumen se loguea después del while y antes del finally.
    log_real = app.log
    def log_espia(texto, kind="info"):
        log_real(texto, kind)
        if "Seguí el progreso" in texto and tarde["state"] == "idle":
            app._enqueue([tarde])
    app.log = log_espia

    app._install_worker("192.168.1.35")

    assert len(_consola_ok) == 2, "el que entró tarde nunca salió"
    assert app.installing is False
