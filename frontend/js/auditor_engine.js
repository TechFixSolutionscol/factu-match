document.addEventListener("DOMContentLoaded", () => {
  // Listen for view changes to load data when auditor view is opened
  const auditorNav = document.getElementById("nav-auditor");
  if (auditorNav) {
    auditorNav.addEventListener("click", loadAuditorDashboard);
  }

  const btnSync = document.getElementById("btn-auditor-sync");
  if (btnSync) {
    btnSync.addEventListener("click", syncAuditorOdoo);
  }

  const btnReadEmails = document.getElementById("btn-auditor-read-emails");
  if (btnReadEmails) {
    btnReadEmails.addEventListener("click", readNewEmails);
  }

  const btnFilter = document.getElementById("btn-auditor-filter");
  if (btnFilter) {
    btnFilter.addEventListener("click", loadAuditorDashboard);
  }

  // Si arranca en esta vista, cargar datos (por si acaso)
  if (document.getElementById("view-auditor") && document.getElementById("view-auditor").style.display === "block") {
    loadAuditorDashboard();
  }
});

async function loadAuditorDashboard() {
  const tbody = document.getElementById("auditor-tabla-body");
  if (!tbody) return;
  
  tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:15px;"><div class="btn-loader" style="margin:0 auto;"></div></td></tr>';
  
  try {
    let url = `${API_URL}/api/dashboard-auditoria`;
    const monthVal = document.getElementById("auditor-filter-month")?.value;
    
    if (monthVal) {
      const [y, m] = monthVal.split("-");
      url += `?month=${m}&year=${y}`;
    }
    
    const res = await fetch(url);
    if (!res.ok) throw new Error("Error en respuesta del servidor");
    
    const data = await res.json();
    
    if (data.success) {
      // Update Metrics
      document.getElementById("auditor-total-recibidas").textContent = data.metrics.total_recibidas;
      document.getElementById("auditor-total-cruzadas").textContent = data.metrics.total_cruzadas;
      document.getElementById("auditor-total-faltantes").textContent = data.metrics.total_faltantes;
      document.getElementById("auditor-accuracy").textContent = data.metrics.accuracy + "%";
      
      // Update Table
      tbody.innerHTML = "";
      if (data.faltantes.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:15px; color:var(--green);">¡Todo está cruzado correctamente! No hay facturas pendientes.</td></tr>';
        return;
      }
      
      data.faltantes.forEach(f => {
        const tr = document.createElement("tr");
        const issueDate = f.issue_date ? new Date(f.issue_date).toLocaleDateString() : 'N/A';
        const totalAmount = parseFloat(f.total_amount || 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
        const subtotal = parseFloat(f.tax_exclusive_amount || 0).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
        
        tr.innerHTML = `
          <td style="padding:0.75rem;">${issueDate}</td>
          <td style="padding:0.75rem;">
            <div style="font-weight:600; color:var(--text);">${f.supplier_name || 'Desconocido'}</div>
            <div style="font-size:0.6rem; color:var(--text-dim);">NIT: ${f.supplier_nit}</div>
          </td>
          <td style="padding:0.75rem; text-align:center; color:var(--cyan);">${f.document_number}</td>
          <td style="padding:0.75rem; text-align:right;">${subtotal}</td>
          <td style="padding:0.75rem; text-align:right; font-weight:700;">${totalAmount}</td>
          <td style="padding:0.75rem; text-align:center;">
            <span class="status-badge pending" style="border-color:var(--red); color:var(--red);">PENDIENTE ODOO</span>
          </td>
        `;
        tbody.appendChild(tr);
      });
      
    } else {
      throw new Error(data.detail || "Error desconocido");
    }
  } catch (error) {
    console.error("Error cargando dashboard auditor:", error);
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding:15px; color:var(--red);">Error cargando datos: ${error.message}</td></tr>`;
  }
}

async function syncAuditorOdoo() {
  const dateFrom = document.getElementById("auditor-date-from").value;
  const dateTo = document.getElementById("auditor-date-to").value;
  const msgDiv = document.getElementById("auditor-sync-msg");
  
  if (!dateFrom || !dateTo) {
    msgDiv.innerHTML = '<span class="t-err">Seleccione un rango de fechas válido.</span>';
    return;
  }
  
  const savedCreds = localStorage.getItem("odoo_credentials");
  if (!savedCreds) {
    msgDiv.innerHTML = '<span class="t-err">No hay credenciales de Odoo guardadas. Vaya a CONFIGURACIÓN.</span>';
    return;
  }
  
  const btn = document.getElementById("btn-auditor-sync");
  const originalHtml = btn.innerHTML;
  btn.innerHTML = '<div class="btn-loader"></div> SINCRONIZANDO...';
  btn.disabled = true;
  msgDiv.innerHTML = '<span class="t-msg">Conectando a Odoo y cruzando datos... Esto puede tomar unos minutos.</span>';
  
  try {
    const formData = new FormData();
    formData.append("credentials", savedCreds);
    formData.append("date_from", dateFrom);
    formData.append("date_to", dateTo);
    
    const res = await fetch(`${API_URL}/api/sync-odoo-invoices`, {
      method: "POST",
      body: formData
    });
    
    const data = await res.json();
    
    if (res.ok && data.success) {
      msgDiv.innerHTML = `<span class="t-ok">${data.message}</span>`;
      // Recargar la tabla
      loadAuditorDashboard();
    } else {
      throw new Error(data.detail || data.error || "Error sincronizando con Odoo.");
    }
  } catch (error) {
    console.error("Error sync Odoo:", error);
    msgDiv.innerHTML = `<span class="t-err">${error.message}</span>`;
  } finally {
    btn.innerHTML = originalHtml;
    btn.disabled = false;
  }
}

async function readNewEmails() {
  const btn = document.getElementById("btn-auditor-read-emails");
  const msgDiv = document.getElementById("auditor-sync-msg");
  const originalHtml = btn.innerHTML;
  
  btn.innerHTML = '<div class="btn-loader"></div> LEYENDO...';
  btn.disabled = true;
  msgDiv.innerHTML = '<span class="t-msg">Conectando a la bandeja de entrada y extrayendo XMLs...</span>';
  
  try {
    const res = await fetch(`${API_URL}/sync-emails`, {
      method: "POST"
    });
    const data = await res.json();
    
    if (res.ok) {
      msgDiv.innerHTML = `<span class="t-ok">Lectura finalizada. Revisa si hay nuevas facturas en la tabla.</span>`;
      // Recargar tabla
      loadAuditorDashboard();
    } else {
      throw new Error(data.detail || "Error leyendo correos.");
    }
  } catch (error) {
    console.error("Error read emails:", error);
    msgDiv.innerHTML = `<span class="t-err">Error: ${error.message}</span>`;
  } finally {
    btn.innerHTML = originalHtml;
    btn.disabled = false;
  }
}

