# AUDITORÍA TÉCNICA Y QA — FACTU-MATCH

**Fecha:** 05-11-2026 · **Commit auditado:** `1708041` (`main`) · **Backend en producción:** commit `65f205a` (último *deploy* en Render, 15-10-2026; el commit `1708041` solo cambió frontend y GAS y no generó despliegue) · **Frontend:** `https://factu-match.vercel.app` · **Backend:** `https://factu-match.onrender.com` (Render, plan `free`, 0,15 CPU, 512 MB, Oregon).

**Alcance:** primera fase, solo auditoría. No se modificó código, variables de entorno, configuración, base de datos ni despliegues. Contra producción solo se enviaron peticiones mínimas de contrato (sin archivos reales, sin Odoo); el volumen se midió en local con datos sintéticos. El detalle de rendimiento está en `AUDITORIA_RENDIMIENTO_FACTU_MATCH.md`; este documento no lo repite, lo referencia.

Convenciones: **HECHO** (observado), **CONFIRMADA / PROBABLE / NO DETERMINADA** (estado de causa), **PASS / FAIL / BLOCKED / NOT RUN** (estado de prueba). Horas en América/Bogotá salvo que se indique UTC.

---

## 1. Resumen ejecutivo

| Incidente | Estado | Conclusión |
|---|---|---|
| **A — 404 + «CORS» en `POST /api/ventas/analizar-contenedor`** | Hoy **no reproducible**: la ruta existe en el backend desplegado, responde `POST` y lleva cabeceras CORS en 200, 400, 404, 405 y 422 (probado desde el origen real de Vercel). | El 404 *sin* cabecera `Access-Control-Allow-Origin` que vio el usuario **no lo produjo esta aplicación**: `CORSMiddleware` añade la cabecera incluso a los 404 (probado). La explicación más probable es que en ese momento el backend desplegado era anterior al módulo de Ventas o la petición no llegó a la app (sección 6). Causa: **PROBABLE**, no confirmable porque el log de Render ya no conserva esa fecha. |
| **B — Carga mensual de 7.235 filas «se bloquea»** | **Reproducido el flujo en local** con la app real y un fixture del mismo formato: termina bien (≈9,5 s de CPU + Odoo). En producción: plan gratuito con *spin-down*, 0,15 CPU y ninguna señal de progreso. En el único arranque registrado hoy (15:52) **ninguna petición llegó a la aplicación**. | Causa del «bloqueo» percibido: **PROBABLE** = arranque en frío (≈1 min) + CPU limitada + petición única sin feedback, con el navegador abandonando la petición. No hay evidencia de caída del proceso (sin OOM ni `server_failed`). |

Defectos nuevos encontrados y confirmados por prueba (sección 10): interbloqueo en Compras con dos comparaciones simultáneas (P1), bucle de eventos bloqueado por pandas/openpyxl en Compras (P1), 500 sin cabeceras CORS (P2), CORS abierto a cualquier origen y API sin autenticación obligatoria (P2), truncamiento silencioso a 10.000 facturas y ruta de reporte compartida en Compras (P2), mensaje incorrecto para un contenedor sin filas (P3), `NEON_DB_URL` ausente en producción (P3), `/comparar` devuelve 500 ante errores de entrada (P3).

Compras sigue funcionando: 34 de 35 pruebas de Compras pasan (la que falla ya fallaba antes del módulo de Ventas) y los endpoints `/comparar`, `/odoo/comparar` y `/odoo/descargar-reporte` responden 200 en local con datos sintéticos (sección 15).

---

## 2. Inventario de endpoints y contratos

Obtenido de la app real (`main.py` + `ventas/router.py`) y del `/openapi.json` desplegado (26 rutas; las 6 de Ventas presentes). Todas las rutas son `async def`; «hilo» indica que el trabajo pesado se delega (no bloquea el servidor) y «bucle» que corre dentro del bucle de eventos.

| Ruta | Método | Entrada | Validación | Éxito | Errores observados | Tiempo medido | Dependencias | Seguridad | Ejecución |
|---|---|---|---|---|---|---|---|---|---|
| `/` | GET | — | — | 200 `{"status":"ok"}` | — | < 0,2 s (prod) | — | pública | sync (hilo) |
| `/api/ventas/analizar-contenedor` | POST | multipart `dian` (.xlsx/.xls ≤ 10 MB) | extensión, tamaño, ZIP, filas ≤ 100.000, columnas, tipos | 200 JSON (conteos, tipos, validaciones, período sugerido) | 400 extensión · 400 multipart vacío · 413 tamaño · 422 archivo dañado / columnas faltantes | 2,0 s (7.235 filas, local) | ninguna | sin auth obligatoria; CORS `*` | hilo |
| `/api/ventas/conciliar` | POST | multipart `dian` + `credentials`, `desde`, `hasta`, `incluir_move`, `incluir_pos`, `tolerancia` | fechas AAAA-MM-DD, ≤ 93 días, tolerancia 0–1000 finita, ≥ 1 fuente, un solo NIT emisor | 200 JSON completo (`run_id`, `documentos[]`, `descomposicion`, `metricas`, `prefijos`, `alcance`, `odoo`) | 400 credenciales inválidas · 422 validación · 502 Odoo inaccesible / timeout · 500 genérico | 4,2 s + Odoo (7.235, local) | Odoo XML-RPC (solo lectura) | credenciales cifradas Fernet en el cuerpo; CORS `*` | hilo |
| `/api/ventas/documento` | GET | `run_id`, `ref` | — | 200 JSON evidencia | 404 conciliación expirada / documento inexistente | 23 ms | memoria del proceso | — | bucle (rápido) |
| `/api/ventas/reporte` | GET | `run_id` | — | 200 `.xlsx` (stream) | 404 expirada | 3,3 s (7.235) | memoria del proceso | — | hilo |
| `/api/ventas/investigar` | POST | `run_id`, `foco_*`, `pregunta` ≤ 500, `credentials` | — | 200 siempre (`disponible:false` si Claude falla) | 404 expirada · 409 en curso · 422 pregunta larga · 429 cupo | no medido (Claude simulado en QA) | Anthropic API, Odoo (20 s/llamada) | clave solo en servidor | hilo |
| `/api/ventas/estado-ia` | GET | — | — | 200 JSON (sin clave) | — | 0,2–0,8 s (prod, caliente); > 45 s (prod, arranque en frío) | — | — | bucle |
| `/comparar` | POST | multipart `dian` + `siesa` (≤ 50 MB), `limit`, `offset` | columnas requeridas | 200 JSON proveedores + narrativa | **500** para archivos inválidos (mensaje interno) | 0,1 s (300 filas) | Groq (opcional, 30 s) | pública; CORS `*` | executor (2 hilos) |
| `/descargar-reporte` | POST | ídem | ídem | 200 `.xlsx` | 500 | — | — | — | executor |
| `/odoo/test-connection` | POST | `credentials` JSON en claro | — | 200 `{success}` | 200 con `success:false` | — | Odoo | credenciales en claro en el cuerpo | **bucle** (XML-RPC sin timeout) |
| `/odoo/save-connection` | POST | ídem | prueba de conexión | 200 cifrado | 400 / 500 | — | Odoo | — | **bucle** |
| `/odoo/comparar` | POST | multipart `dian` + `credentials`, `date_from`, `date_to` | fechas | 200 JSON | **500** archivo de emitidos («Grupo = Recibido») · 500 Odoo | 5,1 s de **bucle bloqueado** (7.235) | Odoo, Groq | — | extracción en executor; **comparación en el bucle** |
| `/odoo/descargar-reporte` | POST | ídem | ídem | 200 `.xlsx` (`FileResponse` de `/tmp/…` fijo) | 500 | +13,7 s de **bucle bloqueado** (7.235) | Odoo | ruta compartida | **bucle** |
| `/api/run-ai-checklist` | POST | `groq_key`, `credentials`, `odoo_url`, `odoo_db`, `odoo_user`, `odoo_pass` | — | 200 | — | — | Groq, Odoo | **recibe contraseña de Odoo y clave Groq en el cuerpo** | — |
| `/api/purchase/*`, `/sync-emails*`, `/api/dashboard-auditoria`, `/api/sync-odoo-invoices`, `/conciliar-ia`, `/api/ai/test-groq` | varios | — | — | — | no probados; los que usan Postgres previsiblemente fallan en producción porque `NEON_DB_URL` no está configurada (`[startup] Error` en cada arranque) | — | Postgres (Neon), IMAP, Groq | — | — |

**Contrato frontend ↔ backend (Ventas):** `ventas_engine.js` consume exactamente los campos que devuelve `ResultadoConciliacion.a_dict()` (`documentos[].estado/numero/tipo/fecha/total_dian/total_odoo/diferencia_total/metodo/odoo_nombre/nota/id`, `descomposicion.*`, `metricas[]`, `prefijos[]`, `alcance[]`, `alertas[]`, `etiquetas_estado`); comprobado en la QA de punta a punta (21 casos, sección 15). Los errores HTTP se leen como JSON `{"detail": …}` y, si el cuerpo no es JSON, se muestra `Error <status>` (`api()` en `ventas_engine.js`): no intenta interpretar HTML como JSON. En Compras, `odoo_conector.js` hace `await res.json()` **antes** de comprobar `res.ok`: una respuesta HTML (p. ej. la página de arranque de Render o un 502 del proxy) produce «Unexpected token <» en vez del mensaje real.

---

## 3. Reproducción de cada incidente

### Incidente A (404 + CORS)

| Paso | Resultado | Evidencia |
|---|---|---|
| `GET /openapi.json` en producción (16:29) | 200; `paths` incluye las 6 rutas `/api/ventas/*`; `analizar-contenedor` solo acepta `post` | prueba desde el origen `https://factu-match.vercel.app` (Chrome del usuario) |
| `POST /api/ventas/analizar-contenedor` sin archivo, desde Vercel | **400** `{"detail":"There was an error parsing the body"}` (multipart vacío), la promesa `fetch` se resolvió → cabeceras CORS presentes | ídem |
| `POST` con `.pdf` / `.xlsx` corrupto | 400 / 422 con mensaje claro | ídem |
| `GET /api/ventas/no-existe` desde Vercel | **404** `{"detail":"Not Found"}` y la promesa se resolvió → **el 404 de esta app sí lleva cabecera CORS** | ídem y en local (`access-control-allow-origin: *` visible) |
| `OPTIONS` (preflight forzado con cabecera personalizada) | 200 | ídem; log de Render: `OPTIONS /api/ventas/estado-ia 200` |
| Log de Render 16:29:09–16:29:52 UTC-5 | todas las peticiones anteriores registradas con su código | conector de Render |

**No se pudo reproducir el 404.** La ruta está registrada (`app.include_router(ventas_router)` en `main.py` línea 69; prefijo único `/api/ventas` definido en `APIRouter(prefix=…)`), el método es `POST`, Vercel apunta a `https://factu-match.onrender.com` (`API_URL` en `app.js`) y el despliegue vivo contiene el módulo (commit `65f205a`).

### Incidente B (7.235 filas)

| Paso | Resultado | Evidencia |
|---|---|---|
| Fixture sintético de 7.235 filas con el formato real (32 columnas, `<dimension>`, cadenas compartidas) y Odoo simulado | `analizar-contenedor` 200 en 2,0 s; `conciliar` 200 en 4,2 s (3,3 MB, 7.259 documentos, 21 llamadas a Odoo, residuo 0,00); `documento` 23 ms; `reporte` 200 en 3,3 s (917 KB) | `servidor_bench.py` + `cliente_bench.py` (app real `main.py` bajo uvicorn), tres corridas consecutivas |
| Memoria del proceso servidor | 129 MB → 256 → 307 → 356 MB tras 1, 2 y 3 conciliaciones | `/proc/<pid>/status` |
| Mismo flujo con 15.000 filas | 200 en 4,2 + 8,6 + 6,8 s; memoria 401 → 522 → 630 MB | ídem: la 2.ª conciliación supera los 512 MB del plan |
| Producción, log de Render del 05-11 | 15:52:54 arranque en frío (el log que pegó el usuario); 15:53 → 16:08 **sin ninguna petición**; 16:08:19 *spin-down*; 16:28:45 nuevo arranque por la primera petición de esta auditoría, servida a las 16:29:08 tras > 45 s de espera | conector de Render (`list_logs`) |
| Producción, métricas | plan `free`, `cpu_limit` 0,15, `memory_limit` 512 MB; pico de memoria observado 209 MB (20-10); CPU máxima por minuto 0,08; sin eventos de fallo ni OOM desde el 15-10 | conector de Render (`get_service`, `get_metrics`, `list_events`) |
| Error del navegador («listener… message channel closed») | el frontend no usa `chrome.runtime`/`postMessage`; procede de una extensión | `grep` en `frontend/`; prueba de aislamiento pendiente en incógnito |

---

## 4. Evidencias

| Tipo | Dónde |
|---|---|
| Código | `backend/main.py` (líneas 27–32 CORS, 48 executor, 57–65 auth opcional, 187–259 Compras-Odoo, 461–548 startup DB), `backend/ventas/router.py`, `backend/odoo_match.py` (137–295 `fetch_invoices`), `backend/comparador.py` (133–196 `leer_dian`, 488–531 `_estilo_detalle`), `frontend/js/ventas_engine.js` (50–58 `api`, 107–131, 174–212), `frontend/js/odoo_conector.js` (355–358), `frontend/js/app.js` (1 `API_URL`) |
| Pruebas automáticas | `cd backend && python -m pytest -q` → 226 pasan, 1 falla previa (`test_comparador.py::TestNormalizacionDeFormatos::test_nit_sin_dv_en_dian_coincide_con_nit_con_dv_en_erp`), 05-11 16:08 |
| QA de punta a punta | `python qa/ventas/qa_ui.py` → 21 pasan · 0 fallan, 05-11 16:49 (`qa/ventas/salida/resultados_qa.json`, capturas QA-01…QA-19) |
| Pruebas de contrato en producción | 05-11 16:28–16:30 desde `https://factu-match.vercel.app` (Chrome del usuario, `fetch`): tabla de la sección 8 |
| Pruebas de contrato en local | `pruebas_api_local*.py` con `TestClient(main.app)`: `matriz_api.jsonl`, `matriz_api_2.jsonl` |
| Reproducción del interbloqueo | `repro_executor_compras.py 2` → sin respuesta tras 20 s; con 1 petición 0,3 s |
| Volumen | `bench_ventas.py`, `bench_compras.py`, `fixtures.py` (sintético), `resultados*.jsonl`; tablas en `AUDITORIA_RENDIMIENTO_FACTU_MATCH.md` §9–10 |
| Render | `get_service`, `get_metrics`, `list_events`, `list_logs`, `list_deploys` leídos el 05-11 16:36–16:40 |

Los scripts de prueba viven fuera del repositorio (carpeta de trabajo de la auditoría) y no usan datos reales; se pueden incorporar a `qa/` si se aprueba.

---

## 5. Causa raíz confirmada o estado de hipótesis

| Observación | Causa | Estado |
|---|---|---|
| 404 sin cabeceras CORS en `analizar-contenedor` (incidente A) | La respuesta no la generó la app actual (sus 404 llevan CORS). Backend desplegado anterior al módulo o petición no entregada por el proxy de Render durante un arranque | **PROBABLE** (no verificable: sin log de esa fecha) |
| «Se bloquea» con 7.235 filas (incidente B) | Arranque en frío del plan `free` (≈1 min) + CPU 0,15 + petición única sin progreso ni timeout; en el arranque del 05-11 la petición nunca llegó a la app | **PROBABLE** para la percepción; **CONFIRMADO** que el flujo termina en local y que el plan es `free` con *spin-down* |
| Error «listener… message channel closed» | Extensión de Chrome | **CONFIRMADA** (código del frontend sin mensajería de extensiones) |
| Log de Render «solo arranque» | Sin `logger.info` por etapa; uvicorn solo registra al responder; además no llegó ninguna petición | **CONFIRMADA** |
| Interbloqueo en Compras con 2 peticiones | `fetch_invoices` envía lotes al mismo executor de 2 hilos en el que corre | **CONFIRMADA** (reproducida) |
| Servidor sin respuesta durante Compras | `comparar_facturas_odoo` y `generar_excel_reporte` en el bucle de eventos | **CONFIRMADA** (código + medición) |
| 500 sin CORS | `ServerErrorMiddleware` por fuera de `CORSMiddleware` | **CONFIRMADA** (prueba local) |

---

## 6. Diagnóstico del 404 y CORS por separado

**404.** Hoy la ruta existe, con `POST`, prefijo único, router incluido y presente en `/openapi.json` del backend desplegado. Un 404 solo pudo venir de (a) un despliegue anterior al 15-10 20:48 UTC (antes de ese *deploy* el backend no tenía `/api/ventas/*`; el frontend de Vercel se publica en segundos y el de Render tarda minutos: durante esa ventana el frontend nuevo llamaba a un backend viejo), o (b) una respuesta del proxy de Render, no de la app. La fecha del incidente no está en el documento; el log de Render ya no conserva nada anterior al 05-11.

**CORS.** Configuración actual: `allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]`, sin credenciales (`main.py` 27–32). Probado desde el origen real: 200, 400, 404, 405, 422 y el preflight `OPTIONS` llevan cabeceras CORS. **La única respuesta de la app sin cabeceras CORS es el 500 por excepción no controlada** (sección 10, D-3). Conclusión: el mensaje «No 'Access-Control-Allow-Origin' header is present» del incidente fue **consecuencia** de que la respuesta 404 no la generó esta app; no hay un problema de CORS independiente. Dos observaciones de seguridad, no de funcionamiento: cualquier origen puede llamar a la API (probado desde `https://example.com`: 200) y la autenticación (`API_AUTH_TOKEN`) es opcional y está desactivada; ambas son configuración previa al módulo de Ventas.

---

## 7. Diagnóstico de la carga de 7.235 filas

Trazado completo, etapa por etapa, en `AUDITORIA_RENDIMIENTO_FACTU_MATCH.md` §4–5 y §9–10. Resumen de lo medido para 7.235 filas (local, sintético, app real): archivo 1,15 MB; `analizar-contenedor` 2,0 s; `conciliar` 4,2 s (lectura 2,7–4,4 s + Odoo 1,8 s de CPU en 21 llamadas + conjunto 1,0 s + motor 0,9 s + JSON 0,45 s); respuesta 3,3 MB con 7.259 documentos; `reporte` 3,3 s y 917 KB; memoria +127 MB en la primera conciliación y +50 MB por cada una siguiente; residuo 0,00; filas 7.235 = 7.089 incluidas + 146 excluidas; conteos de discrepancias iguales a los plantados. En producción, con 0,15 CPU y arranque en frío, el tiempo esperado es de 1,5 a 4 minutos sin ninguna señal en pantalla ni en el log (hipótesis H-3 del informe de rendimiento, pendiente de una medición real con la pestaña *Network* abierta).

---

## 8. Matriz QA

Entorno **PROD** = `https://factu-match.onrender.com` desde el origen `https://factu-match.vercel.app` (05-11 16:28–16:30, Chrome del usuario). Entorno **LOCAL** = app real `backend/main.py` con `TestClient`/uvicorn, commit `1708041`, Odoo simulado (05-11 16:31–16:45). Datos: sin archivos reales en PROD; fixtures sintéticos en LOCAL.

### 8.1 Endpoints

| ID | Escenario | Esperado | Obtenido | Entorno | Estado |
|---|---|---|---|---|---|
| API-01 | Ruta válida, `POST analizar-contenedor` con contenedor de 1.000 filas; `POST conciliar` completo | 200 según contrato | 200 (conteos) · 200 (`run_id`, descomposición, residuo 0,00) | LOCAL (+ `openapi.json` en PROD) | **PASS** |
| API-02 | `GET /api/ventas/no-existe` | 404 claro | 404 `{"detail":"Not Found"}` con CORS | PROD + LOCAL | **PASS** |
| API-03 | `GET /api/ventas/conciliar` | 405 | 405 `Method Not Allowed` | PROD + LOCAL | **PASS** |
| API-04 | `POST conciliar` con cuerpo vacío | validación controlada | 422 campos faltantes | LOCAL | **PASS** |
| API-05 | `POST conciliar` con JSON mal formado (`{mal`) | error controlado sin traceback | 422 (el endpoint espera multipart; sin traceback) | PROD + LOCAL | **PASS** |
| API-06 | `POST conciliar` sin `credentials/desde/hasta` | 422 | 422 lista de campos | PROD + LOCAL | **PASS** |
| API-07 | Fechas `31/10/2026`/`ayer`; tolerancia `NaN` | validación clara | 422 «Las fechas deben tener formato AAAA-MM-DD» · 422 «Tolerancia inválida» | PROD + LOCAL | **PASS** |
| API-08 | `POST analizar-contenedor` sin archivo | error controlado | LOCAL 422 `Field required`; PROD (multipart vacío) 400 «There was an error parsing the body» | ambos | **PASS** (mensaje de PROD poco claro: D-9) |
| API-09 | Archivo `.pdf` | rechazo controlado | 400 «debe ser un archivo .xlsx o .xls» | PROD + LOCAL | **PASS** |
| API-10 | `.xlsx` corrupto | error controlado, sin caída | 422 «el archivo parece dañado»; el proceso siguió atendiendo | PROD + LOCAL | **PASS** |
| API-11 | Contenedor con las 32 columnas y 0 filas | resultado explícito | `analizar`: 200 con `filas: 0`; `conciliar`: 422 **«El contenedor mezcla varios NIT emisores»** (mensaje incorrecto) | LOCAL | **FAIL** (D-8) |
| API-12 | Odoo no disponible (credenciales cifradas reales apuntando a `127.0.0.1:9`) | error de dependencia claro | 502 «No se pudo conectar a Odoo (ConnectionRefusedError)» con CORS | LOCAL | **PASS** |
| API-13 | Timeout de Odoo durante la extracción (`socket.timeout` simulado) | timeout controlado y log identificable | 502 «No se pudo leer Odoo (TimeoutError): intenta de nuevo»; en el backend no hay registro propio del timeout (por código: solo la línea de acceso `502` de uvicorn, sin etapa ni `run_id`) | LOCAL | **PASS** parcial (log no identificable: D-7) |
| API-14 | Excepción interna inesperada en la extracción (con texto `secreto=sk-ant-xxxx`) | 500 sin secretos ni stack trace | 500 `Internal Server Error` genérico, sin secreto ni traceback; **sin cabeceras CORS** | LOCAL | **PASS** con hallazgo D-3 |

### 8.2 CORS

| ID | Escenario | Esperado | Obtenido | Entorno | Estado |
|---|---|---|---|---|---|
| CORS-01 | `GET estado-ia` con `Origin` de Vercel | cabeceras correctas | 200 y `access-control-allow-origin: *` (LOCAL); `fetch` resuelto desde Vercel (PROD) | ambos | **PASS** |
| CORS-02 | Preflight `OPTIONS` (`Access-Control-Request-Method: POST`, `-Headers: content-type`) | respuesta válida | 200 `OK` con `allow-origin: *`; en PROD el preflight forzado con cabecera personalizada pasó | ambos | **PASS** |
| CORS-03 | `POST` permitido | habilitado | `POST analizar-contenedor` desde Vercel resuelto (400/422 según cuerpo) | PROD | **PASS** |
| CORS-04 | Cabeceras reales del frontend | permitidas | el frontend no envía cabeceras personalizadas (multipart y GET simples, sin `Authorization`); `allow_headers=["*"]` | PROD + código | **PASS** |
| CORS-05 | Origen no autorizado (`https://example.com`, `https://sitio-ajeno.example`) | no conceder | **concedido**: 200 con `allow-origin: *` | PROD + LOCAL | **FAIL** (D-4, configuración previa) |
| CORS-06 | Respuesta 404 | comportamiento documentado | 404 **con** cabeceras CORS | PROD + LOCAL | **PASS** |
| CORS-07 | Respuestas 422 / 502 / 500 | cabeceras coherentes | 422 y 502 con CORS; **500 sin CORS** | LOCAL | **FAIL** parcial (D-3) |

### 8.3 Volumen y rendimiento (LOCAL, sintético, app real; detalle en el informe de rendimiento §9)

| ID | Filas | Archivo | Válidas / excluidas | `analizar` | `conciliar` | Respuesta | Llamadas Odoo | Reporte | Memoria del proceso | Residuo | Estado |
|---|---|---|---|---|---|---|---|---|---|---|---|
| PERF-01 | 100 | 16 KB | 98 / 2 | 0,03 s | 0,07 s · 200 | 55 KB | 8 | 0,09 s · 29 KB | — | 0,00 | **PASS** |
| PERF-02 | 1.000 | 167 KB | 980 / 20 | 0,6 s | ≈0,6–0,8 s (por funciones) | 0,46 MB | 9 | 0,8 s · 144 KB | 135 MB | 0,00 | **PASS** |
| PERF-03 | 7.235 | 1,15 MB | 7.089 / 146 | 2,0 s | 4,2 s · 200 | 3,3 MB (7.259 docs) | 21 | 3,3 s · 917 KB | 256 → 356 MB (3 corridas) | 0,00 | **PASS** (local); en PROD **NOT RUN** (sin archivo real ni Odoo) |
| PERF-04 | 15.000 | 2,4 MB | 14.700 / 300 | 4,2 s | 8,6 s · 200 | 6,8 MB | 36 | 6,8 s · 1,9 MB | 401 → 522 → 630 MB | 0,00 | **PASS** local; **excede 512 MB** desde la 2.ª conciliación |
| PERF-05 | 30.000 | 4,8 MB | 29.400 / 600 | — | ≈37 s CPU | 13,7 MB | 66 | 27 s · 3,7 MB | ≈1 GB | 0,00 | **PASS** local (inviable en 512 MB) |
| PERF-06 | 50.000 | 7,9 MB | 49.000 / 1.000 | — | ≈62 s CPU | 22,8 MB | 105 | 44 s · 6,2 MB | ≈1,1–1,7 GB | 0,00 | **PASS** local (objetivo de diseño no demostrado en producción) |
| PERF-07 | 100.000 | 15,9 MB | 98.000 / 2.000 | — | rechazado: 413 (> 10 MB) y `LecturaTruncada` (`pos.order` 68.340 > 50.000) | — | — | — | — | — | **BLOCKED** por límites de diseño; con topes retirados solo en la prueba: completa, residuo 0, 3,2 GB RSS |

### 8.4 Funcionales de conciliación (casos conocidos)

Cubiertos por la suite automática (192 pruebas de Ventas, 05-11 16:08) y la QA de punta a punta (21 casos, 05-11 16:49), con datos sintéticos de 280 documentos y discrepancias plantadas:

| Caso | Prueba / caso QA | Estado |
|---|---|---|
| Presente en DIAN y Odoo | `test_totales_identicos`; QA-07 | PASS |
| Presente en DIAN, ausente en Odoo | `test_un_documento_faltante_en_odoo`; FE-15 en QA | PASS |
| Presente en Odoo, ausente en DIAN | `test_un_documento_adicional_en_odoo`; POS 240–247 en QA | PASS |
| Diferencia de importe | `test_una_diferencia_de_importe`; FE-22 | PASS |
| Diferencia de impuesto | `test_diferencia_de_iva_inc`; FE-30 | PASS |
| Nota crédito | `test_devolucion_pos_y_nota_credito`, `test_nota_credito_con_signo_incorrecto`; NC-3 | PASS |
| Duplicado | `test_cufe_repetido_en_odoo_es_posible_duplicado`, `test_numero_repetido_en_el_contenedor_es_posible_duplicado`; FE-50 | PASS |
| CUFE coincidente | `test_totales_identicos` | PASS |
| Número coincidente | `test_cufe_ausente_en_odoo_y_numero_coincide` | PASS |
| CUFE coincidente con número discordante | `test_cufe_coincide_y_numero_discordante` | PASS |
| Número coincidente con NIT discordante | `test_mismo_numero_con_nit_emisor_diferente`, `test_nit_emisor_distinto_a_la_compania_marca_identidad` | PASS |
| Ambiguo con más de un candidato | `test_varios_candidatos_por_numero_es_ambiguo` | PASS |
| Excluidos por reglas de negocio | `test_tipos_incluidos_excluidos_y_signos`; 12 filas excluidas en QA-05 | PASS |
| Conteos trazables, nada desaparece, totales = detalle o residuo, reproducible | `test_residuo_no_explicado_se_informa_y_no_se_fuerza`; residuo 0,00 en todos los volúmenes; tres corridas idénticas (3,29 MB, 7.259 docs) | PASS |

---

## 9. Tiempos y métricas reales

| Métrica | Valor | Fuente |
|---|---|---|
| Latencia de `GET /api/ventas/estado-ia` en producción, servicio caliente | 0,2–0,8 s | pruebas desde Vercel 16:29 |
| Latencia de la primera petición tras inactividad (arranque en frío) | > 45 s (la herramienta de prueba agotó su espera); arranque del contenedor 16:28:45 → app lista 16:29:05 → petición servida 16:29:08 | log de Render |
| Límites del servicio | `free`, `cpu_limit` 0,15, `memory_limit` 512 MB, 1 instancia | `get_service`, `get_metrics` |
| Uso de memoria en producción (muestras por minuto) | 100–209 MB (20-10 y 21-10); 121 MB el 05-11 | `get_metrics` |
| CPU en producción | máx. 0,08 por minuto (arranque), 0,05 en uso | `get_metrics` |
| Eventos de fallo desde el 15-10 | ninguno | `list_events` |
| CPU por fila en Ventas (local) | ≈1,3 ms conciliar + 0,3 ms analizar + 0,45 ms reporte | §9.2 del informe de rendimiento |
| Memoria retenida por conciliación | ≈6,3 KB por documento; ≈50 MB por conciliación de 7.235 filas | ídem |
| Llamadas a Odoo | 8 (100 filas), 9 (1.000), 21 (7.235), 36 (15.000), 105 (50.000); 1,65 MB de XML por cada 1.000 documentos | Odoo simulado |
| Compras, 7.235 filas | 5,1 s comparación + 13,7 s reporte, ambos en el bucle de eventos | `bench_compras.py` |

---

## 10. Errores funcionales encontrados

```
ID: D-1
Severidad: P1
Componente: backend/main.py (executor de 2 hilos) + backend/odoo_match.py (fetch_invoices)
Descripción: dos comparaciones de Compras simultáneas dejan el servidor sin responder a Compras hasta reiniciar.
Evidencia: repro_executor_compras.py — 1 petición: 0,31 s; 2 peticiones: sin respuesta tras 20 s.
Pasos para reproducir: dos usuarios (o dos pestañas) pulsan «COMPARAR CON ODOO» con ~1 s de diferencia; o recargar y reintentar mientras la primera sigue en curso.
Causa: fetch_invoices corre en `executor` y envía sus lotes de 250 al mismo `executor`; con los 2 hilos ocupados, los lotes nunca se ejecutan.
Estado de causa: CONFIRMADA
Impacto: /comparar y /odoo/comparar quedan bloqueados para todos hasta que Render reinicie el proceso (en plan free, al próximo spin-down).
Corrección recomendada: no pasar `executor` a fetch_invoices (lotes secuenciales; el ServerProxy compartido tampoco es seguro entre hilos) o usar un pool separado para los lotes. Sin tocar comparador.py.
Riesgo de la corrección: bajo; mismo resultado, lotes en serie.
Pruebas de aceptación: dos /odoo/comparar simultáneos con Odoo simulado terminan en < 10 s; pruebas de Compras sin cambios.
```

```
ID: D-2
Severidad: P1
Componente: backend/main.py — /odoo/comparar (línea 211) y /odoo/descargar-reporte (líneas 250–251)
Descripción: la lectura del Excel con pandas, la comparación y la generación del Excel corren dentro del bucle de eventos.
Evidencia: código (llamadas síncronas sin run_in_executor); medición local: 5,1 s + 13,7 s con 7.235 filas, 36 s + 83 s con 50.000.
Pasos para reproducir: lanzar /odoo/descargar-reporte con un archivo de recibidos de 7.235 filas y, en paralelo, GET /: la segunda no responde hasta que termina la primera.
Causa: operaciones bloqueantes en `async def`.
Estado de causa: CONFIRMADA
Impacto: todo el servicio (Ventas incluida) deja de responder durante esos segundos; con 0,15 CPU, varios minutos.
Corrección recomendada: run_in_threadpool para comparar_facturas_odoo y generar_excel_reporte_bytes (ya existe la variante en memoria).
Riesgo de la corrección: bajo.
Pruebas de aceptación: GET / responde en < 100 ms durante una descarga de reporte de 7.235 filas.
```

```
ID: D-3
Severidad: P2
Componente: backend/main.py (orden de middlewares) / Starlette ServerErrorMiddleware
Descripción: las respuestas 500 por excepción no controlada no llevan cabeceras CORS; el navegador las muestra como «blocked by CORS policy» / «Failed to fetch».
Evidencia: API-14 y CORS-07b en local: 500 `Internal Server Error`, `access-control-allow-origin` ausente; 404/422/502 sí la llevan.
Pasos para reproducir: provocar una excepción no capturada en cualquier endpoint y llamar desde el origen de Vercel.
Causa: ServerErrorMiddleware envuelve a CORSMiddleware; la respuesta de error no pasa por él.
Estado de causa: CONFIRMADA
Impacto: diagnóstico engañoso (como en el incidente A) y mensaje inútil para el usuario.
Corrección recomendada: `@app.exception_handler(Exception)` que devuelva JSON 500 genérico (pasa por CORS) y registre la traza en el servidor.
Riesgo de la corrección: bajo.
Pruebas de aceptación: API-14 con cabecera CORS presente y cuerpo JSON `{"detail": "Error interno"}`.
```

```
ID: D-4
Severidad: P2 (seguridad; configuración previa al módulo de Ventas)
Componente: backend/main.py líneas 27–32 y 54–65
Descripción: CORS abierto a cualquier origen y autenticación de la API opcional y desactivada (API_AUTH_TOKEN vacío).
Evidencia: CORS-05: `fetch` desde https://example.com → 200; `curl`-equivalente sin token → 200.
Pasos para reproducir: llamar a cualquier endpoint desde otro sitio o desde una herramienta HTTP.
Causa: configuración.
Estado de causa: CONFIRMADA
Impacto: cualquier sitio puede consumir la API (abuso, agotamiento de la instancia gratuita); el riesgo de datos es limitado porque Ventas exige las credenciales cifradas de Odoo del usuario, pero /comparar, /analizar-contenedor y los endpoints de IA son públicos.
Corrección recomendada: `allow_origins=["https://factu-match.vercel.app"]` (más previews si se usan) y activar API_AUTH_TOKEN o un token de sesión; no usar wildcard con credenciales.
Riesgo de la corrección: medio (hay que ajustar el frontend para enviar el token); hacerlo en una fase aparte.
Pruebas de aceptación: CORS-05 sin cabecera para orígenes ajenos; 401 sin token.
```

```
ID: D-5
Severidad: P2
Componente: backend/odoo_match.py fetch_invoices (search limit=10000) y backend/comparador.py generar_excel_reporte (/tmp/reporte_comparacion_facturas.xlsx)
Descripción: truncamiento silencioso a 10.000 facturas de proveedor; reporte de Compras escrito en una ruta fija compartida.
Evidencia: código; no reproducido con datos (requiere > 10.000 facturas).
Causa: límite de seguridad sin aviso; ruta temporal única.
Estado de causa: CONFIRMADA por lectura de código
Impacto: comparación incompleta sin señal; un usuario puede descargar el reporte de otro.
Corrección recomendada: aviso explícito al alcanzar el tope; usar generar_excel_reporte_bytes.
Riesgo: bajo.
Pruebas de aceptación: con Odoo simulado de 10.001 facturas aparece el aviso; dos descargas concurrentes devuelven archivos distintos.
```

```
ID: D-6
Severidad: P2
Componente: frontend/js/ventas_engine.js, frontend/js/odoo_conector.js
Descripción: sin timeout, sin cancelación y sin progreso durante peticiones de minutos; en Compras `res.json()` se ejecuta antes de comprobar `res.ok`.
Evidencia: código (api(), conciliar(), listener de btn-odoo-comparar); arranque en frío de > 45 s observado.
Causa: diseño del cliente.
Estado de causa: CONFIRMADA
Impacto: el usuario percibe bloqueo, recarga y pierde la petición (compatible con el log del 05-11 sin peticiones); en Compras una respuesta HTML del proxy produce «Unexpected token <».
Corrección recomendada: AbortController con timeout, mensaje de «el servidor está arrancando, puede tardar 1 minuto», progreso por etapa (fase 2), y comprobar `res.ok` y `content-type` antes de `res.json()`.
Riesgo: bajo.
Pruebas de aceptación: QA-14/QA-16 actuales + nuevo caso «servidor no responde en N s».
```

```
ID: D-7
Severidad: P2
Componente: backend (sin logging por etapa; sin request_id)
Descripción: imposible relacionar una petición con sus etapas, duración y filas en el log; uvicorn solo registra al terminar.
Evidencia: `grep logger. backend/ventas` → 4 avisos; log de Render del 05-11 sin ninguna línea entre arranque y apagado.
Causa: ausencia de instrumentación.
Estado de causa: CONFIRMADA
Impacto: el soporte no puede diagnosticar bloqueos ni medir Odoo en producción.
Corrección recomendada: logger.info por etapa con run_id, filas, llamadas y segundos; nunca NIT, nombres, credenciales ni claves.
Riesgo: bajo.
Pruebas de aceptación: una conciliación produce ≥ 6 líneas de etapa; ninguna contiene datos personales.
```

```
ID: D-8
Severidad: P3
Componente: backend/ventas/router.py _conciliar (líneas 181–183)
Descripción: un contenedor con encabezados correctos y 0 filas devuelve «El contenedor mezcla varios NIT emisores».
Evidencia: API-11b: 422 con ese mensaje.
Causa: `len(nits) != 1` agrupa 0 y > 1 en el mismo mensaje.
Estado de causa: CONFIRMADA
Impacto: mensaje engañoso.
Corrección recomendada: comprobar primero `lectura.incluidos` vacío (mensaje «no tiene documentos de venta») y después el número de NIT.
Riesgo: nulo.
Pruebas de aceptación: API-11b → 422 «El contenedor no tiene documentos de venta para conciliar».
```

```
ID: D-9
Severidad: P3
Componente: FastAPI/python-multipart (POST multipart vacío) y backend/main.py /comparar
Descripción: multipart vacío → 400 «There was an error parsing the body» (mensaje técnico); /comparar y /odoo/comparar devuelven 500 con el texto de la excepción ante errores de entrada (archivo ERP sin columnas, archivo no ZIP, contenedor de emitidos en Compras).
Evidencia: API-08 en PROD; COMPRAS-04/05 en local.
Causa: manejo genérico `except Exception → 500`.
Estado de causa: CONFIRMADA
Impacto: códigos inconsistentes (errores del usuario como 500) y mensajes internos expuestos («File is not a zip file»).
Corrección recomendada: mapear ValueError de lectura a 422 con mensaje claro; no cambiar la lógica del comparador.
Riesgo: bajo.
Pruebas de aceptación: COMPRAS-04/05 → 422.
```

```
ID: D-10
Severidad: P3 (configuración)
Componente: Render — variable NEON_DB_URL ausente
Descripción: en cada arranque, `[startup] Error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed`.
Evidencia: log de Render 05-11 16:29:08.
Causa: main.py (líneas 461–548) intenta asegurar tablas en Postgres al arrancar; sin NEON_DB_URL intenta el socket local.
Estado de causa: CONFIRMADA
Impacto: los módulos de correo, órdenes de compra y tablero de auditoría no funcionan en producción; Ventas y Compras por Odoo no se ven afectados.
Corrección recomendada: configurar NEON_DB_URL o desactivar esos módulos explícitamente; no bloquea esta auditoría.
Riesgo: nulo.
Pruebas de aceptación: arranque sin la línea de error.
```

```
ID: D-11
Severidad: P2 (seguridad, fuera del flujo de carga)
Componente: backend/main.py /api/run-ai-checklist, /odoo/test-connection, /odoo/save-connection
Descripción: reciben contraseña/clave API de Odoo y clave Groq en claro en el cuerpo de la petición; /odoo/test-connection y /odoo/save-connection autentican contra Odoo dentro del bucle de eventos sin timeout.
Evidencia: inventario de rutas (sección 2); odoo_match.authenticate sin timeout.
Estado de causa: CONFIRMADA por lectura de código
Impacto: un Odoo lento deja el servidor sin responder; las claves viajan en cada petición (solo bajo HTTPS).
Corrección recomendada: mover a hilo con timeout; guardar claves en servidor. Fase aparte.
```

---

## 11. Riesgo e impacto por defecto

| ID | Severidad | Probabilidad | Impacto | Afecta a |
|---|---|---|---|---|
| D-1 interbloqueo Compras | P1 | media (dos usuarios o un reintento) | Compras inutilizable hasta reinicio | todos |
| D-2 bucle bloqueado Compras | P1 | alta (cada comparación/reporte) | servicio sin respuesta segundos o minutos | todos, Ventas incluida |
| Plan `free` + sin feedback (informe de rendimiento R-1, R-2, R-10) | P1 | alta (cada arranque tras 15 min) | percepción de bloqueo; cargas abandonadas | Ventas y Compras |
| D-3 500 sin CORS | P2 | baja | diagnóstico engañoso | todos |
| D-4 CORS/auth abiertos | P2 | — | abuso de la instancia; endpoints públicos | servicio |
| D-5 truncamiento y ruta fija | P2 | baja | resultados incompletos; reporte ajeno | Compras |
| D-6 cliente sin timeout | P2 | alta | recargas y peticiones perdidas | Ventas y Compras |
| D-7 sin observabilidad | P2 | — | soporte a ciegas | operación |
| D-8, D-9, D-10, D-11 | P3/P2 | — | mensajes, configuración, seguridad | — |

---

## 12. Recomendación de corrección priorizada

Sin implementar nada en esta fase. Orden propuesto (coincide con el plan por fases del informe de rendimiento):

1. **Hoy, sin código:** confirmar el síntoma con la pestaña *Network* abierta (sección 14); decidir el cambio de plan a *Starter* (elimina el arranque en frío) y `VENTAS_MAX_RESULTADOS=2`.
2. **Fase 1 (código pequeño, aislado):** D-1, D-2 (Compras a hilos y executor separado), D-3 (manejador de excepciones con CORS), D-6 (timeout y mensajes en el frontend), D-7 (log por etapa), D-8 y D-9 (mensajes/códigos).
3. **Fase 2:** ejecuciones asíncronas con progreso y resultados paginados en Ventas (informe de rendimiento §14–15).
4. **Fase aparte de seguridad:** D-4 y D-11 (orígenes permitidos, token obligatorio, claves fuera del cuerpo).
5. **Configuración:** D-10 (`NEON_DB_URL`).

---

## 13. Pruebas de regresión necesarias

1. Suite completa `python -m pytest -q` con el mismo resultado (226 pasan; la falla previa de Compras se mantiene hasta que se decida corregirla aparte, nunca cambiando su expectativa).
2. `python qa/ventas/qa_ui.py`: 21 de 21.
3. Matriz de esta sección 8 repetida tras cada fase: API-01…14, CORS-01…07, PERF-01…04 en local; API-01/02/03/09/10 y CORS-01/02/03/06 en producción desde Vercel.
4. Comparación JSON bit a bit del resultado de Ventas (fixtures de 280, 7.235 y 50.000) antes y después de cualquier cambio de rendimiento, excluyendo `run_id` y tiempos.
5. Compras: `/comparar` y `/odoo/comparar` con los mismos archivos sintéticos antes y después → mismo `resumen_general` y mismas listas de faltantes; reporte Excel con las mismas hojas y filas.
6. Concurrencia: dos `/odoo/comparar` simultáneos terminan; `GET /` responde durante `/odoo/descargar-reporte`.
7. Descarga de Excel y visualización en navegador (QA-11 y QA-18 actuales).

---

## 14. Riesgos pendientes y datos que faltan

| Falta | Para qué | Cómo obtenerlo |
|---|---|---|
| Fecha y hora del incidente A y de los intentos de carga anteriores a esta semana | confirmar la causa del 404 | el log del plan `free` ya no los conserva; anotar la hora en los próximos intentos |
| Captura de *Network* de una carga completa sin recargar la página | distinguir «el navegador abandonó» de «el servidor no respondió» y medir el tiempo real | DevTools → Network → filtro `ventas` → columnas Status/Time/Size |
| El archivo real de 7.235 filas | medirlo tal cual (solo en local) | adjuntarlo al chat; no se versiona |
| Latencia real de Odoo por página | dimensionar la fase 2 | línea `POST /api/ventas/conciliar … 200` del log de Render y `odoo.llamadas` del JSON |
| Qué módulo y qué acciones exactas se hicieron a las 15:52 del 05-11 | cerrar el incidente B | relato del usuario |
| Extensiones de Chrome instaladas | aislar el error del *listener* | prueba en incógnito |
| Decisión sobre el plan de Render y sobre los límites (50.000 / 100.000) | fases 2–3 | aprobación |

Riesgos abiertos: la instancia gratuita puede reiniciarse en cualquier momento (resultados en memoria perdidos); la quinta conciliación de 7.235 filas en una hora, o una de 15.000, superará los 512 MB; Compras se bloquea con dos usuarios.

---

## 15. Evidencia de que Compras continúa funcionando

| Prueba | Resultado | Fecha |
|---|---|---|
| `test_comparador.py`, `test_odoo_match.py`, `test_groq_client.py` | 34 pasan, 1 falla previa (`test_nit_sin_dv_en_dian_coincide_con_nit_con_dv_en_erp`, igual en el commit `7f517c8`, anterior al módulo de Ventas) | 05-11 16:08 |
| `POST /comparar` DIAN recibidos (300 filas) vs ERP sintético | 200; 300 proveedores, 294 encontradas, 6 faltantes (las plantadas) | 05-11 16:38 |
| `POST /odoo/comparar` con `fetch_invoices` simulado (300 facturas) | 200; `fuente: odoo`, 298 encontradas, 2 faltantes | 05-11 16:36 |
| `POST /odoo/descargar-reporte` ídem | 200 `.xlsx` | 05-11 16:36 |
| `main.py` registra el router de Ventas sin alterar rutas de Compras | `test_main_registra_ventas_sin_tocar_compras` pasa; 26 rutas en `/openapi.json` de producción | 05-11 |
| QA-19 (navegación entre Comparaciones, tablero y Ventas) | PASA | 05-11 16:49 |
| Código de Compras | `comparador.py`, `odoo_match.py` y sus pruebas sin modificaciones (`git status` limpio salvo los dos documentos de auditoría) | 05-11 |

Lo que **no** se probó: Compras contra Odoo real ni Groq real (sin credenciales en el entorno de auditoría); el defecto D-1 solo se reprodujo con Odoo simulado.
