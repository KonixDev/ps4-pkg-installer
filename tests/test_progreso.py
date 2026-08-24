"""
Tests del progreso medido en el servidor local.

Mientras la consola descarga, su API no contesta: el único que sabe algo es
nuestro propio servidor HTTP. De ahí sale la barra, así que si mide mal, mide
mal durante las horas que dura la transferencia.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps4_pkg_installer import App, _Limited


GB = 1024 ** 3


class _Ceros:
    """
    Archivo de N bytes que no ocupa N bytes de RAM. Los tamaños de acá son
    los reales (37 GB), así que materializarlos no es una opción.
    """

    def __init__(self, n):
        self.quedan = n

    def read(self, n):
        n = min(n, self.quedan)
        self.quedan -= n
        return b"\0" * n

    def close(self):
        pass


def _app(pkg):
    app = App.__new__(App)
    app.pkgs = [pkg]
    app.logs = []
    app.log = lambda t, kind="info": app.logs.append((kind, t))
    app.refresh_rows = lambda: None
    return app


def _pkg(**over):
    p = {
        "name": "juego.pkg", "alias": "/p1.pkg", "size": 37 * GB,
        "served_pos": 0, "last_log": 0, "state": "downloading", "stale": True,
        "transferred": 0, "length": 0, "title": "Mafia",
    }
    p.update(over)
    return p


def test_un_rango_servido_entero_cuenta_hasta_el_final():
    """
    El bug: se guardaba el byte donde EMPIEZA el rango. La consola pide
    chunks de cientos de MB, así que la barra se quedaba corta por el tamaño
    de un chunk entero — medido, ~537 MB sobre 37 GB: se clavaba en 98%.
    """
    pkg = _pkg()
    app = _app(pkg)
    inicio, largo = 36 * GB, 1 * GB

    trozo = _Limited(_Ceros(largo), largo,
                     start=inicio, on_progress=lambda pos: app._on_served("/p1.pkg", pos))
    while trozo.read(1 << 20):
        pass
    trozo.close()

    assert pkg["served_pos"] == inicio + largo


def test_el_avance_se_reporta_durante_el_chunk_y_no_solo_al_final():
    """Un chunk de 537 MB tarda minutos: la barra no puede quedarse quieta."""
    avisos = []
    largo = 64 << 20
    trozo = _Limited(_Ceros(largo), largo,
                     start=0, on_progress=avisos.append)
    while trozo.read(1 << 20):
        pass
    trozo.close()

    assert len(avisos) > 1, avisos
    assert avisos == sorted(avisos)
    assert avisos[-1] == largo


def test_una_conexion_cortada_no_infla_el_progreso():
    """Si la consola corta a mitad del chunk, cuenta lo entregado, no lo pedido."""
    pkg = _pkg()
    app = _app(pkg)
    largo = 100 << 20

    trozo = _Limited(_Ceros(largo), largo,
                     start=0, on_progress=lambda pos: app._on_served("/p1.pkg", pos))
    trozo.read(10 << 20)
    trozo.close()

    assert pkg["served_pos"] == 10 << 20


def test_el_progreso_no_retrocede():
    """La consola pide rangos fuera de orden (header, sfo, icono)."""
    pkg = _pkg(served_pos=30 * GB)
    app = _app(pkg)

    app._on_served("/p1.pkg", 12345)

    assert pkg["served_pos"] == 30 * GB


def test_servido_del_todo_con_la_api_muda_se_dice_con_todas_las_letras():
    """
    Si se sirvió el archivo entero y la consola no contesta, quedarse
    mostrando un porcentaje es lo peor de los dos mundos: ya no falta nada
    por transferir, y lo que falta —la instalación— no lo sabemos.
    """
    pkg = _pkg(served_pos=37 * GB, length=37 * GB)
    app = _app(pkg)

    hechos, total = app._progress_of(pkg)

    assert hechos >= total
    assert app._detalle_transferencia(pkg) == "Transferido entero · la consola está terminando"
