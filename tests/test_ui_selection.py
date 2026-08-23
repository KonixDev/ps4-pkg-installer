"""Filtro, selección en conjunto y orden — la lógica de la lista, sin ventana."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps4_pkg_installer as app


class _CB:
    """Sustituto del checkbox de Flet: solo value y disabled."""
    def __init__(self, value=True, disabled=False):
        self.value, self.disabled = value, disabled


def _pkg(name="a.pkg", title="", cat="ac", sub="", value=True, disabled=False):
    return {"name": name, "title": title, "category": cat, "sub": sub,
            "cb": _CB(value, disabled)}


def test_el_filtro_mira_titulo_nombre_y_subcarpeta():
    p = _pkg(name="X-CUSA02369.pkg", title="HITMAN™ - Episode 3: Marrakesh",
             sub="Hitman.DLCs")

    assert app.matches(p, "marrakesh") is True      # por título
    assert app.matches(p, "cusa02369") is True      # por nombre de archivo
    assert app.matches(p, "hitman.dlcs") is True    # por subcarpeta
    assert app.matches(p, "sapienza") is False


def test_el_filtro_vacio_deja_pasar_todo():
    assert app.matches(_pkg(), "") is True
    assert app.matches(_pkg(), "   ") is True


def test_todos_solo_toca_lo_visible():
    """La regla que no sorprende: lo que el filtro esconde conserva su tilde."""
    pkgs = [_pkg("a", value=False), _pkg("b", value=False), _pkg("c", value=False)]

    app.apply_bulk(pkgs, "all", visibles={0, 2})

    assert [p["cb"].value for p in pkgs] == [True, False, True]


def test_ninguno_solo_toca_lo_visible():
    pkgs = [_pkg("a"), _pkg("b"), _pkg("c")]

    app.apply_bulk(pkgs, "none", visibles={1})

    assert [p["cb"].value for p in pkgs] == [True, False, True]


def test_invertir_solo_toca_lo_visible():
    pkgs = [_pkg("a", value=True), _pkg("b", value=False)]

    app.apply_bulk(pkgs, "invert", visibles={0, 1})

    assert [p["cb"].value for p in pkgs] == [False, True]


def test_el_bulk_no_pisa_una_tarea_en_curso():
    """El checkbox de un paquete descargando está deshabilitado: se saltea."""
    pkgs = [_pkg("a", value=True, disabled=True), _pkg("b", value=True)]

    app.apply_bulk(pkgs, "none", visibles={0, 1})

    assert pkgs[0]["cb"].value is True, "pisó una tarea en curso"
    assert pkgs[1]["cb"].value is False


def test_orden_por_categoria_es_el_orden_de_instalacion():
    pkgs = [_pkg("d", cat="ac", title="DLC"), _pkg("b", cat="gd", title="Juego"),
            _pkg("u", cat="gp", title="Update")]

    assert [p["title"] for p in sorted(pkgs, key=app.group_key)] == \
        ["Juego", "Update", "DLC"]


def test_dentro_de_un_grupo_ordena_por_titulo():
    pkgs = [_pkg("z", cat="ac", title="Sapienza"), _pkg("a", cat="ac", title="Bangkok")]

    assert [p["title"] for p in sorted(pkgs, key=app.group_key)] == ["Bangkok", "Sapienza"]


def test_sin_categoria_cae_al_final():
    pkgs = [_pkg("x", cat="", title="Sin cat"), _pkg("g", cat="gd", title="Juego")]

    assert [p["title"] for p in sorted(pkgs, key=app.group_key)] == ["Juego", "Sin cat"]
