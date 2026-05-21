/* ═══════════════════════════════════════════════════════════════
   ODOO CONNECTOR — FactuMatch
   Maneja la integración con Odoo ERP vía API XML-RPC (backend).
   Depende de: API_URL, archivoDian, currentUser, log() — definidos en app.js
   ═══════════════════════════════════════════════════════════════ */

let odooCredentials = null;   // Token encriptado (Fernet) guardado en localStorage
let odooConnected = false;    // Flag de estado de conexión

// ─────────────────────────────────────────────
// HELPERS DE UI
// ─────────────────────────────────────────────

function setOdooStatus(connected, version) {
  const badge    = document.getElementById('odoo-status-badge');
  const dot      = document.getElementById('odoo-conn-dot');
  const label    = document.getElementById('odoo-conn-label');
  const comparePanel = document.getElementById('odoo-compare-panel');
  const configPanel = document.getElementById('odoo-config-panel');
  const saveBtn  = document.getElementById('btn-odoo-save');
  const odooBtn  = document.getElementById('btn-odoo-comparar');
  const dlBtn    = document.getElementById('btn-odoo-download');
  const inputFields = ['odoo-url', 'odoo-db', 'odoo-user', 'odoo-key'];

  if (connected) {
    if (badge)  { badge.textContent = 'CONECTADO'; badge.className = 'status-badge active'; }
    if (dot)    { dot.style.background = 'var(--green)'; dot.style.boxShadow = '0 0 6px var(--green)'; }
    if (label)  { label.textContent = `ODOO ${version || ''} — CREDENCIALES CONFIGURADAS`; label.style.color = 'var(--green)'; }
    if (comparePanel) comparePanel.style.display = 'block';
    if (configPanel) configPanel.style.display = 'none';  // Ocultar panel de configuración
    if (saveBtn) saveBtn.disabled = false;
    if (odooBtn) odooBtn.disabled = false;
    if (dlBtn)   dlBtn.disabled = false;
    
    // Deshabilitar campos cuando está conectado
    inputFields.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = true;
    });
  } else {
    if (badge)  { badge.textContent = 'SIN CONFIGURAR'; badge.className = 'status-badge'; badge.style.color = 'var(--text-dim)'; badge.style.borderColor = 'var(--border)'; }
    if (dot)    { dot.style.background = 'var(--text-dim)'; dot.style.boxShadow = 'none'; }
    if (label)  { label.textContent = 'SIN CONFIGURAR — IR A CONFIGURACIÓN'; label.style.color = 'var(--text-dim)'; }
    if (comparePanel) comparePanel.style.display = 'none';
    if (configPanel) configPanel.style.display = 'block';  // Mostrar panel de configuración
    if (odooBtn) odooBtn.disabled = true;
    if (dlBtn)   dlBtn.disabled = true;
    
    // Habilitar campos cuando NO está conectado
    inputFields.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = false;
    });
  }
}

// ─────────────────────────────────────────────
// RESTAURAR SESIÓN ODOO AL CARGAR
// ─────────────────────────────────────────────

async function checkOdooSession() {
  // 1. Intentar restaurar de forma local
  const saved = localStorage.getItem('odoo_credentials');
  if (saved) {
    odooCredentials = saved;
    odooConnected = true;
    setOdooStatus(true);
    log('Sesión Odoo restaurada localmente.', 'ok');
    return;
  }

  // 2. Si no hay local, consultar la configuración global en la nube (GAS)
  log('Buscando configuración global de Odoo en la nube...', 'msg');
  try {
    const res = await callGASRobust("getOdooConfig");
    if (res.success && res.data && res.data.configured) {
      odooCredentials = res.data.encrypted_credentials;
      odooConnected   = true;
      localStorage.setItem('odoo_credentials', odooCredentials);
      setOdooStatus(true);
      log('Conexión Odoo restaurada desde la nube.', 'ok');
      // Marcar campos como no editables y ocultar la API key por seguridad
      ['odoo-url','odoo-db','odoo-user','odoo-key'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
          el.value = id === 'odoo-key' ? '••••••••••••••••' : el.value;
          el.disabled = true;
        }
      });
      const saveBtn = document.getElementById('btn-odoo-save');
      if (saveBtn) saveBtn.disabled = false; // allow re-saving if user explicitly wants
    } else {
      setOdooStatus(false);
      log('Sin configuración global de Odoo.', 'msg');
    }
  } catch (e) {
    console.error("Fallo al obtener configuración global de Odoo:", e);
    setOdooStatus(false);
  }
}
checkOdooSession();

// ─────────────────────────────────────────────
// PROBAR CONEXIÓN
// ─────────────────────────────────────────────

document.getElementById('btn-odoo-test').addEventListener('click', async () => {
  const url  = document.getElementById('odoo-url').value.trim();
  const db   = document.getElementById('odoo-db').value.trim();
  const user = document.getElementById('odoo-user').value.trim();
  const key  = document.getElementById('odoo-key').value.trim();
  const msgDiv = document.getElementById('odoo-msg');

  if (!url || !db || !user || !key) {
    msgDiv.innerHTML = '<span class="t-err">COMPLETE TODOS LOS CAMPOS</span>';
    return;
  }

  const btn = document.getElementById('btn-odoo-test');
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> PROBANDO...';
  msgDiv.innerHTML = '';

  try {
    const body = new URLSearchParams({
      credentials: JSON.stringify({ url, database: db, username: user, api_key: key })
    });
    const res  = await fetch(`${API_URL}/odoo/test-connection`, { method: 'POST', body });
    const data = await res.json();

    if (data.success) {
      msgDiv.innerHTML = `<span class="t-ok">✔ CONEXIÓN EXITOSA — Odoo v${data.odoo_version || '?'}</span>`;
      // Habilitar botón guardar tras prueba exitosa
      document.getElementById('btn-odoo-save').disabled = false;
      log(`Odoo conectado: v${data.odoo_version || '?'}`, 'ok');
    } else {
      msgDiv.innerHTML = `<span class="t-err">✘ ERROR: ${data.error}</span>`;
      log(`Error Odoo: ${data.error}`, 'err');
    }
  } catch (e) {
    msgDiv.innerHTML = `<span class="t-err">✘ Sin respuesta del servidor: ${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg> PROBAR CONEXIÓN';
  }
});

// ─────────────────────────────────────────────
// GUARDAR CREDENCIALES (encriptadas)
// ─────────────────────────────────────────────

document.getElementById('btn-odoo-save').addEventListener('click', async () => {
  const url  = document.getElementById('odoo-url').value.trim();
  const db   = document.getElementById('odoo-db').value.trim();
  const user = document.getElementById('odoo-user').value.trim();
  const key  = document.getElementById('odoo-key').value.trim();
  const msgDiv = document.getElementById('odoo-msg');

  const btn = document.getElementById('btn-odoo-save');
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> GUARDANDO...';

  try {
    const body = new URLSearchParams({
      credentials: JSON.stringify({ url, database: db, username: user, api_key: key }),
      user_id: currentUser?.id || 'default'
    });
    const res  = await fetch(`${API_URL}/odoo/save-connection`, { method: 'POST', body });
    const data = await res.json();

    if (data.success) {
      odooCredentials = data.encrypted_credentials;
      odooConnected   = true;
      localStorage.setItem('odoo_credentials', odooCredentials);

      // Guardar de forma centralizada en Google Sheets (GAS) para toda la organización
      log('Sincronizando configuración de Odoo con la nube...', 'msg');
      const gasRes = await callGASRobust("saveOdooConfig", { encrypted_credentials: odooCredentials });
      if (gasRes.success) {
        log('Configuración Odoo sincronizada en la nube.', 'ok');
      } else {
          log('Advertencia: No se pudo sincronizar en la nube: ' + gasRes.error, 'warn');
          // Mostrar detalle al usuario
          msgDiv.innerHTML = `<span class="t-warn">Guardado local OK, pero fallo al sincronizar con la nube: ${gasRes.error}</span>`;
          // Dejar los campos editables para que el usuario pueda reintentar
          return;
      }

      // Ocultar API key por seguridad
        ['odoo-url','odoo-db','odoo-user','odoo-key'].forEach(id => {
          const el = document.getElementById(id);
          if (el) {
            if (id === 'odoo-key') {
              el.value = '••••••••••••••••';
              el.type = 'password';
            }
            el.disabled = true;
          }
        });

      msgDiv.innerHTML = '<span class="t-ok">✔ CREDENCIALES GUARDADAS Y COMPARTIDAS EN LA NUBE</span>';
      setOdooStatus(true, data.odoo_version);
      log('Credenciales Odoo encriptadas y guardadas.', 'ok');
    } else {
      msgDiv.innerHTML = `<span class="t-err">✘ ERROR: ${data.error || data.detail}</span>`;
    }
  } catch (e) {
    msgDiv.innerHTML = `<span class="t-err">✘ ${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M17 16v2a2 2 0 01-2 2H5a2 2 0 01-2-2v-2m3-5l4 4 4-4m-4 4V4"/></svg> GUARDAR CREDENCIALES';
  }
});

// ─────────────────────────────────────────────
// CAMBIAR CONFIGURACIÓN ODOO (sin desconectar)
// ─────────────────────────────────────────────

document.getElementById('btn-odoo-edit').addEventListener('click', () => {
  // Permitir edición: mostrar panel de configuración y habilitar campos
  // Sin borrar las credenciales guardadas
  const configPanel = document.getElementById('odoo-config-panel');
  const comparePanel = document.getElementById('odoo-compare-panel');
  
  if (configPanel) configPanel.style.display = 'block';
  if (comparePanel) comparePanel.style.display = 'none';
  
  // Habilitar campos de entrada
  const inputFields = ['odoo-url', 'odoo-db', 'odoo-user', 'odoo-key'];
  inputFields.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = false;
  });
  
  // Mostrar la contraseña si estaba mascarada
  const keyInput = document.getElementById('odoo-key');
  if (keyInput && keyInput.value === '••••••••••••••••') {
    keyInput.value = '';  // Limpiar para que el usuario ingrese una nueva
  }
  
  log('Puedes ingresar nuevas credenciales de Odoo. Haz clic en "Probar Conexión" para verificar.', 'msg');
});

// ─────────────────────────────────────────────
// DESCONECTAR ODOO
// ─────────────────────────────────────────────

document.getElementById('btn-odoo-disconnect').addEventListener('click', async () => {
  if (!confirm('¿Eliminar la configuración de Odoo guardada en este equipo y en la nube?')) return;
  
  localStorage.removeItem('odoo_credentials');
  odooCredentials = null;
  odooConnected   = false;
  setOdooStatus(false);  // Esto ahora también habilita los campos

  // Eliminar configuración centralizada de la nube (GAS)
  log('Eliminando configuración de Odoo de la nube...', 'warn');
  await callGASRobust("saveOdooConfig", { encrypted_credentials: "" });

  // Limpiar campos y asegurar que estén habilitados
  ['odoo-url', 'odoo-db', 'odoo-user', 'odoo-key'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { 
      el.value = ''; 
      el.type = id === 'odoo-key' ? 'password' : 'text';
      el.disabled = false;  // Explícitamente habilitar
    }
  });
  
  // Habilitar botón de prueba
  const testBtn = document.getElementById('btn-odoo-test');
  if (testBtn) testBtn.disabled = false;
  
  document.getElementById('odoo-msg').innerHTML = '<span class="t-warn">Configuración eliminada. Puedes ingresar nuevas credenciales.</span>';
  log('Configuración Odoo eliminada por completo.', 'warn');
});

// ─────────────────────────────────────────────
// MODO TOGGLE: Siesa (archivo) vs Odoo (API)
// ─────────────────────────────────────────────

document.querySelectorAll('input[name="erp-mode"]').forEach(radio => {
  radio.addEventListener('change', (e) => {
    const mode = e.target.value;
    const isSiesa = mode === 'siesa';

    document.getElementById('panel-siesa-upload').style.display = isSiesa ? 'block' : 'none';
    document.getElementById('panel-odoo-api').style.display     = isSiesa ? 'none'  : 'block';

    document.getElementById('btn-comparar').style.display   = isSiesa ? '' : 'none';
    document.getElementById('btn-descargar').style.display  = isSiesa ? '' : 'none';
    document.getElementById('btn-odoo-comparar').style.display = isSiesa ? 'none' : '';
    document.getElementById('btn-odoo-download').style.display  = isSiesa ? 'none' : '';

    // Estilo activo en los tabs
    document.getElementById('mode-btn-siesa').classList.toggle('active', isSiesa);
    document.getElementById('mode-btn-odoo').classList.toggle('active', !isSiesa);

    // Resetear estado del botón DIAN si cambia de modo
    const compararBtn = document.getElementById('btn-comparar');
    const odooBtn     = document.getElementById('btn-odoo-comparar');
    if (!isSiesa) {
      // En modo Odoo: habilitar solo si hay credenciales
      odooBtn.disabled = !odooConnected;
    } else {
      // En modo Siesa: habilitar solo si ambos archivos están cargados
      compararBtn.disabled = !(archivoDian && archivoSiesa);
    }

    log(`Modo ERP: ${isSiesa ? 'Archivo Siesa' : 'API Odoo'}`, 'msg');
  });
});

// ─────────────────────────────────────────────
// COMPARAR DIAN vs ODOO
// ─────────────────────────────────────────────

document.getElementById('btn-odoo-comparar').addEventListener('click', async () => {
  if (!archivoDian) {
    log('Primero cargue el archivo DIAN.', 'warn');
    return;
  }
  if (!odooCredentials) {
    log('Configure primero las credenciales de Odoo en CONFIGURACIÓN.', 'warn');
    switchView('config');
    return;
  }

  const dateFrom = document.getElementById('odoo-date-from').value;
  const dateTo   = document.getElementById('odoo-date-to').value;
  const dateMsg  = document.getElementById('odoo-date-msg');

  if (!dateFrom || !dateTo) {
    dateMsg.innerHTML = '<span class="t-err">LAS FECHAS SON OBLIGATORIAS</span>';
    return;
  }
  if (dateFrom > dateTo) {
    dateMsg.innerHTML = '<span class="t-err">FECHA INICIO > FECHA FIN</span>';
    return;
  }
  dateMsg.innerHTML = '';

  const btn = document.getElementById('btn-odoo-comparar');
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> CONECTANDO A ODOO...';
  log(`Extrayendo facturas Odoo: ${dateFrom} → ${dateTo}`, 'msg');

  try {
    const form = new FormData();
    form.append('dian', archivoDian);
    form.append('credentials', odooCredentials);
    form.append('date_from', dateFrom);
    form.append('date_to', dateTo);

    const res  = await fetch(`${API_URL}/odoo/comparar`, { method: 'POST', body: form });
    const data = await res.json();

    if (!res.ok) throw new Error(data.detail || 'Error desconocido');

    const totalOdoo = data.resumen_general?.total_odoo ?? '?';
    log(`Odoo: ${totalOdoo} facturas extraídas. Comparando...`, 'ok');
    mostrarResultado(data);
    sincronizarConGAS(data.resumen_general);

  } catch (err) {
    log(`Error Odoo: ${err.message}`, 'err');
    alert(`❌ Error en comparación Odoo:\n${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/></svg> COMPARAR CON ODOO';
  }
});

// ─────────────────────────────────────────────
// DESCARGAR EXCEL (Odoo)
// ─────────────────────────────────────────────

document.getElementById('btn-odoo-download').addEventListener('click', async () => {
  if (!archivoDian || !odooCredentials) {
    log('Cargue el DIAN y configure Odoo primero.', 'warn');
    return;
  }

  const dateFrom = document.getElementById('odoo-date-from').value;
  const dateTo   = document.getElementById('odoo-date-to').value;
  const dateMsg  = document.getElementById('odoo-date-msg');

  if (!dateFrom || !dateTo) {
    dateMsg.innerHTML = '<span class="t-err">LAS FECHAS SON OBLIGATORIAS</span>';
    return;
  }
  if (dateFrom > dateTo) {
    dateMsg.innerHTML = '<span class="t-err">FECHA INICIO > FECHA FIN</span>';
    return;
  }
  dateMsg.innerHTML = '';

  const btn = document.getElementById('btn-odoo-download');
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> GENERANDO...';

  try {
    const form = new FormData();
    form.append('dian', archivoDian);
    form.append('credentials', odooCredentials);
    form.append('date_from', dateFrom);
    form.append('date_to', dateTo);

    const res  = await fetch(`${API_URL}/odoo/descargar-reporte`, { method: 'POST', body: form });
    if (!res.ok) {
      const errBody = await res.json().catch(() => ({}));
      throw new Error(errBody.detail || `Error ${res.status}`);
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = `reporte_dian_vs_odoo_${dateFrom}_${dateTo}.xlsx`;
    a.click();
    URL.revokeObjectURL(url);
    log('Reporte Odoo descargado.', 'ok');

  } catch (err) {
    log(`Error descarga: ${err.message}`, 'err');
    alert(`❌ Error al descargar reporte Odoo:\n${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/></svg> EXPORTAR EXCEL ODOO';
  }
});
