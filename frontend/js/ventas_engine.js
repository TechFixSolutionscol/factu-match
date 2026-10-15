/* ═══════════════════════════════════════════════════════════════
   VENTAS ENGINE — FactuMatch Ventas (DIAN emitidos vs Odoo)
   1. Carga y validación del contenedor DIAN
   2. Conciliación global (determinística, sin IA)
   3. Detalle por documento con evidencia
   4. Agente Claude (opcional: si falla, todo lo demás sigue)
   Depende de: API_URL, currentUser, log() (app.js) y odooCredentials (odoo_conector.js)
   ═══════════════════════════════════════════════════════════════ */

(() => {
  const POR_PAGINA = 50;
  const PENDIENTES = ['missing_in_odoo', 'missing_in_dian', 'amount_mismatch', 'tax_mismatch', 'identity_mismatch',
                      'possible_duplicate', 'ambiguous', 'scope_difference', 'needs_review'];
  const CLASE_ESTADO = {
    matched: 'ok', excluded: 'neutro', scope_difference: 'neutro', needs_review: 'aviso',
    tax_mismatch: 'aviso', amount_mismatch: 'error', missing_in_odoo: 'error', missing_in_dian: 'error',
    identity_mismatch: 'error', possible_duplicate: 'error', ambiguous: 'aviso',
  };
  const ETQ_METODO = { cufe: 'CUFE', invoice_number: 'Número', both: 'CUFE + número', unmatched: '—' };
  const ETQ_TIPO = { factura: 'Factura', nota_credito: 'Nota crédito', nota_debito: 'Nota débito' };
  const ETQ_NIVEL = { high: 'Alta', medium: 'Media', low: 'Baja' };
  const ETQ_RIESGO = { high: 'Alto', medium: 'Medio', low: 'Bajo' };
  const ETQ_CAUSA = { confirmed: 'Confirmada', probable: 'Probable', unresolved: 'Sin resolver' };

  const S = {
    archivo: null, analisis: null, resultado: null, filtro: 'pendientes', busqueda: '', pagina: 0,
    seleccion: null, estadoIa: null, ocupado: false,
  };

  // ── utilidades ──
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const NF = new Intl.NumberFormat('es-CO', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const dinero = (v) => (v === null || v === undefined || v === '') ? '—' : `$ ${NF.format(Number(v))}`;
  const entero = (v) => (v === null || v === undefined || v === '') ? '—' : Number(v).toLocaleString('es-CO');
  const claseSigno = (v) => (Number(v) > 0 ? 'vt-pos' : Number(v) < 0 ? 'vt-neg' : '');
  const etqEstado = (e) => (S.resultado?.etiquetas_estado || {})[e] || e;
  const badgeEstado = (e) => `<span class="vt-estado vt-estado-${CLASE_ESTADO[e] || 'neutro'}">${esc(etqEstado(e))}</span>`;
  // log() de app.js escribe con innerHTML: se escapa todo lo que viene del servidor, de Odoo o del Excel.
  const anunciar = (msg, tipo = 'msg') => { if (typeof log === 'function') log(esc(msg), tipo); };

  function credenciales() {
    return (typeof odooCredentials !== 'undefined' && odooCredentials) ? odooCredentials : null;
  }
  function puedeOperar() {
    const rol = (typeof currentUser !== 'undefined' && currentUser && currentUser.role) || 'Lectura';
    return rol === 'Admin' || rol === 'Operador';
  }

  async function api(ruta, opciones = {}) {
    const res = await fetch(`${API_URL}${ruta}`, opciones);
    if (!res.ok) {
      let mensaje = `Error ${res.status}`;
      try { const j = await res.json(); if (j.detail) mensaje = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (_) { /* sin cuerpo JSON */ }
      throw new Error(mensaje);
    }
    return res;
  }

  function cargando(boton, activo, texto) {
    if (!boton) return;
    if (activo) {
      boton.dataset.texto = boton.innerHTML;
      boton.innerHTML = `<div class="btn-loader"></div> ${esc(texto)}`;
      boton.disabled = true;
    } else {
      boton.innerHTML = boton.dataset.texto || boton.innerHTML;
      boton.disabled = false;
    }
  }

  function alerta(contenedor, mensaje, tipo = 'error') {
    if (!contenedor) return;
    contenedor.insertAdjacentHTML('afterbegin', `<div class="alerta alerta-${tipo === 'error' ? 'error' : 'info'} vt-alerta">${esc(mensaje)}</div>`);
  }

  // ── pestañas ──
  function irA(tab) {
    document.querySelectorAll('.vt-tab').forEach((b) => b.classList.toggle('active', b.dataset.vtTab === tab));
    ['carga', 'global', 'detalle', 'agente'].forEach((t) => { const el = $(`vt-${t}`); if (el) el.style.display = t === tab ? 'block' : 'none'; });
    if (tab === 'agente') cargarEstadoIa();
  }
  function habilitarPestanas(si) {
    document.querySelectorAll('.vt-tab').forEach((b) => { if (b.dataset.vtTab !== 'carga') b.disabled = !si; });
  }

  // ════════════════════════════════════════════
  // 1. CARGA Y PARÁMETROS
  // ════════════════════════════════════════════
  function pintarEstadoOdoo() {
    const el = $('vt-odoo-estado');
    if (!el) return;
    el.innerHTML = credenciales()
      ? '<span class="status-dot"></span>ODOO CONECTADO · SOLO LECTURA'
      : '<span class="status-dot" style="background:var(--text-dim);box-shadow:none;"></span>ODOO SIN CONECTAR — configúralo en CONFIGURACIÓN';
    actualizarBotonConciliar();
  }

  function actualizarBotonConciliar() {
    const b = $('btn-ventas-conciliar');
    if (!b || S.ocupado) return;
    const listo = S.archivo && S.analisis && !S.analisis.bloqueante && credenciales()
      && $('vt-desde').value && $('vt-hasta').value && ($('vt-move').checked || $('vt-pos').checked);
    b.disabled = !(listo && puedeOperar());
  }

  async function alElegirArchivo(archivo) {
    if (!archivo) return;
    S.archivo = archivo;
    S.analisis = null;
    $('vt-nombre-archivo').textContent = archivo.name;
    $('vt-zona').classList.add('cargado');
    const estado = $('vt-estado-archivo');
    estado.textContent = 'VALIDANDO…';
    estado.className = 'status-badge status-processing';
    $('vt-validaciones').innerHTML = '';
    try {
      const form = new FormData();
      form.append('dian', archivo);
      const res = await api('/api/ventas/analizar-contenedor', { method: 'POST', body: form });
      S.analisis = await res.json();
      pintarAnalisis(S.analisis);
      anunciar(`Contenedor DIAN validado: ${S.analisis.documentos_incluidos} documentos de venta, ${S.analisis.documentos_excluidos} excluidos.`, 'ok');
    } catch (e) {
      estado.textContent = 'ERROR';
      estado.className = 'status-badge status-error';
      alerta($('vt-validaciones'), e.message);
      anunciar(`Contenedor DIAN: ${e.message}`, 'err');
    }
    actualizarBotonConciliar();
  }

  function pintarAnalisis(a) {
    const estado = $('vt-estado-archivo');
    estado.textContent = a.bloqueante ? 'CON ERRORES' : 'VALIDADO';
    estado.className = `status-badge ${a.bloqueante ? 'status-error' : 'status-ok'}`;
    const icono = { ok: '✓', advertencia: '!', error: '✕', info: 'i' };
    $('vt-validaciones').innerHTML = `
      <div class="vt-resumen-archivo">
        <div><b>${entero(a.documentos_incluidos)}</b><span>DOCUMENTOS DE VENTA</span></div>
        <div><b>${entero(a.documentos_excluidos)}</b><span>EXCLUIDOS POR REGLA</span></div>
        <div><b>${dinero(a.neto)}</b><span>NETO DIAN</span></div>
        <div><b>${esc(a.fecha_desde || '—')} → ${esc(a.fecha_hasta || '—')}</b><span>FECHAS DE EMISIÓN</span></div>
      </div>
      <ul class="vt-lista-validaciones">
        ${a.validaciones.map((v) => `<li class="vt-v-${esc(v.nivel)}"><span class="vt-v-icono">${icono[v.nivel] || '·'}</span><div><div>${esc(v.mensaje)}</div>${v.detalle ? `<small>${esc(v.detalle)}</small>` : ''}</div></li>`).join('')}
      </ul>`;

    $('vt-panel-tipos').style.display = 'block';
    $('vt-resumen-tipos').textContent = `${a.tipos.length} tipos · hoja ${a.hoja}`;
    $('vt-tabla-tipos').innerHTML = `
      <thead><tr><th>Tipo de documento (valor exacto)</th><th class="num">Filas</th><th class="num">Total</th><th>Prefijos</th><th>Tratamiento</th></tr></thead>
      <tbody>${a.tipos.map((t) => `
        <tr class="${t.incluido ? '' : 'vt-fila-excluida'}">
          <td>${esc(t.tipo_documento)}</td><td class="num">${entero(t.filas)}</td><td class="num">${dinero(t.total)}</td>
          <td>${esc((t.prefijos || []).join(', ') || '—')}</td>
          <td><span class="vt-estado ${t.incluido ? 'vt-estado-ok' : 'vt-estado-neutro'}">${t.incluido ? 'INCLUIDO' : 'EXCLUIDO'}</span> ${esc(t.tratamiento)}</td>
        </tr>`).join('')}</tbody>`;

    $('vt-panel-columnas').style.display = 'block';
    const reconocidas = a.mapeo_columnas.filter((c) => c.reconocida).length;
    $('vt-resumen-columnas').textContent = `${reconocidas} de ${a.mapeo_columnas.length} reconocidas`;
    $('vt-tabla-columnas').innerHTML = `
      <thead><tr><th>Columna</th><th>Requerida</th><th>Estado</th><th>Uso en la conciliación</th></tr></thead>
      <tbody>${a.mapeo_columnas.map((c) => `
        <tr><td class="mono">${esc(c.columna)}</td><td>${c.requerida ? 'Sí' : 'No'}</td>
        <td>${c.reconocida ? '<span class="vt-estado vt-estado-ok">RECONOCIDA</span>' : '<span class="vt-estado vt-estado-aviso">NO ESTÁ</span>'}</td>
        <td>${esc(c.uso)}</td></tr>`).join('')}</tbody>`;

    if (a.periodo_sugerido?.desde && !$('vt-desde').value) $('vt-desde').value = a.periodo_sugerido.desde;
    if (a.periodo_sugerido?.hasta && !$('vt-hasta').value) $('vt-hasta').value = a.periodo_sugerido.hasta;
  }

  async function conciliar() {
    if (!S.archivo || !credenciales()) return;
    const boton = $('btn-ventas-conciliar');
    S.ocupado = true;
    cargando(boton, true, 'CONCILIANDO…');
    anunciar('Conciliando ventas DIAN vs Odoo (solo lectura)…', 'msg');
    try {
      const form = new FormData();
      form.append('dian', S.archivo);
      form.append('credentials', credenciales());
      form.append('desde', $('vt-desde').value);
      form.append('hasta', $('vt-hasta').value);
      form.append('tolerancia', $('vt-tolerancia').value || '');
      form.append('incluir_move', $('vt-move').checked ? 'true' : 'false');
      form.append('incluir_pos', $('vt-pos').checked ? 'true' : 'false');
      const res = await api('/api/ventas/conciliar', { method: 'POST', body: form });
      S.resultado = await res.json();
      S.filtro = 'pendientes'; S.busqueda = ''; S.pagina = 0; S.seleccion = null;
      $('vt-buscar').value = '';
      pintarGlobal();
      pintarFiltros();
      pintarTabla();
      $('vt-evidencia').innerHTML = '<div class="panel-body vt-vacio">Elige un documento para ver la evidencia campo a campo.</div>';
      pintarOpcionesFoco();
      $('vt-ia-resultado').innerHTML = '<div class="panel"><div class="panel-body vt-vacio">Sin investigación todavía para esta conciliación.</div></div>';
      $('vt-panel-traza').style.display = 'none';
      habilitarPestanas(true);
      irA('global');
      const d = S.resultado.descomposicion;
      anunciar(`Conciliación de ventas lista: diferencia neta ${dinero(d.diferencia_neta)}, residuo ${dinero(d.residuo)}.`, 'ok');
    } catch (e) {
      alerta($('vt-validaciones'), e.message);
      anunciar(`Conciliación de ventas: ${e.message}`, 'err');
    } finally {
      S.ocupado = false;
      cargando(boton, false);
      actualizarBotonConciliar();
    }
  }

  // ════════════════════════════════════════════
  // 2. CONCILIACIÓN GLOBAL
  // ════════════════════════════════════════════
  function pintarGlobal() {
    const r = S.resultado;
    const d = r.descomposicion;
    const metrica = (codigo) => r.metricas.find((m) => m.codigo === codigo) || {};
    const neto = metrica('total_neto');

    $('vt-alcance').innerHTML = r.alcance.map((a) => `
      <div class="vt-alcance-item ${a.ok ? 'ok' : 'no'}" title="${esc(a.detalle)}">
        <span>${a.ok ? '✓' : '!'}</span><div><b>${esc(a.etiqueta)}</b><small>${esc(a.detalle)}</small></div>
      </div>`).join('');

    $('vt-kpis').innerHTML = `
      <div class="metrica"><div class="metrica-val">${dinero(neto.dian)}</div><div class="metrica-label">NETO DIAN · ${entero(r.resumen.documentos_dian_elegibles)} DOCS</div></div>
      <div class="metrica"><div class="metrica-val">${dinero(neto.odoo)}</div><div class="metrica-label">NETO ODOO · ${entero(r.resumen.documentos_odoo_canonicos)} DOCS</div></div>
      <div class="metrica"><div class="metrica-val ${Number(d.diferencia_neta) === 0 ? 'ok' : 'danger'}">${dinero(d.diferencia_neta)}</div><div class="metrica-label">DIFERENCIA NETA (DIAN − ODOO)</div></div>
      <div class="metrica"><div class="metrica-val ${d.cuadra ? 'ok' : 'danger'}">${dinero(d.residuo)}</div><div class="metrica-label">RESIDUO NO EXPLICADO</div></div>`;

    $('vt-tabla-metricas').innerHTML = `
      <thead><tr><th>Métrica</th><th class="num">DIAN</th><th class="num">Odoo</th><th class="num">Diferencia</th><th class="num">%</th></tr></thead>
      <tbody>${r.metricas.map((m) => {
        const cantidad = m.codigo.startsWith('cantidad');
        const f = (v) => (v === null || v === undefined) ? '<span class="vt-tenue">requiere mapeo</span>' : (cantidad ? entero(v) : dinero(v));
        return `<tr title="${esc(m.nota || '')}"><td>${esc(m.etiqueta)}${m.nota ? ' <span class="vt-tenue">ⓘ</span>' : ''}</td><td class="num">${f(m.dian)}</td><td class="num">${f(m.odoo)}</td>
          <td class="num ${claseSigno(m.diferencia)}">${m.diferencia === null ? '—' : f(m.diferencia)}</td><td class="num">${m.diferencia_porcentual ?? '—'}</td></tr>`;
      }).join('')}</tbody>`;

    const cuadra = $('vt-cuadra');
    cuadra.textContent = d.cuadra ? 'CUADRA · RESIDUO 0' : 'NO CUADRA';
    cuadra.className = `status-badge ${d.cuadra ? 'status-ok' : 'status-error'}`;
    $('vt-formula').innerHTML = `${esc(d.formula)}<br><b>${dinero(d.diferencia_neta)}</b> = <b>${dinero(d.explicado)}</b> + <b>${dinero(d.residuo)}</b>`;
    $('vt-tabla-descomposicion').innerHTML = `
      <thead><tr><th>Componente</th><th class="num">Docs</th><th class="num">Monto (DIAN − Odoo)</th><th class="num">Peso</th><th></th></tr></thead>
      <tbody>${d.componentes.map((c) => `
        <tr class="${c.con_diferencia ? '' : 'vt-fila-excluida'}">
          <td>${esc(c.etiqueta)} <span class="vt-tenue">${esc(c.signo)}</span></td>
          <td class="num">${entero(c.documentos)}</td>
          <td class="num ${claseSigno(c.monto)}">${dinero(c.monto)}</td>
          <td class="num">${c.participacion ? `${esc(c.participacion)} %` : '—'}</td>
          <td>${c.con_diferencia ? `<button class="vt-link" data-vt-componente="${esc(c.codigo)}">ver casos</button>` : ''}</td>
        </tr>`).join('')}
        <tr class="vt-fila-total"><td>Residuo no explicado</td><td></td><td class="num ${claseSigno(d.residuo)}">${dinero(d.residuo)}</td><td></td><td></td></tr>
      </tbody>`;

    $('vt-tabla-prefijos').innerHTML = `
      <thead><tr><th>Prefijo</th><th>Origen en Odoo</th><th class="num">Facturas</th><th class="num">NC</th><th class="num">Neto DIAN</th><th class="num">Neto Odoo</th><th class="num">Diferencia</th><th>Folios</th><th class="num">Ausentes</th><th></th></tr></thead>
      <tbody>${r.prefijos.map((p) => `
        <tr><td class="mono">${esc(p.prefijo)}</td><td>${esc(p.origen || '—')}</td>
        <td class="num">${entero(p.facturas_dian)}</td><td class="num">${entero(p.notas_credito_dian)}</td>
        <td class="num">${dinero(p.neto_dian)}</td><td class="num">${dinero(p.neto_odoo)}</td>
        <td class="num ${claseSigno(p.diferencia)}">${dinero(p.diferencia)}</td>
        <td class="mono">${p.folio_minimo != null ? `${p.folio_minimo}–${p.folio_maximo}` : '—'}</td>
        <td class="num ${p.ausentes_en_secuencia ? 'vt-neg' : ''}">${entero(p.ausentes_en_secuencia)}</td>
        <td><button class="vt-link" data-vt-prefijo="${esc(p.prefijo)}">ver</button></td></tr>`).join('')}</tbody>`;

    const alertas = r.alertas || [];
    $('vt-panel-alertas').style.display = alertas.length ? 'block' : 'none';
    $('vt-alertas').innerHTML = alertas.slice(0, 30).map((a) => `<div class="vt-alerta-item"><span class="vt-chip">${esc(a.codigo)}</span> ${esc(a.mensaje)}</div>`).join('')
      + (alertas.length > 30 ? `<div class="vt-tenue">… y ${alertas.length - 30} más en el reporte.</div>` : '');
  }

  async function descargarReporte() {
    if (!S.resultado) return;
    const boton = $('btn-ventas-reporte');
    cargando(boton, true, 'GENERANDO…');
    try {
      const res = await api(`/api/ventas/reporte?run_id=${encodeURIComponent(S.resultado.run_id)}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `conciliacion_ventas_${S.resultado.parametros.desde}_${S.resultado.parametros.hasta}.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      anunciar('Reporte de ventas descargado.', 'ok');
    } catch (e) {
      anunciar(`Reporte de ventas: ${e.message}`, 'err');
      alerta($('vt-alcance'), e.message);
    } finally {
      cargando(boton, false);
    }
  }

  // ════════════════════════════════════════════
  // 3. DETALLE
  // ════════════════════════════════════════════
  function documentos() { return S.resultado ? S.resultado.documentos : []; }

  function filasFiltradas() {
    const q = S.busqueda.trim().toLowerCase();
    return documentos().filter((f) => {
      if (S.filtro === 'pendientes' && !PENDIENTES.includes(f.estado)) return false;
      if (S.filtro.startsWith('componente:') && f.componente !== S.filtro.slice(11)) return false;
      if (S.filtro.startsWith('prefijo:') && f.prefijo !== S.filtro.slice(8)) return false;
      if (!['pendientes', 'todos'].includes(S.filtro) && !S.filtro.includes(':') && f.estado !== S.filtro) return false;
      if (!q) return true;
      return [f.numero, f.prefijo, f.odoo_nombre, f.nota, f.id].some((v) => String(v || '').toLowerCase().includes(q));
    });
  }

  function pintarFiltros() {
    const conteo = {};
    documentos().forEach((f) => { conteo[f.estado] = (conteo[f.estado] || 0) + 1; });
    const pend = documentos().filter((f) => PENDIENTES.includes(f.estado)).length;
    const chips = [['pendientes', `Pendientes (${pend})`], ['todos', `Todos (${documentos().length})`]];
    Object.keys(S.resultado.etiquetas_estado).forEach((e) => { if (conteo[e]) chips.push([e, `${etqEstado(e)} (${conteo[e]})`]); });
    if (S.filtro.includes(':')) chips.push([S.filtro, S.filtro.replace('componente:', 'Componente ').replace('prefijo:', 'Prefijo ')]);
    $('vt-filtros').innerHTML = chips.map(([k, t]) => `<button class="vt-filtro ${S.filtro === k ? 'active' : ''}" data-vt-filtro="${esc(k)}">${esc(t)}</button>`).join('');
  }

  function pintarTabla() {
    const filas = filasFiltradas();
    const total = filas.length;
    const paginas = Math.max(1, Math.ceil(total / POR_PAGINA));
    S.pagina = Math.min(S.pagina, paginas - 1);
    const pagina = filas.slice(S.pagina * POR_PAGINA, (S.pagina + 1) * POR_PAGINA);
    $('vt-tabla-detalle').innerHTML = `
      <thead><tr><th>Estado</th><th>Número</th><th>Tipo</th><th>Fecha</th><th class="num">Total DIAN</th><th class="num">Total Odoo</th><th class="num">Diferencia</th><th>Método</th><th>Registro Odoo</th><th>Nota</th></tr></thead>
      <tbody>${pagina.map((f) => `
        <tr data-vt-ref="${esc(f.id)}" class="${S.seleccion === f.id ? 'vt-seleccionada' : ''}">
          <td class="nw">${badgeEstado(f.estado)}</td><td class="mono nw">${esc(f.numero)}</td><td class="nw">${esc(ETQ_TIPO[f.tipo] || f.tipo)}</td>
          <td class="mono nw">${esc(f.fecha || '—')}</td><td class="num">${dinero(f.total_dian)}</td><td class="num">${dinero(f.total_odoo)}</td>
          <td class="num ${claseSigno(f.diferencia_total)}">${f.diferencia_total == null ? '—' : dinero(f.diferencia_total)}</td>
          <td class="nw">${esc(ETQ_METODO[f.metodo] || f.metodo)}</td><td class="mono nw">${esc(f.odoo_nombre || '—')}</td>
          <td class="vt-nota-celda">${esc(f.nota || '')}</td>
        </tr>`).join('') || '<tr><td colspan="10" class="vt-vacio">Sin documentos para este filtro.</td></tr>'}</tbody>`;
    $('vt-pagina-info').textContent = total ? `Mostrando ${S.pagina * POR_PAGINA + 1} a ${Math.min(total, (S.pagina + 1) * POR_PAGINA)} de ${total}` : 'Sin resultados';
    $('btn-vt-anterior').disabled = S.pagina === 0;
    $('btn-vt-siguiente').disabled = S.pagina >= paginas - 1;
  }

  async function verDocumento(ref) {
    S.seleccion = ref;
    pintarTabla();
    const panel = $('vt-evidencia');
    panel.innerHTML = '<div class="panel-body vt-vacio"><div class="btn-loader" style="margin:0 auto;"></div></div>';
    try {
      const res = await api(`/api/ventas/documento?run_id=${encodeURIComponent(S.resultado.run_id)}&ref=${encodeURIComponent(ref)}`);
      pintarEvidencia(await res.json());
    } catch (e) {
      panel.innerHTML = `<div class="panel-body"><div class="alerta alerta-error">${esc(e.message)}</div></div>`;
    }
  }

  function pintarEvidencia(d) {
    const resultado = { ok: '✓', diferente: '✕', redondeo: '≈', no_aplica: '—', no_disponible: '?', informativo: 'i' };
    const lado = (titulo, x, campos) => x ? `
      <div class="vt-lado"><div class="vt-lado-titulo">${titulo}</div>
        ${campos.map(([k, v]) => `<div class="vt-kv"><span>${esc(k)}</span><b>${v}</b></div>`).join('')}</div>` : `<div class="vt-lado vt-lado-vacio"><div class="vt-lado-titulo">${titulo}</div>Sin registro</div>`;
    $('vt-evidencia').innerHTML = `
      <div class="panel-header"><div class="panel-title">${esc(d.numero)} ${badgeEstado(d.estado)}</div>
        <span class="vt-chip">${esc(ETQ_METODO[d.metodo] || d.metodo)}</span></div>
      <div class="panel-body">
        <div class="vt-lados">
          ${lado('DIAN', d.dian, d.dian ? [['Tipo', esc(d.tipo_documento)], ['CUFE', `<span class="mono">${esc(d.dian.cufe_corto || '—')}</span>`],
            ['Total', dinero(d.dian.total)], ['IVA', dinero(d.dian.iva)], ['INC', dinero(d.dian.inc)], ['Base derivada', dinero(d.dian.base_derivada)],
            ['Recepción', esc(d.dian.fecha_recepcion || '—')], ['Regla', esc(d.dian.motivo)]] : [])}
          ${lado('ODOO', d.odoo, d.odoo ? [['Registro', `<span class="mono">${esc(d.odoo.nombre)}</span>`], ['Modelo', esc(d.odoo.modelo)],
            ['CUFE', `<span class="mono">${esc(d.odoo.cufe_corto || '—')}</span>`], ['Total', dinero(d.odoo.total)], ['Impuestos', dinero(d.odoo.impuestos)],
            ['Estado', esc(d.odoo.estado)], ['Estado electrónico', esc(d.odoo.estado_electronico || '—')], ['Diario / POS', esc(d.odoo.diario || '—')],
            ['Orígenes', esc((d.odoo.origenes || []).join(', '))]] : [])}
        </div>
        ${d.validaciones?.length ? `<table class="vt-tabla vt-tabla-compacta"><thead><tr><th>Campo</th><th>DIAN</th><th>Odoo</th><th></th></tr></thead><tbody>
          ${d.validaciones.map((v) => `<tr class="vt-r-${esc(v.resultado)}"><td>${esc(v.campo)}</td><td class="mono">${esc(v.dian)}</td><td class="mono">${esc(v.odoo)}</td>
            <td title="${esc(v.nota || '')}">${resultado[v.resultado] || esc(v.resultado)}${v.diferencia && v.diferencia !== '0.00' ? ` ${dinero(v.diferencia)}` : ''}</td></tr>`).join('')}
        </tbody></table>` : ''}
        ${d.evidencia?.length ? `<div class="vt-subtitulo">EVIDENCIA</div><ul class="vt-evidencia-lista">${d.evidencia.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}
        ${d.candidatos_detalle?.length ? `<div class="vt-subtitulo">CANDIDATOS EN ODOO</div>${d.candidatos_detalle.map((c) => `
          <div class="vt-candidato"><span class="mono">${esc(c.nombre)}</span> · ${esc(c.modelo)} · ${dinero(c.total)} · ${esc(c.estado)}${c.motivo_alcance ? ` · ${esc(c.motivo_alcance)}` : ''}</div>`).join('')}` : ''}
        ${PENDIENTES.includes(d.estado) ? `<button class="btn btn-outline vt-btn-ia" data-vt-investigar="${esc(d.id)}" style="margin-top:0.75rem;">INVESTIGAR ESTE DOCUMENTO CON CLAUDE</button>` : ''}
      </div>`;
  }

  // ════════════════════════════════════════════
  // 4. AGENTE CLAUDE
  // ════════════════════════════════════════════
  async function cargarEstadoIa() {
    const badge = $('vt-ia-estado');
    try {
      if (!S.estadoIa) S.estadoIa = await (await api('/api/ventas/estado-ia')).json();
      badge.textContent = S.estadoIa.configurada ? 'ACTIVO' : 'NO CONFIGURADO';
      badge.className = `status-badge ${S.estadoIa.configurada ? 'status-ok' : ''}`;
      $('vt-ia-nota').textContent = S.estadoIa.configurada
        ? `Modelo ${S.estadoIa.modelo} · máximo ${S.estadoIa.limites.herramientas} consultas por investigación. Envía a Claude solo agregados y los documentos que consulta; NIT enmascarados y sin nombres.`
        : 'Falta ANTHROPIC_API_KEY en el servidor. La conciliación y el reporte funcionan igual.';
    } catch (_) {
      badge.textContent = 'SIN DATOS';
    }
    $('btn-ventas-investigar').disabled = !(S.resultado && S.estadoIa?.configurada && puedeOperar());
  }

  function pintarOpcionesFoco() {
    const tipo = $('vt-foco-tipo').value;
    const sel = $('vt-foco-valor');
    const ref = $('vt-foco-ref');
    sel.style.display = (tipo === 'componente' || tipo === 'prefijo') ? 'block' : 'none';
    ref.style.display = tipo === 'documento' ? 'block' : 'none';
    if (!S.resultado) return;
    if (tipo === 'componente') {
      sel.innerHTML = S.resultado.descomposicion.componentes.filter((c) => c.con_diferencia)
        .map((c) => `<option value="${esc(c.codigo)}">${esc(c.etiqueta)} · ${dinero(c.monto)}</option>`).join('') || '<option value="">Sin componentes con diferencia</option>';
    } else if (tipo === 'prefijo') {
      sel.innerHTML = S.resultado.prefijos.map((p) => `<option value="${esc(p.prefijo)}">${esc(p.prefijo)} · ${dinero(p.diferencia)}</option>`).join('');
    }
  }

  async function investigar() {
    if (!S.resultado) return;
    const boton = $('btn-ventas-investigar');
    const tipo = $('vt-foco-tipo').value;
    const valor = tipo === 'documento' ? $('vt-foco-ref').value.trim() : (tipo === 'global' ? '' : $('vt-foco-valor').value);
    if (tipo !== 'global' && !valor) { alerta($('vt-ia-resultado'), 'Elige el foco de la investigación.'); return; }
    cargando(boton, true, 'CLAUDE INVESTIGANDO…');
    $('vt-ia-resultado').innerHTML = '<div class="panel"><div class="panel-body vt-vacio"><div class="btn-loader" style="margin:0 auto 8px;"></div>Claude consulta la evidencia con herramientas de solo lectura (máximo unos 2 minutos)…</div></div>';
    anunciar('Investigación de Claude en curso…', 'msg');
    try {
      const form = new FormData();
      form.append('run_id', S.resultado.run_id);
      if (tipo !== 'global') { form.append('foco_tipo', tipo); form.append('foco_valor', valor); }
      const pregunta = $('vt-pregunta').value.trim();
      if (pregunta) form.append('pregunta', pregunta);
      if (credenciales()) form.append('credentials', credenciales());
      const res = await api('/api/ventas/investigar', { method: 'POST', body: form });
      const h = await res.json();
      pintarIa(h);
      anunciar(h.disponible ? `Claude devolvió ${h.resultado.hallazgos.length} hallazgos.` : h.mensaje, h.disponible ? 'ok' : 'warn');
    } catch (e) {
      $('vt-ia-resultado').innerHTML = `<div class="panel"><div class="panel-body"><div class="alerta alerta-error">${esc(e.message)}</div><div class="vt-tenue">La conciliación determinística sigue disponible y el reporte se puede descargar.</div></div></div>`;
      anunciar(`Claude: ${e.message}`, 'err');
    } finally {
      cargando(boton, false);
      $('btn-ventas-investigar').disabled = !(S.resultado && S.estadoIa?.configurada && puedeOperar());
    }
  }

  function chipsEvidencia(lista) {
    return (lista || []).map((e) => e.startsWith('dian:') || e.startsWith('odoo:')
      ? `<button class="vt-evid" data-vt-ref-ia="${esc(e)}">${esc(e)}</button>` : `<span class="vt-evid">${esc(e)}</span>`).join(' ');
  }

  function pintarIa(h) {
    const cont = $('vt-ia-resultado');
    pintarTraza(h);
    if (!h.disponible) {
      cont.innerHTML = `<div class="panel"><div class="panel-body">
        <div class="alerta alerta-info vt-alerta">${esc(h.mensaje)}</div>
        ${(h.detalle || []).length ? `<ul class="vt-evidencia-lista">${h.detalle.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
        <div class="vt-tenue">La conciliación determinística sigue disponible y el reporte se puede descargar.</div></div></div>`;
      return;
    }
    const r = h.resultado;
    const v = h.validacion || {};
    cont.innerHTML = `
      <div class="panel vt-ia-resumen"><div class="panel-header"><div class="panel-title"><span class="vt-tag vt-tag-ia">CLAUDE</span> CONCLUSIÓN</div><span class="vt-chip">${esc(h.modelo)}</span></div>
        <div class="panel-body">
          <p>${esc(r.resumen_general)}</p>
          ${h.aviso_odoo ? `<div class="vt-tenue">${esc(h.aviso_odoo)}</div>` : ''}
          ${r.siguiente_conciliacion ? `<div class="vt-siguiente"><b>Después de corregir:</b> ${esc(r.siguiente_conciliacion)}</div>` : ''}
          ${(r.datos_faltantes || []).length ? `<div class="vt-subtitulo">DATOS QUE FALTARON</div><ul class="vt-evidencia-lista">${r.datos_faltantes.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
          ${(v.advertencias || []).length ? `<div class="vt-validacion">${v.advertencias.map((a) => `<div>⚠ ${esc(a)}</div>`).join('')}</div>` : ''}
          ${(v.descartados || []).length ? `<div class="vt-validacion">${v.descartados.map((d) => `<div>✕ Hallazgo descartado por el backend: «${esc(d.resumen)}» — ${esc(d.motivo)}</div>`).join('')}</div>` : ''}
        </div></div>
      ${r.hallazgos.map((x, i) => {
        const imp = x.financial_impact || {};
        return `
        <div class="panel vt-hallazgo vt-riesgo-${esc(x.risk_level)}">
          <div class="panel-header"><div class="panel-title">${i + 1}. ${esc(x.summary)}</div>
            <div class="vt-hallazgo-meta"><span class="vt-chip">${esc(x.etiqueta_tipo)}</span>
              <span class="vt-chip vt-causa-${esc(x.cause_status)}">Causa ${esc(ETQ_CAUSA[x.cause_status] || x.cause_status)}</span>
              <span class="vt-chip">Riesgo ${esc(ETQ_RIESGO[x.risk_level] || x.risk_level)}</span>
              <span class="vt-chip" title="${esc(r.significado_confianza?.[x.confidence] || '')} ${esc(r.significado_confianza?.nota || '')}">Confianza ${esc(ETQ_NIVEL[x.confidence] || x.confidence)}</span></div></div>
          <div class="panel-body">
            <div class="vt-impacto"><span class="vt-tag vt-tag-regla">IMPACTO CALCULADO POR REGLAS</span>
              ${dinero(imp.documentos_citados?.monto)} en ${entero(imp.documentos_citados?.documentos)} documento(s) citado(s)
              ${imp.componente ? ` · componente «${esc(imp.componente.etiqueta)}»: ${dinero(imp.componente.monto)} (${entero(imp.componente.documentos)} docs)` : ''}</div>
            ${x.facts.length ? `<div class="vt-subtitulo">HECHOS CON EVIDENCIA</div>${x.facts.map((f) => `<div class="vt-hecho"><div>${esc(f.texto)}</div><div class="vt-evids">${chipsEvidencia(f.evidencia)}</div></div>`).join('')}` : ''}
            ${x.probable_causes.length ? `<div class="vt-subtitulo">HIPÓTESIS</div>${x.probable_causes.map((c) => `
              <div class="vt-hipotesis"><span class="vt-tag vt-tag-hipotesis">SIN CONFIRMAR · ${esc(ETQ_NIVEL[c.confianza] || c.confianza)}</span> ${esc(c.descripcion)}
                <div class="vt-tenue">Cómo confirmarla: ${esc(c.como_confirmar)}</div></div>`).join('')}` : ''}
            ${x.recommended_actions.length ? `<div class="vt-subtitulo">ACCIONES PROPUESTAS (LAS EJECUTA UNA PERSONA)</div>${x.recommended_actions.map((a) => `
              <div class="vt-accion"><span class="vt-tag vt-tag-accion">PENDIENTE</span> ${esc(a.accion)}
                <div class="vt-tenue">Registro: ${chipsEvidencia([a.registro_afectado])} · Verificación: ${esc(a.verificacion)}</div></div>`).join('')}` : ''}
            ${x.verification_steps.length ? `<div class="vt-subtitulo">VERIFICACIÓN</div><ol class="vt-evidencia-lista">${x.verification_steps.map((p) => `<li>${esc(p)}</li>`).join('')}</ol>` : ''}
            ${x.advertencias.length ? `<div class="vt-validacion">${x.advertencias.map((a) => `<div>⚠ ${esc(a)}</div>`).join('')}</div>` : ''}
            ${x.requires_human_review ? '<div class="vt-revision">Requiere revisión humana antes de actuar.</div>' : ''}
          </div>
        </div>`;
      }).join('')}`;
  }

  function pintarTraza(h) {
    const traza = h.traza || [];
    $('vt-panel-traza').style.display = (traza.length || h.uso) ? 'block' : 'none';
    const u = h.uso || {};
    $('vt-uso').textContent = `${u.herramientas || 0}/${u.limite_herramientas || '—'} · ${u.segundos ?? '—'} s`;
    $('vt-traza').innerHTML = traza.map((t) => `
      <div class="vt-paso ${t.ok ? '' : 'vt-paso-error'}"><span class="mono">${esc(t.herramienta)}</span>
        <small>${esc(Object.entries(t.parametros || {}).map(([k, v]) => `${k}=${v}`).join(' · '))}</small>
        <small class="vt-tenue">${esc(t.resumen || '')}</small></div>`).join('')
      + (u.llamadas_modelo ? `<div class="vt-tenue" style="margin-top:6px;">${u.llamadas_modelo} llamadas al modelo · ${entero(u.input_tokens)} tokens de entrada (${entero(u.cache_read_input_tokens)} desde caché) · ${entero(u.output_tokens)} de salida</div>` : '');
  }

  // ════════════════════════════════════════════
  // EVENTOS
  // ════════════════════════════════════════════
  document.addEventListener('DOMContentLoaded', () => {
    if (!$('view-ventas')) return;

    document.querySelectorAll('.vt-tab').forEach((b) => b.addEventListener('click', () => { if (!b.disabled) irA(b.dataset.vtTab); }));
    $('nav-ventas')?.addEventListener('click', () => setTimeout(pintarEstadoOdoo, 50));

    $('vt-archivo').addEventListener('change', (e) => alElegirArchivo(e.target.files[0]));
    ['vt-desde', 'vt-hasta', 'vt-move', 'vt-pos'].forEach((id) => $(id).addEventListener('change', actualizarBotonConciliar));
    $('btn-ventas-conciliar').addEventListener('click', conciliar);
    $('btn-ventas-reporte').addEventListener('click', descargarReporte);
    $('btn-ventas-ir-detalle').addEventListener('click', () => irA('detalle'));
    $('btn-ventas-ir-agente').addEventListener('click', () => irA('agente'));

    $('vt-global').addEventListener('click', (e) => {
      const comp = e.target.closest('[data-vt-componente]');
      const pref = e.target.closest('[data-vt-prefijo]');
      if (comp) { S.filtro = `componente:${comp.dataset.vtComponente}`; }
      else if (pref) { S.filtro = `prefijo:${pref.dataset.vtPrefijo}`; }
      else return;
      S.pagina = 0; pintarFiltros(); pintarTabla(); irA('detalle');
    });
    $('vt-filtros').addEventListener('click', (e) => {
      const b = e.target.closest('[data-vt-filtro]');
      if (!b) return;
      S.filtro = b.dataset.vtFiltro; S.pagina = 0; pintarFiltros(); pintarTabla();
    });
    let espera = null;
    $('vt-buscar').addEventListener('input', (e) => {
      clearTimeout(espera);
      espera = setTimeout(() => { S.busqueda = e.target.value; S.pagina = 0; pintarTabla(); }, 200);
    });
    $('btn-vt-anterior').addEventListener('click', () => { S.pagina = Math.max(0, S.pagina - 1); pintarTabla(); });
    $('btn-vt-siguiente').addEventListener('click', () => { S.pagina += 1; pintarTabla(); });
    $('vt-tabla-detalle').addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-vt-ref]');
      if (tr) verDocumento(tr.dataset.vtRef);
    });
    $('vt-evidencia').addEventListener('click', (e) => {
      const b = e.target.closest('[data-vt-investigar]');
      if (!b) return;
      $('vt-foco-tipo').value = 'documento';
      pintarOpcionesFoco();
      $('vt-foco-ref').value = b.dataset.vtInvestigar;
      irA('agente');
    });

    $('vt-foco-tipo').addEventListener('change', pintarOpcionesFoco);
    $('btn-ventas-investigar').addEventListener('click', investigar);
    $('vt-ia-resultado').addEventListener('click', (e) => {
      const b = e.target.closest('[data-vt-ref-ia]');
      if (!b || !S.resultado) return;
      irA('detalle');
      verDocumento(b.dataset.vtRefIa);
    });
    pintarEstadoOdoo();
  });
})();
