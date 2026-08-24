"""
Tests del servidor HTTP que le sirve los PKG a la consola.

Lo que se sirve acá es una transferencia de decenas de GB que dura horas: un
error que tire el thread del request o que cambie la ruta a mitad de camino le
corta la descarga a la consola, y hay que empezar de cero.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps4_pkg_installer import App, PkgHandler


class _Headers:
    def __init__(self, valores=None):
        self.valores = valores or {}

    def get(self, k, default=None):
        return self.valores.get(k, default)


def _handler(**attrs):
    """
    Handler crudo, sin `path` ni `headers`: así llega realmente uno cuando
    parse_request se planta en el request line. Pasarle headers de mentira
    esconde el bug (lo escondió una vez).
    """
    h = PkgHandler.__new__(PkgHandler)
    for k, v in attrs.items():
        setattr(h, k, v)
    return h


def test_un_request_malformado_no_revienta_el_log():
    """
    Visto en el log real del servidor: cuando la consola corta una conexión a
    medias, BaseHTTPRequestHandler llama a log_message ANTES de haber parseado
    el request line, así que self.path todavía no existe:

        AttributeError: 'PkgHandler' object has no attribute 'path'

    Y en cuanto se salva `path`, se cae en el de al lado: `headers` tampoco
    existe todavía, porque parse_request lo arma DESPUÉS del request line.

    Se come el evento de progreso y ensucia la consola con un traceback por
    cada request roto.
    """
    visto = []
    PkgHandler.notify = staticmethod(lambda p, r: visto.append((p, r)))
    try:
        _handler().log_message("code %d, message %s", 400, "Bad request")
    finally:
        PkgHandler.notify = None

    assert visto, "el evento se perdió"


def test_un_request_sano_sigue_reportando_el_range():
    visto = []
    PkgHandler.notify = staticmethod(lambda p, r: visto.append((p, r)))
    try:
        _handler(path="/p1.pkg", headers=_Headers({"Range": "bytes=500-"})).log_message("%s", "ok")
    finally:
        PkgHandler.notify = None

    assert visto == [("/p1.pkg", "bytes=500-")]


def test_cambiar_de_carpeta_no_le_corta_la_descarga_a_la_consola():
    """
    La carpeta activa se puede cambiar en caliente (use_folder mueve
    PkgHandler.root sin reiniciar el servidor). Un paquete que ya está
    viajando tiene que seguir resolviéndose igual: la consola pide la misma
    ruta durante horas y no se entera de nada.
    """
    app = App.__new__(App)
    app._alias_seq = 0
    pkg = {"path": "/Volumes/Disco/Juegos/Mafia [CUSA18097]/JUEGO.pkg", "name": "JUEGO.pkg"}
    alias = app._publish(pkg)

    PkgHandler.root = "/otra/carpeta/completamente/distinta"
    h = _handler(directory=PkgHandler.root)

    assert h.translate_path(alias) == pkg["path"]
