"""
Tests del resumen de la tanda: la barra de arriba y su texto.

Es lo primero que se mira en una descarga de horas, y hasta ahora era lo
único que no se movía: sumaba `transferred`, que sale de la API de la
consola, y esa API está muda TODO el tiempo que dura la transferencia.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps4_pkg_installer import App


GB = 1024 ** 3


class _C:
    def __init__(self):
        self.value = None
        self.visible = False


def _app(pkgs):
    app = App.__new__(App)
    app.pkgs = pkgs
    app.progress = _C()
    app.overall = _C()
    app.btn_cancel_all = _C()
    app._hist_global = []
    return app


def _pkg(estado, size, **over):
    p = {"name": "x.pkg", "state": estado, "size": size, "length": 0,
         "transferred": 0, "served_pos": 0, "stale": False}
    p.update(over)
    return p


def test_la_barra_general_avanza_con_la_api_muda():
    """
    El bug: las filas avanzaban (usan _progress_of, que cae en served_pos) y
    la barra de arriba se quedaba en cero, que es cuando más se la mira.
    """
    pkg = _pkg("downloading", 10 * GB, stale=True, served_pos=4 * GB)
    app = _app([pkg])

    app._update_overall()

    assert app.progress.value == pytest_approx(0.4)


def test_lo_que_espera_en_la_cola_cuenta_en_el_total():
    """Si no, el total salta cada vez que un paquete sale de la cola."""
    app = _app([
        _pkg("downloading", 10 * GB, stale=True, served_pos=5 * GB),
        _pkg("pending", 10 * GB),
    ])

    app._update_overall()

    assert app.progress.value == pytest_approx(0.25)
    assert "de 20.0 GB" in app.overall.value


def test_sin_nada_en_juego_el_resumen_desaparece():
    app = _app([_pkg("idle", 10 * GB)])

    app._update_overall()

    assert app.progress.visible is False
    assert app.overall.value == ""


# ------------------------------------------------------------------- el ETA


def test_el_eta_sale_de_la_velocidad_medida():
    """100 MB en 10 s y faltan 900 MB => 90 s."""
    app = _app([])
    app._eta_global(0, 1000 << 20, ahora=1000.0)
    faltan = app._eta_global(100 << 20, 1000 << 20, ahora=1010.0)

    assert 85 <= faltan <= 95, faltan


def test_sin_dos_medidas_todavia_no_hay_eta():
    """Arriesgar un número con una sola muestra es peor que no mostrar nada."""
    app = _app([])

    assert app._eta_global(50 << 20, 1000 << 20, ahora=1000.0) == 0


def test_el_eta_ignora_lo_viejo():
    """
    Una medición de hace media hora describe otra parte de la descarga. La
    velocidad real cambia: la consola alterna entre bajar e instalar.
    """
    app = _app([])
    app._eta_global(0, 1000 << 20, ahora=0.0)
    app._eta_global(500 << 20, 1000 << 20, ahora=3000.0)
    faltan = app._eta_global(600 << 20, 1000 << 20, ahora=3010.0)

    # Sobre la ventana corta: 100 MB en 10 s, faltan 400 MB => 40 s.
    assert 35 <= faltan <= 45, faltan


def pytest_approx(v, tol=0.01):
    class _Aprox:
        def __eq__(self, otro):
            return abs(otro - v) <= tol
        def __repr__(self):
            return f"~{v}"
    return _Aprox()
