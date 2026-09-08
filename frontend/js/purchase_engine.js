/* ═══════════════════════════════════════════════════════════════
   PURCHASE ENGINE — FactuMatch
   Módulo 1: Compras Automáticas desde Correo
   Revisión humana del mapeo IA de facturas a productos Odoo
   Depende de: API_URL, odooCredentials, log() — definidos en app.js / odoo_conector.js
   ═══════════════════════════════════════════════════════════════ */

let pendingDocs = [];
let currentModalDocId = null;

// ── Inicialización ──

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("btn-purchase-refresh")?.addEventListener("click", loadPending);
  document.getElementById("btn-modal-close")?.addEventListener("click", closeModal);
  document.getElementById("btn-modal-cancel")?.addEventListener("click", closeModal);
  document.getElementById("btn-modal-save")?.addEventListener("click", saveCorrections);

  // Cargar al abrir la vista
  const navPurchasing = document.getElementById("nav-purchasing");
  if (navPurchasing) {
    navPurchasing.addEventListener("click", () => {
      setTimeout(loadPending, 100);
    });
  }
});

// ── Cargar pendientes ──

async function loadPending() {
  const tbody = document.getElementById("purchase-table-body");
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:15px;"><div class="btn-loader" style="margin:0 auto;"></div></td></tr>';

  try {
    const res = await fetch(`${API_URL}/api/purchase/pending`);
    if (!res.ok) throw new Error(`Error ${res.status}`);
    const data = await res.json();

    pendingDocs = data.documentos || [];
    document.getElementById("purchase-count").textContent = `${pendingDocs.length} pendientes`;

    if (pendingDocs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:15px; color:var(--green);">✓ No hay facturas pendientes de revisión.</td></tr>';
      return;
    }

    tbody.innerHTML = "";
    pendingDocs.forEach(doc => {
      const tr = document.createElement("tr");
      tr.style.borderBottom = "1px solid var(--border)";
      tr.id = `prow-${doc.id}`;

      const issueDate = doc.issue_date ? new Date(doc.issue_date).toLocaleDateString() : "N/A";
      const total = parseFloat(doc.total_amount || 0).toLocaleString("en-US", { style: "currency", currency: "USD" });
      const lineCount = doc.line_items?.length || 0;
      const status = getStatusBadge(doc);
      const hasAi = doc.ai_suggestions && doc.ai_suggestions.length > 0;

      tr.innerHTML = `
        <td style="padding:0.6rem;">${issueDate}</td>
        <td style="padding:0.6rem;">
          <div style="font-weight:600; color:var(--text);">${doc.supplier_name || "Desconocido"}</div>
          <div style="font-size:0.55rem; color:var(--text-dim);">NIT: ${doc.supplier_nit}</div>
        </td>
        <td style="padding:0.6rem; text-align:center; color:var(--cyan);">${doc.document_number}</td>
        <td style="padding:0.6rem; text-align:right;">${total}</td>
        <td style="padding:0.6rem; text-align:center;">${lineCount}</td>
        <td style="padding:0.6rem; text-align:center;">${status}</td>
        <td style="padding:0.6rem; text-align:center;">
          ${renderActions(doc, hasAi)}
        </td>
      `;

      // Fila oculta con detalle de líneas
      const detailRow = document.createElement("tr");
      detailRow.id = `pdetail-${doc.id}`;
      detailRow.style.display = "none";
      detailRow.innerHTML = `
        <td colspan="7" style="padding:0.6rem 1rem; background:rgba(255,255,255,0.015);">
          <div id="pdetail-content-${doc.id}"></div>
        </td>
      `;

      tbody.appendChild(tr);
      tbody.appendChild(detailRow);
    });

  } catch (err) {
    console.error("Error loading pending:", err);
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding:15px; color:var(--red);">Error: ${err.message}</td></tr>`;
  }
}

function getStatusBadge(doc) {
  const s = doc.review_status || (doc.ai_suggestions ? "SUGGESTED" : "NO_MAP");
  const map = {
    "PENDING": '<span class="status-badge pending" style="color:var(--yellow);border-color:var(--yellow);">PENDIENTE</span>',
    "REVIEWED_OK": '<span class="status-badge active" style="color:var(--green);border-color:var(--green);">APROBADO</span>',
    "CORRECTED": '<span class="status-badge" style="color:var(--cyan);border-color:var(--cyan);">CORREGIDO</span>',
    "COMPLETED": '<span class="status-badge status-ok">COMPLETADO</span>',
    "SUGGESTED": '<span class="status-badge pending" style="color:var(--cyan);border-color:var(--cyan);">MAPEADO IA</span>',
  };
  return map[s] || '<span class="status-badge pending" style="color:var(--text-dim);border-color:var(--border);">SIN MAPEAR</span>';
}

function renderActions(doc, hasAi) {
  const s = doc.review_status;

  // COMPLETADO → mostrar ref de OC
  if (s === "COMPLETED") {
    const ocRef = doc.purchase_order_id || "";
    return `<span style="color:var(--green); font-size:0.55rem;">✓ ${ocRef}</span>`;
  }

  // REVISADO → botón GENERAR OC
  if (s === "REVIEWED_OK" || s === "CORRECTED") {
    return `
      <button class="btn btn-outline" onclick="toggleDetail('${doc.id}')" style="padding:2px 8px; font-size:0.55rem; margin:1px;">
        <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>
        VER
      </button>
      <button class="btn btn-cyan" onclick="createOC('${doc.id}')" style="padding:2px 10px; font-size:0.55rem; margin:1px;">
        GENERAR OC
      </button>
    `;
  }

  // PENDIENTE / SIN MAPEAR
  let html = `
    <button class="btn btn-outline" onclick="toggleDetail('${doc.id}')" style="padding:2px 8px; font-size:0.55rem; margin:1px;">
      <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/></svg>
      VER
    </button>
  `;

  if (!hasAi && odooCredentials) {
    html += `
      <button class="btn btn-outline" onclick="runAiMap('${doc.id}')" style="padding:2px 8px; font-size:0.55rem; margin:1px; color:var(--cyan); border-color:var(--cyan);">
        MAPEAR IA
      </button>
    `;
  }

  if (hasAi) {
    html += `
      <button class="btn btn-outline" onclick="acceptMapping('${doc.id}')" style="padding:2px 8px; font-size:0.55rem; margin:1px; color:var(--green); border-color:var(--green);">✓</button>
      <button class="btn btn-outline" onclick="openCorrectionModal('${doc.id}')" style="padding:2px 8px; font-size:0.55rem; margin:1px; color:var(--yellow); border-color:var(--yellow);">✎</button>
    `;
  }

  return html;
}

// ── Toggle detalle ──

function toggleDetail(docId) {
  const row = document.getElementById(`pdetail-${docId}`);
  if (!row) return;
  const isVisible = row.style.display !== "none";
  row.style.display = isVisible ? "none" : "table-row";

  if (!isVisible) {
    renderDetailLines(docId);
  }
}

function renderDetailLines(docId) {
  const container = document.getElementById(`pdetail-content-${docId}`);
  if (!container) return;
  const doc = pendingDocs.find(d => d.id === docId);
  if (!doc) return;

  const lines = doc.ai_suggestions || doc.line_items || [];
  if (lines.length === 0) {
    container.innerHTML = '<span style="color:var(--text-dim);">Sin líneas de detalle.</span>';
    return;
  }

  let html = `<table style="width:100%; border-collapse:collapse; font-family:var(--mono); font-size:0.6rem;">
    <thead>
      <tr style="border-bottom:1px solid var(--border);">
        <th style="padding:4px 8px; text-align:left;">#</th>
        <th style="padding:4px 8px; text-align:left;">DESCRIPCIÓN</th>
        <th style="padding:4px 8px; text-align:left;">PRODUCTO SUGERIDO</th>
        <th style="padding:4px 8px; text-align:right;">CTD</th>
        <th style="padding:4px 8px; text-align:right;">PRECIO</th>
        <th style="padding:4px 8px; text-align:right;">TOTAL</th>
        <th style="padding:4px 8px; text-align:center;">CONF.</th>
        <th style="padding:4px 8px; text-align:left;">RAZÓN</th>
      </tr>
    </thead>
    <tbody>`;

  lines.forEach((l, i) => {
    const confianza = l.confianza ? (l.confianza * 100).toFixed(0) + "%" : "—";
    const colorConf = l.confianza > 0.8 ? "var(--green)" : l.confianza > 0.6 ? "var(--yellow)" : "var(--red)";
    html += `<tr${i % 2 === 1 ? ' style="background:rgba(255,255,255,0.015)"' : ''}>
      <td style="padding:4px 8px;">${l.numero_linea || l.numero || (i+1)}</td>
      <td style="padding:4px 8px; color:var(--text);">${l.descripcion_original || l.descripcion || "—"}</td>
      <td style="padding:4px 8px; color:var(--cyan);">${l.producto_nombre || l.codigo_producto || "—"}</td>
      <td style="padding:4px 8px; text-align:right;">${l.cantidad || 0}</td>
      <td style="padding:4px 8px; text-align:right;">${(l.precio_unitario || 0).toLocaleString()}</td>
      <td style="padding:4px 8px; text-align:right;">${(l.total || l.cantidad * l.precio_unitario || 0).toLocaleString()}</td>
      <td style="padding:4px 8px; text-align:center; color:${colorConf};">${confianza}</td>
      <td style="padding:4px 8px; color:var(--text-dim); font-size:0.55rem;">${l.razon || ""}</td>
    </tr>`;
  });

  html += "</tbody></table>";
  container.innerHTML = html;
}

// ── Mapeo IA ──

async function runAiMap(docId) {
  if (!odooCredentials) {
    alert("❌ No hay credenciales Odoo guardadas. Ve a CONFIGURACIÓN primero.");
    return;
  }

  const row = document.getElementById(`prow-${docId}`);
  const btns = row?.querySelectorAll('button');
  const btn = btns ? btns[btns.length - 1] : null;
  if (btn) { btn.disabled = true; btn.textContent = "⏳"; }

  try {
    const formData = new FormData();
    formData.append("doc_id", docId);
    formData.append("credentials", odooCredentials);

    const res = await fetch(`${API_URL}/api/purchase/ai-map`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);

    log(`Mapeo IA completado para documento #${docId}: ${data.total_lineas} líneas mapeadas.`, "ok");
    await loadPending();
  } catch (err) {
    console.error("AI Map error:", err);
    alert(`❌ Error en mapeo IA: ${err.message}`);
    if (btn) btn.disabled = false;
  }
}

// ── Aceptar mapeo ──

async function acceptMapping(docId) {
  try {
    const formData = new FormData();
    formData.append("doc_id", docId);
    formData.append("action", "accept");

    const res = await fetch(`${API_URL}/api/purchase/review`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);

    log(`Documento #${docId} aprobado.`, "ok");
    await loadPending();
  } catch (err) {
    alert(`❌ Error: ${err.message}`);
  }
}

// ── Modal de corrección ──

function openCorrectionModal(docId) {
  currentModalDocId = docId;
  const doc = pendingDocs.find(d => d.id === docId);
  if (!doc) return;

  document.getElementById("modal-doc-label").textContent = `#${docId} — ${doc.supplier_name || "Desconocido"}`;

  const tbody = document.getElementById("modal-lines-body");
  const lines = doc.ai_suggestions || doc.line_items || [];
  tbody.innerHTML = "";

  lines.forEach((l, i) => {
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid var(--border)";
    tr.dataset.idx = i;
    // Guardar todos los campos originales en data-* para preservarlos al guardar
    const descOrig = (l.descripcion_original || l.descripcion || "").replace(/"/g, '&quot;');
    const prodName = l.producto_nombre || l.codigo_producto || "";
    const prodId = l.producto_id || "";
    const numLinea = l.numero_linea || l.numero || String(i + 1);
    tr.innerHTML = `
      <td style="padding:6px;">${numLinea}</td>
      <td style="padding:6px; color:var(--text-dim);">${l.descripcion_original || l.descripcion || "—"}</td>
      <td style="padding:6px; position:relative;">
        <input type="hidden" class="modal-pid-input" data-idx="${i}" value="${prodId}" />
        <input type="hidden" class="modal-desc-input" data-idx="${i}" value="${descOrig}" />
        <input type="hidden" class="modal-numlinea-input" data-idx="${i}" value="${numLinea}" />
        <div style="display:flex; gap:3px; align-items:center;">
          <input type="text" class="login-input modal-product-input" data-idx="${i}"
            value="${prodName}"
            style="flex:1; font-size:0.6rem; padding:3px 6px; height:auto; min-width:80px;"
            placeholder="Buscar producto Odoo..."
            autocomplete="off" />
          <button type="button" class="btn btn-outline" onclick="searchOdooProducts(${i})"
            style="padding:2px 6px; font-size:0.5rem;" title="Buscar en Odoo">🔍</button>
        </div>
        <div class="modal-search-results" data-idx="${i}" style="display:none; position:absolute; top:100%; left:0; right:0; background:var(--bg-card); border:1px solid var(--border); border-radius:4px; max-height:150px; overflow-y:auto; z-index:10; font-size:0.55rem;"></div>
      </td>
      <td style="padding:6px;">
        <input type="number" step="0.01" class="login-input modal-qty-input" data-idx="${i}"
          value="${l.cantidad || 0}"
          style="width:60px; font-size:0.6rem; padding:3px 6px; height:auto; text-align:right;" />
      </td>
      <td style="padding:6px;">
        <input type="number" step="0.01" class="login-input modal-price-input" data-idx="${i}"
          value="${l.precio_unitario || 0}"
          style="width:80px; font-size:0.6rem; padding:3px 6px; height:auto; text-align:right;" />
      </td>
    `;
    tbody.appendChild(tr);
  });

  document.getElementById("purchase-modal-overlay").style.display = "flex";
}

function closeModal() {
  currentModalDocId = null;
  document.getElementById("purchase-modal-overlay").style.display = "none";
}

// ── Buscar productos Odoo (autocomplete modal) ──

async function searchOdooProducts(idx) {
  if (!odooCredentials) {
    alert("❌ No hay credenciales Odoo. Configúralas primero.");
    return;
  }
  const input = document.querySelector(`.modal-product-input[data-idx="${idx}"]`);
  const resultsDiv = document.querySelector(`.modal-search-results[data-idx="${idx}"]`);
  if (!input || !resultsDiv) return;

  const query = input.value.trim();
  if (!query || query.length < 2) {
    resultsDiv.innerHTML = '<div style="padding:4px 8px; color:var(--text-dim);">Escribe al menos 2 caracteres</div>';
    resultsDiv.style.display = "block";
    return;
  }

  resultsDiv.innerHTML = '<div style="padding:4px 8px; color:var(--text-dim);">Buscando...</div>';
  resultsDiv.style.display = "block";

  try {
    const formData = new FormData();
    formData.append("query", query);
    formData.append("credentials", odooCredentials);
    formData.append("limit", "20");

    const res = await fetch(`${API_URL}/api/purchase/search-products`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Error");

    if (!data.products || data.products.length === 0) {
      resultsDiv.innerHTML = '<div style="padding:4px 8px; color:var(--text-dim);">Sin resultados</div>';
      return;
    }

    resultsDiv.innerHTML = data.products.map(p => {
      const code = p.default_code ? ` [${p.default_code}]` : "";
      return `<div class="search-result-item" data-id="${p.id}" data-name="${p.name}"
        style="padding:3px 8px; cursor:pointer; border-bottom:1px solid var(--border);"
        onclick="selectProduct(${idx}, ${p.id}, '${p.name.replace(/'/g, "\\'")}')">
        ${p.name}${code}
      </div>`;
    }).join("");
  } catch (err) {
    resultsDiv.innerHTML = `<div style="padding:4px 8px; color:var(--red);">Error: ${err.message}</div>`;
  }
}

function selectProduct(idx, pid, pname) {
  const input = document.querySelector(`.modal-product-input[data-idx="${idx}"]`);
  const hidden = document.querySelector(`.modal-pid-input[data-idx="${idx}"]`);
  const resultsDiv = document.querySelector(`.modal-search-results[data-idx="${idx}"]`);
  if (input) input.value = pname;
  if (hidden) hidden.value = pid;
  if (resultsDiv) resultsDiv.style.display = "none";
}

// Cerrar resultados al hacer clic fuera
document.addEventListener("click", (e) => {
  if (!e.target.closest(".modal-search-results") && !e.target.closest(".modal-product-input") && !e.target.closest("[onclick*='searchOdooProducts']")) {
    document.querySelectorAll(".modal-search-results").forEach(el => el.style.display = "none");
  }
});

async function saveCorrections() {
  if (!currentModalDocId) return;

  const productInputs = document.querySelectorAll(".modal-product-input");
  const pidInputs = document.querySelectorAll(".modal-pid-input");
  const descInputs = document.querySelectorAll(".modal-desc-input");
  const numLineaInputs = document.querySelectorAll(".modal-numlinea-input");
  const qtyInputs = document.querySelectorAll(".modal-qty-input");
  const priceInputs = document.querySelectorAll(".modal-price-input");

  const manualLines = [];
  productInputs.forEach((input, i) => {
    const pid = parseInt(pidInputs[i]?.value) || null;
    const pname = input.value.trim();
    // Validar que la línea tiene producto asignado
    if (!pid) {
      console.warn(`Línea ${i+1}: sin producto_id asignado (se usará null)`);
    }
    manualLines.push({
      numero_linea: numLineaInputs[i]?.value || String(i + 1),
      // descripcion_original es clave para correction_history
      descripcion_original: descInputs[i]?.value || "",
      producto_id: pid,
      producto_nombre: pname,
      confianza: pid ? 1.0 : 0.0,   // línea corregida manualmente = confianza total
      razon: pid ? "Corregido manualmente por el usuario" : "Sin producto asignado",
      cantidad: parseFloat(qtyInputs[i]?.value) || 0,
      precio_unitario: parseFloat(priceInputs[i]?.value) || 0,
    });
  });

  // Validar que al menos una línea tiene producto asignado
  const lineasSinProducto = manualLines.filter(l => !l.producto_id);
  if (lineasSinProducto.length > 0) {
    const nombres = lineasSinProducto.map((l, i) => `Línea ${l.numero_linea}: "${l.descripcion_original || '—'}"`).join("\n");
    const continuar = confirm(`⚠️ Las siguientes líneas no tienen producto asignado:\n${nombres}\n\n¿Continuar de todas formas?`);
    if (!continuar) return;
  }

  try {
    const formData = new FormData();
    formData.append("doc_id", currentModalDocId);
    formData.append("action", "correct");
    formData.append("manual_lines", JSON.stringify(manualLines));

    const res = await fetch(`${API_URL}/api/purchase/review`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);

    log(`✅ Correcciones guardadas para documento #${currentModalDocId}. ${manualLines.length} línea(s) procesadas.`, "ok");
    closeModal();
    await loadPending();
  } catch (err) {
    alert(`❌ Error guardando correcciones: ${err.message}`);
  }
}

// ── Crear OC en Odoo ──

async function createOC(docId) {
  if (!odooCredentials) {
    alert("❌ No hay credenciales Odoo guardadas. Ve a CONFIGURACIÓN primero.");
    return;
  }

  const confirmed = confirm("¿Generar Orden de Compra en Odoo con las líneas revisadas?");
  if (!confirmed) return;

  try {
    const formData = new FormData();
    formData.append("doc_id", docId);
    formData.append("credentials", odooCredentials);
    formData.append("action", "create");

    const res = await fetch(`${API_URL}/api/purchase/create-oc`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);

    log(`✅ OC #${data.purchase_order_name} creada en Odoo (Estado: ${data.state}).`, "ok");
    alert(`✅ OC ${data.purchase_order_name} creada exitosamente.\nProveedor: ${data.partner_name}\nLíneas: ${data.total_lineas}\nEstado: ${data.state}`);
    await loadPending();
  } catch (err) {
    console.error("createOC error:", err);
    alert(`❌ Error creando OC: ${err.message}`);
  }
}
