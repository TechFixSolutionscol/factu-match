# AUDITORÍA DE RENDIMIENTO, ESTABILIDAD Y ESCALABILIDAD — FACTU-MATCH

**Fecha:** 05-11-2026 · **Commit auditado:** `1708041` (`main`, 05-11-2026 15:04) · **Entorno de medición:** contenedor Linux local (2 CPU, 8 GB), Python 3.10, mismas versiones que `requirements.txt` (FastAPI 0.111.0, uvicorn 0.29.0, pandas 2.0.3, openpyxl 3.1.2) · **Alcance:** solo lectura; no se modificó código, dependencias, configuración ni despliegues.

Convenciones: **[HECHO]** = observado o medido en esta auditoría; **[HIPÓTESIS]** = explicación plausible pendiente de evidencia; **[NO VERIFICABLE]** = requiere acceso o datos que no estaban disponibles. Todas las mediciones de volumen son **pruebas locales con datos sintéticos** (sección 9); contra producción solo se enviaron peticiones mínimas de contrato sin archivos (sección 4.3), y nunca contra Odoo real. La configuración, métricas, eventos y logs de producción se leyeron con el conector oficial de Render en modo lectura.

---

## 1. Resumen ejecutivo

1. **El motor de Ventas es lineal y exacto a cualquier volumen probado** [HECHO]: de 1.000 a 100.000 documentos el tiempo y la memoria crecen en proporción directa, ningún documento se pierde ni se duplica y la descomposición cuadra con residuo 0 en todos los casos. No hay un algoritmo cuadrático que explique un «bloqueo».
2. **El servicio corre en el plan gratuito de Render y eso explica la mayor parte del síntoma** [HECHO, verificado con el conector de Render]: `factu-match` (srv-d869amh9rddc73br4ekg, Oregon) tiene plan `free`, 1 instancia, límite de **0,15 CPU** y **512 MB**, sin *health check*. Un servicio gratuito se apaga tras 15 minutos sin tráfico y tarda ≈1 minuto en volver: hoy, 05-11-2026, el log muestra un arranque a las 15:52:54 (hora de Bogotá), **ninguna petición** durante 15 minutos y el apagado a las 16:08:19; a las 16:28 mi primera petición tardó más de 45 s (arranque en frío de 16:28:45 a 16:29:05) y la siguiente 0,8 s. Es decir: el log que el usuario pegó corresponde a un arranque en frío provocado por su intento de carga, y **esa carga nunca llegó a la aplicación**: no hay una sola línea `POST /api/ventas/…` en ese arranque.
3. **El coste por documento es alto para ese plan** [HECHO]: el flujo completo de Ventas con 7.235 filas cuesta ≈9,5 s de CPU en este equipo (2,0 s analizar + 4,2 s conciliar + 3,3 s reporte, medido contra la app real) —y hasta ≈19 s con contención—, de los cuales un 35 % es leer el mismo Excel dos veces; cada conciliación deja ≈50 MB retenidos en el proceso (129 → 256 → 307 → 356 MB tras tres conciliaciones de 7.235 filas; con 15.000 filas la segunda conciliación ya supera los 512 MB). Con 0,15 CPU esos ≈10 s de CPU pueden convertirse en más de un minuto, y a eso se suma la latencia de Odoo: 21 llamadas XML-RPC secuenciales (12 MB de XML) para 7.235 filas, sin ninguna señal de progreso [NO VERIFICABLE en segundos: no hay Odoo real en el entorno de auditoría].
4. **El usuario no recibe ninguna información durante todo ese tiempo** [HECHO]: una sola petición HTTP de minutos, sin progreso, sin timeout, sin cancelación, y un backend que no escribe ni una línea de log por etapa. Por eso el log de Render «solo confirma el arranque» y la pantalla parece bloqueada. Las métricas de Render no muestran ningún agotamiento de memoria (pico observado 209 MB el 20-10) ni evento de fallo o reinicio desde el 15-10: hasta donde la evidencia llega, el servicio no «se cae»; **se duerme, arranca lento y procesa lento sin avisar**.
5. **El error del navegador no es de Factu-Match** [HECHO]: «A listener indicated an asynchronous response…» lo emite una extensión de Chrome; el frontend no usa `chrome.runtime` ni `postMessage`. Es un síntoma secundario.
6. **Compras tiene dos defectos de estabilidad que afectan a todo el servidor** [HECHO, reproducidos en local]: (a) `/odoo/comparar` y `/odoo/descargar-reporte` ejecutan pandas y la generación del Excel **dentro del bucle de eventos**, bloqueando todas las peticiones de todos los usuarios (5 s de comparación y 14 s de reporte medidos con 7.235 filas, durante los cuales nadie es atendido); (b) dos comparaciones simultáneas **interbloquean** el executor de 2 hilos y Compras deja de responder hasta reiniciar.
7. **Límites reales hoy** [HECHO]: 10 MB por archivo (un contenedor de 100.000 filas pesa ≈16 MB → rechazado con 413 antes de leerlo), 100.000 filas, 50.000 registros por modelo de Odoo. **Capacidad demostrada en local:** hasta 50.000 filas con ≈1,1 GB de memoria pico; en un plan de 512 MB la capacidad práctica es de ~10.000–15.000 filas por conciliación. 50.000 documentos es alcanzable solo con procesamiento en segundo plano, resultados paginados y un plan con ≥2 GB; 100.000 exige además persistencia fuera de memoria.
8. **Plan recomendado** (sección 15): fase 0 completar la evidencia que aún falta (archivo real de 7.235 filas, latencia de Odoo, captura de *Network*); fase 1 quitar los bloqueos del bucle de eventos y el interbloqueo en Compras, leer el Excel una sola vez, registrar duración por etapa y dar feedback y timeout en el frontend; fase 2 ejecuciones asíncronas con progreso y resultados paginados; fase 3 persistencia en la Postgres existente y pruebas de estrés en staging. Pasar de `free` a `starter` (sin *spin-down*, 0,5 CPU) elimina el arranque en frío y multiplica por tres la CPU, y es la medida más barata de aplicar de inmediato; pero no corrige el bloqueo del bucle de eventos de Compras, la memoria retenida ni la falta de progreso, y el límite de 512 MB sigue igual.

---

## 2. Problemas observados

| # | Problema reportado | Qué se pudo verificar |
|---|---|---|
| P-1 | Con un archivo mensual (~7.235 filas) la aplicación «se bloquea o no termina» | No se dispone del archivo. Se reprodujo el flujo completo en local, con la app real y un fixture sintético de 7.235 filas del mismo formato (sección 9): termina correctamente en ≈9,5 s de CPU más el tiempo de Odoo. En producción el servicio es `free` (0,15 CPU, 512 MB, *spin-down*): el arranque en frío de ≈1 min, la CPU reducida y la ausencia total de feedback explican la percepción de bloqueo (sección 4). En el único arranque registrado hoy (15:52) la petición de carga nunca llegó a la aplicación |
| P-2 | Error en consola: «A listener indicated an asynchronous response by returning true, but the message channel closed…» | Procede de una extensión del navegador (sección 3) |
| P-3 | El log de Render solo muestra el arranque de Uvicorn | Es exactamente lo que hay en el log de Render para el arranque de las 15:52 de hoy (sección 4.5): tras `Uvicorn running…` no hay ninguna línea más hasta `Shutting down` 15 minutos después. Además el código no escribe ningún `logger.info` por etapa y la línea de acceso de uvicorn solo aparece al terminar la respuesta |
| P-4 | Hoy, primera petición al backend sin respuesta en 45 s (16:28), 0,8 s a las 16:29 | Confirmado como arranque en frío: el log muestra `Running 'uvicorn…'` a las 16:28:45 y `Application startup complete` a las 16:29:05; la petición retenida se sirvió a las 16:29:08 (sección 4.5) |

---

## 3. Diagnóstico del error del navegador

```
(index):1 Uncaught (in promise) Error: A listener indicated an asynchronous response by returning true,
but the message channel closed before a response was received
```

| Pregunta | Respuesta | Evidencia |
|---|---|---|
| ¿Procede de una extensión? | **Sí** [HECHO] | El texto es el que emite Chrome cuando un *listener* de `chrome.runtime.onMessage` de una **extensión** devuelve `true` (promete responder) y el canal se cierra antes de responder. Solo el código de extensiones usa esa API |
| ¿Lo genera código de Factu-Match? | **No** [HECHO] | `grep -rn "chrome\.\|browser\.runtime\|postMessage\|addListener\|onMessage" frontend/js/*.js frontend/index.html` no devuelve nada. Los únicos *scripts* externos son `chart.js` y `xlsx` (CDN) y ninguno usa mensajería de extensiones |
| ¿Se relaciona con un *listener*, promesa o canal? | Sí, pero de la extensión, no de la página | `(index):1` indica código inyectado en el documento (sin archivo propio), típico de *content scripts* |
| ¿Es un síntoma secundario? | **Sí** | Aparece cuando la página cambia de estado o espera mucho (p. ej. extensiones de traducción, bloqueadores, gestores de contraseñas, asistentes). No interrumpe el `fetch` de la aplicación |
| ¿Coincide con una petición fallida al backend? | No se puede afirmar | No hay captura de la pestaña *Network* |

**Cómo aislarlo (5 minutos, sin cambios):**
1. Abrir `https://factu-match.vercel.app` en una **ventana de incógnito** (las extensiones están desactivadas por defecto) o en un perfil nuevo de Chrome, y repetir la carga. Si el mensaje desaparece, está confirmado.
2. Si persiste, desactivar extensiones una a una en `chrome://extensions`.
3. En la pestaña *Network* (filtro `conciliar`), anotar estado (*pending*, 200, 502, *failed*), tiempo y tamaño de la respuesta: eso sí describe el bloqueo real.

Fuente: [bobbyhadz — causas del error](https://bobbyhadz.com/blog/a-listener-indicated-asynchronous-response-by-returning-true).

---

## 4. Diagnóstico de Render / backend

### 4.1 Qué sucede después de que el navegador envía el archivo (Ventas)

Trazado con el código actual (sección 5) y reproducido en local con la app real (`main.py` completa) y Odoo simulado:

| Etapa | ¿Se alcanza? | Cómo se sabe |
|---|---|---|
| Recepción del archivo | Sí: `_leer_archivo` copia el multipart a memoria y aplica el tope de 10 MB | Prueba local `/api/ventas/analizar-contenedor` → 200 (1.000 filas) y 413 con >10 MB (prueba unitaria) |
| Validación y lectura | Sí: `leer_contenedor` en hilo (`run_in_threadpool`) | 7.235 filas: 2,7–4,4 s locales |
| Normalización | Sí, dentro de la lectura | ídem |
| Consulta a Odoo | Sí, secuencial: `fields_get`×4 + 1 página de compañías + páginas de 500 de `account.move` y `pos.order` + `read` de contactos | 21 llamadas para 7.235 filas (contadas con Odoo simulado) |
| Comparación y cálculos globales | Sí | 1,9 s locales (conjunto + motor) |
| Respuesta JSON | Sí: un solo JSON con todas las filas | 3,3 MB para 7.235 filas; 0,45 s de serialización |
| Excel | Solo bajo demanda (`/reporte`), en hilo | 6,4 s locales para 7.235 filas |
| Respuesta devuelta | Sí, cuando todo lo anterior termina | Prueba HTTP local completa: 200 |

**Nada en esta cadena falla con 7.235 filas en local.** Lo que no se puede medir desde aquí es cuánto tarda cada página de Odoo en producción.

### 4.2 Configuración y límites reales del servicio en Render [HECHO, leídos con el conector de Render el 05-11-2026]

| Dato | Valor observado | Relevancia |
|---|---|---|
| Servicio | `factu-match` · `srv-d869amh9rddc73br4ekg` · web service Python · región Oregon · `rootDir: backend` · `uvicorn main:app --host 0.0.0.0 --port $PORT` | — |
| **Plan** | **`free`**, 1 instancia, sin *health check path*, *auto-deploy* por commit a `main` | Sin *health check* Render no detecta un proceso colgado |
| **CPU** | métrica `cpu_limit` = **0,15** CPU | 3× menos que *Starter* (0,5) y 6–13× menos que un núcleo: los ≈9,5 s de CPU medidos para 7.235 filas pueden ser 1–2 minutos |
| **Memoria** | métrica `memory_limit` = **512 MB** (536.870.900 bytes) | Una conciliación de 7.235 filas cabe (≈256 MB en el proceso); cinco seguidas o una de 15.000 superan el límite (sección 9.2) |
| Uso real observado (muestras por minuto, 20-10 y 21-10) | memoria entre 100 y **209 MB**; CPU máxima por minuto **0,08** (arranque) y 0,05 durante uso | Ningún agotamiento de memoria en las ventanas con actividad; el uso de CPU observado es compatible con cargas pequeñas, no con una conciliación de 7.235 filas completada |
| Eventos desde el 15-10 | solo `build/deploy` manuales del 15-10 (último *deploy live*: commit `65f205a`, PR #4); **ningún** `server_failed`, `server_restarted` ni OOM | El commit `1708041` del 05-11 (solo frontend y GAS) no generó despliegue: el backend en producción es el del 15-10 |
| Instancias vistas en métricas | 15-10 (2), 19-10, 20-10, 21-10 (3 instancias en 2 h), 05-11 (2) | Cada arranque es una instancia nueva: el servicio pasa la mayor parte del tiempo apagado y arranca por demanda |
| *Spin-down* | documentado: 15 min sin tráfico; arranque «about one minute» ([Render — free instances](https://render.com/docs/free)) | Observado hoy: `Shutting down` 15 min después del arranque sin peticiones; arranque en frío de 16:28:45 a 16:29:05 (más ≈25 s de programación del contenedor) |
| Memoria excedida | «Ran out of memory (used over 512MB)» y reinicio ([foro Render](https://community.render.com/t/server-unhealthy-ran-out-of-memory-used-over-512mb-while-running-your-code/14648)) | No ocurrió en las ventanas observadas; ocurriría con la quinta conciliación de 7.235 filas en una hora o con 15.000 filas |
| Timeout HTTP | 100 minutos ([foro Render](https://community.render.com/t/need-to-increase-requests-timeout-of-5-minutes/4001)) | No es el factor limitante |
| Retención de logs | No se pudo recuperar ninguna línea anterior al 05-11 (consulta desde el 15-10): la retención del plan gratuito es corta | Los intentos de carga anteriores a esta semana ya no son verificables en el log |
| Error en cada arranque | `[startup] Error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed` (`main.py` línea 548: `NEON_DB_URL` no configurada en Render) | No afecta a Ventas ni a Compras por Odoo; deja sin base de datos a los módulos de correo y órdenes de compra y añade ruido al log |

### 4.3 Códigos HTTP y errores controlados (probados)

En producción (05-11-2026 16:29, desde el origen `https://factu-match.vercel.app`, peticiones mínimas sin archivo real) y en local con la app completa: 400 (extensión inválida), 404, 405, 413 (prueba unitaria), 422 (validación, fechas, tolerancia, archivo dañado, sin filas), 502 (Odoo rechaza conexión o agota tiempo), 500 (excepción inesperada). Detalle en `AUDITORIA_TECNICA_QA_FACTUMATCH.md`. **Hallazgo:** las respuestas 500 por excepción no prevista salen **sin cabeceras CORS** (las genera `ServerErrorMiddleware`, por fuera de `CORSMiddleware`), de modo que el navegador las muestra como «error CORS» / «Failed to fetch» en vez de como error del servidor.

### 4.4 Operaciones bloqueantes dentro del bucle de eventos (Compras) [HECHO]

`main.py` → `/odoo/comparar` y `/odoo/descargar-reporte` ejecutan `comparar_facturas_odoo` (pandas `read_excel` + `apply` por fila + `iterrows`) y `generar_excel_reporte` **sin** `run_in_executor`. Con 7.235 filas eso bloquea el servidor completo ≈5 s en la comparación y ≈14 s más en el reporte (medición local, sección 9.3; ≈1,9 ms por fila el reporte, hasta 5 ms por fila cuando hay pocas facturas por proveedor). Ventas no tiene este problema (todo va a hilos).

### 4.5 Cronología del 05-11-2026 en el log de Render (hora de Bogotá) [HECHO]

| Hora | Instancia | Línea de log | Lectura |
|---|---|---|---|
| 15:52:54 | `…-jndr4` | `==> Running 'uvicorn main:app --host 0.0.0.0 --port $PORT'` | Arranque en frío provocado por una petición entrante (es el log que pegó el usuario) |
| 15:53:11 | `…-jndr4` | `Started server process [42]` · `Application startup complete` · `Uvicorn running on http://0.0.0.0:10000` | 17 s de arranque de la app |
| 15:53 → 16:08 | `…-jndr4` | **ninguna línea** | Ninguna petición llegó a la aplicación: ni `POST /api/ventas/analizar-contenedor`, ni `/conciliar`, ni `/odoo/comparar`. La carga que despertó el servicio no fue entregada (el navegador la abandonó, o se recargó la página durante el arranque) |
| 16:08:19 | `…-jndr4` | `Shutting down` · `Finished server process` | *Spin-down* exactamente a los 15 min sin tráfico; el apagado fue limpio (sin peticiones en curso) |
| 16:28:45 | `…-ht2kp` | `==> Running 'uvicorn…'` | Arranque provocado por la primera petición de esta auditoría (enviada ≈16:28:20) |
| 16:29:05 | `…-ht2kp` | `Application startup complete` | — |
| 16:29:08 | `…-ht2kp` | `[startup] Error: connection to server on socket "/var/run/postgresql/…"` y `GET /api/ventas/estado-ia 200` ×3 | La petición retenida ≈45–65 s se sirvió; las siguientes tardaron < 1 s |
| 16:29:09 → 16:29:52 | `…-ht2kp` | 400 / 404 / 405 / 422 / OPTIONS 200 de las pruebas de contrato | Todas las respuestas controladas que esperaba el frontend |

**Conclusión del diagnóstico de Render.** No hay evidencia de caída (OOM, *server_failed*) en el período observable. Hay evidencia directa de (1) plan gratuito con *spin-down* y arranque en frío de ≈1 minuto, (2) CPU limitada a 0,15, (3) un intento de carga hoy que nunca llegó a la aplicación, y (4) un backend que no deja rastro de sus etapas. Lo que el usuario vive como «se bloquea» es, con la evidencia disponible, la suma de arranque en frío + procesamiento lento + ningún feedback; si además el navegador abandona la petición (recarga, cierre), el servidor nunca la procesa y el log se queda en el arranque.

---

## 5. Mapa del flujo actual

Hay dos flujos distintos que leen un archivo DIAN. El síntoma reportado («archivo DIAN de un mes completo, ~7.235 filas») corresponde, por el volumen y por la mención a órdenes POS, al módulo de **Ventas** (documentos emitidos); el módulo de **Compras** (documentos recibidos) se audita también porque comparte servidor, conector de Odoo y el mismo plan de Render, y porque su comportamiento ante carga afecta a todos los usuarios del servicio.

### 5.1 Ventas (`/api/ventas/…`)

| # | Etapa | Dónde (archivo / función) | Entrada → salida | Operaciones costosas | Riesgos |
|---|---|---|---|---|---|
| 1 | Selección del archivo | `frontend/js/ventas_engine.js` → `alElegirArchivo` | `File` del navegador | ninguna: el archivo no se lee en el navegador | — |
| 2 | Validación en frontend | ídem | solo extensión y tamaño implícito | ninguna | sin límite de tamaño en el cliente; el servidor rechaza >10 MB |
| 3 | Envío HTTP (1.ª subida) | `fetch POST /api/ventas/analizar-contenedor` (multipart) | archivo completo | subida del archivo | sin `AbortController` ni timeout; la UI muestra «VALIDANDO…» sin progreso |
| 4 | Recepción en FastAPI | `backend/ventas/router.py` → `_leer_archivo` | `UploadFile` → `bytes` en memoria (tope 10 MB) | copia completa en RAM | — |
| 5 | Lectura del Excel | `dian_emitidos.py` → `leer_contenedor` → `_revisar_xlsx` + `_leer_filas` (openpyxl `read_only`) | bytes → lista de tuplas (todas las filas en memoria) → `DocumentoDian` | parseo XML de openpyxl (≈60 % del tiempo de lectura) | se ejecuta **dos veces** por conciliación (analizar + conciliar) |
| 6 | Limpieza y exclusión | `dian_emitidos.py` → reglas por `Tipo de documento`, `Estado`, `Grupo` | `DocumentoDian.incluido/motivo` | lineal | — |
| 7 | Normalización | `normalizacion.py` (`a_decimal`, `normalizar_nit`, fechas) | strings → `Decimal`, `date` | lineal, `Decimal` por celda | — |
| 8 | Envío HTTP (2.ª subida) + parámetros | `conciliar()` → `fetch POST /api/ventas/conciliar` | archivo completo otra vez + credenciales | subida del archivo | la petición dura todo el proceso (subida + Odoo + cálculo + JSON) sin feedback |
| 9 | Consulta de facturas en Odoo | `odoo_ventas.py` → `extraer_ventas` → `search_read_todo` (`account.move`, páginas de 500, `offset`, `order=id`) | dominio por `move_type`, `company_id`, `invoice_date` ± 5 días → dicts | 1 llamada XML-RPC **secuencial** por página; `fields_get` previo | tope duro: 50.000 registros por modelo (`LecturaTruncada`); paginación por `offset` (coste creciente en Odoo) |
| 10 | Consulta de órdenes POS | ídem sobre `pos.order` por `date_order` (UTC) | dicts | ídem | ídem |
| 11 | Normalización de Odoo | `canonico.py` → `construir_conjunto` | dicts → `DocumentoOdoo`; agrupa POS facturadas con su factura | lineal, índices por `id` | — |
| 12 | Comparación | `motor.py` → `conciliar` (dos pasadas: CUFE, número) | `FilaConciliacion` por documento | diccionarios `por_cufe`, `por_clave`: lineal | — |
| 13 | Cálculos globales | `motor.py` → `_totales`, `_descomponer`, `_por_prefijo`, `secuencias.py` | métricas, descomposición, residuo | lineal (secuencias con topes) | — |
| 14 | Construcción de respuesta | `router.py` → `resultado.a_dict()` → `jsonable_encoder` → `JSONResponse` | **todas** las filas en un solo JSON | recorrido completo del resultado dos veces (dict + encoder) | respuesta de ~0,45 KB por documento; 7.235 docs ≈ 3,3 MB |
| 15 | Almacenamiento temporal | `almacen.py` (`TTLCache`, 5 resultados, 1 h) | `ResultadoConciliacion` en RAM del proceso | — | se pierde al reiniciar; con varias instancias no se comparte |
| 16 | Generación de Excel | `reporte.py` → `generar_reporte` (openpyxl, 10 hojas, estilos compartidos) | resultado → bytes | escritura de ~10 celdas por documento + `_neutralizar_formulas` | corre en hilo (`run_in_threadpool`): no bloquea el servidor |
| 17 | Presentación | `ventas_engine.js` → `pintarGlobal`, `pintarTabla` (50 filas por página), filtros en memoria | JSON → DOM | lineal sobre `documentos` en cada filtro | correcto: no se pintan miles de filas |

Los endpoints de Ventas son `async def` y delegan el trabajo pesado a `run_in_threadpool` (hilos de Starlette, 40 por defecto): **no bloquean el bucle de eventos**. Cada llamada XML-RPC tiene tope de 120 s (`_TransporteConTiempo`), pero no hay tope total por petición.

### 5.2 Compras (`/odoo/comparar`, `/odoo/descargar-reporte`, `/comparar`)

| # | Etapa | Dónde | Observación |
|---|---|---|---|
| 1 | Subida del archivo DIAN de recibidos | `frontend/js/odoo_conector.js` → `fetch POST /odoo/comparar` | sin timeout ni cancelación; el botón se deshabilita mientras dura |
| 2 | Extracción de Odoo | `odoo_match.py` → `fetch_invoices`: `search` (tope 10.000, **silencioso**) + `read` en lotes de 250 enviados al `executor` global de `main.py` (2 hilos) | corre en el executor **y** envía sus lotes al mismo executor (véase hallazgo C-1) |
| 3 | Lectura del Excel y comparación | `comparador.py` → `leer_dian` (pandas `read_excel` + `apply` por fila) y `_ejecutar_comparacion` (`iterrows`) | se ejecuta **dentro del bucle de eventos** (`comparar_facturas_odoo` no va al executor): bloquea el servidor completo mientras dura |
| 4 | Narrativa IA | `generar_narrativa_groq` (httpx, 30 s) | hash `json.dumps` del resultado completo + prompt con todos los proveedores |
| 5 | Respuesta | JSON con todos los proveedores y facturas | sin paginar salvo `limit/offset` opcionales que el frontend no usa |
| 6 | Presentación | `app.js` → `mostrarResultado`: una tarjeta por proveedor y un `span` por factura | **todas** las facturas al DOM, sin paginación |
| 7 | Reporte Excel | `/odoo/descargar-reporte` vuelve a extraer Odoo y a comparar; `generar_excel_reporte` escribe en `/tmp/reporte_comparacion_facturas.xlsx` (ruta fija) y responde con `FileResponse` | generación **en el bucle de eventos**; ruta compartida entre usuarios |

## 6. Archivos y funciones involucrados

| Archivo | Funciones relevantes | Papel |
|---|---|---|
| `frontend/js/ventas_engine.js` | `alElegirArchivo`, `conciliar`, `api`, `pintarTabla`, `filasFiltradas`, `descargarReporte` | UI de Ventas; dos subidas del archivo; sin timeout |
| `frontend/js/odoo_conector.js` | listeners de `btn-odoo-comparar`, `btn-odoo-download` | UI de Compras con Odoo |
| `frontend/js/app.js` | `mostrarResultado`, `callGASRobust`, `switchView` | render de Compras (sin paginación), login |
| `backend/main.py` | `executor = ThreadPoolExecutor(2)`, `/comparar`, `/odoo/comparar`, `/odoo/descargar-reporte`, `generar_narrativa_groq`, `include_router(ventas_router)` | app FastAPI; middleware de auth opcional; CORS abierto |
| `backend/ventas/router.py` | `_leer_archivo`, `_conciliar`, `conciliar_ventas`, `reporte`, `documento`, `investigar` | endpoints de Ventas en hilos |
| `backend/ventas/dian_emitidos.py` | `leer_contenedor`, `_revisar_xlsx`, `_leer_filas`, `_ubicar_encabezado`, `_validar` | lectura y reglas del contenedor; topes 10 MB / 200 MB descomprimido / 100.000 filas / 100 columnas |
| `backend/ventas/odoo_ventas.py` | `OdooSoloLectura.search_read_todo`, `leer`, `extraer_ventas`, `ubicar_compania`, `_autenticar` | extracción paginada y secuencial; tope 50.000 por modelo |
| `backend/ventas/canonico.py` | `construir_conjunto` | normalización de Odoo y agrupación POS/factura |
| `backend/ventas/motor.py` | `conciliar`, `_indexar`, `_descomponer`, `ResultadoConciliacion.a_dict/fila` | motor determinístico |
| `backend/ventas/reporte.py` | `generar_reporte`, `_neutralizar_formulas` | Excel de 10 hojas |
| `backend/ventas/almacen.py` | `guardar`, `obtener` (`TTLCache`) | resultados en memoria del proceso |
| `backend/ventas/config.py` | `cargar_config` | `VENTAS_MAX_RESULTADOS`=5, `VENTAS_TTL_RESULTADOS`=3600, topes del agente |
| `backend/odoo_match.py` | `OdooConnector.authenticate` (sin timeout), `fetch_invoices`, `_read_invoice_batch`, `CredentialManager` | conector de Compras; `ServerProxy` compartido entre hilos |
| `backend/comparador.py` | `leer_dian`, `_ejecutar_comparacion`, `_build_workbook`, `_estilo_detalle`, `generar_excel_reporte` | motor y reporte de Compras (no se modifica) |
| `backend/requirements.txt` | `fastapi==0.111.0`, `uvicorn==0.29.0`, `pandas==2.0.3`, `openpyxl==3.1.2`, `xlrd==2.0.1` | versiones usadas en Render (Python 3.10.11) |

---

## 7. Cuellos de botella confirmados

Cada hallazgo: evidencia, componente, causa, impacto, prioridad (P0 crítico · P1 alto · P2 medio · P3 bajo), solución propuesta y prueba de validación.

### R-1 · CPU lineal pero cara: ≈2,7 ms por fila en Ventas (P1)
- **Evidencia:** flujo HTTP completo con la app real (sección 9.2), 7.235 filas: `analizar-contenedor` 2,0 s + `conciliar` 4,2 s + `reporte` 3,3 s ≈ **9,5 s de CPU** de un núcleo de este equipo; por etapas (sección 10, corrida con contención, hasta 2× más lenta): lectura 2,7–4,4 s (×2 porque `analizar-contenedor` y `conciliar` leen el mismo archivo), extracción 1,8 s de CPU solo en parsear XML-RPC, conjunto 1,0 s, motor 0,9 s, JSON 0,45 s, Excel 6,4 s.
- **Componente:** `dian_emitidos._leer_filas` (openpyxl `parse_cell`: 60 % de la lectura, perfil con cProfile), `reporte.generar_reporte`, `router.conciliar_ventas`.
- **Causa:** openpyxl materializa las 32 columnas de cada fila como objetos Python; el Excel se lee dos veces; el reporte escribe ≈10 celdas con estilo por documento.
- **Impacto:** con el límite real de **0,15 CPU** del servicio (sección 4.2) esos ≈9,5 s se convierten en ≈1–2 minutos de procesamiento, más el arranque en frío y la red de Odoo; con *Starter* (0,5 CPU), en ≈20–30 s. Es el principal factor de «no termina» junto con la ausencia de feedback (R-10).
- **Solución:** leer una sola vez (fase 1); leer solo las columnas usadas (B); generar el Excel en segundo plano y cachearlo (fase 2).
- **Validación:** repetir `bench_ventas.py 7235 --fixture fx` tras el cambio: CPU total < 10 s en este entorno; mismo JSON de resultado.

### R-2 · Memoria: ≈6,3 KB retenidos por documento y hasta 5 conciliaciones en memoria (P1)
- **Evidencia:** servidor real medido en `/proc` (sección 9.2): 7.235 filas → 129 MB base, **256 MB** tras la 1.ª conciliación, 307 MB tras la 2.ª, 356 MB tras la 3.ª (≈50 MB retenidos por conciliación); 15.000 filas → 401 MB tras la 1.ª, **522 MB** tras la 2.ª, 630 MB tras la 3.ª. Por funciones: resultado guardado 46 MB (7.235), 95 MB (15.000), 317 MB (50.000); pico durante el Excel 57 MB (7.235) y 400 MB (50.000).
- **Componente:** `almacen.py` (`TTLCache(maxsize=5, ttl=3600)`), `motor.ResultadoConciliacion` (conserva `LecturaContenedor` y `ConjuntoCanonico` completos), `reporte.generar_reporte`.
- **Causa:** el resultado conserva todos los objetos intermedios (documentos DIAN, canónicos, candidatos) para servir `/documento`, `/reporte` y el agente; cinco resultados pueden convivir una hora.
- **Impacto:** con el límite real de 512 MB (sección 4.2): una conciliación mensual de 7.235 filas cabe; la quinta en una misma hora, o la segunda de 15.000 filas, supera el límite → Render mata el proceso («Ran out of memory») → la petición en curso se corta, el navegador recibe un fallo de red y las conciliaciones guardadas desaparecen. En las métricas de producción el pico observado fue 209 MB, es decir, todavía no ha ocurrido.
- **Solución:** fase 2: guardar solo lo necesario para `/documento` y el reporte (filas compactas + índice), generar el Excel una vez y descartarlo; fase 3: persistir en Postgres y reducir `VENTAS_MAX_RESULTADOS` a 1–2 mientras tanto (variable de entorno, sin código).
- **Validación:** `VmHWM` del proceso servidor tras 5 conciliaciones de 7.235 filas < 60 % del plan.

### R-3 · El Excel se lee dos veces por conciliación (P2)
- **Evidencia:** `ventas_engine.js` → `alElegirArchivo` sube el archivo a `analizar-contenedor`; `conciliar()` lo vuelve a subir y `router._conciliar` llama otra vez a `leer_contenedor`. 2,7–4,4 s extra por conciliación de 7.235 filas y el doble de subida.
- **Causa:** diseño sin estado (el servidor no conserva el análisis).
- **Impacto:** +35 % de CPU y +1,2 MB de subida por conciliación; crece linealmente.
- **Solución:** conservar la lectura unos minutos en memoria indexada por hash del archivo, o hacer el análisis dentro de la ejecución asíncrona (fase 1/2).
- **Validación:** una sola llamada a `leer_contenedor` por conciliación en el log de etapas.

### R-4 · Extracción de Odoo secuencial, por `offset`, sin progreso ni tope total (P1)
- **Evidencia:** `odoo_ventas.search_read_todo`: páginas de 500 con `offset` creciente, una llamada tras otra; 21 llamadas y 12 MB de XML para 7.235 filas, 105 llamadas y 83 MB para 50.000 (medido con Odoo simulado, que sí paga la serialización XML-RPC). Tope de 120 s **por llamada** (`_TransporteConTiempo`), ninguno por petición.
- **Causa:** XML-RPC no soporta flujo; la paginación por `offset` obliga a Odoo a saltar los registros anteriores en cada página; el extractor no informa avance.
- **Impacto:** el tiempo total de Odoo es `n_páginas × latencia`. **[NO VERIFICABLE]** la latencia real por página; con 0,5–3 s típicos de Odoo SaaS: 10–60 s para 7.235 filas y 1–5 min para 50.000. En el peor caso permitido (120 s por llamada) una conciliación de 7.235 filas podría tardar 42 min sin que nada lo corte.
- **Solución:** paginación por `id > último_id` (keyset) manteniendo `order=id`; leer `account.move` y `pos.order` en paralelo con dos conexiones; reducir campos no usados; progreso por página (fase 2); tope total configurable por ejecución.
- **Validación:** número de llamadas igual, tiempo de pared menor; ids únicos y mismo conjunto de registros que con `offset` (prueba con Odoo simulado de 50.000 registros).

### R-5 · Compras bloquea el bucle de eventos del servidor (P1)
- **Evidencia:** `main.py` → `/odoo/comparar` ejecuta `comparar_facturas_odoo(dian_bytes, facturas_odoo)` directamente en la corrutina (línea 211) y `/odoo/descargar-reporte` ejecuta `comparar_facturas_odoo` y `generar_excel_reporte` igual (líneas 250–251). Medición local (`bench_compras.py`): 7.235 filas → lectura+comparación 5,1 s y reporte 13,7 s; 15.000 → 11 s y 26 s; 50.000 → 36 s y 83 s. Perfil: `_estilo_detalle` crea `Font`/`PatternFill`/`Border` nuevos por celda (≈35.000 asignaciones de estilo por 1.000 filas).
- **Causa:** llamadas síncronas en `async def` sin `run_in_executor`.
- **Impacto:** durante esos segundos **ninguna** petición de **ningún** usuario se atiende (ni `GET /`, ni Ventas): el servicio parece caído y los *health checks* pueden fallar. Es independiente del plan.
- **Solución:** mover ambas llamadas a hilo (`run_in_threadpool`) y generar el reporte en memoria con `generar_excel_reporte_bytes`; sin tocar `comparador.py`.
- **Validación:** prueba de aceptación 2 (sección 17).

### R-6 · Interbloqueo del executor de Compras con dos comparaciones simultáneas (P1)
- **Evidencia:** reproducido en local con el código real (`repro_executor_compras.py`): `fetch_invoices` corre en `executor` (2 hilos) y envía sus lotes de 250 al **mismo** executor; con 1 petición termina en 0,3 s; con 2 simultáneas **ninguna responde** (20 s de espera, interbloqueo). Como los hilos quedan ocupados para siempre, cualquier `/comparar` o `/odoo/comparar` posterior también se queda esperando hasta reiniciar el servicio.
- **Componente:** `main.py` (`executor = ThreadPoolExecutor(max_workers=2)`), `odoo_match.fetch_invoices` (parámetro `executor`).
- **Causa:** anidar `executor.submit` dentro de una tarea del mismo pool sin hilos libres.
- **Impacto:** dos usuarios (o un reintento mientras la primera petición sigue viva) dejan Compras «bloqueado».
- **Solución:** no pasar `executor` a `fetch_invoices` (los lotes pasan a ser secuenciales: el `ServerProxy` compartido tampoco es seguro entre hilos) o usar un pool separado y mayor para los lotes.
- **Validación:** `repro_executor_compras.py 2` termina en < 2 s.

### R-7 · Truncamiento silencioso y ruta compartida en Compras (P2)
- **Evidencia:** `fetch_invoices` → `search(limit=10000)`: la factura 10.001 desaparece sin aviso. `generar_excel_reporte` escribe siempre `/tmp/reporte_comparacion_facturas.xlsx`: dos descargas simultáneas se pisan.
- **Impacto:** resultados incompletos sin señal; reporte de otro usuario.
- **Solución:** aviso explícito al alcanzar el tope; reporte en memoria. **Validación:** prueba con Odoo simulado de 10.001 facturas → mensaje; dos descargas concurrentes → cada una recibe su archivo.

### R-8 · Respuestas monolíticas y DOM sin paginar en Compras (P2)
- **Evidencia:** Ventas devuelve todas las filas en un JSON (0,45 KB/doc: 3,3 MB a 7.235, 23 MB a 50.000, 46 MB a 100.000) y el navegador las conserva en memoria (`S.resultado.documentos`), aunque **pinta solo 50 por página** (correcto). Compras (`app.js` → `mostrarResultado`) crea una tarjeta por proveedor y un `span` por factura: 7.235 nodos de golpe.
- **Impacto:** a 7.235 filas es tolerable (3,3 MB, pintado en < 1 s); a 50.000 el JSON (23 MB) y el parseo en el navegador se vuelven el límite.
- **Solución:** resumen completo + filas paginadas/filtradas en servidor cuando haya > 5.000 filas (fase 2); en Compras, paginar proveedores.
- **Validación:** tamaño de `/resultado` < 1 MB por página; mismo total de filas.

### R-9 · Sin observabilidad (P2)
- **Evidencia:** `grep logger\. backend/ventas` → 4 avisos, ninguno de duración; Compras usa `print`. Uvicorn escribe la línea de acceso solo al terminar la respuesta. No hay `request_id`.
- **Impacto:** imposible saber desde el log en qué etapa está una conciliación o cuánto tardó cada una; el log «solo confirma el arranque».
- **Solución:** `logger.info` por etapa con `run_id`, filas, llamadas y segundos, sin datos personales (fase 1).
- **Validación:** una conciliación produce ≥ 6 líneas de etapa con duración.

### R-10 · Frontend sin timeout, progreso ni cancelación (P2)
- **Evidencia:** `ventas_engine.js` → `api()` usa `fetch` sin `AbortController`; `conciliar()` muestra «CONCILIANDO…» hasta que llega la respuesta o falla la red. Igual en `odoo_conector.js`.
- **Impacto:** una petición de varios minutos sin ninguna señal: el usuario la percibe como bloqueo y recarga, lo que (en Compras) puede provocar R-6.
- **Solución:** fase 1: timeout y mensajes; fase 2: progreso por etapa y `run_id` recuperable.

### R-11 · Límites duros que hoy impiden 100.000 documentos (P2, por diseño)
- **Evidencia:** `MAX_ARCHIVO = 10 MB` (un contenedor de 100.000 filas en el formato real pesa ≈16 MB → 413 antes de leerlo); `MAX_FILAS = 100.000`; `search_read_todo(maximo=50000)` → `LecturaTruncada` (reproducido: el fixture de 100.000 filas falla en `pos.order` con 68.340 registros). 50.000 filas (7,9 MB) sí pasan los topes.
- **Impacto:** 100.000 es inviable sin cambiar topes **y** arquitectura (memoria 0,8 GB pico y 634 MB retenidos por resultado, sección 10).
- **Solución:** mantener los topes hasta la fase 3; entonces subirlos con persistencia y pruebas de estrés.

### R-12 · Respuestas 500 sin cabeceras CORS (P2)
- **Evidencia:** prueba local con la app real: una excepción no prevista en `/api/ventas/conciliar` devuelve `500 Internal Server Error` sin `Access-Control-Allow-Origin` (API-14, CORS-07b en `AUDITORIA_TECNICA_QA_FACTUMATCH.md`); los 4xx y 502 controlados sí la llevan.
- **Causa:** `ServerErrorMiddleware` de Starlette envuelve a `CORSMiddleware`; la respuesta de error no pasa por él.
- **Impacto:** el navegador reporta «blocked by CORS policy» en vez del 500 real; el mensaje confunde el diagnóstico.
- **Solución:** manejador de excepciones (`@app.exception_handler(Exception)`) que devuelva JSON 500 genérico pasando por CORS, sin exponer detalles.

---

## 8. Hipótesis pendientes

| ID | Hipótesis | Evidencia a favor | Evidencia que falta | Cómo obtenerla |
|---|---|---|---|---|
| ~~H-1~~ → HECHO | El servicio corre en el plan **free** con 0,15 CPU y 512 MB y se apaga por inactividad | Confirmado con el conector de Render (sección 4.2 y 4.5) | — | — |
| ~~H-2~~ → descartada en el período observable | El proceso se reinició por memoria (OOM) durante una carga | pico de memoria observado 209 MB; ningún evento `server_failed`; el apagado del 05-11 fue un *spin-down* limpio | — (fuera del período de retención no es verificable) | Si vuelve a ocurrir: *Events* del servicio el mismo día |
| H-3 | Una conciliación de 7.235 filas en producción tarda entre 1,5 y 4 minutos: ≈1 min de arranque en frío + ≈1–2 min de CPU a 0,15 + 10–60 s de Odoo | CPU medida (9,5 s a un núcleo) y límite 0,15; 21 llamadas secuenciales a Odoo | el tiempo real: ningún `POST /api/ventas/conciliar` completado aparece en el log retenido | Ejecutar una conciliación con la pestaña *Network* abierta y leer en el log de Render la línea `POST /api/ventas/conciliar … 200`; dividir por `odoo.llamadas` del JSON para obtener la latencia por página |
| H-4 | La petición de carga de hoy no llegó a la aplicación porque el navegador la abandonó durante el arranque en frío (recarga o cierre de la pestaña, o el *fetch* agotó el tiempo del proxy) | arranque 15:52:54 sin ninguna petición posterior; mi petición retenida sí llegó tras ≈65 s | qué hizo exactamente el usuario a las 15:52 | Repetir la carga con *Network* abierto y sin recargar; anotar el estado final de la petición |
| H-5 | El bloqueo reportado ocurrió en el módulo de **Ventas** (no en Compras) | 7.235 filas mensuales con POS solo las produce el contenedor de emitidos; Compras rechaza ese archivo al instante («Grupo = Recibido») | confirmación del usuario | sección 19 |
| H-6 | Si fue Compras con un archivo de recibidos, el bloqueo pudo ser el interbloqueo R-6 o el bucle bloqueado R-5 | ambos reproducidos en local | log con `POST /odoo/comparar` sin línea de respuesta | log de Render de la franja (dentro de la retención) |

---

## 9. Resultados de pruebas por volumen

**Todas son pruebas locales con datos sintéticos** generados por `fixtures.py` (formato idéntico al export real: hoja con `<dimension>`, cadenas compartidas, 32 columnas; 68 % documentos POS, 27 % facturas FE, 3 % notas crédito, 2 % filas excluidas: acuses, nómina y documento soporte) y un Odoo simulado que devuelve páginas reales de `search_read`/`read` serializadas y parseadas con `xmlrpc.client` (paga la CPU de XML-RPC, **no** la latencia de red). Discrepancias plantadas: 0,5 % faltantes en Odoo, 0,3 % diferencias de importe, 0,5 % órdenes POS solo en Odoo. Equipo: 2 CPU / 8 GB; los tiempos variaron ±40 % entre corridas por contención del contenedor (se informa la corrida completa, no la mejor).

### 9.1 Ventas — flujo completo por funciones (`bench_ventas.py`)

| Escenario | Filas | Archivo | Incluidos / excluidos | Registros Odoo leídos | Llamadas Odoo | XML-RPC transferido | Resultado | Residuo |
|---|---|---|---|---|---|---|---|---|
| Pequeño | 1.000 | 167 KB | 980 / 20 | 298 move + 683 POS | 9 | 1,7 MB | completo | 0,00 ✔ |
| **Archivo real (reproducción)** | **7.235** | **1,15 MB** | 7.089 / 146 | 2.160 move + 4.943 POS | **21** | **12,0 MB** | completo | 0,00 ✔ |
| Medio | 15.000 | 2,4 MB | 14.700 / 300 | 4.479 + 10.251 | 36 | 24,9 MB | completo | 0,00 ✔ |
| Alto | 30.000 | 4,8 MB | 29.400 / 600 | 8.959 + 20.502 | 66 | 49,6 MB | completo | 0,00 ✔ |
| Objetivo inicial | 50.000 | 7,9 MB | 49.000 / 1.000 | 14.932 + 34.170 | 105 | 82,7 MB | completo | 0,00 ✔ |
| Estrés (comportamiento real) | 100.000 | 15,9 MB | 98.000 / 2.000 | — | — | — | **rechazado**: supera 10 MB (413) y, si se omite ese tope, `LecturaTruncada` en `pos.order` (68.340 > 50.000) tras 61 s de lectura | — |
| Estrés (topes retirados solo en la prueba) | 100.000 | 15,9 MB | 98.000 / 2.000 | 29.865 + 68.340 | 203 | 165 MB | completo | 0,00 ✔ |

Estados obtenidos a 7.235 filas: 7.072 coinciden, 7 diferencias de valor, 10 faltan en Odoo, 24 faltan en DIAN (plantados + corte), 146 excluidos — exactamente lo plantado. Los conteos cuadran en todos los volúmenes: filas leídas = incluidas + excluidas; documentos en la respuesta = incluidas + solo-Odoo.

### 9.2 Ventas — flujo HTTP completo con la app real (`servidor_bench.py` + `cliente_bench.py`)

Servidor real (`main.py` completo con uvicorn, Odoo simulado por `OdooRapido`), cliente `httpx` que repite el flujo del navegador tres veces seguidas sin reiniciar el proceso, como harían tres conciliaciones de un usuario en una hora. Memoria = `VmRSS`/`VmHWM` del proceso servidor leídos de `/proc` tras cada corrida (baseline antes de la primera: 129 MB con 7.235, 140 MB con 15.000).

| Filas | Corrida | `analizar-contenedor` | `conciliar` (respuesta) | `documento` | `reporte` | Memoria del servidor tras la corrida | Residuo |
|---|---|---|---|---|---|---|---|
| 100 (PERF-01) | 1 | 0,03 s | 0,07 s · 55 KB · 100 docs | — | 0,09 s · 29 KB | — | 0,00 ✔ |
| 7.235 | 1 | 1.98 s | 4.18 s · 3.29 MB · 7259 docs | 23 ms | 3.24 s · 917 KB | 254 MB (pico 256) | 0.00 ✔ |
| 7.235 | 2 | 2.07 s | 4.14 s · 3.29 MB · 7259 docs | 24 ms | 3.33 s · 917 KB | 303 MB (pico 307) | 0.00 ✔ |
| 7.235 | 3 | 2.29 s | 4.25 s · 3.29 MB · 7259 docs | 25 ms | 3.39 s · 917 KB | 354 MB (pico 356) | 0.00 ✔ |
| 15.000 | 1 | 4.19 s | 8.63 s · 6.82 MB · 15051 docs | 49 ms | 6.83 s · 1882 KB | 389 MB (pico 401) | 0.00 ✔ |
| 15.000 | 2 | 4.38 s | 8.9 s · 6.82 MB · 15051 docs | 49 ms | 6.93 s · 1882 KB | 510 MB (pico 522) | 0.00 ✔ |
| 15.000 | 3 | 4.71 s | 9.35 s · 6.82 MB · 15051 docs | 47 ms | 7.27 s · 1882 KB | 618 MB (pico 630) | 0.00 ✔ |

**Lectura.** Con 7.235 filas el flujo completo cuesta ≈9,5 s de CPU en este equipo (2,0 + 4,2 + 3,3) y cada conciliación guardada deja ≈50 MB más en el proceso (129 → 256 → 307 → 356 MB). Con 15.000 filas la **segunda** conciliación ya supera los 512 MB (522 MB) y la tercera llega a 630 MB. Sobre el plan actual de Render (512 MB, sección 4.2) esto significa: una conciliación mensual de 7.235 filas cabe; la quinta en una misma hora (`VENTAS_MAX_RESULTADOS=5`, TTL 1 h) o una de ~15.000 filas repetida mata el proceso.

### 9.3 Compras — `/odoo/comparar` por funciones (`bench_compras.py`, archivo de recibidos sintético, 400 proveedores)

| Filas | Archivo | `leer_dian` (pandas) | `comparar_facturas_odoo` total (en el bucle de eventos) | JSON | Reporte Excel (en el bucle de eventos en `/odoo/descargar-reporte`) | RSS proceso |
|---|---|---|---|---|---|---|
| 1.000 | 65 KB | 0.52 s · 2.6 MB | **0.78 s** | 0.06 MB | **3.52 s** · 60 KB · 4.8 MB | 106 MB |
| 7.235 | 432 KB | 3.45 s · 11.7 MB | **5.07 s** | 0.14 MB | **13.73 s** · 210 KB · 17.7 MB | 143 MB |
| 15.000 | 891 KB | 7.14 s · 23.1 MB | **11.03 s** | 0.24 MB | **25.69 s** · 358 KB · 33.7 MB | 188 MB |
| 30.000 | 1779 KB | 15.09 s · 45.2 MB | **22.19 s** | 0.43 MB | **49.53 s** · 661 KB · 64.8 MB | 278 MB |
| 50.000 | 2965 KB | 24.97 s · 75.1 MB | **36.02 s** | 0.69 MB | **83.07 s** · 1088 KB · 108.9 MB | 407 MB |
| 100.000 | 5934 KB | 50.41 s · 149.2 MB | **75.29 s** | 1.35 MB | **167.55 s** · 2077 KB · 215.4 MB | 707 MB |

Todos los conteos cuadran (faltantes plantados = detectados). La lectura con pandas cuesta ≈0,5 ms por fila y el reporte ≈1,7–1,9 ms por fila; ambos son lineales. Lo grave no es el tiempo sino **dónde** corre: durante esos segundos el bucle de eventos no atiende a nadie (hallazgo R-5). Memoria moderada: Compras no retiene resultados.

### 9.4 Contenedor real (solo en local, agregados)

Se leyó en local el contenedor real que entregó el negocio en octubre (3.178 filas, quincena; el archivo mensual de 7.235 filas no estaba disponible), con el código actual: 630 KB; lectura y validación **1,8 s** (8,7 MB de pico); 3.178 filas leídas = 3.086 incluidas + 92 excluidas, 0 vacías, sin validaciones bloqueantes; reporte Excel 4,1 s (488 KB); respuesta JSON 1,5 MB; resultado retenido 19,4 MB (≈6,1 KB por documento, igual que el sintético). La extracción de Odoo se simuló con un espejo simplificado del propio contenedor, así que los estados de conciliación de esta corrida no son representativos (la validación funcional con el contenedor real y discrepancias plantadas está en `docs/QA_VENTAS.md`, sección 5: todas detectadas, residuo 0). Extrapolación lineal al archivo mensual de 7.235 filas: ≈1,5 MB de archivo, ≈4 s de lectura, ≈45 MB retenidos, 3,4 MB de JSON, lo mismo que mide el fixture sintético.

---

## 10. Tiempos y memoria por etapa (Ventas, local)

| Etapa | 1.000 | 7.235 | 15.000 | 30.000 | 50.000 | 100.000* | Por fila |
|---|---|---|---|---|---|---|---|
| Lectura + validación (`leer_contenedor`) | 0,6 s · 3 MB | 4,4 s · 18 MB | 8,7 s · 37 MB | 18,2 s · 74 MB | 31,1 s · 124 MB | 60,7 s · 247 MB | ≈0,6 ms · 2,5 KB |
| Extracción Odoo (CPU XML-RPC; sin red) | 0,3 s | 1,8 s | 3,7 s | 8,2 s | 12,8 s | 27,1 s | ≈0,25 ms |
| Conjunto canónico | 0,15 s | 1,0 s | 2,2 s | 4,7 s | 8,1 s | 17,1 s | ≈0,16 ms |
| Motor de conciliación (pico memoria) | 0,1 s · 5 MB | 0,9 s · 38 MB | 1,9 s · 78 MB | 3,9 s · 158 MB | 6,3 s · 264 MB | 12,7 s · 529 MB | ≈0,13 ms · 5 KB |
| Respuesta JSON (`a_dict` + `jsonable_encoder` + `dumps`) | 0,06 s · 0,46 MB | 0,45 s · 3,3 MB | 0,8 s · 6,8 MB | 1,7 s · 13,7 MB | 3,1 s · 22,8 MB | 6,1 s · 45,6 MB | ≈0,06 ms · 0,45 KB |
| `/documento` (primera consulta, construye índice) | 2 ms | 21 ms | 42 ms | 93 ms | 156 ms | — | — |
| Reporte Excel (pico memoria) | 0,8 s · 8 MB · 144 KB | 6,4 s · 57 MB · 917 KB | 13,0 s · 117 MB · 1,9 MB | 26,9 s · 233 MB · 3,7 MB | 44,2 s · 400 MB · 6,2 MB | 85,4 s · 799 MB · 12,4 MB | ≈0,9 ms · 8 KB |
| **Resultado retenido en memoria** | 6 MB | **46 MB** | 95 MB | 190 MB | 317 MB | 634 MB | ≈6,3 KB |
| **CPU total conciliación (sin reporte, con doble lectura)** | ≈1,3 s | ≈9,2 s | ≈18 s | ≈37 s | ≈62 s | ≈124 s | ≈1,8 ms + 0,6 ms |

\* 100.000 solo con los topes retirados en la prueba. Memoria = pico de `tracemalloc` de la etapa (objetos Python), no RSS.

**Lectura de la tabla.** Todo crece en línea recta: no hay etapa cuadrática. El coste por fila (≈2,7 ms de CPU y ≈6,3 KB retenidos) es lo que fija la capacidad: con 512 MB y una fracción de CPU, el límite práctico está entre 10.000 y 15.000 filas por conciliación; con 2 GB y 1 CPU, 50.000 filas caben (≈1,1 GB pico medido en el proceso de prueba) pero la petición dura minutos y necesita la arquitectura de la sección 14.

---

## 11. Capacidad actual demostrada y límites no verificados

| | Demostrado (local, sintético) | No verificado |
|---|---|---|
| Exactitud | Residuo 0 y conteos trazables de 1.000 a 100.000 filas | — |
| 7.235 filas | Termina en ≈9 s de CPU + Odoo; 3,3 MB de respuesta; 46 MB retenidos | Tiempo real en Render (CPU del plan) y latencia de Odoo |
| 15.000 filas | Termina; ≈18 s CPU; 95 MB retenidos + 117 MB pico en reporte | Si cabe en 512 MB junto con la base de 108 MB y otros resultados (probablemente no) |
| 50.000 filas | Termina; ≈62 s CPU; 317 MB retenidos; 400 MB pico en reporte; 105 llamadas a Odoo | Inviable en 512 MB; requiere ≥ 2 GB y la fase 2 |
| 100.000 filas | Rechazado por diseño (10 MB / 50.000 por modelo). Con topes retirados: ≈124 s CPU, 0,8 GB pico, 634 MB retenidos | Requiere fase 3 |
| Concurrencia Ventas | Hilos de Starlette (40): varias conciliaciones en paralelo no bloquean el servidor | Memoria acumulada de conciliaciones simultáneas |
| Concurrencia Compras | 2 simultáneas → interbloqueo (R-6); 1 en curso bloquea el bucle (R-5) | — |

---

## 12. Riesgos de integridad

| Riesgo | Estado en el código actual | Evidencia / control |
|---|---|---|
| Perder filas en silencio | **Protegido en Ventas**: `LecturaTruncada` falla en vez de truncar; `MAX_FILAS` rechaza; conteos `filas = incluidas + excluidas + vacías` en la respuesta. **No protegido en Compras**: `search(limit=10000)` trunca sin aviso (R-7) | pruebas `test_lectura_que_supera_el_maximo_falla_en_vez_de_truncar`, `test_maximo_de_filas` |
| Duplicar documentos | Protegido: índices por CUFE y número, POS facturada agrupada con su factura, ids únicos por fila | residuo 0 en todos los volúmenes; prueba aleatoria de 400 escenarios (QA de octubre) |
| Omitir por paginación | Protegido: páginas por `offset` con `order=id` sobre una ventana fija; el conjunto no cambia durante la lectura salvo escrituras concurrentes en Odoo (riesgo inherente, mitigable con keyset) | conteo `registros_leidos` en la respuesta |
| Reglas de exclusión, CUFE/número, notas crédito, totales e impuestos | Sin cambios respecto a la QA de octubre | 192 pruebas de Ventas; 226/227 en esta auditoría |
| Excel coherente con el resumen | Protegido: el reporte se genera del mismo objeto en memoria | QA-11 (octubre) |
| Resultado perdido por reinicio | **No protegido**: `almacen.py` en memoria | mensaje 404 «ya no está en memoria» (probado en producción hoy) |
| Controles de integridad que faltan | Compras no informa filas excluidas por tipo/grupo ni vacías; Ventas sí (`tipos`, `validaciones`, `filas_vacias`) | — |

---

## 13. Alternativas técnicas comparadas

Cada alternativa se evalúa contra el cuello de botella que ataca. «Coste» es esfuerzo relativo (B/M/A) y «Riesgo» el de regresión funcional.

| Alternativa | Qué cuello de botella ataca | Beneficio demostrable | Coste | Riesgo | Veredicto |
|---|---|---|---|---|---|
| **A. Procesamiento por lotes** (leer/normalizar/comparar por bloques) | memoria pico | Bajo en Ventas: lectura, canónico y motor ya son lineales y la memoria pico (≈7 KB por documento) no es el límite hasta ~15.000 filas. Lotear la comparación rompería la identificación en dos pasadas (un CUFE puede emparejar con un registro de otro bloque) | M | **A** (exactitud) | No, como primera medida. Solo tiene sentido para la lectura del Excel (que ya transmite por filas) y para generar el Excel por hojas |
| **B. Lectura optimizada del Excel** (leer solo columnas usadas, no materializar las 32 columnas en tuplas, una sola lectura por conciliación) | CPU (0,4–0,6 ms por fila, ejecutado dos veces) | Elimina la segunda lectura: −35 % del tiempo de CPU del flujo. Reducir columnas ahorra ≈30 % de la lectura (medible con cProfile: `parse_cell` domina) | B | B | **Sí**, fase 1: pasar a `/conciliar` el resultado del análisis o un hash del archivo para no releerlo |
| **C. Consultas paginadas a Odoo** (ya existe; mejorar con paginación por `id > último` en vez de `offset`, menos campos, varias páginas en paralelo con proxies independientes) | latencia de red (1 llamada por 500 registros, secuencial); coste creciente de `offset` en Odoo | Keyset elimina el coste O(offset) en Odoo; 2–3 páginas en paralelo dividen el tiempo de red; el 50 % de los campos de `account.move` no se usan en el motor | B–M | B (solo lectura; la paginación por `id` es exacta porque el orden ya es `id`) | **Sí**, fase 2. No cambiar XML-RPC por JSON-2 todavía: no hay evidencia de que el transporte sea el límite |
| **D. Procesamiento en segundo plano** (POST crea ejecución y devuelve `run_id`; un hilo/worker procesa; el frontend consulta `/estado`; el resultado se descarga aparte) | timeout de la petición, falta de progreso, reinicios de Render, bloqueo percibido | Convierte una petición de minutos en varias de milisegundos; permite progreso por etapa y cancelación; sobrevive a la pestaña cerrada mientras el proceso viva | M | B si el motor no cambia (solo se mueve la llamada) | **Sí**, fase 2–3. En Render con una instancia: un `ThreadPoolExecutor` dedicado dentro del mismo proceso (sin Redis/Celery) basta; un worker separado solo cuando haya persistencia |
| **E. Persistencia y recuperación** (tabla `ejecuciones` en la Postgres que ya usa compras: id, estado, parámetros, progreso, resumen, errores, reporte, inicio/fin) | pérdida del resultado al reiniciar; imposibilidad de escalar a 2 instancias | El resultado deja de depender de la memoria del proceso; el reporte se puede regenerar o guardar como archivo en la BD | M | B | **Sí**, fase 3. Render no tiene disco persistente en free/starter: usar Postgres (ya existe `DATABASE_URL`) |
| **F. Frontend** (progreso real por etapa, cancelación con `AbortController`, estado consultable, resultados paginados desde el servidor) | bloqueo percibido; respuestas de MB | El usuario ve «Leyendo Odoo 3/15 páginas», puede cancelar y recuperar un `run_id` | B–M | B | **Sí**, fase 1 (timeout + mensajes) y fase 2 (progreso) |
| Subir el plan de Render | CPU y memoria | Starter quita el spin-down y da 5× CPU; Standard da 2 GB. Es la medida más rápida, pero no corrige el bloqueo del bucle de eventos en Compras ni la falta de progreso | B (coste mensual) | — | Solo después de confirmar con las métricas de Render que el límite es CPU/memoria (sección 19) |

## 14. Arquitectura recomendada

Objetivo: que una conciliación de 50.000 documentos termine siempre, con progreso visible, sin depender de que la pestaña siga abierta ni de que la instancia no se reinicie, y sin cambiar un solo resultado.

```
Navegador ──POST /api/ventas/conciliar (archivo, parámetros)──► FastAPI
   │            responde en <1 s: {run_id, estado: "en_cola"}            │
   │                                                                      ▼
   │                                               cola en proceso (ThreadPoolExecutor
   │                                               dedicado, 1–2 trabajos a la vez)
   │                                                        │
   │   GET /api/ventas/ejecucion/{run_id} cada 2–3 s        ▼
   ◄── {etapa: "odoo", progreso: "pos.order 7/14", %} ◄── motor actual sin cambios
   │                                                        │ al terminar: resumen +
   │                                                        ▼ filas → Postgres (tabla ejecuciones
   ◄── GET …/resultado?pagina=&filtro=  (JSON por páginas)   y ejecucion_filas) o, en fase 2,
   ◄── GET …/reporte (Excel generado una vez y cacheado)      en memoria como hoy
```

Principios:
1. **El motor no cambia.** `leer_contenedor`, `extraer_ventas`, `construir_conjunto`, `conciliar` y `generar_reporte` se invocan igual; solo cambia quién los llama y cuándo. Las 192 pruebas de Ventas siguen siendo el contrato.
2. **Una sola lectura del Excel por conciliación.** El análisis devuelve un identificador del archivo (hash) y el servidor conserva la lectura unos minutos, o el análisis se hace dentro de la ejecución.
3. **Progreso por etapa** emitido por el extractor (una línea por página de Odoo) y por el motor (inicio/fin de cada etapa), guardado en la ejecución y escrito al log de Render con duración: así el log deja de mostrar solo el arranque.
4. **Resultados paginados** desde el servidor cuando haya más de ~5.000 filas; el resumen, la descomposición y los prefijos siguen viniendo completos (son pequeños).
5. **Persistencia en Postgres** (fase 3) para sobrevivir a reinicios y permitir más de una instancia.
6. **Límites explícitos y visibles**: 10 MB / 100.000 filas / 50.000 registros por modelo se mantienen hasta que las pruebas de estrés demuestren otra cosa; el mensaje al usuario debe decir cuál se alcanzó.
7. **Compras se protege, no se toca**: la única intervención en Compras es mover sus llamadas bloqueantes a hilos y aislar el executor; el comparador y sus pruebas no cambian.

## 15. Plan de modificación por fases

Orden según la prioridad de la sección 12 del prompt de auditoría. Ninguna fase se implementa sin aprobación.

### Fase 0 — Completar la evidencia (sin código; medio día)
- Plan, métricas y eventos de Render ya están leídos (sección 4.2): plan `free`, 0,15 CPU, 512 MB, sin OOM observado.
- Ejecutar la prueba de aislamiento del error del navegador (sección 3).
- Medir **una conciliación real completa** con la pestaña *Network* abierta y sin recargar la página, y leer después en el log de Render la línea `POST /api/ventas/conciliar … 200` con su hora: da el tiempo real de extremo a extremo y, dividiendo por `odoo.llamadas`, la latencia por página de Odoo (H-3).
- Decisión inmediata y reversible, sin código: pasar el servicio a *Starter* (sin *spin-down*, 0,5 CPU) elimina el arranque en frío de ≈1 minuto y triplica la CPU. Con 512 MB sigue siendo necesaria la fase 2 para 15.000+ filas. Alternativa de coste cero mientras tanto: `VENTAS_MAX_RESULTADOS=2` en las variables de entorno para que nunca convivan cinco conciliaciones en memoria.

### Fase 1 — Evitar bloqueo y truncamiento (código pequeño; 2–3 días)
1. **Compras**: ejecutar `comparar_facturas_odoo` y `generar_excel_reporte` en hilo (`run_in_threadpool`) y usar un executor distinto (o `max_workers` ≥ lotes + peticiones) para los lotes de `fetch_invoices`; reporte en memoria (`generar_excel_reporte_bytes`) en vez de la ruta fija en `/tmp`. Sin tocar `comparador.py` ni sus pruebas.
2. **Compras**: avisar cuando `search` alcance el tope de 10.000 facturas en vez de truncar en silencio.
3. **Ventas**: una sola lectura del Excel por conciliación; registro de duración por etapa en el log (`logger.info`), sin datos personales.
4. **Frontend**: `AbortController` con timeout configurable, mensaje claro cuando el servidor no responde y cuando el resultado expiró (reinicio).
5. Pruebas: las existentes + una prueba de concurrencia en Compras (dos comparaciones a la vez deben terminar) + prueba de que el log registra las etapas.

### Fase 2 — Ejecuciones asíncronas con progreso (Ventas; 1–2 semanas)
1. `POST /api/ventas/conciliar` devuelve `run_id` al instante; un `ThreadPoolExecutor` dedicado (1–2 trabajos) ejecuta el flujo actual.
2. `GET /api/ventas/ejecucion/{run_id}`: etapa, progreso (páginas de Odoo leídas / estimadas), duración, error.
3. `GET /api/ventas/resultado`: resumen completo + filas paginadas y filtrables en servidor (los filtros actuales del frontend se mueven al backend sin cambiar su semántica).
4. Extractor: progreso por página y paginación por `id` (keyset); lectura de `account.move` y `pos.order` en paralelo con conexiones independientes (dos `ServerProxy`), manteniendo el orden determinista del resultado.
5. Frontend: barra de progreso por etapa, cancelación, recuperación de la ejecución por `run_id` al recargar.
6. Pruebas: suite actual intacta + pruebas de la cola (estado, cancelación, expiración) + comparación bit a bit del JSON del resultado contra el flujo síncrono para los 280 documentos de QA y los fixtures de 7.235 y 50.000.

### Fase 3 — Persistencia y escala (2 semanas)
1. Tablas `ventas_ejecuciones` y `ventas_filas` en la Postgres existente; el reporte se guarda como bytes o se regenera.
2. Límite de retención configurable; limpieza programada.
3. Pruebas de estrés con fixtures de 50.000 y 100.000 (sección 17) en un servicio de staging del mismo plan que producción.
4. Si 100.000 sigue siendo objetivo: subir `MAX_FILAS`/`LecturaTruncada` solo tras medir memoria con persistencia activa, y elevar el tope de 10 MB a ~20 MB (un contenedor real de 100.000 filas pesa ≈16–20 MB).

### Fase 4 — Opcional
- Migrar el transporte a JSON-2 antes de Odoo 20 (la función `ejecutar` de `OdooSoloLectura` es el único punto de cambio).
- Instancias múltiples en Render (solo con la fase 3 hecha).

## 16. Complejidad y coste operativo relativo

| Fase | Código | Infraestructura | Riesgo funcional | Beneficio |
|---|---|---|---|---|
| 0 | ninguno | ninguna | ninguno | decide las demás |
| 1 | pequeño, localizado | ninguna (o Starter de Render si la fase 0 muestra CPU/memoria como límite) | bajo: no se toca el comparador ni el motor | elimina el bloqueo del servidor en Compras y el interbloqueo; −35 % CPU en Ventas; errores claros |
| 2 | medio (nuevo endpoint de estado, cola, paginación) | ninguna | bajo-medio: el resultado debe ser idéntico; se verifica con comparación JSON | progreso, cancelación, peticiones cortas, 50.000 documentos sin timeout |
| 3 | medio (modelo de datos, migración) | Postgres ya existe | bajo | sobrevive a reinicios; escalable |

## 17. Pruebas de aceptación

Por fase, además de la suite actual (227 pruebas, 226 pasan, 1 falla previa de Compras que no se toca):

1. **Integridad** (toda fase): para los fixtures de 280 (QA), 7.235 y 50.000 documentos, el JSON de resultado (`documentos`, `descomposicion`, `metricas`, `prefijos`, `alcance`) es idéntico al del flujo actual, salvo `run_id` y tiempos. Residuo 0 en los fixtures sintéticos; sin filas perdidas: filas recibidas = leídas + vacías; leídas = incluidas + excluidas; incluidas = conciliadas (cualquier estado).
2. **Concurrencia en Compras** (fase 1): dos `/odoo/comparar` simultáneos con Odoo simulado terminan ambos en < 10 s; durante un `/odoo/descargar-reporte` de 7.235 filas, `GET /` responde en < 100 ms.
3. **Sin bloqueo del servidor** (fase 1): durante una conciliación de Ventas de 15.000 filas, `GET /api/ventas/estado-ia` responde en < 200 ms.
4. **Progreso** (fase 2): la ejecución pasa por los estados `en_cola → leyendo → odoo → conciliando → reporte → lista` y el progreso de Odoo avanza página a página; cancelar deja el estado `cancelada` y libera memoria.
5. **Recuperación** (fase 2/3): cerrar la pestaña y reabrir con el `run_id` muestra el mismo resultado; tras reiniciar el servicio (fase 3) el resultado sigue disponible.
6. **Volumen** (fase 3, staging con el plan de producción): 7.235 filas < 2 min de extremo a extremo con Odoo real; 50.000 filas terminan sin reinicio y con memoria < 70 % del plan; 100.000 filas producen un mensaje de límite claro o terminan, según el objetivo aprobado.
7. **Regresión de Compras**: `test_comparador.py`, `test_odoo_match.py`, `test_groq_client.py` con el mismo resultado que hoy (34 pasan, 1 falla previa) y una comparación real de Compras con el mismo archivo antes y después produce el mismo reporte.

## 18. Riesgos de regresión y protección

| Riesgo | Protección |
|---|---|
| Cambiar resultados al mover el motor a una cola | Comparación JSON bit a bit contra el flujo actual (prueba 1) antes de retirar el endpoint síncrono; mantener ambos durante una versión |
| Paginar en servidor y perder filas en el frontend | El resumen trae `documentos_dian_elegibles` y el total de filas; la prueba 1 verifica que la suma de páginas = total |
| Paralelizar Odoo y alterar el orden o duplicar registros | Keyset por `id` por modelo; cada modelo en su propio hilo; `assert` de ids únicos antes de construir el conjunto |
| Tocar Compras al aislar el executor | Solo `main.py` cambia; `comparador.py` y `odoo_match.py` intactos; pruebas de Compras sin cambios de expectativas |
| Persistencia que guarde datos personales | Guardar lo mismo que hoy se devuelve al navegador (nombres de receptor sí, nada más); cifrar o purgar según retención |
| Subir topes (50.000 / 100.000) sin memoria suficiente | Solo tras medir en staging con el plan real; mantener `LecturaTruncada` como error explícito |

## 19. Información adicional requerida del usuario o de Render

Ya obtenido con el conector de Render (05-11-2026): plan, límites de CPU y memoria, métricas de uso, eventos y log del servicio (secciones 4.2 y 4.5). Queda pendiente:

1. **Fecha y hora exactas** de los intentos de carga anteriores a hoy: el log de Render del plan gratuito ya no conserva nada anterior al 05-11, así que esos intentos no son verificables; los próximos sí lo serán si se anota la hora.
2. **Pestaña Network del navegador** durante una carga completa sin recargar: estado final de `analizar-contenedor` y `conciliar` (200 / 502 / *failed* / *canceled*), tiempo y tamaño de la respuesta. Es la evidencia que distingue «el navegador abandonó» de «el servidor no respondió».
3. **Cuál módulo** se estaba usando (Ventas: pestaña «Ventas DIAN vs Odoo»; Compras: «Comparaciones → API Odoo») y si había otra comparación en curso en otra pestaña o por otro usuario.
4. **El archivo de 7.235 filas** (o uno equivalente del mismo tipo de export) para medirlo tal cual; solo se usaría en local y no se versiona.
5. **Latencia real de Odoo**: tiempo de una conciliación completa (hora de inicio y línea `POST /api/ventas/conciliar … 200` en el log) para calcular segundos por página de 500 registros.
6. **Extensiones de Chrome instaladas** (para la prueba de aislamiento del error del navegador).
7. Confirmar el objetivo de 100.000 documentos: hoy el tope de 10 MB del archivo lo impide (un contenedor de 100.000 filas pesa ≈16–20 MB) antes que cualquier otro límite.
8. Decisión sobre el plan de Render (*Starter* elimina el *spin-down*; *Standard* da 2 GB y 1 CPU) y sobre `NEON_DB_URL`, ausente en producción (los módulos de correo y órdenes de compra no tienen base de datos).
