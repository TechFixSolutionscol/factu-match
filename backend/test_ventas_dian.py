"""Lectura del contenedor DIAN de documentos emitidos con los nombres exactos de columna."""

from decimal import Decimal

import pytest

from ventas.dian_emitidos import COLUMNAS_REQUERIDAS, leer_contenedor


def _niveles(lectura):
    return {v["codigo"]: v["nivel"] for v in lectura.validaciones}


def test_tipos_incluidos_excluidos_y_signos(ventas):
    filas = [
        ventas.fila("FE", 1, 119000, iva=19000),
        ventas.fila("POS", 10, 8000, tipo="Documento equivalente POS", inc=592.59),
        ventas.fila("NC", 1, 5000, tipo="Nota de crédito electrónica"),
        ventas.fila("ND", 1, 1000, tipo="Nota de débito electrónica"),
        ventas.fila("", "", 0, tipo="Application response"),
        ventas.fila("NE", 3, 2500000, tipo="Nomina Individual"),
        ventas.fila("DSFR", 4, 300000, tipo="Documento soporte con no obligados"),
        ventas.fila("XX", 1, 777, tipo="Factura de exportación especial"),       # tipo sin regla
        ventas.fila("FE", 2, 5000, estado="Rechazado"),                          # no aprobado
        ventas.fila("FE", 3, 6000, grupo="Recibido"),                            # no es emitido
    ]
    lectura = leer_contenedor(ventas.contenedor(filas), "prueba.xlsx")
    por_ref = {d.ref: d for d in lectura.documentos}

    incluidos = {d.ref for d in lectura.incluidos}
    assert incluidos == {"dian:FE-1", "dian:POS-10", "dian:NC-1", "dian:ND-1"}
    assert por_ref["dian:NC-1"].signo == -1 and por_ref["dian:NC-1"].categoria == "nota_credito"
    assert por_ref["dian:ND-1"].signo == 1 and por_ref["dian:ND-1"].categoria == "nota_debito"
    assert "compra" in por_ref["dian:DSFR-4"].motivo                         # decisión: documento soporte se excluye
    assert por_ref["dian:XX-1"].categoria == "desconocido" and not por_ref["dian:XX-1"].incluido
    assert "aprobados" in por_ref["dian:FE-2"].motivo
    assert "emitidos" in por_ref["dian:FE-3"].motivo
    assert _niveles(lectura)["tipos"] == "advertencia"
    assert _niveles(lectura)["estado"] == "advertencia"
    neto = sum((d.total_firmado for d in lectura.incluidos), Decimal("0"))
    assert neto == Decimal("119000") + Decimal("8000") - Decimal("5000") + Decimal("1000")


def test_encabezado_debajo_de_filas_de_titulo_y_hoja_con_cufe(ventas):
    contenido = ventas.contenedor([ventas.fila("FE", 7, 1000)], filas_titulo=3)
    lectura = leer_contenedor(contenido, "prueba.xlsx")
    assert lectura.fila_encabezado == 4
    assert lectura.hoja.startswith("Rp_Doc_")
    assert lectura.incluidos[0].numero.legible == "FE-7"


def test_falta_columna_requerida(ventas):
    encabezados = [c for c in ventas.ENCABEZADOS if c != "Total"]
    with pytest.raises(ValueError, match="Total"):
        leer_contenedor(ventas.contenedor([ventas.fila("FE", 1, 10)], encabezados=encabezados), "x.xlsx")
    assert "Total" in COLUMNAS_REQUERIDAS


def test_columnas_opcionales_faltantes_no_bloquean(ventas):
    encabezados = [c for c in ventas.ENCABEZADOS if c not in ("ICUI", "Rete ICA", "Divisa")]
    lectura = leer_contenedor(ventas.contenedor([ventas.fila("FE", 1, 10)], encabezados=encabezados), "x.xlsx")
    assert set(lectura.columnas_opcionales_faltantes) == {"ICUI", "Rete ICA", "Divisa"}
    assert _niveles(lectura)["columnas_opcionales"] == "advertencia"


def test_archivo_que_no_es_excel():
    with pytest.raises(ValueError, match="formato Excel"):
        leer_contenedor(b"%PDF-1.7 esto no es un excel", "x.xlsx")


def test_varios_nit_emisores_es_error(ventas):
    filas = [ventas.fila("FE", 1, 10), ventas.fila("FE", 2, 10, nit_emisor="800000002")]
    lectura = leer_contenedor(ventas.contenedor(filas), "x.xlsx")
    assert _niveles(lectura)["nit_emisor"] == "error"


def test_corte_del_contenedor_temprano_genera_advertencia(ventas):
    filas = [ventas.fila("FE", 1, 10, fecha="14-10-2026", recepcion="2026-10-14T23:30:00"),
             ventas.fila("FE", 2, 10, fecha="15-10-2026", recepcion="2026-10-15T07:47:58")]
    lectura = leer_contenedor(ventas.contenedor(filas), "x.xlsx")
    corte = next(v for v in lectura.validaciones if v["codigo"] == "corte")
    assert corte["nivel"] == "advertencia" and "07:47" in corte["mensaje"]


def test_base_derivada_y_otros_impuestos(ventas):
    filas = [ventas.fila("FE", 1, 120000, iva=15000, inc=4000, IBUA=1000, **{"Rete Renta": 2500})]
    d = leer_contenedor(ventas.contenedor(filas), "x.xlsx").incluidos[0]
    assert d.impuestos == Decimal("20000")
    assert d.base_derivada == Decimal("100000")
    assert d.otros_impuestos == {"IBUA": Decimal("1000.00")}          # solo los distintos de cero
    assert d.retenciones == {"Rete Renta": Decimal("2500.00")}         # informativas: no alteran el total
    assert d.total == Decimal("120000")


def test_folios_repetidos_y_cufe_repetido_se_advierten(ventas):
    c = ventas.cufe("repetido")
    filas = [ventas.fila("FE", 1, 10, cufe_valor=c), ventas.fila("FE", 1, 10, cufe_valor=c)]
    niveles = _niveles(leer_contenedor(ventas.contenedor(filas), "x.xlsx"))
    assert niveles["cufe"] == "advertencia" and niveles["numero"] == "advertencia"


def test_maximo_de_filas(ventas, monkeypatch):
    import ventas.dian_emitidos as modulo
    monkeypatch.setattr(modulo, "MAX_FILAS", 2)
    filas = [ventas.fila("FE", i, 10) for i in range(1, 30)]
    with pytest.raises(ValueError, match="más de 2 filas"):
        leer_contenedor(ventas.contenedor(filas), "x.xlsx")


def _xlsx_comprimible(encabezados: list[str], valores: list[str], filas: int) -> bytes:
    """xlsx armado a mano, sin <dimension> y con filas idénticas: comprime ~1000:1."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    textos = list(dict.fromkeys(encabezados + valores))
    pos = {t: i for i, t in enumerate(textos)}
    fila = lambda celdas: "<row>" + "".join(f'<c t="s"><v>{pos[c]}</v></c>' for c in celdas) + "</row>"
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
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
                   f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook {ns} xmlns:r="{rel}"><sheets>'
                   '<sheet name="Rp_Doc" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
                   f'<Relationship Id="rId2" Type="{rel}/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst {ns}>'
                   + "".join(f"<si><t>{escape(t)}</t></si>" for t in textos) + "</sst>")
        with z.open("xl/worksheets/sheet1.xml", "w") as hoja:
            hoja.write(f'<?xml version="1.0"?><worksheet {ns}><sheetData>'.encode() + fila(encabezados).encode())
            bloque = (fila(valores) * 1000).encode()
            for _ in range(filas // 1000):
                hoja.write(bloque)
            hoja.write(b"</sheetData></worksheet>")
    return buf.getvalue()


def test_xlsx_muy_comprimido_se_rechaza_sin_abrirlo(ventas, monkeypatch):
    """Millones de filas en pocos MB: se rechaza contando filas antes de que openpyxl recorra la hoja."""
    import openpyxl

    import ventas.dian_emitidos as modulo

    fila = ventas.fila("POS", 1, 8000)
    valores = [str(fila.get(c, 0)) for c in ventas.ENCABEZADOS]
    contenido = _xlsx_comprimible(ventas.ENCABEZADOS, valores, 200_000)
    assert len(contenido) < 1024 * 1024                       # 200.000 filas en menos de 1 MB

    def no_abrir(*_a, **_k):
        raise AssertionError("openpyxl no debe abrir un archivo que supera el tope")

    monkeypatch.setattr(modulo, "MAX_FILAS", 1_000)
    monkeypatch.setattr(openpyxl, "load_workbook", no_abrir)
    with pytest.raises(ValueError, match="más de 1000 filas"):
        leer_contenedor(contenido, "x.xlsx")


def test_xlsx_comprimido_dentro_del_tope_se_lee(ventas):
    """La revisión previa no rechaza un archivo sin <dimension> que respeta los topes."""
    fila = ventas.fila("POS", 1, 8000, tipo="Documento equivalente POS")
    valores = [str(fila.get(c, 0)) for c in ventas.ENCABEZADOS]
    lectura = leer_contenedor(_xlsx_comprimible(ventas.ENCABEZADOS, valores, 2_000), "x.xlsx")
    assert len(lectura.documentos) == 2_000


def test_xlsx_que_descomprimido_supera_el_tope(ventas, monkeypatch):
    import ventas.dian_emitidos as modulo

    contenido = ventas.contenedor([ventas.fila("FE", 1, 1000)])
    monkeypatch.setattr(modulo, "MAX_XLSX_DESCOMPRIMIDO", 1024)
    with pytest.raises(ValueError, match="al descomprimirlo"):
        leer_contenedor(contenido, "x.xlsx")


def test_importes_en_texto_ambiguos_se_advierten(ventas):
    filas = [ventas.fila("FE", 1, "592.593", inc="15.000")]
    lectura = leer_contenedor(ventas.contenedor(filas), "x.xlsx")
    d = lectura.incluidos[0]
    assert d.total == Decimal("592593") and d.inc == Decimal("15000")
    aviso = next(v for v in lectura.validaciones if v["codigo"] == "importes_ambiguos")
    assert aviso["nivel"] == "advertencia" and "592.593" in aviso["detalle"]
