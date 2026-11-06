"""
Lector rápido de .xlsx para el contenedor DIAN.

openpyxl construye un objeto por celda y pasa por ElementTree: con el contenedor
real de un mes (7.235 filas × 32 columnas = 231.552 celdas) eso cuesta ~1,1 s.
Este módulo recorre el XML de la hoja con `find`/`split` sobre bytes y resuelve
las cadenas compartidas por índice, que es lo único que necesita el contenedor.
La hoja se lee del zip a trozos, nunca entera: el XML descomprimido de un mes
pesa 10 MB y en Render el límite de memoria es lo que primero se agota.

Es una RUTA RÁPIDA, no un reemplazo: en cuanto encuentra algo que no cubre
(formatos de fecha aplicados a números, una entidad desconocida, un XML que no
encaja) lanza NoAplicable y la lectura sigue por openpyxl, que es el camino de
referencia. Los valores que produce son los mismos que devuelve
`iter_rows(values_only=True)`: str, int, float, bool o None.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Callable, Optional

# Formatos numéricos de fecha/hora integrados en Excel. Si alguno está aplicado a
# una celda, un número puede ser en realidad una fecha y hay que ceder a openpyxl.
_NUMFMT_FECHA = frozenset(range(14, 23)) | frozenset(range(45, 48)) | {27, 30, 36, 50, 57}
_TOKENS_FECHA = re.compile(r"(?<!\\)[dmyhs]", re.IGNORECASE)

_RE_TIPO = re.compile(rb't="(\w+)"')
_RE_ESTILO = re.compile(rb's="(\d+)"')
_RE_V = re.compile(rb'<(?:\w+:)?v[^>]*>(.*?)</(?:\w+:)?v>', re.S)
_RE_T = re.compile(rb'<(?:\w+:)?t[^>]*>(.*?)</(?:\w+:)?t>', re.S)
_RE_SI = re.compile(rb'<(?:\w+:)?si\b.*?</(?:\w+:)?si>|<(?:\w+:)?si\s*/>', re.S)
_RE_SHEET = re.compile(rb'<(?:\w+:)?sheet\s([^>]*)/?>')
_RE_REL = re.compile(rb'<(?:\w+:)?Relationship\s([^>]*)/?>')
# El orden de los atributos no está fijado por el formato: el export de la DIAN
# escribe Type, Target, Id, y openpyxl escribe Id, Type, Target.
_RE_ATRIBUTO = re.compile(rb'([\w:]+)\s*=\s*"([^"]*)"')
_RE_DIM = re.compile(rb'<(?:\w+:)?dimension\s[^>]*?ref="[A-Z]+\d+(?::([A-Z]+)\d+)?"')
_RE_CELLXFS = re.compile(rb'<(?:\w+:)?cellXfs\b[^>]*>(.*?)</(?:\w+:)?cellXfs>', re.S)
_RE_XF_NUMFMT = re.compile(rb'<(?:\w+:)?xf\s[^>]*?numFmtId="(\d+)"')
_RE_NUMFMT = re.compile(rb'<(?:\w+:)?numFmt\s[^>]*?numFmtId="(\d+)"[^>]*?formatCode="([^"]*)"')


class NoAplicable(Exception):
    """El archivo usa algo que la ruta rápida no cubre."""


_RE_PREFIJO = re.compile(rb'<(\w+:)?(?:row|sheetData)[\s>]')


def _prefijo(datos: bytes) -> bytes:
    """Prefijo de espacio de nombres de la hoja: b'x:' en el export de la DIAN, b'' en openpyxl."""
    m = _RE_PREFIJO.search(datos)
    if m is None:
        raise NoAplicable("la hoja no declara filas")
    return m.group(1) or b""


_INDICE_COLUMNA: dict[bytes, int] = {}


def _columna_a_indice(letras: bytes) -> int:
    """A→0, B→1, …, AF→31. Memoizado: hay pocas columnas y millones de celdas."""
    n = _INDICE_COLUMNA.get(letras)
    if n is None:
        n = 0
        for c in letras:
            n = n * 26 + (c - 64)
        n -= 1
        _INDICE_COLUMNA[letras] = n
    return n


_ENTIDADES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_RE_ENTIDAD = re.compile(r"&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|(amp|lt|gt|quot|apos));")
_RE_ENTIDAD_RARA = re.compile(r"&[^\s;]{1,32};")


def _sustituir_entidad(m: "re.Match[str]") -> str:
    if m.group(1):
        return chr(int(m.group(1)))
    if m.group(2):
        return chr(int(m.group(2), 16))
    return _ENTIDADES[m.group(3)]


def _texto(bruto: bytes) -> str:
    """Igual que el parser XML: resuelve las 5 entidades y las referencias numéricas (&#243;)."""
    s = bruto.decode("utf-8")
    if "&" not in s:
        return s
    s = _RE_ENTIDAD.sub(_sustituir_entidad, s)
    if "&" in s and _RE_ENTIDAD_RARA.search(s):
        raise NoAplicable("entidad XML no estándar")
    return s


def _numero(texto: str):
    """Misma conversión que openpyxl: entero salvo que tenga punto o exponente."""
    return float(texto) if ("." in texto or "E" in texto or "e" in texto) else int(texto)


def _estilos_de_fecha(datos: bytes) -> set[int]:
    """Índices de cellXfs cuyo formato numérico es una fecha u hora."""
    personalizados = {int(i): c for i, c in _RE_NUMFMT.findall(datos)}
    bloque = _RE_CELLXFS.search(datos)
    if not bloque:
        return set()
    indices = set()
    for pos, numfmt in enumerate(_RE_XF_NUMFMT.findall(bloque.group(1))):
        nid = int(numfmt)
        if nid in _NUMFMT_FECHA:
            indices.add(pos)
        elif nid >= 164:
            codigo = personalizados.get(nid, b"").decode("utf-8", "replace")
            sin_literales = re.sub(r'"[^"]*"|\[[^\]]*\]', "", codigo)
            if _TOKENS_FECHA.search(sin_literales):
                indices.add(pos)
    return indices


def _cadenas_compartidas(datos: bytes) -> list[str]:
    """Una entrada por <si>, uniendo sus <t> (texto con formato enriquecido)."""
    return ["".join(_texto(t) for t in _RE_T.findall(si)) for si in _RE_SI.findall(datos)]


def _valor_raro(attrs: bytes, simple: Optional[bytes], interior: Optional[bytes],
                cadenas: list[str], estilos_fecha: set[int]):
    """Tipos de celda poco frecuentes: inlineStr, fórmula, booleano, error."""
    tipo = _RE_TIPO.search(attrs)
    tipo = tipo.group(1) if tipo else b"n"
    if tipo == b"inlineStr":
        return "".join(_texto(t) for t in _RE_T.findall(interior or b"")) or None
    crudo = simple
    if crudo is None:
        v = _RE_V.search(interior or b"")
        if v is None:
            return None
        crudo = v.group(1)
    if tipo == b"s":
        try:
            return cadenas[int(crudo)]
        except (IndexError, ValueError) as exc:
            raise NoAplicable("índice de cadena compartida fuera de rango") from exc
    if tipo in (b"str", b"e"):
        return _texto(crudo)
    if tipo == b"b":
        return crudo == b"1"
    if tipo == b"n":
        if not crudo:
            return None
        return _numero_bytes(crudo, attrs, estilos_fecha)
    raise NoAplicable(f"tipo de celda no contemplado: {tipo!r}")


def _numero_bytes(crudo: bytes, attrs: bytes, estilos_fecha: set[int]):
    if estilos_fecha:
        est = _RE_ESTILO.search(attrs)
        if est and int(est.group(1)) in estilos_fecha:
            raise NoAplicable("la hoja aplica formatos de fecha a celdas numéricas")
    try:
        return float(crudo) if (b"." in crudo or b"E" in crudo or b"e" in crudo) else int(crudo)
    except ValueError:
        return crudo.decode()


class _Marcas:
    """Etiquetas concretas de este archivo (con o sin prefijo de espacio de nombres)."""

    __slots__ = ("fila", "celda", "celda_corta", "cierre_celda", "v", "largo_v", "datos", "cierre_datos")

    def __init__(self, prefijo: bytes):
        self.fila = b"<" + prefijo + b"row"                 # sin espacio: <row> también es válido
        self.celda = b"<" + prefijo + b"c "
        self.celda_corta = b"<" + prefijo + b"c"
        self.cierre_celda = b"</" + prefijo + b"c>"
        self.v = b"<" + prefijo + b"v>"
        self.largo_v = len(self.v)
        self.datos = b"<" + prefijo + b"sheetData"
        self.cierre_datos = b"</" + prefijo + b"sheetData>"


def _celdas(cuerpo: bytes, cadenas: list[str], estilos_fecha: set[int], marcas: _Marcas) -> list[tuple[int, object]]:
    """
    Pares (índice de columna, valor) de una fila.

    Recorre con `bytes.split`/`find` en vez de expresiones regulares: evita crear
    un objeto `Match` por celda, que con 230.000 celdas es el coste dominante.
    Resuelve en línea los dos tipos que copan el contenedor —cadena compartida
    (`t="s"`) y número sin tipo— y deja el resto a `_valor_raro`.
    """
    trozos = cuerpo.split(marcas.celda)
    # Invariante: toda etiqueta de celda tiene que haber entrado en el split. Si
    # no cuadra, el archivo trae alguna forma que esta ruta no sabe leer (por
    # ejemplo <c> sin atributos) y hay que ceder, nunca perder celdas en silencio.
    if cuerpo.count(marcas.celda_corta) != len(trozos) - 1:
        raise NoAplicable("hay celdas sin atributos")
    if len(trozos) == 1:
        return []
    salida: list[tuple[int, object]] = []
    agregar = salida.append
    indice = _INDICE_COLUMNA
    marca_v, largo_v = marcas.v, marcas.largo_v
    for trozo in trozos[1:]:
        fin = trozo.find(b">")
        if fin < 1 or not trozo.startswith(b'r="'):
            raise NoAplicable("celda sin atributo de posición al inicio")
        autocierre = trozo[fin - 1] == 47                   # '/'
        attrs = trozo[:fin - 1] if autocierre else trozo[:fin]
        k, n = 3, len(attrs)
        while k < n and attrs[k] >= 65:                     # letras de la columna
            k += 1
        letras = attrs[3:k]
        col = indice.get(letras)
        if col is None:
            col = _columna_a_indice(letras)
        if autocierre:
            agregar((col, None))
        elif trozo.startswith(marca_v, fin + 1):
            ini = fin + 1 + largo_v
            crudo = trozo[ini:trozo.find(b"<", ini)]
            if b't="s"' in attrs:
                try:
                    agregar((col, cadenas[int(crudo)]))
                except (IndexError, ValueError) as exc:
                    raise NoAplicable("índice de cadena compartida fuera de rango") from exc
            elif b"t=" not in attrs:
                agregar((col, _numero_bytes(crudo, attrs, estilos_fecha) if crudo else None))
            else:
                agregar((col, _valor_raro(attrs, crudo, None, cadenas, estilos_fecha)))
        else:
            cierre = trozo.find(marcas.cierre_celda, fin)
            interior = trozo[fin + 1:cierre if cierre >= 0 else len(trozo)]
            agregar((col, _valor_raro(attrs, None, interior, cadenas, estilos_fecha)))
    return salida


def _elegir_hoja(z: zipfile.ZipFile, es_la_buena: Callable[[list[tuple]], bool]) -> tuple[str, str]:
    """Primera hoja cuyas 15 primeras filas convenzan a `es_la_buena`; si ninguna, la primera."""
    try:
        libro = z.read("xl/workbook.xml")
        relaciones = z.read("xl/_rels/workbook.xml.rels")
    except KeyError as exc:
        raise NoAplicable("falta workbook.xml o sus relaciones") from exc
    rels = {}
    for cuerpo in _RE_REL.findall(relaciones):
        attrs = dict(_RE_ATRIBUTO.findall(cuerpo))
        if b"Id" in attrs and b"Target" in attrs:
            rels[attrs[b"Id"]] = attrs[b"Target"]
    rutas: list[tuple[str, str]] = []
    for cuerpo in _RE_SHEET.findall(libro):
        attrs = dict(_RE_ATRIBUTO.findall(cuerpo))
        destino = rels.get(attrs.get(b"r:id", b""))
        if destino is None or b"name" not in attrs:
            continue
        ruta = destino.decode().lstrip("/")
        if not ruta.startswith("xl/"):
            ruta = "xl/" + ruta
        rutas.append((_texto(attrs[b"name"]), ruta))
    if not rutas:
        raise NoAplicable("el libro no declara hojas")
    cadenas = _cadenas_compartidas(z.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in z.namelist() else []
    for nombre, ruta in rutas:
        if ruta not in z.namelist():
            continue
        with z.open(ruta) as f:
            cabeza = f.read(1 << 18)                       # 256 KB: suficiente para 15 filas
        marcas = _Marcas(_prefijo(cabeza))
        filas = []
        for trozo in cabeza.split(marcas.fila)[1:16]:
            corte = trozo.find(b">")
            filas.append(tuple(v for _, v in _celdas(trozo[corte + 1:] if corte >= 0 else trozo,
                                                     cadenas, set(), marcas)))
        if es_la_buena(filas):
            return nombre, ruta
    return rutas[0]


_TROZO = 1 << 18                                            # 256 KB por lectura del zip
_CABECERA_MAX = 1 << 21                                     # 2 MB sin ver <sheetData>: no aplica


def _abrir_hoja(f, max_columnas: int) -> tuple["_Marcas", int, bytes]:
    """
    Avanza hasta pasar la etiqueta <sheetData>. Devuelve las marcas con el prefijo
    de espacio de nombres del archivo, el ancho declarado en <dimension> y lo que
    ya se había leído de más.
    """
    buf = b""
    while True:
        pedazo = f.read(_TROZO)
        if pedazo:
            buf += pedazo
        m = _RE_PREFIJO.search(buf)
        if m is not None:
            marcas = _Marcas(m.group(1) or b"")
            ini = buf.find(marcas.datos)
            corte = buf.find(b">", ini) if ini >= 0 else -1
            if corte >= 0:
                ancho = 0
                dim = _RE_DIM.search(buf, 0, ini)
                if dim and dim.group(1):
                    ancho = min(_columna_a_indice(dim.group(1)) + 1, max_columnas)
                return marcas, ancho, buf[corte + 1:]
        if not pedazo:
            raise NoAplicable("la hoja no declara sheetData")
        if len(buf) > _CABECERA_MAX:
            raise NoAplicable("la cabecera de la hoja es demasiado grande")


def _filas_crudas(f, marcas: "_Marcas", buf: bytes):
    """
    Genera (atributos, cuerpo) por fila leyendo el zip a trozos: en memoria solo
    está el trozo en curso, nunca la hoja completa. Se detiene en </sheetData>,
    porque después vienen elementos como <rowBreaks> que no son filas.
    """
    marca, cierre = marcas.fila, marcas.cierre_datos
    guarda = max(len(marca), len(cierre))                   # por si una marca quedó partida entre trozos
    agotado = False
    while True:
        fin = buf.find(cierre)
        limite = fin if fin >= 0 else len(buf)
        primera = buf.find(marca, 0, limite)
        consumido = 0
        ini = primera
        while ini >= 0:
            corte = buf.find(b">", ini)
            if corte < 0 or corte >= limite:
                break                                       # etiqueta partida: hace falta más
            siguiente = buf.find(marca, corte, limite)
            if siguiente < 0 and fin < 0 and not agotado:
                break                                       # el cuerpo puede seguir en el próximo trozo
            yield buf[ini + len(marca):corte], buf[corte + 1:siguiente if siguiente >= 0 else limite]
            consumido = siguiente if siguiente >= 0 else limite
            ini = siguiente
        if fin >= 0 or agotado:
            return
        pedazo = f.read(_TROZO)
        if consumido:
            resto = consumido
        elif primera >= 0:
            resto = primera                                 # fila incompleta: se conserva entera
        else:
            resto = max(0, limite - guarda + 1)             # nada que guardar salvo una marca partida
        buf = buf[resto:] + pedazo
        agotado = not pedazo


def leer_filas(contenido: bytes, tope_filas: int, max_columnas: int,
               es_la_buena: Callable[[list[tuple]], bool],
               demasiadas_filas: Callable[[int], Exception]) -> tuple[str, list[tuple]]:
    """
    Devuelve (nombre de la hoja, filas) leyendo el XML directamente.

    Lanza NoAplicable si el archivo no encaja en esta ruta; el llamador debe
    reintentar con openpyxl. `demasiadas_filas` se lanza tal cual si se supera
    el tope, igual que en la ruta de openpyxl.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(contenido))
    except zipfile.BadZipFile as exc:
        raise NoAplicable("no es un ZIP válido") from exc
    with z:
        nombre, ruta = _elegir_hoja(z, es_la_buena)
        nombres = z.namelist()
        estilos_fecha = _estilos_de_fecha(z.read("xl/styles.xml")) if "xl/styles.xml" in nombres else set()
        cadenas = _cadenas_compartidas(z.read("xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in nombres else []

        with z.open(ruta) as f:
            marcas, ancho, resto = _abrir_hoja(f, max_columnas)
            filas = _recorrer(_filas_crudas(f, marcas, resto), tope_filas, max_columnas,
                              ancho, cadenas, estilos_fecha, marcas, demasiadas_filas)
    return nombre, filas


def _recorrer(crudas, tope_filas: int, max_columnas: int, ancho: int, cadenas: list[str],
              estilos_fecha: set[int], marcas: "_Marcas",
              demasiadas_filas: Callable[[int], Exception]) -> list[tuple]:
    """Convierte las filas crudas en tuplas de valores, como iter_rows(values_only=True)."""
    filas: list[tuple] = []
    siguiente = 1
    fila_vacia = (None,) * ancho
    for attrs, trozo in crudas:
        if attrs[:1] not in (b"", b" ", b"\t", b"\n", b"\r", b"/"):
            raise NoAplicable("etiqueta inesperada al partir por filas")
        pos = attrs.find(b'r="')                            # el número de fila es opcional
        numero = int(attrs[pos + 3:attrs.find(b'"', pos + 3)]) if pos >= 0 else siguiente
        if numero > siguiente:                              # huecos: openpyxl los rellena vacíos
            vacias = min(numero - siguiente, tope_filas + 1 - len(filas))
            filas.extend([fila_vacia] * vacias)
            if len(filas) > tope_filas:
                raise demasiadas_filas(len(filas))
        siguiente = numero + 1
        pares = _celdas(trozo, cadenas, estilos_fecha, marcas)
        if not pares:
            filas.append(fila_vacia)
        else:
            if pares[-1][0] < max_columnas and len(pares) == pares[-1][0] + 1:
                # Caso normal: columnas completas y en orden, sin huecos.
                valores = [v for _, v in pares]
                if len(valores) < ancho:
                    valores.extend([None] * (ancho - len(valores)))
                filas.append(tuple(valores))
            else:
                celdas = {c: v for c, v in pares if c < max_columnas}
                n = max(ancho, (max(celdas) + 1) if celdas else 0)
                filas.append(tuple(celdas.get(i) for i in range(n)))
        if len(filas) > tope_filas:
            raise demasiadas_filas(len(filas))

    if ancho:                                               # mismo ancho en todas, como openpyxl
        for i, f in enumerate(filas):                       # en el sitio: no se duplica la lista
            if len(f) != ancho:
                filas[i] = f + (None,) * (ancho - len(f)) if len(f) < ancho else f[:ancho]
    return filas
