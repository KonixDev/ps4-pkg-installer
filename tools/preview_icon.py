#!/usr/bin/env python3
"""
Tira de contactos del icono a los tamaños en que se lo ve de verdad.

El icono se diseña a 1024 y se mira a 32. Esta hoja existe para juzgarlo
donde importa, no donde se dibuja.
"""

import os
import sys

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TAMANOS = (16, 24, 32, 48, 64, 128, 256)
FONDOS = ((240, 240, 242), (28, 28, 30))       # barra clara y oscura


def main():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icono = Image.open(os.path.join(raiz, "assets", "icon.png")).convert("RGBA")

    margen, sep = 30, 26
    ancho = margen * 2 + sum(TAMANOS) + sep * (len(TAMANOS) - 1)
    alto_fila = 256 + 40
    hoja = Image.new("RGB", (ancho, alto_fila * len(FONDOS)), FONDOS[0])

    for fila, fondo in enumerate(FONDOS):
        banda = Image.new("RGB", (ancho, alto_fila), fondo)
        x = margen
        for t in TAMANOS:
            chico = icono.resize((t, t), Image.LANCZOS)
            banda.paste(chico, (x, alto_fila - 20 - t), chico)
            x += t + sep
        hoja.paste(banda, (0, fila * alto_fila))

    destino = os.path.join(raiz, "assets", "preview.png")
    hoja.save(destino)
    print(destino)


if __name__ == "__main__":
    main()
