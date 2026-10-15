# FactuMatch Ventas — DIAN emitidos vs Odoo

Concilia los documentos electrónicos **emitidos** que reporta la DIAN (contenedor exportado del portal) contra las ventas de Odoo (`account.move` y `pos.order`), en **solo lectura**. Calcula la conciliación individual y global con reglas determinísticas, descompone la diferencia global de forma exacta y, de forma opcional, deja que **Claude** investigue las diferencias con herramientas controladas.

> La conciliación y el reporte nunca dependen de la IA. Si Claude no está configurado o falla, la interfaz muestra «Análisis IA no disponible» y todo lo demás sigue funcionando.

Resultados de pruebas y QA: [`QA_VENTAS.md`](QA_VENTAS.md).

---

## 1. Arquitectura

```
frontend/ (Vercel)                          backend/ (Render, FastAPI)
 index.html  · vista «Ventas DIAN vs Odoo»   main.py ── app.include_router(ventas_router)   ← única línea nueva de lógica
 js/ventas_engine.js                         ventas/
 css/ventas.css                               ├─ router.py         /api/ventas/…
 js/app.js   · permiso de módulo «ventas»     ├─ dian_emitidos.py  lector del contenedor (nombres exactos de columna)
                                              ├─ odoo_ventas.py    OdooSoloLectura + extracción (XML-RPC con tiempo máximo)
                                              ├─ canonico.py       conjunto canónico account.move + pos.order
                                              ├─ motor.py          conciliación individual y global
                                              ├─ secuencias.py     saltos de numeración por prefijo
                                              ├─ reporte.py        Excel de 10 hojas
                                              ├─ almacen.py        resultados en memoria (TTL)
                                              ├─ normalizacion.py  Decimal, NIT, CUFE, números, fechas
                                              ├─ config.py         variables de entorno
                                              └─ agente/           Claude: herramientas, esquema, prompts, proveedor
qa/ventas/                                   datos sintéticos, servidor y guion de QA (no se despliegan)
```

Flujo: contenedor DIAN → validación → extracción Odoo (solo lectura) → conjunto canónico → conciliación → descomposición → (opcional) investigación de Claude → reporte.

**Compras no se modificó.** `comparador.py`, `odoo_match.py` y sus endpoints quedan igual; ventas solo reutiliza `OdooConnector`/`CredentialManager` (credenciales cifradas) y `_detect_excel_format` (reconocer el Excel).

---

## 2. Reglas de negocio

### Contenedor DIAN
Columnas con su nombre exacto: `Tipo de documento, CUFE/CUDE, Folio, Prefijo, Divisa, Forma de Pago, Medio de Pago, Fecha Emisión, Fecha Recepción, NIT Emisor, Nombre Emisor, NIT Receptor, Nombre Receptor, IVA, ICA, IC, INC, Timbre, INC Bolsas, IN Carbono, IN Combustibles, IC Datos, ICL, INPP, IBUA, ICUI, Rete IVA, Rete Renta, Rete ICA, Total, Estado, Grupo`. El encabezado se busca en las primeras 15 filas; la comparación ignora mayúsculas, tildes y espacios dobles.

| Tipo de documento | Tratamiento |
|---|---|
| Factura electrónica · Documento equivalente POS | Suma al bruto |
| Nota de débito electrónica | Suma al neto (separada del bruto) |
| Nota de crédito electrónica | Resta del neto (separada del bruto) |
| Documento soporte con no obligados · Nota de ajuste del documento soporte | **Excluido: es compra** (decisión del 15-10-2026) |
| Nómina individual (y de ajuste) · Application response | Excluido: no es venta |
| Tipo desconocido | No suma; queda como advertencia hasta definir su regla |

- Solo `Grupo = Emitido` y `Estado` aprobado (`Aprobado`, `Aprobado con notificación`).
- Un solo `NIT Emisor` por contenedor; si hay varios, la conciliación se bloquea.
- La base no viene en el contenedor: se **deriva** como `Total − IVA − INC − otros impuestos` y se marca como derivada. Las retenciones son informativas.
- **Corte del contenedor:** la última `Fecha Recepción`. Si el último día llega antes de las 22:00 se advierte que puede estar incompleto. Lo de Odoo posterior al corte es *diferencia de alcance*; lo del mismo día del corte se marca como faltante con la nota «la DIAN puede no haberlo recibido aún».
- Importes en texto: formato colombiano (`1.234.567,89`). Un texto con un solo punto y tres dígitos (`15.000`) se lee como miles y se advierte como ambiguo. Las celdas numéricas (lo normal en la exportación de la DIAN) no tienen esta ambigüedad.
- Límites: archivo de 10 MB, 200 MB descomprimido y 100.000 filas. Un `.xlsx` es un ZIP: antes de abrirlo se revisa su tamaño descomprimido y se cuentan sus filas por bloques, así un archivo muy comprimido (millones de filas en pocos MB) se rechaza sin cargarlo. Solo se conservan las primeras 100 columnas de cada fila.

### Odoo
- Compañías: todas las de Odoo cuyo NIT coincide con el emisor (tolerando el dígito de verificación). Si Odoo guarda el NIT con guion (`900123456-7`), solo cuenta lo que va antes del guion, para no confundir otra entidad (`90012345-6`).
- `account.move` con `move_type` en `out_invoice`/`out_refund`; `pos.order` de esas compañías. `date_order` está en UTC y se convierte a hora de Colombia (UTC−5).
- Se lee una ventana ampliada (±`VENTAS_MARGEN_DIAS`) y **todos los estados**: lo que queda fuera sirve como evidencia.
- En alcance: `account.move` publicado (`posted`) y POS `paid`/`done`/`invoiced`, con fecha local dentro del período.
- Antes de leer se consulta `fields_get`: los campos que no existen se informan y no se usan.
- **Totales fiscales:** `amount_total` / `amount_untaxed` / `amount_tax`. Nunca `amount_residual` ni `amount_paid`.
- Si una lectura supera 50.000 registros se detiene con error (nunca un resultado parcial). Si el usuario de Odoo no puede leer `account.move` o `pos.order`, la conciliación se rechaza con un mensaje claro; si el módulo POS no está instalado, se advierte en «Fuentes de Odoo».

### Deduplicación POS/factura
1. Una orden POS con `account_move` leído se agrupa en esa factura: la venta cuenta **una vez** (con trazabilidad).
2. Si la orden y su factura tienen CUFE distintos se conservan las dos, con alerta, y la orden queda como *posible duplicado*.
3. Una orden facturada en un asiento fuera de la ventana queda fuera de alcance (la venta fiscal es ese asiento).
4. Nunca se elimina un registro por tener igual total o fecha.

### Identificación (dos pasadas)
1. **CUFE/CUDE** igual en ambas fuentes. Si además el número difiere → *identidad contradictoria*.
2. **Número normalizado** `PREFIJO|FOLIO` (sin `/`, `-`, espacios ni ceros a la izquierda; `FE/2026/00300` = `FE-300`), solo con registros libres. Si el CUFE difiere → *identidad contradictoria*.
- Varios candidatos por número → *ambiguo* (no se elige al azar). Varios por CUFE → *posible duplicado*.
- Un número que el contenedor repite y que ya quedó emparejado → *posible duplicado*; un número que en Odoo ya corresponde por CUFE a otro documento DIAN → *identidad contradictoria*.
- Prefijos conocidos se prueban del más largo al más corto: `FE1706` con `FE1` conocido es `FE1|706`, distinto de `FE-1706`.
- Cada fila tiene un id único: `dian:PREFIJO-FOLIO` para la que entra en Δ; las excluidas, fuera del período o con número repetido usan `dian:filaN`. Las búsquedas por número prefieren la fila que entra en Δ.

### Estados
`matched` (Coincide), `missing_in_odoo`, `missing_in_dian`, `amount_mismatch`, `tax_mismatch`, `identity_mismatch`, `possible_duplicate`, `ambiguous`, `scope_difference`, `needs_review`, `excluded`.

### Tolerancia y precisión
- Todo con `Decimal`, redondeo *half-up* a centavos.
- **Tolerancia por documento: $1,00** (`VENTAS_TOLERANCIA_DOCUMENTO`): una diferencia ≤ $1 queda *Coincide* y su importe exacto va al componente «Redondeos dentro de la tolerancia».
- **La diferencia global es exacta**, sin tolerancia.

### Descomposición de la diferencia global
```
Δ neta (DIAN − Odoo) = Σ componentes + residuo
```
Cada documento DIAN elegible y cada documento canónico de Odoo en alcance cae en exactamente un componente: faltantes en Odoo, faltantes en DIAN, diferencias de valor, redondeos, notas crédito con tipo/signo distinto, identidad contradictoria, duplicidades, ambiguos y diferencias de alcance. Las filas fuera de Δ (excluidas o fuera del período) aportan 0. El **residuo** solo puede ser distinto de cero si algún documento quedara sin componente: se informa, nunca se fuerza.

### Criterios de alcance que se muestran
Fuentes de Odoo leídas, empresa, período cubierto por el contenedor, tipos documentales, estados, notas crédito, deduplicación, campos de identificación disponibles y corte del contenedor. Un criterio en amarillo explica qué parte de la diferencia no es comparable.

### Impuestos
Se compara el total de impuestos DIAN (IVA + INC + otros) contra `amount_tax`. **IVA e INC de Odoo no se separan** hasta confirmar el mapeo de impuestos de la instancia (en pantalla y en el reporte aparece «requiere mapeo»).

---

## 3. Agente Claude

| Herramienta | Qué devuelve |
|---|---|
| `obtener_resumen` | Alcance, totales, descomposición, prefijos y alertas |
| `listar_casos` | Casos por componente, estado, prefijo o fechas (máx. 25) con agregados |
| `investigar_diferencia` | Evidencia completa de un caso (validaciones campo a campo) |
| `buscar_documento` | Búsqueda alternativa por CUFE, número, folio o receptor en el contenedor, en lo leído y en Odoo en vivo |
| `consultar_documento_odoo` | Lectura en vivo de campos autorizados y relaciones (factura ↔ POS, reversos) |
| `analizar_impuestos` | Impuestos DIAN vs `amount_tax` por documento, prefijo o total |
| `analizar_secuencias` | Saltos de numeración y si los folios ausentes existen en Odoo |
| `generar_plan_revision` | Casos pendientes agrupados y priorizados |

`consultar_historial` no se implementó: la primera versión no persiste conciliaciones (ver pendientes).

**Búsqueda alternativa.** `buscar_documento` revisa el contenedor completo, todo lo leído de Odoo y, si la investigación tiene credenciales, Odoo **en vivo sin límite de fecha ni estado**, con patrones anclados al final del número (`FE/2026/00005`, `FE-5`, `FE0005` coinciden con FE-5; `FE/2026/00015` no). Solo una búsqueda en vivo **completa** (sin errores ni recortes) cuenta para descartar que el documento exista en Odoo; sin conexión en vivo la búsqueda no es concluyente.

**Seguridad**
- Solo lectura: `OdooSoloLectura` permite únicamente `fields_get`, `search_read`, `read`, `search_count`, `check_access_rights` sobre `account.move`, `pos.order`, `res.partner`, `res.company`, `account.journal`, `pos.config`. Los dominios los arma el backend con parámetros tipados; no hay consultas libres ni código arbitrario.
- Las lecturas en vivo solo devuelven ventas de la compañía conciliada (facturas y notas crédito de cliente u órdenes POS). Asientos de nómina, facturas de proveedor o registros de otra compañía no se consultan.
- Claude no crea, edita, publica, cancela ni elimina nada; solo propone acciones para una persona.
- Minimización: NIT enmascarados (`***123`), sin nombres de clientes ni CUFE completos, sin el texto libre `ref` de los asientos; nunca se envían todas las filas, solo agregados y los documentos consultados.
- La clave vive solo en `ANTHROPIC_API_KEY` del servidor; no se registra en logs, no aparece en `repr` de la configuración ni en las respuestas.

**Límites:** `VENTAS_IA_MAX_HERRAMIENTAS` (12) llamadas a herramientas; `VENTAS_IA_TIMEOUT_TOTAL` (120 s: el bucle de herramientas usa hasta el 60 % y la respuesta final el resto; si el bucle se queda sin tiempo o una llamada vence, se pasa a la respuesta final con la evidencia reunida); `VENTAS_IA_TIMEOUT_LLAMADA` (60 s); sin reintentos automáticos del SDK; cada consulta a Odoo durante la investigación tiene 20 s y no se inicia fuera de plazo; máximo 2 investigaciones simultáneas y 1 por conciliación.

**Razonamiento (thinking).** Los modelos actuales (Haiku/Sonnet/Opus 5.5) razonan por defecto. Los bloques de razonamiento se devuelven a la API **sin modificar** en cada turno con herramientas, como exige la API. `VENTAS_IA_MAX_TOKENS` (16.000) incluye ese razonamiento.

**Respuesta estructurada:** la llamada final usa salida estructurada (JSON Schema) y el backend la valida otra vez:
1. Estructura y valores permitidos del esquema.
2. Cada hecho debe citar evidencias que una herramienta devolvió **en esa investigación**; si no, se descarta. Un hallazgo sin hechos válidos se descarta completo.
3. Las acciones deben apuntar a un registro devuelto por las herramientas.
4. Importes del texto que el backend no devolvió → advertencia y revisión humana.
5. `confirmed` exige que **cada** documento citado como faltante en Odoo tenga una búsqueda en vivo completa, y un hallazgo de faltantes debe citarlos uno por uno; si no, baja a `probable`. Una acción de «registrar» un faltante sin esa búsqueda queda advertida.
6. El **impacto financiero lo calcula el backend** con los documentos citados (y el componente), nunca se toma del texto.

`confidence`: *Alta* = todos los hechos con evidencia directa; *Media* = evidencia parcial; *Baja* = hipótesis plausible. Es cualitativa, no una probabilidad calibrada.

**Errores manejados:** clave ausente o inválida, permisos, cuota/crédito/límite de velocidad, timeout, conexión, servicio saturado, modelo inexistente, contexto demasiado grande, respuesta inválida o cortada, rechazo, Odoo caído durante la investigación. En todos los casos la conciliación sigue intacta y la respuesta es `{"disponible": false, "mensaje": "Análisis IA no disponible: …"}`.

**Coste:** instrucciones y definiciones de herramientas con caché de prompts; la conversación usa caché incremental. La llamada final cambia `tool_choice` y agrega el formato de salida, así que su parte de conversación no se reutiliza de la caché (instrucciones y herramientas sí). Cada investigación registra en el log tokens de entrada/salida, lecturas de caché, llamadas y segundos (sin contenido ni datos personales).

**Proveedor anterior:** compras sigue usando Groq (`main.py`) y `ai_auditor.py` tal como estaban; no se tocaron.

---

## 4. API

| Método | Ruta | Uso |
|---|---|---|
| POST | `/api/ventas/analizar-contenedor` | `dian` (archivo). Valida estructura, columnas y tipos. No consulta Odoo |
| POST | `/api/ventas/conciliar` | `dian`, `credentials` (token cifrado), `desde`, `hasta`, `incluir_move`, `incluir_pos`, `tolerancia` |
| GET | `/api/ventas/documento?run_id=&ref=` | Evidencia completa de un documento |
| GET | `/api/ventas/reporte?run_id=` | Excel: Resumen, Parámetros, Global, Descomposición, Prefijos, Secuencias, Detalle, Evidencia Odoo, Hallazgos IA, Pendientes |
| POST | `/api/ventas/investigar` | `run_id`, `foco_tipo` (`componente`/`prefijo`/`documento`), `foco_valor`, `pregunta`, `credentials` (opcional, para consultas en vivo) |
| GET | `/api/ventas/estado-ia` | Si Claude está configurado, modelo y límites (nunca la clave) |

Errores: 400 (archivo o credenciales), 404 (conciliación expirada o documento inexistente), 409 (investigación en curso), 413 (archivo > 10 MB), 422 (validación, período, tolerancia, compañía, permisos de Odoo, demasiados registros), 429 (demasiadas investigaciones), 502 (Odoo no responde).

Las conciliaciones se guardan en memoria (`VENTAS_MAX_RESULTADOS`, `VENTAS_TTL_RESULTADOS`). Si Render reinicia, la interfaz pide volver a conciliar. Con más de una instancia cada una tiene su propia memoria y el reporte o el agente podrían no encontrar la conciliación: mantener una sola instancia hasta tener persistencia.

El reporte Excel guarda como **texto** cualquier valor que empiece por «=» (nombres de clientes, de registros o texto de Claude): no se ejecutan fórmulas inyectadas.

---

## 5. Variables de entorno (Render)

| Variable | Defecto | Descripción |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Activa el agente. Sin ella todo funciona salvo el análisis IA |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | Modelo del agente (p. ej. `claude-sonnet-5-5` para investigaciones más profundas) |
| `VENTAS_TOLERANCIA_DOCUMENTO` | `1.00` | Tolerancia por documento en pesos |
| `VENTAS_MARGEN_DIAS` | `5` | Días extra que se leen de Odoo a cada lado del período |
| `VENTAS_TTL_RESULTADOS` | `3600` | Segundos que una conciliación queda disponible para reporte y agente |
| `VENTAS_MAX_RESULTADOS` | `5` | Conciliaciones en memoria (~21 MB cada una con ~3.000 documentos) |
| `VENTAS_IA_MAX_HERRAMIENTAS` | `12` | Llamadas a herramientas por investigación |
| `VENTAS_IA_TIMEOUT_TOTAL` | `120` | Segundos por investigación |
| `VENTAS_IA_TIMEOUT_LLAMADA` | `60` | Segundos por llamada a Anthropic |
| `VENTAS_IA_MAX_TOKENS` | `16000` | Tokens máximos por respuesta (incluye el razonamiento) |

## 6. Despliegue

1. Render → servicio del backend → *Environment*: agregar `ANTHROPIC_API_KEY` (y, si se quiere, las demás variables). No poner la clave en el repositorio ni en el frontend.
2. Desplegar la rama (Render instala `anthropic` desde `requirements.txt`; no hay migraciones de base de datos).
3. Vercel publica `frontend/` sin cambios de configuración.
4. **Permisos:** el módulo se llama `ventas`. Un administrador lo habilita en *Gestión usuarios → Editar módulos → Ventas DIAN vs Odoo* (si se edita a sí mismo, se aplica sin cerrar sesión). La conexión de Odoo se configura donde siempre (*Configuración*). El rol *Lectura* puede ver, pero no conciliar ni investigar.
5. **Prueba de aceptación en staging** (no se pudo hacer desde el entorno de desarrollo, sin credenciales reales): ver la lista en [`QA_VENTAS.md`](QA_VENTAS.md#9-prueba-de-aceptación-pendiente-en-staging).

## 7. Pruebas

```bash
cd backend
python -m pytest -q          # 227 pruebas: 226 pasan; 1 falla previa de compras
python ../qa/ventas/qa_ui.py # QA de punta a punta en navegador (requiere playwright)
```

- Todas las pruebas de ventas usan **datos sintéticos** (`backend/conftest.py`, `qa/ventas/datos_sinteticos.py`): no se usa el contenedor real ni una instancia de Odoo o de Anthropic.
- `conftest.py` define una `ENCRYPTION_KEY` efímera si no existe, para poder importar `main.py`.
- Falla **previa** (no relacionada con ventas, no se modificó): `test_comparador.py::TestNormalizacionDeFormatos::test_nit_sin_dv_en_dian_coincide_con_nit_con_dv_en_erp` (compras: NIT sin DV en la DIAN vs NIT con DV en el ERP).

## 8. Auditoría del repositorio (fase 0) y hallazgos fuera de ventas

- **XML-RPC:** Odoo lo marca como deprecado desde Odoo 19 y programado para retirarse en Odoo 20. Todo el acceso de ventas pasa por `OdooSoloLectura(ejecutar)`: migrar a la API JSON-2 solo exige otra función `ejecutar`. Compras (`odoo_match.py`) tendrá que migrar también.
- **Proveedores de IA existentes:** `main.py` usa Groq (`GROQ_API_KEY`, `llama-3.3-70b-specdec`); `ai_auditor.py` llama a una URL de OpenRouter con variables `GROQ_*`. Conviene revisar esa inconsistencia aparte.
- **XML-RPC sin timeout en compras:** `OdooConnector` no fija tiempo máximo; ventas usa su propio transporte y autenticación con tiempo máximo.
- **Inyección de fórmulas en el reporte de compras:** `comparador.generar_excel_reporte_bytes` escribe nombres de proveedores del Excel DIAN tal cual; un nombre que empiece por «=» quedaría como fórmula. En ventas se neutraliza; en compras no se tocó (recomendación: la misma conversión a texto antes de guardar).
- **Repositorio público:** el contenedor real (NIT y nombres de clientes, nómina) **no** se sube; `.gitignore` bloquea `Nuevo modulo/` y `contenedor*dian*.xlsx`. `.gitignore` no retira archivos que ya estén versionados: si alguno llegó al repositorio, hay que quitarlo con `git rm --cached` y purgarlo del historial. Revisar también `backend/odoo_dump.json` (un registro de ejemplo versionado desde el inicio).
- `API_AUTH_TOKEN` es opcional y el frontend no envía `Authorization`; CORS abierto (`*`). Sin cambios en esta entrega.

## 9. Pendientes y supuestos

- **Mapeo de impuestos de Odoo** (IVA vs INC vs otros por `account.tax`) para comparar IVA e INC por separado.
- **Campos de estado electrónico** según la localización instalada (`l10n_co_dian_state`, `edi_state`, `l10n_co_edi_pos_dian_state`): se usan solo si existen en la instancia.
- **Historial de conciliaciones** (`consultar_historial`): requiere persistencia (por ejemplo en Neon); hoy los resultados viven en memoria.
- Se asume que la fecha fiscal de una orden POS es su `date_order` en hora de Colombia.
- Pendiente validar en staging con Odoo y Anthropic reales (ver QA).
