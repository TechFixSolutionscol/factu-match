let bankFile = null;
let erpFile = null;
let reconciliationResults = null;

// UI Setup
document.addEventListener("DOMContentLoaded", () => {
  setupReconUpload("input-banco", "zona-banco", "nombre-banco", f => bankFile = f, "BANCO");
  setupReconUpload("input-erp-banco", "zona-erp-banco", "nombre-erp-banco", f => erpFile = f, "ERP");

  document.getElementById("btn-limpiar-recon").addEventListener("click", () => {
    bankFile = null; erpFile = null; reconciliationResults = null;
    ["banco", "erp-banco"].forEach(k => {
      document.getElementById(`input-${k}`).value = "";
      document.getElementById(`nombre-${k}`).textContent = "";
      document.getElementById(`zona-${k}`).classList.remove("cargado");
    });
    document.getElementById("recon-seccion-resultado").style.display = "none";
    document.getElementById("btn-conciliar").disabled = true;
    log("Conciliación reiniciada.", 'msg');
  });

  document.getElementById("btn-conciliar").addEventListener("click", async () => {
    const btn = document.getElementById("btn-conciliar");
    btn.disabled = true;
    btn.innerHTML = '<div class="btn-loader"></div> PROCESANDO...';
    try {
      await runReconciliation();
    } catch(err) {
      log(`Error en conciliación: ${err.message}`, 'err');
    } finally {
      btn.disabled = false;
      btn.innerHTML = 'EJECUTAR CONCILIACIÓN';
    }
  });

  document.getElementById("btn-export-recon").addEventListener("click", exportReconciliation);
});

function setupReconUpload(inputId, zonaId, nombreId, setter, label) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.addEventListener("change", e => {
    const file = e.target.files[0];
    if (!file) return;
    setter(file);
    document.getElementById(nombreId).textContent = file.name;
    document.getElementById(zonaId).classList.add("cargado");
    log(`Archivo ${label} cargado: ${file.name}`, 'ok');
    const listo = bankFile && erpFile;
    document.getElementById("btn-conciliar").disabled = !listo;
  });
}

// Data Parsing
async function readExcelFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const data = new Uint8Array(e.target.result);
        const workbook = XLSX.read(data, { type: 'array' });
        const firstSheet = workbook.SheetNames[0];
        const rows = XLSX.utils.sheet_to_json(workbook.Sheets[firstSheet], { defval: "" });
        resolve(rows);
      } catch (err) {
        reject(err);
      }
    };
    reader.onerror = reject;
    reader.readAsArrayBuffer(file);
  });
}

// The Matching Logic
async function runReconciliation() {
  log("Iniciando motor de conciliación...", 'msg');
  
  const bankRowsRaw = await readExcelFile(bankFile);
  const erpRowsRaw = await readExcelFile(erpFile);

  // Normalize Bank Data (Assume standard columns or search for keywords)
  const bankData = normalizeBankData(bankRowsRaw);
  const erpData = normalizeERPData(erpRowsRaw);

  log(`Filas Banco extraídas: ${bankData.length}`, 'ok');
  log(`Filas ERP extraídas: ${erpData.length}`, 'ok');

  // Matching Logic
  const matches = [];
  const unmatchedBank = [];
  const unmatchedERP = [...erpData]; // Copy to track un-matched

  for (let b of bankData) {
    let matched = false;
    
    const hasRefMatch = (erpItem, bankItem) => {
        const erpRef = String(erpItem.reference).trim().toLowerCase();
        const bankDesc = String(bankItem.description).trim().toLowerCase();
        const bankDoc = String(bankItem.document).trim().toLowerCase();
        
        if (!erpRef) return false; // Evitar que strings vacíos hagan match con todo
        
        if (bankDesc.includes(erpRef)) return true;
        if (bankDoc && bankDoc.includes(erpRef)) return true;
        if (bankDoc && erpRef.includes(bankDoc)) return true;
        
        return false;
    };

    // Phase 1: Exact Match (Referencia + Valor)
    let idx1 = unmatchedERP.findIndex(e => Math.abs(e.amount) === Math.abs(b.amount) && hasRefMatch(e, b));
    if (idx1 !== -1) {
      matches.push({ bank: b, erp: unmatchedERP[idx1], type: 'MATCH_EXACTO' });
      unmatchedERP.splice(idx1, 1);
      continue;
    }
    
    // Phase 2: Solo Referencia (Montos pueden diferir por retenciones o parciales)
    let idx2 = unmatchedERP.findIndex(e => hasRefMatch(e, b));
    if (idx2 !== -1) {
      matches.push({ bank: b, erp: unmatchedERP[idx2], type: 'MATCH_REFERENCIA' });
      unmatchedERP.splice(idx2, 1);
      continue;
    }
    
    // Phase 3: Solo Valor (Riesgo de falso positivo, se marca diferente)
    let idx3 = unmatchedERP.findIndex(e => Math.abs(e.amount) === Math.abs(b.amount));
    if (idx3 !== -1) {
      matches.push({ bank: b, erp: unmatchedERP[idx3], type: 'MATCH_VALOR' });
      unmatchedERP.splice(idx3, 1);
      continue;
    }

    unmatchedBank.push(b);
  }

  reconciliationResults = {
    matches,
    unmatchedBank,
    unmatchedERP,
    totalBank: bankData.length,
    totalERP: erpData.length
  };

  renderReconciliationResults();
  log("Conciliación completada.", 'ok');
}

function normalizeBankData(rows) {
  return rows.map((r, index) => {
    // Extraer valores exactos del formato Bancolombia
    let date = r["FECHA"] || r["Fecha"] || "";
    let desc = r["DESCRIPCIÓN"] || r["Descripción"] || "";
    let amount = r["VALOR"] || r["Valor"] || 0;
    let doc = r["DCTO."] || r["DOCUMENTO"] || r["Documento"] || "";
    
    // Fallback heurístico por si cambian de formato (opcional)
    if (!date) {
      const vals = Object.values(r);
      date = vals[0] || "";
    }
    if (!desc) {
      const keys = Object.keys(r);
      let descKey = keys.find(k=>k.toLowerCase().includes("descrip"));
      desc = descKey ? r[descKey] : `Movimiento ${index+1}`;
    }
    if (amount === 0) {
      const keys = Object.keys(r);
      let amtKey = keys.find(k => ['valor', 'monto'].some(word => k.toLowerCase().includes(word)));
      amount = amtKey ? r[amtKey] : 0;
    }

    if (typeof amount === 'string') amount = parseFloat(amount.replace(/[^\d.-]/g, '')) || 0;
    if (typeof desc === 'object') desc = JSON.stringify(desc);

    return {
      id: `B-${index}`,
      date: date,
      description: desc,
      document: doc, // Añadido documento para matching
      amount: amount,
      original: r
    };
  }).filter(r => r.amount !== 0);
}

function normalizeERPData(rows) {
  return rows.map((r, index) => {
    const keys = Object.keys(r);

    // Mapeo exacto para SIESA u Odoo v15
    let date = r["Fecha de Factura/Recibo"] || r["Fecha docto. prov."] || r["Fecha"] || r["FECHA"] || r["Date"] || "";
    let ref = r["Número"] || r["Docto. proveedor"] || r["Factura"] || r["Referencia"] || r["Documento"] || r["Reference"] || "";
    
    // Búsqueda del monto
    let amtKey = keys.find(k => ['total', 'valor', 'monto', 'amount', 'pagado', 'saldo'].some(word => k.toLowerCase().includes(word)));
    let amount = r["Total con signo"] || (amtKey ? r[amtKey] : 0);

    // Fallbacks
    if (!date) {
        const vals = Object.values(r);
        date = vals.length > 0 ? vals[0] : "";
    }
    if (!ref) {
        const vals = Object.values(r);
        ref = vals.length > 1 ? vals[1] : "";
    }
    if (amount === 0) {
        const vals = Object.values(r);
        amount = vals[vals.length - 1] || 0;
    }

    if (typeof amount === 'string') amount = parseFloat(amount.replace(/[^\d.-]/g, '')) || 0;

    return {
      id: `E-${index}`,
      date: date,
      reference: ref,
      amount: amount,
      original: r
    };
  }).filter(r => r.amount !== 0);
}

let currentReconTab = 'matches';
let reconCurrentPage = 1;
const reconItemsPerPage = 50;

function renderReconciliationResults() {
  const r = reconciliationResults;
  if (!r) return;

  document.getElementById("recon-seccion-resultado").style.display = "block";
  
  // Actualizar contadores generales
  if (document.getElementById("r-banco")) document.getElementById("r-banco").textContent = r.totalBank;
  if (document.getElementById("r-erp")) document.getElementById("r-erp").textContent = r.totalERP;
  if (document.getElementById("r-match")) document.getElementById("r-match").textContent = r.matches.length;
  if (document.getElementById("r-unmatch")) document.getElementById("r-unmatch").textContent = r.unmatchedBank.length + r.unmatchedERP.length;
  
  const pct = r.totalBank > 0 ? Math.round((r.matches.length / r.totalBank) * 100) : 0;
  if (document.getElementById("recon-pct-completitud")) document.getElementById("recon-pct-completitud").textContent = pct + "%";

  // Actualizar contadores de pestañas
  document.getElementById("count-tab-matches").textContent = r.matches.length;
  document.getElementById("count-tab-bank").textContent = r.unmatchedBank.length;
  document.getElementById("count-tab-erp").textContent = r.unmatchedERP.length;

  reconCurrentPage = 1; // Reiniciar a la primera página al conciliar
  renderReconPage();
}

function renderReconPage() {
  if (!reconciliationResults) return;
  const r = reconciliationResults;
  
  let data = [];
  let theadHtml = '';
  
  if (currentReconTab === 'matches') {
    data = r.matches;
    theadHtml = `
      <tr style="border-bottom:1px solid var(--border); background:rgba(255,255,255,0.02);">
        <th style="padding:10px; text-align:left;">FECHA BANCO</th>
        <th style="padding:10px; text-align:left;">DESCRIPCIÓN</th>
        <th style="padding:10px; text-align:right;">VALOR BANCO</th>
        <th style="padding:10px; text-align:center;">ESTADO</th>
        <th style="padding:10px; text-align:left;">ERP REF</th>
      </tr>
    `;
  } else if (currentReconTab === 'unmatchedBank') {
    data = r.unmatchedBank;
    theadHtml = `
      <tr style="border-bottom:1px solid var(--border); background:rgba(255,255,255,0.02);">
        <th style="padding:10px; text-align:left;">FECHA</th>
        <th style="padding:10px; text-align:left;">DESCRIPCIÓN</th>
        <th style="padding:10px; text-align:right;">VALOR BANCO</th>
        <th style="padding:10px; text-align:center;">DOCUMENTO</th>
        <th style="padding:10px; text-align:center;">ESTADO</th>
      </tr>
    `;
  } else if (currentReconTab === 'unmatchedERP') {
    data = r.unmatchedERP;
    theadHtml = `
      <tr style="border-bottom:1px solid var(--border); background:rgba(255,255,255,0.02);">
        <th style="padding:10px; text-align:left;">FECHA</th>
        <th style="padding:10px; text-align:left;">REFERENCIA</th>
        <th style="padding:10px; text-align:right;">VALOR ERP</th>
        <th style="padding:10px; text-align:center;">-</th>
        <th style="padding:10px; text-align:center;">ESTADO</th>
      </tr>
    `;
  }

  document.getElementById("recon-table-head").innerHTML = theadHtml;
  
  const totalItems = data.length;
  const totalPages = Math.ceil(totalItems / reconItemsPerPage) || 1;
  
  if (reconCurrentPage > totalPages) reconCurrentPage = totalPages;
  if (reconCurrentPage < 1) reconCurrentPage = 1;
  
  const startIndex = (reconCurrentPage - 1) * reconItemsPerPage;
  const endIndex = Math.min(startIndex + reconItemsPerPage, totalItems);
  const pageData = data.slice(startIndex, endIndex);

  document.getElementById("recon-page-info").textContent = `Mostrando ${totalItems === 0 ? 0 : startIndex + 1} a ${endIndex} de ${totalItems}`;
  
  const btnPrev = document.getElementById("btn-recon-prev");
  const btnNext = document.getElementById("btn-recon-next");
  if(btnPrev) btnPrev.disabled = reconCurrentPage === 1;
  if(btnNext) btnNext.disabled = reconCurrentPage === totalPages;

  const tbody = document.getElementById("recon-table-body");
  tbody.innerHTML = "";

  pageData.forEach(item => {
    const tr = document.createElement("tr");
    
    if (currentReconTab === 'matches') {
      const m = item;
      let badgeStyle = 'class="status-badge status-ok"';
      if (m.type === 'MATCH_VALOR') {
        badgeStyle = 'class="status-badge pending" style="border-color:#f59e0b; color:#f59e0b;"';
      } else if (m.type === 'MATCH_REFERENCIA') {
        badgeStyle = 'class="status-badge pending" style="border-color:var(--cyan); color:var(--cyan);"';
      } else if (m.type === 'MATCH_IA') {
        badgeStyle = 'class="status-badge pending" style="border-color:#9333ea; color:#9333ea; text-shadow:0 0 5px rgba(147,51,234,0.3);"';
      }
      tr.innerHTML = `
        <td style="padding:10px;">${m.bank.date}</td>
        <td style="padding:10px; color:var(--text-mid); font-size:0.6rem;">${m.bank.description}</td>
        <td style="padding:10px; text-align:right;">$ ${m.bank.amount.toLocaleString()}</td>
        <td style="padding:10px; text-align:center;"><span ${badgeStyle}>${m.type}</span></td>
        <td style="padding:10px; color:var(--cyan); font-size:0.6rem;">${m.erp.reference}</td>
      `;
    } else if (currentReconTab === 'unmatchedBank') {
      const b = item;
      tr.innerHTML = `
        <td style="padding:10px;">${b.date}</td>
        <td style="padding:10px; color:var(--text-mid); font-size:0.6rem;">${b.description}</td>
        <td style="padding:10px; text-align:right;">$ ${b.amount.toLocaleString()}</td>
        <td style="padding:10px; text-align:center;">${b.document || '-'}</td>
        <td style="padding:10px; text-align:center;"><span class="status-badge pending" style="border-color:var(--red);color:var(--red);">SIN MATCH</span></td>
      `;
    } else if (currentReconTab === 'unmatchedERP') {
      const e = item;
      tr.innerHTML = `
        <td style="padding:10px;">${e.date}</td>
        <td style="padding:10px; color:var(--cyan); font-size:0.6rem;">${e.reference}</td>
        <td style="padding:10px; text-align:right;">$ ${e.amount.toLocaleString()}</td>
        <td style="padding:10px; text-align:center;">-</td>
        <td style="padding:10px; text-align:center;"><span class="status-badge pending" style="border-color:var(--red);color:var(--red);">SIN MATCH</span></td>
      `;
    }
    tbody.appendChild(tr);
  });
}

// Configuración de eventos para pestañas y paginación
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".recon-tab-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      document.querySelectorAll(".recon-tab-btn").forEach(b => {
        b.classList.remove("active");
        b.style.color = "var(--text-mid)";
        b.style.borderBottomColor = "transparent";
      });
      const target = e.currentTarget;
      target.classList.add("active");
      target.style.color = "var(--cyan)";
      target.style.borderBottomColor = "var(--cyan)";
      
      currentReconTab = target.getAttribute("data-tab");
      reconCurrentPage = 1;
      renderReconPage();
    });
  });

  const btnPrev = document.getElementById("btn-recon-prev");
  const btnNext = document.getElementById("btn-recon-next");
  if(btnPrev) {
    btnPrev.addEventListener("click", () => {
      if(reconCurrentPage > 1) {
        reconCurrentPage--;
        renderReconPage();
      }
    });
  }
  if(btnNext) {
    btnNext.addEventListener("click", () => {
      reconCurrentPage++;
      renderReconPage();
    });
  }

  const btnExport = document.getElementById("btn-export-recon");
  if(btnExport) {
    btnExport.addEventListener("click", exportReconciliation);
  }

  const btnReconIA = document.getElementById("btn-recon-ia");
  if (btnReconIA) {
    btnReconIA.addEventListener("click", runAIReconciliation);
  }
});

async function runAIReconciliation() {
  if (!reconciliationResults) return;
  
  const r = reconciliationResults;
  if (r.unmatchedBank.length === 0 || r.unmatchedERP.length === 0) {
    alert("No hay suficientes registros huérfanos de banco y ERP para que la IA realice un cruce.");
    return;
  }

  const btn = document.getElementById("btn-recon-ia");
  const originalHtml = btn.innerHTML;
  btn.innerHTML = `<span style="display:inline-block; width:10px; height:10px; border:2px solid #9333ea; border-top-color:transparent; border-radius:50%; animation:spin 1s linear infinite;"></span> PROCESANDO IA...`;
  btn.disabled = true;

  try {
    const payload = {
      banco: r.unmatchedBank,
      erp: r.unmatchedERP
    };

    const response = await fetch(`${API_URL}/conciliar-ia`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    if (!response.ok) {
      throw new Error(`Error HTTP: ${response.status}`);
    }

    const data = await response.json();
    
    if (data.success && data.matches && data.matches.length > 0) {
      let matchesCount = 0;

      data.matches.forEach(m => {
        const bId = m.id_banco;
        const eId = m.id_erp;

        const bIndex = r.unmatchedBank.findIndex(x => x.id === bId);
        const eIndex = r.unmatchedERP.findIndex(x => x.id === eId);

        if (bIndex !== -1 && eIndex !== -1) {
          const bankItem = r.unmatchedBank[bIndex];
          const erpItem = r.unmatchedERP[eIndex];

          r.matches.push({
            bank: bankItem,
            erp: erpItem,
            type: 'MATCH_IA',
            reason: m.razon || "Cruce semántico inteligente"
          });

          // Eliminamos en orden descendente (por seguridad, aunque son únicos)
          r.unmatchedBank.splice(bIndex, 1);
          r.unmatchedERP.splice(eIndex, 1);
          matchesCount++;
        }
      });

      if (matchesCount > 0) {
        alert(`¡Inteligencia Artificial aplicada! Se encontraron ${matchesCount} cruces semánticos.`);
        // Cambiar pestaña activa a matches para que el usuario lo vea de inmediato
        currentReconTab = 'matches';
        document.querySelectorAll(".recon-tab-btn").forEach(b => {
          b.classList.remove("active");
          b.style.color = "var(--text-mid)";
          b.style.borderBottomColor = "transparent";
        });
        const matchTab = document.querySelector('.recon-tab-btn[data-tab="matches"]');
        if(matchTab) {
          matchTab.classList.add("active");
          matchTab.style.color = "var(--cyan)";
          matchTab.style.borderBottomColor = "var(--cyan)";
        }
        renderReconciliationResults();
      } else {
        alert("La IA procesó los datos pero no encontró cruces con alta certeza.");
      }
    } else {
      alert("La IA analizó los datos pero no encontró cruces seguros (>80% certeza).");
    }

  } catch (err) {
    console.error(err);
    alert("Error de conexión con la IA: " + err.message);
  } finally {
    btn.innerHTML = originalHtml;
    btn.disabled = false;
  }
}

// Estilo para la animación de carga IA
const style = document.createElement('style');
style.textContent = `
@keyframes spin { 100% { transform: rotate(360deg); } }
`;
document.head.appendChild(style);

function exportReconciliation() {
  if (!reconciliationResults) return;
  const { matches, unmatchedBank, unmatchedERP } = reconciliationResults;

  const wb = XLSX.utils.book_new();

  const wsMatches = XLSX.utils.json_to_sheet(matches.map(m => ({
    "Fecha Banco": m.bank.date,
    "Descripción Banco": m.bank.description,
    "Valor Banco": m.bank.amount,
    "Fecha ERP": m.erp.date,
    "Referencia ERP": m.erp.reference,
    "Valor ERP": m.erp.amount,
    "Tipo Match": m.type
  })));
  XLSX.utils.book_append_sheet(wb, wsMatches, "Conciliados");

  const wsBank = XLSX.utils.json_to_sheet(unmatchedBank.map(b => ({
    "Fecha": b.date,
    "Descripción": b.description,
    "Valor": b.amount
  })));
  XLSX.utils.book_append_sheet(wb, wsBank, "Pendientes Banco");

  const wsErp = XLSX.utils.json_to_sheet(unmatchedERP.map(e => ({
    "Fecha": e.date,
    "Referencia": e.reference,
    "Valor": e.amount
  })));
  XLSX.utils.book_append_sheet(wb, wsErp, "Pendientes ERP");

  XLSX.writeFile(wb, "Reporte_Conciliacion.xlsx");
}
