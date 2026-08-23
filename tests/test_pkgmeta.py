"""Tests del parseo del formato PKG de PS4."""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pkgmeta


def _sfo(pairs):
    """Arma un param.sfo mínimo con pares clave/valor de texto."""
    keys, data, entries = b"", b"", b""
    for k, v in pairs:
        vb = v.encode() + b"\x00"
        entries += struct.pack("<HHIII", len(keys), 0x0204, len(vb), len(vb), len(data))
        keys += k.encode() + b"\x00"
        data += vb
    key_table = 0x14 + len(entries)
    data_table = key_table + len(keys)
    head = b"\x00PSF" + struct.pack("<I", 0x0101) + struct.pack(
        "<III", key_table, data_table, len(pairs))
    return head + entries + keys + data


def _pkg(tmp_path, sfo_blob, icon=b"\x89PNG_fake", name="t.pkg"):
    """PKG mínimo: header, tabla de entradas y los blobs."""
    table_off, entry_count = 0x100, 2
    sfo_off = table_off + entry_count * 32
    icon_off = sfo_off + len(sfo_blob)

    head = bytearray(b"\x00" * table_off)
    head[0:4] = b"\x7fCNT"
    head[0x10:0x14] = struct.pack(">I", entry_count)
    head[0x18:0x1C] = struct.pack(">I", table_off)

    table = struct.pack(">IIIIII", 0x1000, 0, 0, 0, sfo_off, len(sfo_blob)) + b"\x00" * 8
    table += struct.pack(">IIIIII", 0x1200, 0, 0, 0, icon_off, len(icon)) + b"\x00" * 8

    p = tmp_path / name
    p.write_bytes(bytes(head) + table + sfo_blob + icon)
    return str(p)


def test_lee_titulo_categoria_y_id(tmp_path):
    path = _pkg(tmp_path, _sfo([
        ("CATEGORY", "gd"), ("TITLE", "HITMAN™"), ("TITLE_ID", "CUSA02369")]))

    info = pkgmeta.read_pkg(path)

    assert info.title == "HITMAN™"
    assert info.title_id == "CUSA02369"
    assert info.category == "gd"


def test_ubica_el_icono_sin_leerlo(tmp_path):
    path = _pkg(tmp_path, _sfo([("TITLE", "X")]), icon=b"\x89PNG" + b"z" * 40)

    info = pkgmeta.read_pkg(path)

    assert info.icon_size == 44
    assert info.icon_offset > 0


def test_un_archivo_que_no_es_pkg_devuelve_None(tmp_path):
    p = tmp_path / "no.pkg"
    p.write_bytes(b"esto no es un paquete")

    assert pkgmeta.read_pkg(str(p)) is None


def test_pkg_sin_sfo_no_explota(tmp_path):
    head = bytearray(b"\x00" * 0x100)
    head[0:4] = b"\x7fCNT"
    head[0x10:0x14] = struct.pack(">I", 0)
    head[0x18:0x1C] = struct.pack(">I", 0x100)
    p = tmp_path / "vacio.pkg"
    p.write_bytes(bytes(head))

    info = pkgmeta.read_pkg(str(p))

    assert info is not None
    assert info.title == ""
    assert info.icon_size == 0


def test_extrae_el_icono_al_cache(tmp_path):
    path = _pkg(tmp_path, _sfo([("TITLE", "X")]), icon=b"\x89PNG" + b"q" * 30)
    info = pkgmeta.read_pkg(path)

    dest = pkgmeta.icon_path(path, info, str(tmp_path / "cache"))

    assert dest and os.path.exists(dest)
    assert open(dest, "rb").read() == b"\x89PNG" + b"q" * 30


def test_no_reextrae_si_ya_esta_en_cache(tmp_path):
    path = _pkg(tmp_path, _sfo([("TITLE", "X")]))
    info = pkgmeta.read_pkg(path)
    cache = str(tmp_path / "cache")

    primero = pkgmeta.icon_path(path, info, cache)
    os.utime(primero, (0, 0))
    mtime = os.path.getmtime(primero)
    segundo = pkgmeta.icon_path(path, info, cache)

    assert segundo == primero
    assert os.path.getmtime(segundo) == mtime, "lo volvió a escribir"


def test_sin_icono_devuelve_None(tmp_path):
    info = pkgmeta.PkgInfo(title="X")      # icon_size = 0

    assert pkgmeta.icon_path("/x/y.pkg", info, str(tmp_path)) is None
