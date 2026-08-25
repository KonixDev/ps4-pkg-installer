#!/usr/bin/env python3
"""
Genera el icono de la app: una caja con un 4.

Se ejecuta a mano, no en el build — sus resultados se versionan en assets/.
Así el CI no necesita Pillow ni fuentes instaladas, que es justo lo que no
se puede dar por sentado en un runner.

    python3 tools/make_icon.py

El 4 y la caja se dibujan con polígonos, sin depender de ninguna tipografía:
la misma corrida tiene que dar el mismo icono en cualquier máquina.

El riesgo conocido de este diseño es el tamaño chico —a 32px un 4 dentro de
una caja puede volverse una mancha—, así que todo acá está puesto para
sobrevivir esa reducción: silueta simple, un solo acento de color y trazos
gruesos. La prueba de fuego está en tools/preview_icon.py.
"""

import os
import sys

from PIL import Image, ImageDraw

BG_1 = (26, 32, 46)        # el fondo de la app, apenas levantado
BG_2 = (15, 17, 21)
AZUL = (74, 158, 255)      # BLUE de la app
AZUL_OSCURO = (32, 84, 150)
BLANCO = (238, 242, 248)

LADO = 1024
SS = 4                     # se dibuja 4x y se baja: bordes limpios sin antialias propio


def _fondo(d, lado):
    """Cuadrado redondeado con un degradado vertical sutil."""
    radio = int(lado * 0.22)
    for y in range(lado):
        t = y / lado
        color = tuple(int(a + (b - a) * t) for a, b in zip(BG_1, BG_2))
        d.line([(0, y), (lado, y)], fill=color)
    # Las esquinas se recortan después con una máscara.
    return radio


def _caja(d, lado):
    """
    Caja de frente: cuerpo, tapa y la línea de cinta al medio.

    De frente y no isométrica a propósito: la silueta rectangular se reconoce
    a 32px, la isométrica se empasta. Y ocupa casi todo el lienzo — el aire
    alrededor es lo primero que se pierde cuando el icono baja a 16px.
    """
    ancho = int(lado * 0.72)
    alto = int(lado * 0.56)
    x0 = (lado - ancho) // 2
    y0 = int(lado * 0.315)
    x1, y1 = x0 + ancho, y0 + alto
    r = int(lado * 0.045)

    tapa_alto = int(alto * 0.30)
    d.rounded_rectangle([x0, y0 - tapa_alto, x1, y0 + r * 2], radius=r, fill=AZUL)
    d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=AZUL_OSCURO)
    cinta = int(lado * 0.05)
    d.rectangle([(lado - cinta) // 2, y0 - tapa_alto, (lado + cinta) // 2, y0 + r],
                fill=AZUL_OSCURO)
    return x0, y0, x1, y1


def _cuatro(d, caja, lado):
    """
    Un 4 de tres trazos: diagonal, travesaño y asta.

    Lo que hace legible un 4 no son los trazos sino el hueco triangular que
    dejan entre ellos. Con trazos gruesos ese hueco se cierra y a 32px queda
    una mancha con forma de flecha; rellenar la silueta y vaciarla después da una
    rendija, que es igual de ilegible. Así que los trazos van finos y el hueco
    dimensionado a propósito: ocupa cerca de un tercio del ancho del número.

    Sin fuentes: el icono tiene que salir igual en cualquier máquina.
    """
    x0, y0, x1, y1 = caja
    ancho, alto = x1 - x0, y1 - y0

    tx0 = x0 + ancho * 0.15
    tx1 = x1 - ancho * 0.15
    ty0 = y0 + alto * 0.15
    ty1 = y1 - alto * 0.12
    th, tw = ty1 - ty0, tx1 - tx0

    trazo = tw * 0.20
    ax0 = tx0 + tw * 0.62           # asta vertical, a la derecha
    ax1 = ax0 + trazo
    tv0 = ty0 + th * 0.58           # travesaño
    tv1 = tv0 + trazo * 0.92

    # Diagonal: baja desde el tope del asta hasta la izquierda del travesaño.
    d.polygon([
        (ax0, ty0), (ax1, ty0),
        (tx0 + trazo, tv1), (tx0, tv1),
    ], fill=BLANCO)
    d.rectangle([tx0, tv0, ax1, tv1], fill=BLANCO)
    d.rectangle([ax0, ty0, ax1, ty1], fill=BLANCO)


def dibujar(lado=LADO):
    grande = lado * SS
    img = Image.new("RGB", (grande, grande), BG_2)
    d = ImageDraw.Draw(img)
    radio = _fondo(d, grande)
    caja = _caja(d, grande)
    _cuatro(d, caja, grande)

    # Máscara para las esquinas redondeadas.
    mascara = Image.new("L", (grande, grande), 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, grande - 1, grande - 1],
                                              radius=radio, fill=255)
    salida = Image.new("RGBA", (grande, grande), (0, 0, 0, 0))
    salida.paste(img, (0, 0), mascara)
    return salida.resize((lado, lado), Image.LANCZOS)


def main():
    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    destino = os.path.join(raiz, "assets")
    os.makedirs(destino, exist_ok=True)

    icono = dibujar()
    png = os.path.join(destino, "icon.png")
    icono.save(png)

    # Windows: un .ico multi-tamaño. Sin el de 16 y 24 la barra de tareas
    # escala el de 256 y se ve sucio.
    icono.save(os.path.join(destino, "icon.ico"),
               sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    # macOS: iconutil arma el .icns desde un .iconset. Solo existe en macOS;
    # en otro sistema se saltea y queda el .icns que ya está versionado.
    if sys.platform == "darwin":
        import shutil
        import subprocess
        iconset = os.path.join(destino, "icon.iconset")
        shutil.rmtree(iconset, ignore_errors=True)
        os.makedirs(iconset)
        for base in (16, 32, 128, 256, 512):
            icono.resize((base, base), Image.LANCZOS).save(
                os.path.join(iconset, f"icon_{base}x{base}.png"))
            icono.resize((base * 2, base * 2), Image.LANCZOS).save(
                os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset,
                        "-o", os.path.join(destino, "icon.icns")], check=True)
        shutil.rmtree(iconset)

    for nombre in sorted(os.listdir(destino)):
        ruta = os.path.join(destino, nombre)
        print(f"  {nombre:12} {os.path.getsize(ruta) // 1024:5} KB")


if __name__ == "__main__":
    main()
