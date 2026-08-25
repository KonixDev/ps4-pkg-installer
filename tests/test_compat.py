"""
Compatibilidad de controles de Flet con el Python que corre la app.

macOS trae Python 3.9 y es el que usa quien corre desde fuente o compila en su
máquina. Flet 0.28.3 tiene al menos un control que no funciona ahí.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FUENTE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ps4_pkg_installer.py")


def test_flet_tab_no_funciona_en_python_39():
    """
    Documenta el porqué: Tab.before_update hace isinstance(icon, IconValue) y
    IconValue es un Union. isinstance con Union recién anda desde 3.10; en 3.9
    lanza TypeError y la ventana no llega a montarse.
    """
    if sys.version_info >= (3, 10):
        pytest.skip("isinstance con Union funciona desde 3.10")

    from flet.core.types import IconValue

    with pytest.raises(TypeError, match="Subscripted generics"):
        isinstance(None, IconValue)


def test_la_app_no_usa_ft_Tabs():
    """
    Las pestañas están hechas a mano justamente por lo de arriba. Este test
    existe para que nadie las reemplace por ft.Tabs sin darse cuenta.
    """
    fuente = open(FUENTE, encoding="utf8").read()

    assert "ft.Tabs(" not in fuente, "ft.Tabs rompe en Python 3.9"
    assert "ft.Tab(" not in fuente, "ft.Tab rompe en Python 3.9"
