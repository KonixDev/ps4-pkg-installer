"""
Tests de la fila de un paquete.

Acá no se juzga el gusto sino lo medible: que ninguna columna se coma a otra
y que los controles aparezcan sólo cuando corresponden. El síntoma que dio
origen a esto fue un tamaño mostrado como "34." en vez de "34.5 GB".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flet as ft

from ps4_pkg_installer import App, human_size


GB = 1024 ** 3


def _app(pkgs=()):
    app = App.__new__(App)
    app.pkgs = list(pkgs)
    app.queue = []
    import threading
    app.queue_lock = threading.Lock()
    app.logs = []
    app.log = lambda t, kind="info": app.logs.append((kind, t))
    app.refresh_rows = lambda: None
    return app


def _pkg(**over):
    p = {"name": "MAFIAGAME.pkg", "title": "Mafia: Definitive Edition",
         "path": "/x/MAFIAGAME.pkg", "size": 34 * GB, "state": "idle",
         "icon": None, "length": 0, "transferred": 0, "served_pos": 0,
         "cb": ft.Checkbox(value=True)}
    p.update(over)
    return p


def _columnas(fila):
    return fila.content.controls


def test_el_tamano_no_lo_puede_empujar_un_estado_largo():
    """
    "En cola, sin confirmar" es el estado más largo que existe. Sin anchos
    fijos empuja la columna del tamaño fuera de la fila y la recorta.
    """
    pkg = _pkg()
    app = _app([pkg])
    fila = app._build_row(pkg)

    tamano = next(c for c in _columnas(fila)
                  if isinstance(c, ft.Text) and c.value == human_size(pkg["size"]))
    assert tamano.width and tamano.width >= 84
    # Se lee el atributo serializado y no `.no_wrap`: en Flet 0.28.3 el setter
    # guarda en "nowrap" y el getter lee otra clave, así que devuelve siempre
    # False aunque el control lo tenga puesto.
    assert tamano._get_attr("nowrap") is True

    estado = pkg["ui_state"]
    assert estado.width, "sin ancho, el estado empuja al resto"
    assert estado.overflow == ft.TextOverflow.ELLIPSIS


def test_el_ancho_alcanza_para_el_tamano_mas_largo():
    """1023.9 MB es el peor caso: 8 caracteres más el espacio."""
    pkg = _pkg(size=1023 * 1024 * 1024 + 900 * 1024)
    app = _app([pkg])
    fila = app._build_row(pkg)

    texto = human_size(pkg["size"])
    tamano = next(c for c in _columnas(fila)
                  if isinstance(c, ft.Text) and c.value == texto)
    # ~6.2 px por caracter a size 11.5 en la tipografía por defecto.
    assert tamano.width >= len(texto) * 6.2, f"{texto!r} no entra en {tamano.width}px"


def test_la_x_para_salir_de_la_cola_solo_aparece_en_la_cola():
    pkg = _pkg()
    app = _app([pkg])
    app._build_row(pkg)

    for estado, visible in [("pending", True), ("downloading", False),
                            ("idle", False), ("done", False)]:
        pkg["state"] = estado
        app._paint_row(pkg)
        assert pkg["ui_unqueue"].visible is visible, estado


def test_el_que_espera_dice_que_lugar_ocupa():
    a, b = _pkg(name="a.pkg"), _pkg(name="b.pkg")
    app = _app([a, b])
    app._build_row(a); app._build_row(b)
    app._enqueue([a, b])

    app._paint_row(b)

    assert pkg_detalle(b) == "2º en la cola"


def pkg_detalle(pkg):
    return pkg["ui_detail"].value
