# QA — FactuMatch Ventas (DIAN emitidos vs Odoo)

**Fecha:** 15-10-2026 · **Rama:** `feature/ventas-dian-odoo` · **Base:** `main` en `b54b893`

Reglas, arquitectura, variables y despliegue del módulo: [`VENTAS.md`](VENTAS.md).

---

## 1. Resumen

| Verificación | Resultado |
|---|---|
| Pruebas automáticas de ventas (6 archivos) | **192 de 192 pasan** |
| Suite completa del backend | 227 pruebas: 226 pasan y 1 falla **previa** de compras, que ya fallaba antes de este cambio |
| QA de punta a punta en navegador | **21 de 21 casos pasan** |
| Datos sintéticos de QA | 277 de 277 estados esperados y residuo $0,00 |
| Contenedor real, solo en local | 3.178 filas leídas; todas las discrepancias plantadas detectadas; residuo 0 |
| Revisión independiente | 20 hallazgos y 4 defectos que introdujeron las primeras correcciones: todos atendidos y con prueba (sección 7) |
| Hallazgo de esta QA | Un `.xlsx` muy comprimido demoraba minutos en rechazarse: corregido y con prueba (sección 6) |
| Odoo y Anthropic reales | **Pendiente:** prueba de aceptación en staging (sección 9) |

Nada de lo anterior usó una instancia real de Odoo ni la API de Anthropic: el entorno de desarrollo no tenía credenciales ni salida a esos servicios. Por eso la sección 9 es obligatoria antes de producción.

---

## 2. Entorno y datos

- **Versiones:** Python 3.10.20 (Render usa 3.10.11), FastAPI 0.111.0, anthropic 1.14.0, openpyxl 3.1.2, xlrd 2.0.1, pytest 9.1.1 y Playwright 1.56.0 con Chromium.
- **Odoo simulado** (`OdooFalso`, en `backend/conftest.py`): evalúa los dominios reales que envía el backend (notación polaca, `ilike`, `=ilike`, `in`, fechas), respeta `order`, `limit` y `offset`, y puede simular campos o módulos ausentes, errores de permisos y caídas.
- **Claude simulado:** clientes guionados que devuelven lo mismo que la API real: bloques de razonamiento con firma, llamadas a herramientas, la respuesta final en JSON y los errores del SDK (timeout, cuota, autenticación, modelo inexistente, servicio saturado…).
- **Solo datos sintéticos en el repositorio**, que es público. El contenedor real se usó una vez, en local, para verificar el formato (sección 5). No se sube, y `.gitignore` bloquea `Nuevo modulo/` y `contenedor*dian*.xlsx`.

### Conjunto sintético de QA (`qa/ventas/datos_sinteticos.py`)

- **Contenedor DIAN:** 280 filas del 01-10-2026 al 15-10-2026.
  - **268 incluidas:** 70 facturas FE, 192 documentos equivalentes POS, 5 notas crédito y 1 nota débito.
  - **12 excluidas por regla:** 3 de documento soporte, 5 *application response* y 4 de nómina.
- **Odoo:** 77 `account.move` y 201 `pos.order` de una compañía de prueba.
- **Resultado esperado:**
  - neto DIAN $43.069.530,00;
  - neto Odoo $43.892.140,40;
  - Δ −$822.610,40;
  - residuo $0,00.

| Caso plantado | Estado obtenido (igual al esperado) |
|---|---|
| FE-15 aprobada en la DIAN y sin asiento en Odoo | Falta en Odoo |
| FE-22 con $3.000 más en Odoo | Diferencia de valor |
| FE-30 con $1.500 menos de impuesto en Odoo y el mismo total | Diferencia de impuestos |
| FE-31 con $0,40 más en Odoo | Coincide: redondeo dentro de la tolerancia de $1 |
| FE-40 en borrador en Odoo | Diferencia de alcance |
| FE-50 con el mismo CUFE en dos asientos | Posible duplicado |
| FE-12 asociada en Odoo a un cliente con otro NIT | Requiere revisión |
| POS-240 a POS-247 solo en Odoo, sin CUFE | Falta en DIAN (8) |
| FE/2026/00071 en Odoo el día del corte | Falta en DIAN, con la nota «la DIAN puede no haberlo recibido aún» |
| NC-3 registrada como factura en Odoo | Identidad contradictoria, en el componente «Notas crédito con tipo o signo distinto» |
| Orden POS facturada con FE-61 | Coincide; la venta cuenta una sola vez |
| Nombre de cliente con HTML (FE-45) | Se muestra como texto y no se ejecuta |
| Nombre de cliente con una fórmula `=HYPERLINK(…)` (FE-46) | Queda como texto en el Excel, no como fórmula |

---

## 3. Pruebas automáticas

```bash
cd backend
python -m pytest -q        # 227 pruebas: 226 pasan; 1 falla previa de compras
```

| Archivo | Casos | Qué cubre |
|---|---|---|
| `test_ventas_normalizacion.py` | 37 | Decimal y formato colombiano, NIT con y sin DV, CUFE, número `PREFIJO\|FOLIO`, prefijos que no colisionan, UTC → hora de Colombia, enmascarado |
| `test_ventas_dian.py` | 14 | Nombres exactos de columna, tipos incluidos y excluidos, encabezado desplazado, varios NIT, corte, base derivada, repetidos, topes de filas y de tamaño, importes ambiguos |
| `test_ventas_odoo.py` | 33 | Lista blanca de solo lectura, compañía por NIT, campos inexistentes, estados, POS facturado, permisos, lecturas truncadas, autenticación con tiempo máximo |
| `test_ventas_motor.py` | 36 | Identificación en dos pasadas, estados, tolerancia, descomposición y residuo, secuencias, reporte |
| `test_ventas_agente.py` | 54 | Herramientas, validación contra la invención, regla de «confirmada», errores del proveedor, límites de llamadas y tiempo, razonamiento, minimización de datos |
| `test_ventas_api.py` | 18 | Endpoints, errores HTTP, concurrencia, clave nunca expuesta, `main.py` registra ventas sin tocar compras |
| Compras: `test_comparador`, `test_odoo_match`, `test_groq_client` | 35 | Sin cambios: 34 pasan y 1 falla igual que antes |

**Regresión de compras.**
- La falla es `test_comparador.py::TestNormalizacionDeFormatos::test_nit_sin_dv_en_dian_coincide_con_nit_con_dv_en_erp`.
- Se comprobó en una copia limpia de `7f517c8`, el commit previo al módulo: la misma prueba falla allí, con los mismos 34 aprobados. `b54b893` no cambia código de compras.
- No se modificó ningún archivo de compras ni sus pruebas.
- `test_main_registra_ventas_sin_tocar_compras` verifica que las rutas de compras siguen registradas.

### Pruebas obligatorias del prompt maestro (sección 10)

| Requisito | Pruebas |
|---|---|
| CUFE coincidente | `test_totales_identicos` |
| CUFE ausente y número coincidente | `test_cufe_ausente_en_odoo_y_numero_coincide` |
| Número con `/`, `-`, espacios y ceros | `test_numero_con_barras_guiones_espacios_y_ceros`, `test_mismo_numero_con_barras_guiones_espacios_y_ceros` |
| Mismo número con NIT emisor diferente | `test_mismo_numero_con_nit_emisor_diferente`, `test_nit_emisor_distinto_a_la_compania_marca_identidad` |
| CUFE coincidente y número discordante | `test_cufe_coincide_y_numero_discordante`, `test_numero_coincide_y_cufe_distinto` |
| Varios candidatos y duplicados | `test_varios_candidatos_por_numero_es_ambiguo`, `test_cufe_repetido_en_odoo_es_posible_duplicado`, `test_numero_repetido_en_el_contenedor_es_posible_duplicado` |
| Prefijos que no deben colisionar | `test_prefijos_distintos_no_colisionan` (motor y normalización) |
| Facturas de `account.move` y estados | `test_estados_en_alcance`, `test_tipos_incluidos_excluidos_y_signos` |
| POS independiente y POS vinculado sin duplicar | `test_fecha_pos_en_hora_de_colombia`, `test_pos_facturado_cuenta_una_sola_vez`, `test_pos_facturado_no_duplica_el_total`, `test_pos_facturado_fuera_de_la_ventana_queda_fuera_de_alcance` |
| Devolución y nota crédito | `test_devolucion_pos_y_nota_credito`, `test_nota_credito_con_signo_incorrecto` |
| Compañías distintas | `test_registros_de_otra_compania_no_se_leen`, `test_compania_distinta_en_odoo`, `test_varias_companias_con_el_mismo_nit_se_leen_juntas`, `test_nit_con_guion_no_confunde_otra_entidad` |
| Totales idénticos, un faltante, un adicional, diferencia de importe | `test_totales_identicos`, `test_un_documento_faltante_en_odoo`, `test_un_documento_adicional_en_odoo`, `test_una_diferencia_de_importe` |
| Diferencia de IVA/INC | `test_diferencia_de_iva_inc` |
| Duplicación POS/factura | `test_duplicidad_pos_factura_con_cufe_distinto`, `test_pos_facturado_con_cufe_distinto_se_conserva_con_alerta` |
| Períodos o empresas diferentes | `test_periodos_distintos`, `test_contenedor_que_no_cubre_el_periodo`, `test_documento_de_odoo_posterior_al_corte` |
| Residuo no explicado | `test_residuo_no_explicado_se_informa_y_no_se_fuerza` |
| Tolerancia y redondeo | `test_tolerancia_monetaria_y_redondeo`, `test_tolerancia_cero` |
| Bruto y neto separados | `test_totales_bruto_y_neto_separados` |
| Hallazgos estructurados válidos | `test_hallazgos_estructurados_validos`, `test_esquema_compatible_con_salida_estructurada`, `test_validador_de_esquema` |
| Respuesta inválida o incompleta | `test_respuesta_que_no_es_json`, `test_respuesta_incompleta_segun_esquema`, `test_respuesta_cortada_por_max_tokens` |
| Timeout, cuota y autenticación | `test_fallas_del_proveedor_no_afectan_la_conciliacion` (10 errores del SDK), `test_limite_de_tiempo_total` |
| La conciliación sigue disponible si Claude falla | `test_fallas_del_proveedor_no_afectan_la_conciliacion`, `test_investigar_con_falla_del_proveedor_mantiene_el_reporte`, `test_falla_a_mitad_de_la_investigacion` |
| La IA no inventa documentos ni importes | `test_no_inventa_documentos_ni_importes`, `test_sin_evidencia_devuelta_no_hay_hechos` |
| Límites de herramientas y llamadas | `test_limite_de_llamadas_a_herramientas`, `test_herramientas_respetan_el_plazo`, `test_consultas_a_odoo_se_detienen_al_vencer_el_plazo` |
| Causas probables distintas de confirmadas | `test_causa_confirmada_sin_busqueda_alternativa_baja_a_probable`, `test_hay_que_buscar_todos_los_faltantes_citados`, `test_faltantes_sin_documentos_citados_no_se_confirman` |

---

## 4. QA de punta a punta (navegador)

```bash
pip install playwright && playwright install chromium     # una vez
python qa/ventas/qa_ui.py
```

**Montaje** (`qa/ventas/qa_ui.py`):
- `servidor_qa.py` monta el router **real** de `/api/ventas` con el Odoo simulado (datos sintéticos) y un Claude guionado. Ese Claude usa las herramientas reales y responde con razonamiento, igual que la API.
- Un servidor estático sirve `frontend/`. `app.js` se sirve apuntando al API local; Apps Script y los CDN quedan bloqueados.
- Se usan tres usuarios: administrador con el módulo `ventas`, operador sin él, y rol *Lectura*.
- Los resultados y las capturas quedan en `qa/ventas/salida/`, que está ignorada por git.

**Última ejecución:** 15-10-2026 14:57 con el código final. **21 pasan · 0 fallan.**

| Código | Caso | Qué se verificó | Resultado |
|---|---|---|---|
| QA-01 | Acceso con permiso `ventas` | Menú visible, vista abierta, pestañas 2–4 bloqueadas hasta conciliar | PASA |
| QA-02 | Usuario sin el módulo | Menú oculto y `switchView('ventas')` bloqueado | PASA |
| QA-03 | Archivo que no es Excel | Mensaje «no tiene un formato Excel válido… parece ser un PDF» y botón deshabilitado | PASA |
| QA-04 | Contenedor con dos NIT emisores | Validación bloqueante y botón deshabilitado | PASA |
| QA-05 | Contenedor válido | 268 incluidos, 12 excluidos, 7 tipos, 32 de 32 columnas, período sugerido y aviso de corte | PASA |
| QA-06 | Sin Odoo conectado | Estado «SIN CONECTAR» y botón deshabilitado | PASA |
| QA-07 | Conciliación global | Neto DIAN $43.069.530,00 · neto Odoo $43.892.140,40 · Δ −$822.610,40 · residuo $0,00; «CUADRA»; 9 criterios de alcance en verde | PASA |
| QA-08 | De la descomposición a los casos | El componente «Documentos de Odoo sin reporte DIAN» filtra 9 casos | PASA |
| QA-09 | Filtros, paginación y búsqueda | «Mostrando 1 a 50 de 291», página siguiente, búsqueda FE-22 → 1 caso | PASA |
| QA-10 | Evidencia de un documento | DIAN y Odoo lado a lado; la validación del total en rojo | PASA |
| QA-11 | Reporte Excel | 10 hojas; la fórmula inyectada queda guardada como texto | PASA |
| QA-12 | Investigación de Claude | 3 hallazgos con hechos, hipótesis y acciones; 4 herramientas; FE-15 «confirmada» solo tras búsqueda en vivo completa; impacto calculado por reglas | PASA |
| QA-13 | Investigar desde la evidencia | El foco pasa a `dian:FE-15` y la investigación termina | PASA |
| QA-14 | Falla de Claude | «Análisis IA no disponible… límite» y el reporte se sigue descargando | PASA |
| QA-15 | HTML inyectado en el contenedor | Se muestra literal; `window.__xss` nunca se define | PASA |
| QA-16 | Sin `ANTHROPIC_API_KEY` | Agente «NO CONFIGURADO» con explicación; la conciliación funciona | PASA |
| QA-17 | Rol *Lectura* | Carga y conciliación deshabilitadas | PASA |
| QA-18 | Pantalla de 1024 px | Sin desplazamiento horizontal de la página | PASA |
| QA-19 | Regresión de navegación | Comparaciones y tablero siguen funcionando; al volver, ventas conserva el resultado | PASA |
| QA-20 | Contratos de la API | `estado-ia` sin la clave; 404 con una conciliación inexistente; 400 con credenciales inválidas | PASA |
| QA-21 | Consola del navegador | Sin errores de JavaScript. Los 4 avisos de red son esperados: CDN bloqueados y 4xx de los casos negativos | PASA |

---

## 5. Validación con el contenedor real (solo en local)

El contenedor real que entregó el negocio se leyó **solo en el entorno local**. Odoo se simuló a partir de ese mismo contenedor, con discrepancias plantadas. Aquí solo se publican agregados: ni importes, ni NIT, ni nombres, ni prefijos.

**Lectura del contenedor**
- 3.178 filas: 3.086 incluidas y 92 excluidas.
- Encabezado y 32 columnas reconocidos.
- Ninguna validación bloqueante.

**Discrepancias plantadas y resultado**

| Discrepancia plantada | Resultado |
|---|---|
| Un documento sin asiento en Odoo | Falta en Odoo |
| Un documento solo en borrador | Diferencia de alcance |
| Un total con $5.000 de más | Diferencia de valor |
| Un impuesto con $900 de más y el mismo total | Diferencia de impuestos |
| Una diferencia de $0,50 | Coincide: redondeo dentro de la tolerancia |
| Una orden POS facturada | Agrupada con su factura; cuenta una vez |
| 16 órdenes POS sin CUFE y una factura del día del corte | 17 «Falta en DIAN» |

La descomposición **cuadra con residuo 0** y los 9 criterios de alcance quedan en verde.

**Rendimiento con 3.086 documentos**

| Paso | Medición |
|---|---|
| Lectura y validación del contenedor | ~1,0 s |
| Extracción, conjunto canónico y conciliación (Odoo simulado, sin red) | ~0,5 s |
| Reporte Excel | ~1,8 s, 404 KB, 10 hojas |
| Respuesta JSON de `/conciliar` | ~1,5 MB |
| Memoria que retiene una conciliación guardada | ~21 MB (`VENTAS_MAX_RESULTADOS=5` → ~105 MB) |

Contra Odoo real, el tiempo dominante será la red: lecturas paginadas de 500 registros por XML-RPC.

---

## 6. Hallazgo de esta QA: `.xlsx` muy comprimido

**Qué pasaba.** La QA volvió a ejecutar los scripts de reproducción de la revisión independiente sobre el código final.
- El caso #19 seguía siendo lento: un `.xlsx` de 4,9 MB con 1,5 millones de filas idénticas y sin `<dimension>` tardaba **223 s** y usaba +132 MB antes de rechazarse. Antes de cualquier corrección tardaba 495 s y usaba ~600 MB.
- La causa: si la hoja no declara `<dimension>`, openpyxl la recorre completa al abrirla, antes de que el tope de filas pueda actuar.
- `/analizar-contenedor` no exige credenciales, así que el problema era explotable con un solo archivo.

**Corrección** (`dian_emitidos._revisar_xlsx`, antes de abrir el archivo con openpyxl):
1. Se rechaza un `.xlsx` cuyo tamaño descomprimido declarado supera **200 MB**. `zipfile` nunca entrega más bytes de los declarados. Un contenedor real de 10 MB descomprime en unos 70 MB.
2. Se cuentan las filas de cada hoja descomprimiendo por bloques de 1 MB, y se rechaza en cuanto superan 100.000.
3. Solo se conservan las primeras 100 columnas de cada fila; el contenedor tiene 32.

| Archivo armado a mano | Antes | Después |
|---|---|---|
| 1.500.000 filas (4,9 MB) | 223 s, +132 MB | Rechazado en 0,0 s (tamaño descomprimido) |
| 250.000 filas | Rechazo tras leer todo | Rechazado en 0,8 s, +6 MB (conteo de filas) |
| 90.000 filas sin `<dimension>`, dentro de los topes | — | Aceptado en ~45 s: peor caso dentro de los límites |

- Un contenedor real de 90.000 filas pesaría ~18 MB, así que no cabe en el límite de 10 MB.
- El contenedor real sigue leyéndose igual: 3.178 filas en 0,9 s.
- **Pruebas:** `test_xlsx_muy_comprimido_se_rechaza_sin_abrirlo` (verifica que openpyxl ni siquiera se llama), `test_xlsx_comprimido_dentro_del_tope_se_lee` y `test_xlsx_que_descomprimido_supera_el_tope`.

---

## 7. Revisión independiente

**Cómo se hizo**
- Un agente independiente revisó el módulo en solo lectura, en dos rondas, con scripts que reproducían cada defecto.
- Las correcciones se verificaron volviendo a ejecutar esos scripts sobre el código final el 15-10-2026.

**Lo que se confirmó sin cambios**
- Una prueba aleatoria de 400 escenarios dio **residuo 0 en todos**, sin errores y sin documentos contados dos veces. Los escenarios combinaban duplicados, CUFE compartidos, POS↔factura, devoluciones, filas rechazadas y bordes de período.
- No se encontró ninguna forma de escribir en Odoo saltando la lista blanca.
- La clave no aparece en respuestas ni en logs.
- Todo `innerHTML` de ventas pasa por `esc()`.

| # | Severidad | Hallazgo | Corrección | Evidencia |
|---|---|---|---|---|
| 1 | Media-alta | Filas con el mismo número se tapaban: evidencia e impacto equivocados | Id único `dian:filaN` para filas excluidas, fuera de período o repetidas; las búsquedas prefieren la fila incluida | `test_numero_repetido_en_fila_excluida_no_tapa_la_incluida`, `test_herramientas_prefieren_la_fila_incluida`; reproducción: impacto 5.500,00 correcto |
| 2 | Media-alta | La búsqueda en vivo podía no encontrar un documento existente y aun así contar como «buscado» | Patrones anclados (`=ilike`); un resultado recortado no es concluyente; solo cuenta una búsqueda completa | `test_busqueda_en_vivo_encuentra_el_numero_aunque_haya_muchos_parecidos`, `test_busqueda_en_vivo_recortada_no_es_concluyente` |
| 3 | Media | La regla de causa «confirmada» se podía eludir | Cada faltante citado necesita búsqueda completa; aplica aunque no se citen documentos; una herramienta que falla no cuenta | `test_hay_que_buscar_todos_los_faltantes_citados`, `test_faltantes_sin_documentos_citados_no_se_confirman`, `test_busqueda_fallida_no_cuenta_como_busqueda` |
| 4 | Media | Inyección de fórmulas en el Excel | Toda celda que empieza por «=» se guarda como texto | `test_reporte_no_ejecuta_formulas_inyectadas`, QA-11 |
| 5 | Media (privacidad) | El agente podía leer nómina, facturas de proveedor y el texto libre `ref` | Lecturas en vivo limitadas a ventas de la compañía, sin `ref` ni nombres que lo contengan | `test_no_lee_asientos_que_no_son_ventas_ni_texto_libre`, `test_nombre_de_asiento_sin_referencia_libre`, `test_minimizacion_de_datos_personales` |
| 6 | Media | Se descartaban los bloques de razonamiento: error 400 esperado con Haiku y Sonnet 5.5 | El contenido de cada turno vuelve a la API sin modificar | `test_bloques_de_razonamiento_vuelven_sin_modificar`, `test_respuesta_final_con_razonamiento_y_json`; reproducción con el SDK real contra una API local |
| 7 | Media | El análisis de secuencias era cuadrático: un folio atípico bloqueaba `/conciliar` | Recorrido lineal y topes de bloques y folios | `test_secuencia_con_folio_atipico_no_bloquea`, `test_herramienta_de_secuencias_con_folio_atipico`; folios 1 y 20.000: 7,8 s → 0,00 s |
| 8 | Media | Filas fuera del período informaban un aporte distinto de cero | Sin componente, el aporte es 0 | `test_documento_fuera_del_periodo_no_aporta` |
| 9 | Media-baja | Tiempos: el corte del bucle descartaba la investigación, había reintentos y no había plazo en las herramientas ni en la autenticación | El corte pasa a la respuesta final; sin reintentos; plazo verificado antes de cada consulta a Odoo (20 s cada una); autenticación con tiempo máximo | `test_corte_del_bucle_por_tiempo_pasa_a_la_respuesta_final`, `test_timeout_de_una_llamada_del_bucle_no_descarta_la_investigacion`, `test_herramientas_respetan_el_plazo`, `test_consultas_a_odoo_se_detienen_al_vencer_el_plazo`; reproducción: 75 s de 120 |
| 10 | Media-baja | Truncado silencioso a 50.000 registros | Error en vez de un resultado parcial | `test_lectura_que_supera_el_maximo_falla_en_vez_de_truncar` |
| 11 | Media-baja | Varias compañías con el mismo NIT: solo se leía la primera | Se leen todas las que coinciden con el NIT | `test_varias_companias_con_el_mismo_nit_se_leen_juntas` |
| 12 | Baja | Importes en texto: signo perdido y miles mal leídos | Signo antes del primer dígito; «15.000» son miles | `test_importes_de_texto`, `test_a_decimal` |
| 13 | Baja | Sin permiso sobre POS, la respuesta era 502 | 422 con un mensaje claro | `test_sin_permiso_de_lectura_sobre_pos`, `test_permiso_insuficiente_en_odoo` |
| 14 | Baja | Tolerancia `NaN` devolvía 500 | Valores no finitos → 422 | `test_parametros_invalidos` |
| 15 | Baja | Un `run_id` con espacios eludía el bloqueo de concurrencia | Se normaliza el `run_id` | `test_investigacion_en_curso_no_se_duplica` |
| 16 | Baja | Un número repetido en el contenedor quedaba como identidad contradictoria | Posible duplicado, con nota clara | `test_numero_repetido_en_el_contenedor_es_posible_duplicado` |
| 17 | Baja | Mensajes de error del servidor se insertaban como HTML | Se escapan antes de mostrarlos | Revisión de código; QA-15 y QA-21 |
| 18 | Baja | Prefijo numérico de `.xls`: 20.0 se leía como «200» | Se convierte a entero primero | `test_prefijo_numerico_de_xls` |
| 19 | Baja | Memoria y tiempo con archivos muy comprimidos | Tope de filas durante la lectura y revisión previa del ZIP (sección 6) | `test_maximo_de_filas`, `test_xlsx_muy_comprimido_se_rechaza_sin_abrirlo`, `test_xlsx_que_descomprimido_supera_el_tope` |
| 20 | Baja | La clave aparecía en el `repr` de la configuración; la llamada final no reutiliza la caché de la conversación | `repr=False`. Instrucciones y herramientas quedan en caché; el coste de la llamada final está documentado en `VENTAS.md` | `test_la_configuracion_no_muestra_la_clave` |
| N1 | Media-baja | Folios de 9 o más dígitos quedaban sin búsqueda por número, pero la búsqueda contaba como completa | Patrones también para folios largos; si un modelo no tiene campos de búsqueda, la búsqueda no es completa | `test_folio_largo_tambien_se_busca`, `test_modelo_sin_campos_de_busqueda_no_es_busqueda_completa`, `test_sin_modulo_pos_la_busqueda_puede_ser_completa` |
| N2 | Media-baja | Si Odoo respondía con una página HTML, la respuesta era 500 | 502 con un mensaje | `test_autenticacion_ante_una_pagina_html` |
| N3 | Baja | Otra entidad cuyo NIT con DV se parece al del emisor se mezclaba | Con guion, solo cuenta la base antes del guion | `test_nit_con_guion_no_confunde_otra_entidad` |
| N4 | Baja | «592.593» se leía como 592593 sin aviso | Se advierte como importe ambiguo | `test_importes_en_texto_ambiguos_se_advierten` |

También se cerraron los vacíos de prueba que señaló la revisión:
- los clientes simulados ya devuelven razonamiento;
- el Odoo simulado respeta `order` y `=ilike`;
- los datos de privacidad incluyen `ref` y asientos que no son ventas;
- `test_hallazgos_estructurados_validos` ya no acepta «confirmada» sin búsqueda en vivo.

---

## 8. Limitaciones conocidas y riesgos

1. **Sin prueba con Odoo y Anthropic reales:** los nombres de campo de la localización colombiana, los permisos del usuario y la calidad de las respuestas del modelo se validan en staging (sección 9).
2. **IVA e INC no se separan** hasta confirmar el mapeo de impuestos de la instancia; aparece «requiere mapeo».
3. **Resultados en memoria:**
   - se pierden si Render reinicia;
   - con varias instancias, cada una tiene su propia memoria, así que conviene mantener una sola hasta tener persistencia.
4. **API sin autenticación obligatoria y CORS abierto.** Es así desde antes del módulo (`API_AUTH_TOKEN` es opcional) y aplica igual a compras. Se recomienda exigir token o sesión y limitar solicitudes por usuario.
5. **Peor caso de lectura:** un archivo armado a mano dentro de los topes puede tardar ~45 s (sección 6).
6. **XML-RPC** está deprecado desde Odoo 19 y se retirará en Odoo 20. El acceso de ventas está aislado en `OdooSoloLectura`, así que migrar a JSON-2 solo exige otra función `ejecutar`.
7. **Frontend sin pruebas unitarias:** se cubre con la QA de punta a punta.
8. **Una prueba de compras falla desde antes** de este cambio y no se tocó.
9. **Fuera de ventas** (detalle en `VENTAS.md` §8):
   - inyección de fórmulas en el reporte de compras;
   - `backend/odoo_dump.json` versionado;
   - proveedores de IA inconsistentes en compras.

---

## 9. Prueba de aceptación pendiente en staging

**Preparación**
- [ ] Desplegar la rama en un servicio de staging de Render (no en producción).
- [ ] Configurar `ANTHROPIC_API_KEY` en *Environment* de Render. Opcionalmente, configurar también `ANTHROPIC_MODEL`.
- [ ] Usar en Odoo un usuario **sin permisos de escritura** que pueda leer `account.move`, `pos.order`, `res.partner`, `res.company`, `account.journal` y `pos.config`.
- [ ] Exportar un contenedor DIAN de un período **cerrado**, por ejemplo una quincena anterior. No subirlo al repositorio.
- [ ] Habilitar el módulo `ventas` a un usuario de prueba.

**Conciliación**
- [ ] `GET /api/ventas/estado-ia` → `configurado: true`, el modelo esperado y ninguna clave en la respuesta.
- [ ] Cargar el contenedor:
  - 32 de 32 columnas;
  - los incluidos y excluidos coinciden con lo que muestra el portal de la DIAN;
  - el aviso de corte tiene sentido.
- [ ] Conciliar el mismo período contra Odoo:
  - criterio «Empresa» en verde, con la compañía correcta;
  - «Fuentes» con `account.move` y `pos.order` leídos;
  - «Campos de identificación» con los campos CUFE/CUDE que existen en la instancia.
- [ ] La descomposición cuadra con **residuo $0,00**. Si no, registrar el caso: es un defecto.
- [ ] Revisar a mano, en Odoo y en el portal de la DIAN:
  - 5 documentos «Coincide»;
  - **todos** los «Falta en Odoo» y «Falta en DIAN»;
  - una orden POS facturada, que debe contar una sola vez;
  - una venta POS cercana a medianoche, que debe quedar en el día de Colombia.
- [ ] Anotar la duración y la cantidad de registros leídos. Debe estar muy por debajo de los 120 s por consulta.
- [ ] Descargar el reporte: 10 hojas, totales iguales a la pantalla, y Excel no avisa de fórmulas.

**Agente Claude**
- [ ] Investigar la diferencia global y comprobar:
  - cada hecho cita evidencia que existe;
  - los importes coinciden con la pantalla;
  - las acciones son solo propuestas.
- [ ] Investigar un documento «Falta en Odoo»: debe hacer búsqueda en vivo, y solo con búsqueda completa puede quedar «confirmada».
- [ ] En los logs de Render, la línea de uso por investigación:
  - tiene tokens de entrada y salida, lecturas de caché mayores que 0 desde la segunda llamada, llamadas y segundos;
  - no incluye NIT, nombres ni la clave.
- [ ] Estimar el coste por investigación con esos tokens y el precio vigente del modelo.
- [ ] Poner una clave inválida en staging:
  - debe aparecer «Análisis IA no disponible» y el reporte debe seguir descargándose;
  - después, restaurar la clave.

**Solo lectura y regresión**
- [ ] Confirmar en Odoo, con el registro de cambios o `write_date`, que ningún registro se modificó durante la prueba.
- [ ] Ejecutar una comparación de compras como siempre y verificar que el resultado no cambió.
- [ ] Revisar la memoria del servicio tras 5 conciliaciones: ~21 MB por cada 3.000 documentos.

**Criterio de aceptación:**
- residuo 0;
- cada diferencia explicada;
- ninguna escritura en Odoo;
- ningún secreto ni dato personal en logs;
- hallazgos de Claude con evidencia verificable;
- compras sin cambios.

---

## 10. Cómo reproducir esta QA

```bash
# Pruebas automáticas (desde backend/)
python -m pytest -q
python -m pytest -q test_ventas_*.py          # solo ventas

# QA de punta a punta (desde la raíz; requiere playwright y chromium)
python qa/ventas/qa_ui.py                     # QA_PYTHON=<intérprete con las dependencias del backend>

# Servidor de QA para revisar la interfaz a mano
python qa/ventas/servidor_qa.py               # API en http://127.0.0.1:8765, credencial de Odoo «token-qa»
python -m http.server 8766 -d frontend        # cambiar API_URL de app.js a http://127.0.0.1:8765 solo en local
```

La QA nunca usa red, credenciales ni datos reales. `qa/` no se despliega: Render usa `backend/` y Vercel usa `frontend/`.

---

## 11. Criterios de aceptación del prompt maestro (sección 12)

| Criterio | Estado | Evidencia |
|---|---|---|
| Compras conserva su comportamiento | Cumple | Sin cambios en compras; 34 de 35 pruebas igual que en la base; QA-19 |
| Ventas compara DIAN con `account.move` y `pos.order` | Cumple en simulación; falta staging | QA-07; `test_ventas_odoo.py` |
| Identificación por CUFE/CUDE y número | Cumple | Sección 3 (identificación) |
| POS/contabilidad sin duplicar ventas | Cumple | `test_pos_facturado_*`; FE-61 en QA |
| Notas crédito y devoluciones con reglas explícitas | Cumple | `VENTAS.md` §2; `test_nota_credito_con_signo_incorrecto`, `test_devolucion_pos_y_nota_credito` |
| Totales con alcance equivalente y precisión monetaria | Cumple | Decimal; 9 criterios de alcance; QA-07 |
| Descomposición y residuo visible | Cumple | `test_residuo_no_explicado_se_informa_y_no_se_fuerza`; prueba aleatoria de 400 escenarios con residuo 0 |
| IVA/INC solo con mapeo confirmado | Cumple: no se separan sin mapeo | «requiere mapeo» en pantalla y reporte |
| Claude investiga con herramientas y evidencia | Cumple con Claude simulado; falta staging | QA-12; `test_ventas_agente.py` |
| Hechos, hipótesis y acciones separados | Cumple | Esquema de salida; QA-12 |
| Una falla de Claude no impide conciliar ni descargar | Cumple | QA-14, QA-16; `test_fallas_del_proveedor_no_afectan_la_conciliacion` |
| Sin escrituras contables automáticas | Cumple | Lista blanca `OdooSoloLectura`; `test_metodos_de_escritura_bloqueados`; revisión independiente |
| Secretos fuera del código y los logs | Cumple | `test_la_clave_no_aparece_en_respuestas_ni_logs`; búsqueda de claves, NIT y nombres reales en los 37 archivos de la entrega: sin coincidencias |
| Pruebas nuevas y de regresión | Cumple | 192 de 192 de ventas; falla previa de compras documentada |
| Documentación | Cumple | `VENTAS.md`, este documento, `backend/.env.example`, `README.md` |
