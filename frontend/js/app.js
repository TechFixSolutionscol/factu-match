const API_URL = "https://factu-match.onrender.com";
const GAS_URL = "https://script.google.com/macros/s/AKfycbyKUEKGeeioDvs9Sl-V66-DVj8o4l97bAly4ejfGCu1bOPLOQkK9kUGA0sttMTT7pxVjg/exec";
let archivoDian = null, archivoSiesa = null;
let currentUser = null;
let chartHistorico = null, chartAccuracy = null;

function togglePassword(id) {
  const el = document.getElementById(id);
  if (el) el.type = el.type === 'password' ? 'text' : 'password';
}

/* --- GAS INTEGRATION & VIEW MANAGEMENT --- */

async function callGASRobust(action, data = {}) {
  try {
    const response = await fetch(GAS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain;charset=utf-8' },
      body: JSON.stringify({ action, data })
    });
    const text = await response.text();
    try {
      const json = JSON.parse(text);
      if (json.success === false) throw new Error(json.error || "Error en el servidor");
      return json;
    } catch (e) {
      console.error("No se pudo parsear JSON de GAS:", text);
      return { success: false, error: e.message || "Respuesta inválida del servidor" };
    }
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// LOGIN SUBMIT
document.getElementById("btn-login-submit").addEventListener("click", async () => {
  const user = document.getElementById("login-user").value;
  const pass = document.getElementById("login-pass").value;
  const errorDiv = document.getElementById("login-error");
  
  if (!user || !pass) {
    errorDiv.textContent = "INGRESE CREDENCIALES";
    return;
  }

  errorDiv.textContent = "";
  const btn = document.getElementById("btn-login-submit");
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> AUTENTICANDO...';
  
  const res = await callGASRobust("login", { user, pass });
  
  if (res.success) {
    currentUser = res.data;
    localStorage.setItem("factura_user", JSON.stringify(currentUser));
    initUserSession();
    
    document.getElementById("login-overlay").style.transition = "opacity 0.5s";
    document.getElementById("login-overlay").style.opacity = "0";
    setTimeout(() => {
      document.getElementById("login-overlay").style.display = "none";
      btn.disabled = false;
      btn.innerHTML = "INGRESAR AL SISTEMA";
    }, 500);
    
    log(`Usuario ${currentUser.name} conectado.`, 'ok');
    switchView('dashboard');
  } else {
    btn.disabled = false;
    btn.innerHTML = "INGRESAR AL SISTEMA";
    errorDiv.textContent = res.error || "ACCESO DENEGADO";
  }
});

// FORGOT PASSWORD FLOW
document.getElementById("link-forgot-password").addEventListener("click", (e) => {
  e.preventDefault();
  document.getElementById("forgot-overlay").style.display = "flex";
});

document.getElementById("btn-forgot-back").addEventListener("click", () => {
  document.getElementById("forgot-overlay").style.display = "none";
});

document.getElementById("btn-forgot-submit").addEventListener("click", async () => {
  const email = document.getElementById("forgot-email").value;
  const msgDiv = document.getElementById("forgot-msg");
  if (!email) return;

  msgDiv.innerHTML = '<div class="btn-loader" style="margin:0 auto"></div>';
  const res = await callGASRobust("forgotPassword", { email });
  if (res.success) {
    msgDiv.innerHTML = '<span class="t-ok">ENLACE ENVIADO. REVISA TU CORREO.</span>';
  } else {
    msgDiv.innerHTML = `<span class="t-err">${res.error}</span>`;
  }
});

// LOGOUT
document.getElementById("btn-logout").addEventListener("click", () => {
  if (confirm("¿Cerrar sesión del sistema?")) {
    localStorage.removeItem("factura_user");
    location.reload(); 
  }
});

function initUserSession() {
  if (!currentUser) return;
  document.getElementById("user-name").textContent = currentUser.name.toUpperCase();
  document.getElementById("user-display").style.display = "flex";
  
  const badge = document.getElementById("user-role-badge");
  badge.textContent = (currentUser.role || "OPERADOR").toUpperCase();
  badge.className = "status-badge active";
  
  // Control de permisos visuales
  const isAdmin = currentUser.role === "Admin";
  document.getElementById("nav-users").style.display = isAdmin ? "flex" : "none";
  
  // Prellenar perfil
  document.getElementById("profile-name").value = currentUser.name;
  document.getElementById("profile-email").value = currentUser.email;
}

function checkSession() {
  const saved = localStorage.getItem("factura_user");
  if (saved) {
    currentUser = JSON.parse(saved);
    initUserSession();
    document.getElementById("login-overlay").style.display = "none";
    log(`Sesión restaurada: ${currentUser.name}`, 'ok');
    switchView('dashboard');
  }
}
checkSession();

/* --- GESTIÓN DE VISTAS --- */
function switchView(viewId) {
  // Ocultar todas las vistas
  const views = ['dashboard', 'auditor', 'comparisons', 'checklist', 'reconciliation', 'logs', 'users', 'config', 'profile'];
  views.forEach(v => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.style.display = 'none';
  });

  // Mostrar la vista seleccionada
  const activeView = document.getElementById(`view-${viewId}`);
  if (activeView) activeView.style.display = 'block';

  // Remover activo de todos los nav items
  const navs = ['dashboard', 'auditor', 'comparisons', 'checklist', 'reconciliation', 'logs', 'users', 'config', 'profile-btn'];
  navs.forEach(n => {
    const el = document.getElementById(`nav-${n}`);
    if (el) el.classList.remove('active');
  });

  // Activar el nav item correspondiente
  const activeNav = document.getElementById(`nav-${viewId === 'profile' ? 'profile-btn' : viewId}`);
  if (activeNav) activeNav.classList.add('active');

  const titles = {
    dashboard: 'TABLERO DE CONTROL',
    comparisons: 'PROCESAMIENTO DE ARCHIVOS',
    reconciliation: 'CONCILIACIÓN BANCARIA',
    checklist: 'CHECKLIST MENSUAL (AUDITOR IA)',
    logs: 'REGISTROS DEL SISTEMA',
    users: 'GESTIÓN DE USUARIOS',
    profile: 'MI PERFIL DE USUARIO',
    config: 'CONFIGURACIÓN DEL SISTEMA',
    auditor: 'AUDITORÍA TRIBUTARIA'
  };
  
  document.querySelector(".topbar-title").textContent = titles[viewId] || 'SISTEMA';
  
  if (viewId === 'dashboard') cargarStats();
  if (viewId === 'users') loadUsersTable();
}

document.getElementById("nav-dashboard").addEventListener("click", () => switchView('dashboard'));
document.getElementById("nav-auditor").addEventListener("click", () => switchView('auditor'));
document.getElementById("nav-comparisons").addEventListener("click", () => switchView('comparisons'));
document.getElementById("nav-checklist").addEventListener("click", () => switchView('checklist'));
document.getElementById("nav-reconciliation").addEventListener("click", () => switchView('reconciliation'));
document.getElementById("nav-logs").addEventListener("click", () => switchView('logs'));
document.getElementById("nav-users").addEventListener("click", () => switchView('users'));
document.getElementById("nav-profile-btn").addEventListener("click", () => switchView('profile'));
document.getElementById("nav-config").addEventListener("click", () => switchView('config'));

/* --- GESTIÓN DE USUARIOS (ADMIN) --- */
async function loadUsersTable() {
  const tbody = document.getElementById("users-table-body");
  tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; padding:2rem;"><div class="btn-loader" style="margin:0 auto"></div></td></tr>';
  
  const res = await callGASRobust("getUsers");
  if (res.success) {
    const users = res.data;
    document.getElementById("users-count").textContent = `${users.length} registros`;
    tbody.innerHTML = "";
    users.forEach(u => {
      const row = document.createElement("tr");
      const lastLogin = u.lastLogin ? new Date(u.lastLogin).toLocaleString() : "NUNCA";
      const statusCls = u.status === 'active' ? 'active' : (u.status === 'pending' ? 'pending' : 'archived');
      
      row.innerHTML = `
        <td>
          <div style="font-weight:700; color:var(--text);">${u.name}</div>
          <div style="font-size:0.55rem; color:var(--text-dim);">${u.email}</div>
        </td>
        <td style="text-align:center;">
          <select class="login-input" style="font-size:0.55rem; padding:2px 4px; width:auto;" onchange="updateUserRole('${u.id}', this.value)">
            <option value="Operador" ${u.role === 'Operador' ? 'selected' : ''}>OPERADOR</option>
            <option value="Admin" ${u.role === 'Admin' ? 'selected' : ''}>ADMIN</option>
            <option value="Lectura" ${u.role === 'Lectura' ? 'selected' : ''}>LECTURA</option>
          </select>
        </td>
        <td style="text-align:center;">
          <span class="status-badge ${statusCls}">${u.status}</span>
        </td>
        <td style="text-align:center; color:var(--text-dim); font-size:0.55rem;">${lastLogin}</td>
        <td style="text-align:right;">
          <div style="display:flex; gap:5px; justify-content:flex-end;">
            <button class="action-btn" title="Alternar Estado" onclick="toggleUserStatus('${u.id}', '${u.status}')">
              <svg fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" /></svg>
            </button>
            <button class="action-btn danger" title="Eliminar" onclick="confirmDeleteUser('${u.id}', '${u.name}')">
              <svg fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg>
            </button>
          </div>
        </td>
      `;
      tbody.appendChild(row);
    });
  }
}

window.updateUserRole = async (id, role) => {
  log(`Actualizando rol de usuario: ${id} -> ${role}`, 'msg');
  const res = await callGASRobust("updateUser", { id, role });
  if (res.success) log("Rol actualizado.", 'ok');
};

window.toggleUserStatus = async (id, currentStatus) => {
  const newStatus = currentStatus === 'active' ? 'archived' : 'active';
  log(`Cambiando estado de usuario: ${id} -> ${newStatus}`, 'msg');
  const res = await callGASRobust("updateUser", { id, status: newStatus });
  if (res.success) {
    log("Estado actualizado.", 'ok');
    loadUsersTable();
  }
};

window.confirmDeleteUser = async (id, name) => {
  if (confirm(`¿ELIMINAR DEFINITIVAMENTE A ${name.toUpperCase()}?\nEsta acción no se puede deshacer.`)) {
    log(`Eliminando usuario: ${name}`, 'warn');
    const res = await callGASRobust("deleteUser", { id });
    if (res.success) {
      log("Usuario eliminado.", 'ok');
      loadUsersTable();
    }
  }
};

document.getElementById("btn-create-user").addEventListener("click", async () => {
  const name = document.getElementById("new-user-name").value;
  const email = document.getElementById("new-user-email").value;
  const role = document.getElementById("new-user-role").value;
  const msgDiv = document.getElementById("new-user-msg");

  if (!name || !email) {
    msgDiv.innerHTML = '<span class="t-err">COMPLETE TODOS LOS CAMPOS</span>';
    return;
  }

  msgDiv.innerHTML = "";
  const btn = document.getElementById("btn-create-user");
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> ENVIANDO...';
  
  const res = await callGASRobust("createUser", { name, email, role });
  btn.disabled = false;
  btn.innerHTML = "ENVIAR INVITACIÓN";

  if (res.success) {
    msgDiv.innerHTML = '<span class="t-ok">INVITACIÓN ENVIADA A ' + email + '</span>';
    document.getElementById("new-user-name").value = "";
    document.getElementById("new-user-email").value = "";
    log(`Invitación enviada: ${email} (${role})`, 'ok');
    loadUsersTable();
  } else {
    msgDiv.innerHTML = `<span class="t-err">ERROR: ${res.error}</span>`;
  }
});

/* --- PERFIL DE USUARIO --- */
document.getElementById("btn-update-profile").addEventListener("click", async () => {
  const name = document.getElementById("profile-name").value;
  const msgDiv = document.getElementById("profile-msg");
  if (!name) return;

  msgDiv.innerHTML = '<div class="btn-loader" style="margin:0 auto"></div>';
  const res = await callGASRobust("updateProfile", { id: currentUser.id, name });
  if (res.success) {
    msgDiv.innerHTML = '<span class="t-ok">DATOS ACTUALIZADOS</span>';
    currentUser.name = name;
    localStorage.setItem("factura_user", JSON.stringify(currentUser));
    initUserSession();
    log("Perfil actualizado.", 'ok');
  } else {
    msgDiv.innerHTML = `<span class="t-err">${res.error}</span>`;
  }
});

document.getElementById("btn-update-pass").addEventListener("click", async () => {
  const oldPass = document.getElementById("pass-old").value;
  const newPass = document.getElementById("pass-new").value;
  const confirmP = document.getElementById("pass-confirm").value;
  const msgDiv = document.getElementById("pass-msg");

  if (!oldPass || !newPass || !confirmP) {
    msgDiv.innerHTML = '<span class="t-err">COMPLETE TODOS LOS CAMPOS</span>';
    return;
  }
  if (newPass !== confirmP) {
    msgDiv.innerHTML = '<span class="t-err">LAS CONTRASEÑAS NO COINCIDEN</span>';
    return;
  }
  if (newPass.length < 8 || !/[A-Z]/.test(newPass) || !/[0-9]/.test(newPass)) {
    msgDiv.innerHTML = '<span class="t-err">LA CONTRASEÑA NO CUMPLE LOS REQUISITOS</span>';
    return;
  }

  msgDiv.innerHTML = '<div class="btn-loader" style="margin:0 auto"></div>';
  const res = await callGASRobust("changePassword", { id: currentUser.id, oldPass, newPass });
  if (res.success) {
    msgDiv.innerHTML = '<span class="t-ok">CONTRASEÑA ACTUALIZADA</span>';
    document.getElementById("pass-old").value = "";
    document.getElementById("pass-new").value = "";
    document.getElementById("pass-confirm").value = "";
    log("Contraseña cambiada con éxito.", 'ok');
  } else {
    msgDiv.innerHTML = `<span class="t-err">${res.error}</span>`;
  }
});

/* --- COMPARADOR & HISTÓRICO --- */
let lastSummaryData = null;

async function sincronizarConGAS(resumen, force = false) {
  log("Sincronizando con Google Sheets...", 'msg');
  if (force) resumen.force = true;
  lastSummaryData = resumen;

  const res = await callGASRobust("saveSummary", resumen);
  
  if (res.success) {
    if (res.warning) {
      log("ADVERTENCIA: Ya existe un registro para hoy.", 'warn');
      document.getElementById("warning-overlay").style.display = "flex";
      return;
    }
    log("Sincronización exitosa.", 'ok');
  } else {
    log("Fallo al sincronizar: " + res.error, 'err');
  }
}

// Botones de advertencia
document.getElementById("btn-warning-force").addEventListener("click", () => {
  document.getElementById("warning-overlay").style.display = "none";
  if (lastSummaryData) {
    sincronizarConGAS(lastSummaryData, true);
  }
});

document.getElementById("btn-warning-cancel").addEventListener("click", () => {
  document.getElementById("warning-overlay").style.display = "none";
  log("Guardado cancelado por el usuario.", 'msg');
});

async function cargarStats() {
  log("Solicitando datos históricos a GAS...", 'msg');
  const res = await callGASRobust("getStats");
  if (res.success) {
    const data = res.data;
    log(`Datos recibidos: ${data.length} registros encontrados.`, 'ok');
    if (!data || data.length === 0) {
      log("No hay registros históricos suficientes para graficar.", 'warn');
      // Limpiar metrics si no hay datos
      document.getElementById("d-total-meses").textContent = "0";
      document.getElementById("d-promedio-accuracy").textContent = "0%";
      document.getElementById("d-total-encontradas").textContent = "0";
      document.getElementById("d-total-faltantes").textContent = "0";
      return;
    }
    
    // Limpiar y parsear datos para seguridad
    const parsedData = data.map(d => ({
      ...d,
      accuracy: parseFloat(String(d.accuracy).replace('%', '')) || 0,
      dian: parseInt(d.dian) || 0,
      siesa: parseInt(d.siesa) || 0,
      faltantes: parseInt(d.faltantes) || 0
    }));

    renderCharts(parsedData);
    
    document.getElementById("d-total-meses").textContent = parsedData.length;
    const avgAcc = parsedData.reduce((acc, curr) => acc + curr.accuracy, 0) / (parsedData.length || 1);
    document.getElementById("d-promedio-accuracy").textContent = avgAcc.toFixed(1) + "%";
    document.getElementById("d-total-encontradas").textContent = parsedData.reduce((acc, curr) => acc + curr.siesa, 0).toLocaleString();
    document.getElementById("d-total-faltantes").textContent = parsedData.reduce((acc, curr) => acc + curr.faltantes, 0).toLocaleString();

    const tbody = document.getElementById("tabla-historico-body");
    tbody.innerHTML = "";
    [...parsedData].reverse().forEach(row => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td style="padding:0.75rem;">${row.fecha ? new Date(row.fecha).toLocaleDateString() : 'N/A'}</td>
        <td style="padding:0.75rem; color:var(--cyan);">${row.mes} ${row.anio}</td>
        <td style="padding:0.75rem; text-align:right;">${row.dian}</td>
        <td style="padding:0.75rem; text-align:right;">${row.siesa}</td>
        <td style="padding:0.75rem; text-align:right; color:var(--red);">${row.faltantes}</td>
        <td style="padding:0.75rem; text-align:right; font-weight:700;">${row.accuracy.toFixed(1)}%</td>
      `;
      tbody.appendChild(tr);
    });
  } else {
    log("Error al cargar dashboard: " + res.error, 'err');
  }
}

function renderCharts(data) {
  const labels = data.map(d => `${d.mes} ${d.anio}`);
  const dianData = data.map(d => d.dian);
  const siesaData = data.map(d => d.siesa);
  const accuracyData = data.map(d => d.accuracy);

  if (chartHistorico) chartHistorico.destroy();
  if (chartAccuracy) chartAccuracy.destroy();

  const ctxH = document.getElementById('chart-historico').getContext('2d');
  chartHistorico = new Chart(ctxH, {
    type: 'bar',
    data: {
      labels: labels,
      datasets: [
        { label: 'DIAN', data: dianData, backgroundColor: 'rgba(0, 229, 255, 0.4)', borderColor: '#00e5ff', borderWidth: 1 },
        { label: 'SIESA', data: siesaData, backgroundColor: 'rgba(0, 230, 118, 0.4)', borderColor: '#00e676', borderWidth: 1 }
      ]
    },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#8892aa' } } } }
  });

  const ctxA = document.getElementById('chart-accuracy').getContext('2d');
  chartAccuracy = new Chart(ctxA, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{ label: '% Accuracy', data: accuracyData, borderColor: '#00e5ff', fill: true, tension: 0.4 }]
    },
    options: { responsive: true, maintainAspectRatio: false }
  });
}

// LOGIC FOR COMPARATOR (UNCHANGED CORE)
setupUpload("input-dian","zona-dian","nombre-dian", f => archivoDian=f, "DIAN");
setupUpload("input-siesa","zona-siesa","nombre-siesa", f => archivoSiesa=f, "SIESA");

document.getElementById("btn-limpiar").addEventListener("click", () => {
  archivoDian = null; archivoSiesa = null;
  ["dian","siesa"].forEach(k => {
    document.getElementById(`input-${k}`).value = "";
    document.getElementById(`nombre-${k}`).textContent = "";
    document.getElementById(`zona-${k}`).classList.remove("cargado");
  });
  document.getElementById("seccion-resultado").style.display = "none";
  setBadge("AWAITING INPUT","status-waiting");
  log("Sistema reiniciado.", 'msg');
});

document.getElementById("btn-comparar").addEventListener("click", async () => {
  const btn = document.getElementById("btn-comparar");
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> PROCESANDO...';
  try {
    const form = new FormData();
    form.append("dian", archivoDian);
    form.append("siesa", archivoSiesa);
    const res = await fetch(`${API_URL}/comparar`, { method:"POST", body:form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Error desconocido");
    mostrarResultado(data);
    sincronizarConGAS(data.resumen_general);
  } catch(err) {
    log(`Error: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'COMPARAR FACTURAS';
  }
});

document.getElementById("btn-descargar").addEventListener("click", async () => {
  const btn = document.getElementById("btn-descargar");
  btn.disabled = true;
  btn.innerHTML = '<div class="btn-loader"></div> GENERANDO...';
  try {
    const form = new FormData();
    form.append("dian", archivoDian);
    form.append("siesa", archivoSiesa);
    const res = await fetch(`${API_URL}/descargar-reporte`, { method:"POST", body:form });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "reporte.xlsx"; a.click();
  } catch(err) {
    log(`Error: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'EXPORTAR EXCEL';
  }
});

function setupUpload(inputId, zonaId, nombreId, setter, label) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.addEventListener("change", e => {
    const file = e.target.files[0];
    if (!file) return;
    setter(file);
    document.getElementById(nombreId).textContent = file.name;
    document.getElementById(zonaId).classList.add("cargado");
    log(`Archivo ${label} cargado: ${file.name}`, 'ok');
    const listo = archivoDian && archivoSiesa;
    document.getElementById("btn-comparar").disabled = !listo;
    if (listo) setBadge("READY", "status-ok");
  });
}

function log(msg, tipo='msg') {
  const t = document.getElementById('terminal');
  if (!t) return;
  const line = document.createElement('span');
  line.className = 't-line';
  line.innerHTML = `<span class="t-time">${ahora()}</span><span class="t-${tipo}">${msg}</span>`;
  t.appendChild(line);
  t.scrollTop = t.scrollHeight;
}

function ahora() {
  const d = new Date();
  return `[${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}]`;
}

function setBadge(txt, cls) {
  const b = document.getElementById("badge-estado");
  if (b) { b.textContent = txt; b.className = "status-badge " + cls; }
}

function mostrarResultado(data) {
  const r = data.resumen_general;
  document.getElementById("seccion-resultado").style.display = "block";
  document.getElementById("m-proveedores").textContent = r.total_proveedores;
  document.getElementById("m-dian").textContent = r.total_dian.toLocaleString();
  document.getElementById("m-siesa").textContent = r.total_en_siesa.toLocaleString();
  document.getElementById("m-faltantes").textContent = r.total_faltantes.toLocaleString();
  document.getElementById("pct-completitud").textContent = r.porcentaje_completitud + "%";
  document.getElementById("pct-barra").textContent = r.porcentaje_completitud + "%";
  document.getElementById("barra-fill").style.width = r.porcentaje_completitud + "%";
  document.getElementById("narrativa-texto").textContent = data.narrativa;
  
  const lista = document.getElementById("lista-proveedores");
  lista.innerHTML = "";
  data.proveedores.forEach((p, i) => {
    const card = document.createElement("div");
    card.className = "proveedor-card";
    card.innerHTML = `
      <div class="proveedor-header" onclick="toggleDetalle(${i})">
        <div><div class="proveedor-nombre">${p.nombre}</div><div class="proveedor-nit">NIT: ${p.nit}</div></div>
        <div class="proveedor-badges">
          <span class="badge badge-dian">DIAN: ${p.total_dian}</span>
          <span class="badge badge-siesa">SIESA: ${p.total_en_siesa}</span>
          <span class="badge ${p.total_faltantes > 0 ? 'badge-faltante' : 'badge-ok'}">${p.total_faltantes > 0 ? '⚠ '+p.total_faltantes+' FALTANTES' : '✔ COMPLETO'}</span>
          <span class="chevron" id="chevron-${i}">▼</span>
        </div>
      </div>
      <div class="proveedor-detalle" id="detalle-${i}">
        ${p.faltantes.length > 0 ? `<div class="seccion-label">// FALTANTES</div><div class="facturas-grid">${p.faltantes.map(f=>`<span class="factura-tag factura-faltante">${f.factura}<span class="factura-fecha">${f.fecha}</span></span>`).join("")}</div>` : ""}
        ${p.encontradas.length > 0 ? `<div class="seccion-label">// ENCONTRADAS</div><div class="facturas-grid">${p.encontradas.map(f=>`<span class="factura-tag factura-ok">${f}</span>`).join("")}</div>` : ""}
      </div>`;
    lista.appendChild(card);
  });
}

window.toggleDetalle = (i) => {
  document.getElementById(`detalle-${i}`).classList.toggle("visible");
  document.getElementById(`chevron-${i}`).classList.toggle("abierto");
};

