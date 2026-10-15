# PROMPT MAESTRO --- FACTU-MATCH VENTAS + AGENTE DE AUDITORÍA CON CLAUDE

**Proyecto:** `TechFixSolutionscol/factu-match`\
**Objetivo:** ampliar Factu-Match con conciliación de ventas DIAN
vs. Odoo, conciliación global de totales y un agente de IA capaz de
investigar diferencias y proponer correcciones fundamentadas.\
**Instrucción principal:** primero audita el proyecto existente y sus
datos reales. No empieces a modificar código hasta entender la
arquitectura, los flujos actuales y las pruebas. Implementa de forma
incremental, verificable y segura.

------------------------------------------------------------------------

## 1. Tu rol y resultado esperado

Actúa como ingeniero de software sénior con experiencia en Python,
FastAPI, integraciones ERP, Odoo, facturación electrónica colombiana,
conciliación de datos y APIs de modelos de lenguaje.

Debes trabajar sobre el repositorio existente
`TechFixSolutionscol/factu-match`, salvo que la inspección demuestre una
razón técnica concreta para no hacerlo. La nueva funcionalidad debe
quedar aislada del comparador de compras y no debe romper ni alterar sus
resultados actuales.

El resultado esperado es una ampliación de Factu-Match que permita:

1.  Comparar documentos emitidos reportados por la DIAN contra las
    ventas de Odoo.
2.  Consultar tanto `account.move` como `pos.order`, con reglas para
    evitar contar dos veces una misma operación fiscal.
3.  Identificar documentos por CUFE/CUDE y también por número de factura
    normalizado.
4.  Comparar documento por documento y, por separado, comparar los
    totales globales del contenedor DIAN frente a Odoo.
5.  Investigar diferencias globales, determinar qué documentos las
    explican y producir hipótesis de causa basadas en evidencia.
6.  Usar la API oficial de Anthropic/Claude para una investigación más
    útil que un simple resumen.
7.  Generar un reporte auditable con resultados, importes, impuestos,
    evidencias, prioridades y acciones recomendadas.
8.  Preservar íntegramente el comportamiento existente de conciliación
    de compras.

No afirmes que una tarea está completada hasta haber ejecutado las
pruebas correspondientes. No inventes campos, estados, resultados,
rutas, variables de entorno ni comportamientos de Odoo.

------------------------------------------------------------------------

## 2. Fase obligatoria: auditoría inicial, sin modificar código

Antes de editar archivos:

1.  Inspecciona la estructura completa del repositorio, instrucciones
    del proyecto, dependencias, configuración, tests y documentación.
2.  Identifica el flujo actual del comparador de compras: carga DIAN,
    normalización, conexión Odoo, motor de comparación, reportes,
    proveedor de IA y endpoints.
3.  Identifica el proveedor de IA actual, cómo se configura y dónde se
    invoca. La documentación previa menciona Groq, pero el estado real
    del repositorio manda. No supongas que el código sigue igual.
4.  Revisa los endpoints existentes, formatos de respuesta, manejo de
    errores, autenticación, cifrado de credenciales y despliegue en
    Render.
5.  Ejecuta la suite de pruebas existente y registra el resultado
    inicial.
6.  Identifica qué componentes pueden reutilizarse sin cambiar la lógica
    de compras y cuáles deben ser nuevos.
7.  Comprueba las versiones y limitaciones reales de Odoo, XML-RPC,
    Python y librerías presentes.
8.  Verifica los campos reales mediante metadatos/modelos de Odoo o las
    exportaciones de campos proporcionadas. Si un campo no se puede
    verificar, documenta la incertidumbre y no lo des por confirmado.
9.  Presenta un plan breve con archivos a crear/modificar, riesgos,
    pruebas y orden de ejecución. Después continúa por fases, sin
    esperar aprobación para cambios internos de bajo riesgo; detente y
    pregunta solo si hay una decisión de negocio indispensable o una
    acción destructiva/no reversible.

**Restricción:** no refactorices el comparador de compras por
conveniencia. Reutiliza funciones existentes cuando sean adecuadas, pero
protege sus contratos mediante pruebas de regresión.

------------------------------------------------------------------------

## 3. Contexto técnico conocido, sujeto a verificación en el repositorio

La documentación aportada describe:

-   Backend Python con FastAPI.
-   Conector de Odoo mediante XML-RPC.
-   Motor de comparación y generación de reportes Excel.
-   Despliegue en Render.
-   Comparador actual de facturas de compra contra DIAN/ERP.
-   Proveedor de IA usado para generar narrativa, que se debe
    inspeccionar antes de reemplazarlo.

No cambies automáticamente XML-RPC por otra tecnología. Mantén el
mecanismo que funcione en el proyecto, salvo que exista una limitación
demostrada y documentada.

El módulo nuevo debe ser independiente a nivel funcional, por ejemplo
con un motor, esquemas, servicios y endpoints propios para ventas,
adaptados a la organización real del repositorio.

------------------------------------------------------------------------

## 4. Alcance funcional de Factu-Match Ventas

### 4.1 Fuentes de datos

**Fuente A: archivo/contenedor DIAN de documentos emitidos.**

El Excel de referencia incluye, entre otros, campos relacionados con:

-   Tipo de documento
-   CUFE/CUDE
-   Folio
-   Prefijo
-   Fecha de emisión
-   Fecha de recepción
-   NIT emisor
-   NIT receptor
-   Nombre receptor
-   Total
-   IVA
-   INC
-   Otros conceptos tributarios
-   Estado del documento

La especificación funcional aportada indica excluir las filas cuyo
`Tipo de documento` sea `Application response` o `Nomina Individual`.
Confirma cómo aparecen realmente estos valores y aplica la exclusión
normalizando espacios/capitalización de forma segura. No excluyas otros
tipos sin una regla documentada.

No asumas que todas las filas restantes son facturas positivas válidas:
identifica el tipo y estado de cada documento y trata facturas, notas
crédito y otros documentos según reglas explícitas.

**Fuente B: Odoo `account.move`.**

Campos identificados en la exportación de campos compartida por el
usuario; verificar disponibilidad y semántica en la instancia real:

-   `name`: secuencia/número mostrado por Odoo; verificar si es el
    número legal de factura en todos los casos.
-   `partner_id`: contacto relacionado.
-   `l10n_co_edi_cufe_cude_ref`: identificador electrónico.
-   `amount_residual`: saldo pendiente; **no usarlo como total de
    factura**.
-   `amount_total`: total del documento.
-   `invoice_partner_display_name`: nombre del cliente mostrado.
-   `invoice_date`: fecha de factura.
-   `invoice_date_due`: fecha de vencimiento.
-   `move_type`: tipo de movimiento.
-   `state`: estado contable.
-   `amount_untaxed`: subtotal antes de impuestos.
-   `amount_tax`: total de impuestos.
-   `amount_total_signed`: importe con signo contable; usar solo si su
    semántica es adecuada para el cálculo definido.
-   `tax_totals`: datos agregados de impuestos, sujeto a la estructura
    real.
-   `company_id`: empresa.
-   `edi_state`, `l10n_co_dian_state`, `l10n_co_dian_commercial_state`,
    `l10n_co_edi_type`: verificar disponibilidad y significado real
    antes de utilizarlos.
-   `invoice_line_ids`: líneas de factura para análisis detallado de
    impuestos si es necesario.
-   `l10n_latam_document_number`: verificar si existe y qué valor
    contiene; no asumir que está disponible.

`invoice_name` no se confirmó como campo real de `account.move`; no lo
utilices sin comprobarlo.

**Fuente C: Odoo `pos.order`.**

Campos identificados en la exportación compartida; verificar
disponibilidad y semántica:

-   `date_order`: fecha/hora de la orden.
-   `partner_id`: cliente.
-   `l10n_co_edi_pos_name`: número/nombre del documento POS.
-   `l10n_co_edi_pos_cufe_cude_ref`: CUFE/CUDE/CUDS según la
    implementación.
-   `amount_total`: total de la orden.
-   `amount_tax`: impuestos totales.
-   `amount_paid`: importe pagado; **no usar como sustituto del total
    fiscal**.
-   `account_move`: relación con movimiento contable.
-   `is_invoiced`: indicador relacionado con facturación.
-   `is_refund`: indicador de devolución/reembolso.
-   `refunded_order_id`: orden relacionada/original.
-   `reversed_move_ids`: movimientos de reversión relacionados.
-   `state`: estado de la orden.
-   `company_id`: empresa.
-   `l10n_co_edi_pos_dian_state`: estado electrónico relacionado con
    DIAN, sujeto a verificación.
-   `l10n_co_edi_pos_dian_date`: fecha relacionada con el proceso DIAN,
    sujeto a verificación.
-   `lines`: líneas de orden para análisis de impuestos si es necesario.

No asumas que los nombres de campos de una exportación de
`ir.model.fields` garantizan que todos los registros tengan valor o que
la API permita leerlos en cualquier usuario. Verifica permisos y
comportamiento.

### 4.2 Identificación de documentos: CUFE y número de factura son ambos obligatorios

No diseñes un sistema que dependa únicamente del CUFE/CUDE.

La conciliación debe permitir identificar documentos mediante dos rutas
explícitas:

1.  **Coincidencia por CUFE/CUDE**, cuando el valor está disponible y es
    válido en ambas fuentes.
2.  **Coincidencia por número de documento**, usando el prefijo y folio
    DIAN y el número correspondiente en Odoo/POS.

La normalización debe admitir diferencias de formato, por ejemplo
prefijo y folio separados, `/`, `-`, espacios y ceros a la izquierda,
siguiendo reglas probadas con ejemplos reales. No elimines caracteres
indiscriminadamente ni mezcles prefijos diferentes. Documenta cómo se
manejan los ceros iniciales y los números que contienen letras.

Campos iniciales: - `account.move`: `l10n_co_edi_cufe_cude_ref` y
`name`; verificar también `l10n_latam_document_number`. - `pos.order`:
`l10n_co_edi_pos_cufe_cude_ref` y `l10n_co_edi_pos_name`.

La salida debe indicar el método de identificación: `cufe`,
`invoice_number`, `both` o `unmatched`.

Reglas de seguridad de coincidencia: - CUFE coincidente con número
discordante: marcar inconsistencia para revisión; no ocultar el
conflicto. - CUFE ausente, número coincidente: aceptar como candidato y
validar empresa/NIT, tipo, fecha e importe según reglas. - Número
coincidente con NIT emisor diferente: no aceptar automáticamente. - Más
de un candidato: marcar posible duplicado/ambiguo, no escoger
arbitrariamente. - Un documento que no coincide por CUFE puede coincidir
por número, pero la explicación debe registrar la ruta y cualquier
contradicción.

### 4.3 Validaciones complementarias

Usa, según disponibilidad real: - NIT emisor contra la empresa conectada
a Odoo. - NIT receptor contra el NIT del `res.partner` asociado. - Tipo
de documento. - Fecha de emisión/factura. - Total y componentes
tributarios. - Estado contable y electrónico. - Relaciones entre POS y
movimiento contable.

Para obtener el NIT del contacto, consulta el modelo relacionado
`res.partner` y verifica el campo real, habitualmente `vat`. Para el NIT
emisor, verifica el dato de la compañía conectada y el modelo/campo
real. No supongas que estos valores están directamente en
`account.move`.

En consumidores finales o documentos sin cliente individual, no exijas
una coincidencia de NIT del receptor si la modalidad fiscal y las reglas
de negocio permiten que no exista. Registra esa condición como
validación no aplicable, no como error automático.

------------------------------------------------------------------------

## 5. Deduplicación y modelo canónico de documentos

`account.move` y `pos.order` no se pueden sumar como dos conjuntos
independientes sin analizar sus relaciones. Una orden POS puede estar
vinculada a un movimiento contable y ambos registros podrían representar
la misma operación fiscal.

Implementa una capa de normalización que construya un conjunto canónico
de documentos fiscales. Cada documento canónico debe conservar: -
origen/orígenes en Odoo; - IDs y referencias originales; - CUFE/CUDE; -
número normalizado; - tipo y signo documental; - empresa; - fechas; -
cliente/NIT cuando aplique; - total, base e impuestos; - estados
contable y electrónico; - relaciones con POS, factura y devoluciones.

Reglas: - Usar `account_move`, CUFE, número, tipo y otras evidencias
para identificar duplicación entre modelos. - No eliminar dos registros
solo porque tengan el mismo total o una fecha parecida. - No contar una
orden POS y su factura vinculada como dos ventas fiscales si representan
el mismo documento. - Tratar devoluciones y notas crédito según el tipo
de documento, signo y relación con el original. - Mantener trazabilidad
para poder explicar por qué un registro se incluyó, excluyó o agrupó. -
Cuando la evidencia no alcance para deduplicar con seguridad, marcar
ambigüedad y mostrar el impacto potencial en vez de descartar registros
silenciosamente.

------------------------------------------------------------------------

## 6. Dos niveles obligatorios de conciliación

### 6.1 Conciliación individual

Para cada documento DIAN elegible: - buscar coincidencia en el conjunto
canónico de Odoo; - clasificar si coincide, falta, tiene discrepancias,
es ambiguo o parece duplicado; - comparar total y, cuando sea viable,
base, IVA, INC y otros impuestos mapeados; - conservar valores DIAN y
Odoo por separado; - calcular diferencias con precisión decimal; -
registrar la evidencia que respalda cada estado.

También identifica documentos elegibles de Odoo que no aparecen en el
contenedor DIAN. No los etiquetes automáticamente como incumplimiento:
considera el tipo, estado, fecha, alcance y posibles diferencias de
sincronización.

Estados funcionales mínimos, ajustables a la arquitectura actual: -
`matched` - `missing_in_odoo` - `missing_in_dian` - `amount_mismatch` -
`tax_mismatch` - `identity_mismatch` - `possible_duplicate` -
`ambiguous` - `excluded` - `needs_review`

La interfaz puede mostrar nombres en español, pero los códigos deben ser
estables y documentados.

### 6.2 Conciliación global de totales --- funcionalidad central

El sistema debe comparar los totales del contenedor DIAN contra los
totales de Odoo, no solo comparar factura por factura.

Ambos totales deben usar exactamente: - misma empresa; - mismo período y
criterio de fecha documentado; - mismos tipos de documento elegibles; -
mismos criterios de estado; - mismo tratamiento de notas crédito,
devoluciones y documentos anulados/rechazados; - misma regla de
inclusión/exclusión; - deduplicación canónica consistente.

No compares totales con alcances distintos. Si no es posible igualar los
alcances, muestra la diferencia de alcance antes de interpretar una
diferencia monetaria.

Calcular, como mínimo: - cantidad de documentos elegibles DIAN y Odoo; -
cantidad de documentos coincidentes, faltantes, adicionales, ambiguos y
duplicados; - total bruto de facturas; - total de notas
crédito/devoluciones separado; - total neto según la convención
definida; - base gravable/subtotal, si es comparable; - IVA; - INC; -
otros impuestos mapeados cuando exista correspondencia verificable; -
diferencia absoluta y relativa de cada métrica; - suma de diferencias
por documentos individuales; - diferencia global residual que no quede
explicada por las discrepancias identificadas.

No sumar `amount_residual` ni `amount_paid` como sustitutos de los
totales fiscales. Verifica la semántica de los campos y utiliza
`Decimal` o un mecanismo equivalente de precisión monetaria; no uses
comparaciones monetarias basadas en flotantes sin control de redondeo.

La tolerancia debe ser configurable y documentada. No ocultes
diferencias por redondeo: clasifícalas conforme a la tolerancia aprobada
y conserva el importe exacto calculado.

### 6.3 Análisis de causas de la diferencia global

Si existe diferencia entre el total global DIAN y Odoo, el backend debe
descomponerla en componentes comprobables, por ejemplo: - documentos
DIAN sin coincidencia en Odoo; - documentos Odoo sin correspondencia
DIAN; - diferencias de valor de documentos emparejados; - notas
crédito/devoluciones omitidas o tratadas con signo distinto; -
duplicidades POS/factura; - diferencias de impuestos; - diferencias de
filtros, período, empresa o estado; - documentos con identidad
contradictoria; - residuo no explicado.

Define una conciliación matemática explícita para que los componentes
expliquen la diferencia global. Si no la explican por completo, informa
el residuo; no inventes una causa para cuadrar el resultado.

Incluye análisis por prefijo, fecha, tipo documental, empresa y punto de
venta cuando existan datos confiables. El agente debe investigar
patrones como grupos de facturas faltantes, rangos de numeración,
errores repetidos y diferencias concentradas en una caja o período.

------------------------------------------------------------------------

## 7. Agente de Claude: de resumen a investigación con herramientas

Integra la API oficial de Anthropic mediante una capa/proveedor
desacoplado del motor de conciliación. El proveedor actual debe
inspeccionarse primero. No elimines una integración existente hasta que
la nueva esté implementada, probada y configurada.

### 7.1 Responsabilidades de Python/backend

El backend es la fuente de verdad para: - consultar Odoo; - parsear y
normalizar DIAN; - identificar documentos; - calcular totales, impuestos
y diferencias; - detectar duplicados; - deduplicar POS/facturas; -
ejecutar filtros; - guardar resultados y evidencia; - validar la
respuesta de Claude.

Claude no debe volver a calcular a mano los totales oficiales ni
reemplazar la lógica determinística. Puede revisar los datos calculados,
solicitar consultas adicionales mediante herramientas permitidas,
generar hipótesis y priorizar la revisión.

### 7.2 Herramientas controladas para Claude

Implementa las herramientas que encajen con la arquitectura real.
Nombres orientativos: - `buscar_documento`: buscar por CUFE/CUDE, número
normalizado, prefijo/folio y NIT. - `consultar_documento_odoo`:
recuperar campos autorizados de `account.move`/`pos.order` y sus
relaciones. - `investigar_diferencia`: obtener evidencia detallada de un
caso. - `analizar_impuestos`: recuperar el desglose de impuestos
comparable. - `analizar_secuencias`: identificar saltos, repeticiones y
patrones de numeración con consultas determinísticas. -
`consultar_historial`: comparar con conciliaciones anteriores si el
proyecto dispone de persistencia adecuada. - `generar_plan_revision`:
producir una lista de casos priorizados.

Las herramientas deben validar parámetros, limitar el número de
resultados, aplicar permisos, evitar consultas arbitrarias y devolver
datos estructurados. En primera versión, acceso de solo lectura a Odoo.

### 7.3 Flujo del agente investigador

Para una diferencia individual o global: 1. Recibir el hallazgo
determinístico y su evidencia. 2. Identificar qué información adicional
hace falta. 3. Llamar solo las herramientas relevantes y permitidas. 4.
Revisar resultados de CUFE, número de factura, NIT, fechas, total,
impuestos, estado y relaciones POS/contabilidad. 5. Formular una causa
solo si está respaldada; separar hechos, hipótesis y datos faltantes. 6.
Determinar la severidad y explicar el impacto. 7. Proponer pasos
concretos y verificables. 8. Indicar cómo comprobar que la corrección
funcionó tras volver a ejecutar la conciliación.

El agente no debe afirmar que una factura falta definitivamente hasta
descartar búsquedas alternativas razonables y verificar alcance,
permisos, estados y relación POS/contabilidad.

### 7.4 Respuesta estructurada

Usa el mecanismo de salida estructurada disponible en la API/SDK
seleccionado y valida el resultado con el esquema del backend. Un
formato lógico puede incluir: - `finding_type` - `summary` - `facts` -
`evidence` - `probable_causes` - `cause_status` (`confirmed`,
`probable`, `unresolved`) - `confidence` (con significado definido y sin
presentarlo como certeza estadística calibrada si no existe
calibración) - `risk_level` - `financial_impact` -
`recommended_actions` - `verification_steps` - `requires_human_review`

No dependas de texto libre para alimentar lógica financiera crítica. Si
la respuesta es inválida, incompleta o el proveedor falla, conserva la
conciliación determinística y marca el análisis IA como no disponible.

### 7.5 Qué debe hacer el agente con diferencias globales

Ejemplo de solicitud: "El total del contenedor DIAN difiere del total de
Odoo".

El agente debe: - consultar el resumen calculado por el backend; -
revisar si empresa, período y alcance coinciden; - obtener la
descomposición por faltantes, extras, diferencias de valor, impuestos,
notas crédito y duplicados; - identificar los grupos que explican mayor
proporción de la diferencia; - consultar documentos concretos para
comprobar hipótesis; - identificar patrones por prefijo, fechas y POS; -
separar importe explicado y residuo no explicado; - devolver una
conclusión y un plan priorizado de corrección; - indicar qué nueva
conciliación debe ejecutarse después de la corrección.

No inventes importes, documentos, causas ni correcciones. Toda
afirmación concreta debe referenciar evidencia que haya devuelto el
backend.

### 7.6 Seguridad y coste

-   Clave Anthropic solo en variable de entorno de servidor, por ejemplo
    `ANTHROPIC_API_KEY`.
-   Modelo configurable mediante variable de entorno, por ejemplo
    `ANTHROPIC_MODEL`; el nombre debe corresponder a un modelo realmente
    disponible y no quedar fijado sin motivo.
-   No incluir claves en el repositorio, navegador, logs ni respuestas
    API.
-   Limitar tiempo de ejecución, número de llamadas a herramientas,
    tamaño de contexto y reintentos.
-   Manejar errores de autenticación, cuota, límite de velocidad,
    timeout y disponibilidad.
-   Registrar métricas de uso/coste cuando sea posible, sin exponer
    datos sensibles.
-   Enviar a Claude solo los datos necesarios; minimizar NIT/nombres y
    otros datos personales cuando no sean indispensables.
-   No enviar todas las filas del Excel en cada llamada. Priorizar
    agregados, grupos de anomalías y documentos concretos.
-   Considerar caché de instrucciones/contexto estático cuando la API y
    el patrón de uso lo justifiquen.
-   Mantener el proveedor anterior como alternativa solo si encaja con
    la arquitectura y no introduce complejidad innecesaria.
-   Una caída de Claude nunca debe impedir calcular ni descargar el
    resultado determinístico.

------------------------------------------------------------------------

## 8. Reportes y experiencia de usuario

Reutiliza el formato de reportes existente cuando sea conveniente, pero
no alteres las salidas de compras.

Para ventas, el reporte debería incluir: 1. Resumen ejecutivo. 2.
Parámetros de ejecución: empresa, período, fecha usada, tipos de
documento, filtros y tolerancias. 3. Conciliación global DIAN vs. Odoo.
4. Totales brutos, notas crédito/devoluciones, total neto, base, IVA,
INC y demás impuestos soportados. 5. Diferencia global y descomposición
matemática de sus componentes. 6. Detalle de documentos coincidentes,
faltantes, adicionales, con discrepancias, ambiguos y duplicados. 7.
Método de identificación utilizado: CUFE, número o ambos. 8. Evidencia y
referencias de los registros Odoo. 9. Hallazgos de Claude: hechos,
causas probables, prioridad y acciones. 10. Residuo no explicado y casos
pendientes de revisión.

La interfaz debe distinguir visualmente: - resultado calculado por
reglas; - interpretación de Claude; - hipótesis aún no confirmadas; -
acciones pendientes del usuario.

No presentes una hipótesis de IA como un hecho contable confirmado.

------------------------------------------------------------------------

## 9. Correcciones y acciones: límites de seguridad

Primera versión: - Claude puede consultar datos, investigar y proponer
correcciones. - No puede crear, editar, publicar, cancelar ni eliminar
documentos de Odoo. - No puede modificar credenciales, configuración
fiscal ni secuencias. - No puede ejecutar código arbitrario ni consultas
libres contra Odoo. - No debe sugerir registrar una factura sin revisar
antes si existe en POS, bajo otro número, o vinculada a un movimiento
contable. - Toda recomendación debe indicar el registro afectado,
evidencia, acción y método de verificación. - Si en una fase futura se
consideran acciones de escritura, deben diseñarse por separado, con
autorización explícita, permisos mínimos, auditoría y confirmación
humana por operación.

------------------------------------------------------------------------

## 10. Pruebas obligatorias

Crea pruebas unitarias, de integración con mocks y regresión. No
dependas de una instancia de producción para pruebas automatizadas.

### Identificación

-   CUFE coincidente.
-   CUFE ausente y número coincidente.
-   Coincidencia por prefijo/folio con `/`, `-`, espacios y ceros
    iniciales.
-   Mismo número con NIT emisor diferente.
-   CUFE coincidente y número discordante.
-   Múltiples candidatos y duplicados.
-   Prefijos diferentes que no deben colisionar.

### Fuentes y deduplicación

-   Factura de `account.move`.
-   Documento POS independiente.
-   POS vinculado a `account.move`, sin duplicar el total fiscal.
-   Devolución y nota crédito relacionadas.
-   Registros de compañías distintas.
-   Estados borrador, publicados, cancelados, rechazados o pendientes
    según los estados reales.

### Conciliación global

-   Totales idénticos.
-   Un documento faltante.
-   Un documento adicional en Odoo.
-   Una diferencia de importe.
-   Diferencia de IVA/INC.
-   Nota crédito con signo incorrecto.
-   Duplicación POS/factura.
-   Períodos o empresas diferentes.
-   Diferencia explicada parcialmente y residuo no explicado.
-   Tolerancia monetaria y redondeo.
-   Totales bruto y neto separados.

### IA

-   Claude devuelve hallazgos estructurados válidos.
-   Respuesta inválida o incompleta.
-   Timeout, error de cuota y error de autenticación.
-   El proveedor falla y el resultado determinístico sigue disponible.
-   La IA no inventa documentos ni importes cuando no hay evidencia.
-   Se respetan límites de herramientas y de llamadas.
-   Las causas probables se distinguen de las confirmadas.

### Regresión de compras

-   Ejecuta todas las pruebas existentes.
-   Conserva contratos, formato de respuesta y comportamiento del
    comparador de compras.
-   Añade pruebas de regresión con fixtures previos si existen.
-   No cambies expectativas de tests de compras solo para hacerlos
    pasar; justifica cualquier cambio necesario y verifica si es una
    regresión real.

------------------------------------------------------------------------

## 11. Fases de implementación

### Fase 0 --- Auditoría

Mapear el repositorio, probar estado inicial, identificar proveedor IA,
arquitectura y riesgos. Documentar el plan.

### Fase 1 --- Especificación de datos

Confirmar campos y semántica de Odoo, reglas de documentos, fechas,
estados, impuestos, notas crédito, deduplicación y tolerancias.

### Fase 2 --- Extracción y normalización de ventas

Implementar consulta de `account.move` y `pos.order`, NIT de
empresa/contacto y conjunto canónico de documentos, con pruebas.

### Fase 3 --- Conciliación individual

Añadir motor de ventas aislado, identificación por CUFE y número,
discrepancias y reporte de detalle.

### Fase 4 --- Conciliación global

Implementar totales comparables, descomposición matemática de
diferencias, métricas tributarias y residuo no explicado.

### Fase 5 --- Agente Claude

Implementar proveedor Anthropic, herramientas controladas, flujo
investigador, respuesta estructurada, gestión de errores y límites de
coste.

### Fase 6 --- Reportes y experiencia

Incorporar resumen global, detalle individual, evidencia y análisis IA
al reporte sin romper contratos de compras.

### Fase 7 --- Validación y despliegue

Ejecutar pruebas, documentar variables de entorno, actualizar
instrucciones de despliegue de Render y preparar despliegue de prueba.
No publicar secretos ni modificar producción sin autorización.

Trabaja por fases. Al finalizar cada una: - resume los archivos
creados/modificados; - indica decisiones tomadas y supuestos
pendientes; - lista las pruebas ejecutadas y sus resultados; - informa
fallos reales sin ocultarlos; - continúa con la siguiente fase si no
existe un bloqueo de negocio o de seguridad.

------------------------------------------------------------------------

## 12. Criterios de aceptación final

El trabajo solo se considera aceptado si:

-   La conciliación de compras conserva su comportamiento.
-   Factu-Match Ventas compara DIAN con `account.move` y `pos.order`.
-   La identificación utiliza CUFE/CUDE y número de factura.
-   Las relaciones POS/contabilidad se tratan sin duplicar ventas.
-   Las notas crédito y devoluciones se gestionan según reglas
    explícitas.
-   Los totales globales se calculan con alcance equivalente y precisión
    monetaria.
-   El sistema descompone la diferencia global y expone cualquier
    residuo no explicado.
-   El análisis de impuestos distingue IVA, INC y otros conceptos solo
    cuando el mapeo está confirmado.
-   Claude investiga con herramientas y evidencia; no se limita a
    resumir el resultado.
-   El agente separa hechos, hipótesis y acciones recomendadas.
-   Una falla de Claude no impide obtener la conciliación ni descargar
    el reporte.
-   No se ejecutan escrituras contables automáticas.
-   Los secretos están fuera del código y los logs.
-   Las pruebas nuevas y las de regresión pasan, o los fallos pendientes
    están claramente documentados.
-   Existe documentación de arquitectura, reglas de negocio, variables
    de entorno y despliegue.

------------------------------------------------------------------------

## 13. Instrucción final para Claude Code

Comienza ahora con la **Fase 0: auditoría del repositorio**. No empieces
reescribiendo archivos ni construyendo una implementación paralela sin
inspeccionar el código actual.

Después, implementa las fases de manera incremental. La prioridad es la
exactitud de la conciliación, la trazabilidad de cada hallazgo y la
preservación del comparador de compras. Cuando falte información de Odoo
o una regla fiscal no esté confirmada, documenta la incertidumbre y crea
una validación o pregunta concreta; no inventes una respuesta.

El objetivo no es que Claude "explique un Excel", sino que investigue
discrepancias reales con herramientas controladas, explique qué
evidencia respalda cada hallazgo y ayude al usuario a determinar y
verificar la corrección adecuada.
