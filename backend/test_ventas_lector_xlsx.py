"""
Pruebas del lector rápido de .xlsx (ventas/lector_xlsx.py).

La ruta rápida existe para no pagar openpyxl (un objeto por celda) en los
contenedores de un mes. Dos cosas tienen que seguir siendo verdad:

  1. Produce EXACTAMENTE lo mismo que `iter_rows(values_only=True)`, incluido el
     tipo de cada valor, o cede a openpyxl con NoAplicable.
  2. Se usa de verdad. Un cambio que la haga ceder siempre no rompe nada
     visible: el resultado sigue siendo correcto y solo se vuelve lento. Por eso
     varias pruebas comprueban que openpyxl NO se abre.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from ventas import lector_xlsx
from ventas.dian_emitidos import MAX_COLUMNAS, leer_contenedor

REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xlsx(hoja_xml: str, textos: list[str], *, prefijo: str = "",
          orden_rel: str = "id-type-target", nombre_hoja: str = "Rp_Doc") -> bytes:
    """
    Construye un .xlsx mínimo con el XML de hoja dado.

    `prefijo` reproduce el export de la DIAN, que escribe <x:row> en vez de <row>.
    `orden_rel` reproduce que el orden de los atributos no está fijado por el formato.
    """
    p = f"{prefijo}:" if prefijo else ""
    decl_ns = f'xmlns:{prefijo}="{NS}"' if prefijo else f'xmlns="{NS}"'
    rel_hoja = {
        "id-type-target": f'<Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>',
        "type-target-id": f'<Relationship Type="{REL}/worksheet" Target="worksheets/sheet1.xml" Id="rId1"/>',
    }[orden_rel]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
                   f'<sheet name="{nombre_hoja}" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + rel_hoja + f'<Relationship Id="rId2" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst xmlns="{NS}">'
                   + "".join(f"<si><t>{t}</t></si>" for t in textos) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<?xml version="1.0"?><{p}worksheet {decl_ns}>' + hoja_xml.replace("{p}", p)
                   + f"</{p}worksheet>")
    return buf.getvalue()


def _por_openpyxl(contenido: bytes) -> list[tuple]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    try:
        return [tuple(r[:MAX_COLUMNAS]) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def _rapido(contenido: bytes) -> tuple[str, list[tuple]]:
    return lector_xlsx.leer_filas(contenido, 10_000, MAX_COLUMNAS, lambda _f: True,
                                  lambda n: AssertionError(f"tope: {n}"))


def _igual_que_openpyxl(contenido: bytes) -> list[tuple]:
    """Comprueba valor y TIPO celda a celda; devuelve las filas para seguir afirmando."""
    esperado = _por_openpyxl(contenido)
    _, obtenido = _rapido(contenido)
    assert len(obtenido) == len(esperado)
    for i, (a, b) in enumerate(zip(esperado, obtenido), 1):
        assert a == b, f"fila {i}: {a!r} vs {b!r}"
        assert [type(x) for x in a] == [type(x) for x in b], f"fila {i}: tipos {a!r} vs {b!r}"
    return obtenido


# --- lo que la ruta rápida tiene que leer igual que openpyxl ------------------

@pytest.mark.parametrize("prefijo", ["", "x"])
def test_prefijo_de_espacio_de_nombres(prefijo):
    """El export de la DIAN escribe <x:row>; openpyxl escribe <row>. Ambos valen."""
    hoja = ('<{p}dimension ref="A1:C2"/><{p}sheetData>'
            '<{p}row r="1"><{p}c r="A1" t="s"><{p}v>0</{p}v></{p}c>'
            '<{p}c r="B1" t="s"><{p}v>1</{p}v></{p}c><{p}c r="C1" t="s"><{p}v>2</{p}v></{p}c></{p}row>'
            '<{p}row r="2"><{p}c r="A2"><{p}v>7</{p}v></{p}c>'
            '<{p}c r="B2"><{p}v>1234.56</{p}v></{p}c><{p}c r="C2" t="b"><{p}v>1</{p}v></{p}c></{p}row>'
            '</{p}sheetData>')
    filas = _igual_que_openpyxl(_xlsx(hoja, ["Uno", "Dos", "Tres"], prefijo=prefijo))
    assert filas == [("Uno", "Dos", "Tres"), (7, 1234.56, True)]


def test_referencias_numericas_de_caracter():
    """'Fecha Emisi&#243;n' tiene que leerse 'Fecha Emisión', o no se reconoce la columna."""
    hoja = ('<{p}dimension ref="A1:B1"/><{p}sheetData><{p}row r="1">'
            '<{p}c r="A1" t="s"><{p}v>0</{p}v></{p}c><{p}c r="B1" t="s"><{p}v>1</{p}v></{p}c>'
            '</{p}row></{p}sheetData>')
    filas = _igual_que_openpyxl(_xlsx(hoja, ["Fecha Emisi&#243;n", "N&#xC3;&#xBA;mero &amp; co"]))
    assert filas[0][0] == "Fecha Emisión"


def test_orden_de_atributos_de_la_relacion_de_la_hoja():
    """El formato no fija el orden: Type/Target/Id tiene que encontrar la hoja igual que Id/Type/Target."""
    hoja = ('<{p}dimension ref="A1:A1"/><{p}sheetData><{p}row r="1">'
            '<{p}c r="A1" t="s"><{p}v>0</{p}v></{p}c></{p}row></{p}sheetData>')
    nombre, filas = _rapido(_xlsx(hoja, ["Hola"], orden_rel="type-target-id", nombre_hoja="Hoja DIAN"))
    assert (nombre, filas) == ("Hoja DIAN", [("Hola",)])


def test_filas_sin_atributos_y_con_huecos():
    """<row> sin r=, y saltos de fila: openpyxl numera en orden y rellena los huecos."""
    hoja = ('<{p}dimension ref="A1:B4"/><{p}sheetData>'
            '<{p}row><{p}c r="A1" t="s"><{p}v>0</{p}v></{p}c></{p}row>'
            '<{p}row><{p}c r="A2"><{p}v>1</{p}v></{p}c></{p}row>'
            '<{p}row r="4"><{p}c r="B4"><{p}v>2</{p}v></{p}c></{p}row>'
            '</{p}sheetData>')
    filas = _igual_que_openpyxl(_xlsx(hoja, ["Cabecera"]))
    assert filas == [("Cabecera", None), (1, None), (None, None), (None, 2)]


def test_celda_en_linea_vacia_y_error():
    """inlineStr, cadena directa, celda vacía y celda de error."""
    hoja = ('<{p}dimension ref="A1:E1"/><{p}sheetData><{p}row r="1">'
            '<{p}c r="A1" t="inlineStr"><{p}is><{p}t>En l&#237;nea</{p}t></{p}is></{p}c>'
            '<{p}c r="B1" t="str"><{p}v>Resultado</{p}v></{p}c>'
            '<{p}c r="C1"/>'
            '<{p}c r="D1" t="e"><{p}v>#DIV/0!</{p}v></{p}c>'
            '<{p}c r="E1" t="s"><{p}v>0</{p}v></{p}c>'
            '</{p}row></{p}sheetData>')
    filas = _igual_que_openpyxl(_xlsx(hoja, ["Última"]))
    assert filas[0][0] == "En línea" and filas[0][2] is None


def test_lo_que_sobra_despues_de_sheetdata_no_es_una_fila():
    """<rowBreaks> va después de </sheetData> y contiene <brk>, no filas."""
    hoja = ('<{p}dimension ref="A1:A1"/><{p}sheetData><{p}row r="1">'
            '<{p}c r="A1" t="s"><{p}v>0</{p}v></{p}c></{p}row></{p}sheetData>'
            '<{p}rowBreaks count="1"><{p}brk id="1" max="16383" man="1"/></{p}rowBreaks>')
    assert _igual_que_openpyxl(_xlsx(hoja, ["Sola"])) == [("Sola",)]


def test_hoja_mas_grande_que_el_trozo_de_lectura(monkeypatch):
    """
    La hoja se lee a trozos para no tenerla entera en memoria. Con un trozo
    diminuto, cada marca de fila y cada celda cae partida entre lecturas.
    """
    monkeypatch.setattr(lector_xlsx, "_TROZO", 64)
    cuerpo = "".join(f'<{{p}}row r="{i}"><{{p}}c r="A{i}" t="s"><{{p}}v>0</{{p}}v></{{p}}c>'
                     f'<{{p}}c r="B{i}"><{{p}}v>{i}.5</{{p}}v></{{p}}c></{{p}}row>'
                     for i in range(1, 61))
    hoja = '<{p}dimension ref="A1:B60"/><{p}sheetData>' + cuerpo + "</{p}sheetData>"
    filas = _igual_que_openpyxl(_xlsx(hoja, ["Texto"]))
    assert len(filas) == 60 and filas[59] == ("Texto", 60.5)


# --- lo que la ruta rápida tiene que rechazar, para que lea openpyxl ---------

def test_cede_con_formato_de_fecha():
    """Un numFmt de fecha convierte un número en datetime: eso solo lo hace openpyxl."""
    contenido = _xlsx('<{p}dimension ref="A1:A1"/><{p}sheetData><{p}row r="1">'
                      '<{p}c r="A1" s="1"><{p}v>46022</{p}v></{p}c></{p}row></{p}sheetData>', [])
    # se le añade styles.xml con un formato de fecha aplicado al estilo 1
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(contenido)) as origen, zipfile.ZipFile(buf, "w") as destino:
        for i in origen.infolist():
            destino.writestr(i, origen.read(i.filename))
        destino.writestr("xl/styles.xml", f'<?xml version="1.0"?><styleSheet xmlns="{NS}"><cellXfs count="2">'
                         '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
    with pytest.raises(lector_xlsx.NoAplicable):
        _rapido(buf.getvalue())


def test_cede_con_una_entidad_desconocida():
    """Una entidad que no sea de XML podría cambiar el texto: mejor ceder que adivinar."""
    hoja = ('<{p}dimension ref="A1:A1"/><{p}sheetData><{p}row r="1">'
            '<{p}c r="A1" t="s"><{p}v>0</{p}v></{p}c></{p}row></{p}sheetData>')
    with pytest.raises(lector_xlsx.NoAplicable):
        _rapido(_xlsx(hoja, ["Pesos &pound;"]))


def test_cede_si_no_es_un_zip():
    with pytest.raises(lector_xlsx.NoAplicable):
        _rapido(b"esto no es un xlsx")


# --- que la ruta rápida se use de verdad ------------------------------------

def test_el_contenedor_normal_no_abre_openpyxl(ventas, monkeypatch):
    """
    Regresión del propósito del módulo: si algún cambio hace que la ruta rápida
    ceda siempre, el resultado sigue correcto y solo se vuelve lento. Esta
    prueba falla en ese caso.
    """
    import openpyxl

    def no_abrir(*_a, **_k):
        raise AssertionError("la ruta rápida tendría que haber leído el contenedor sin openpyxl")

    contenido = ventas.contenedor([ventas.fila("FE", i, 1000 * i) for i in (1, 2, 3)])
    monkeypatch.setattr(openpyxl, "load_workbook", no_abrir)
    lectura = leer_contenedor(contenido, "x.xlsx")
    assert len(lectura.incluidos) == 3


def test_si_la_ruta_rapida_falla_la_lectura_sigue_por_openpyxl(ventas, monkeypatch):
    """Un error inesperado en la ruta rápida no puede romper la lectura."""
    import ventas.dian_emitidos as modulo

    def revienta(*_a, **_k):
        raise RuntimeError("fallo inesperado")

    contenido = ventas.contenedor([ventas.fila("FE", 1, 5000)])
    monkeypatch.setattr(modulo.lector_xlsx, "leer_filas", revienta)
    lectura = leer_contenedor(contenido, "x.xlsx")
    assert len(lectura.incluidos) == 1


def test_el_tope_de_filas_lo_aplica_la_propia_ruta_rapida(ventas, monkeypatch):
    """
    El tope (MAX_FILAS más 15 filas de holgura para títulos) se aplica con el
    mismo mensaje por las dos rutas, y la rápida lo hace sin abrir openpyxl:
    así deja de leer en cuanto lo supera, en vez de cargar la hoja entera.
    """
    import openpyxl

    import ventas.dian_emitidos as modulo

    def no_abrir(*_a, **_k):
        raise AssertionError("la ruta rápida tiene que detenerse sin openpyxl")

    monkeypatch.setattr(modulo, "MAX_FILAS", 2)
    contenido = ventas.contenedor([ventas.fila("FE", i, 1000) for i in range(1, 30)])
    monkeypatch.setattr(openpyxl, "load_workbook", no_abrir)
    with pytest.raises(ValueError, match="más de 2 filas"):
        leer_contenedor(contenido, "x.xlsx")
