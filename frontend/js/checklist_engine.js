// checklist_engine.js – Ejecuta el auditor IA y muestra resultados

// Elementos del DOM
const btnRun = document.getElementById('btn-run-ai-checklist');
const monthInput = document.getElementById('checklist-filter-month');
const resultsContainer = document.getElementById('checklist-results');
const statusMsg = document.createElement('div');
statusMsg.style.fontSize = '0.7rem';
statusMsg.style.marginTop = '0.5rem';

function clearResults() {
  resultsContainer.innerHTML = '';
  statusMsg.textContent = '';
}

function renderAnomalies(anomalies) {
  if (!Array.isArray(anomalies) || anomalies.length === 0) {
    resultsContainer.innerHTML = '<div style="padding:1rem; text-align:center;">No se encontraron anomalías.</div>';
    return;
  }
  anomalies.forEach(a => {
    const card = document.createElement('div');
    card.style.border = '1px solid var(--border)';
    card.style.borderRadius = '6px';
    card.style.padding = '0.75rem';
    card.style.margin = '0.5rem';
    card.style.background = 'rgba(255,255,255,0.02)';
    const badgeColor = a.gravedad === 'alta' ? '#ef4444' : a.gravedad === 'media' ? '#f59e0b' : '#10b981';
    card.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:0.4rem;">
        <strong style="font-size:0.85rem; color:var(--text);">${a.titulo || 'Anomalía'}</strong>
        <span style="background:${badgeColor}22; color:${badgeColor}; border:1px solid ${badgeColor}44; padding:2px 6px; border-radius:4px; font-size:0.6rem; text-transform:uppercase; font-weight:bold;">${a.gravedad || 'info'}</span>
      </div>
      <p style="font-size:0.75rem; color:var(--text-mid); margin:0.3rem 0; line-height:1.3;">${a.hallazgo || ''}</p>
      ${a.recomendacion ? `<div style="font-size:0.7rem; color:var(--cyan); margin-top:0.4rem; border-top:1px dashed var(--border); padding-top:0.4rem;">💡 <strong>Recomendación:</strong> ${a.recomendacion}</div>` : ''}
    `;
    resultsContainer.appendChild(card);
  });
}

async function runChecklist() {
  const monthVal = monthInput.value; // format YYYY-MM
  if (!monthVal) {
    alert('Selecciona un mes para evaluar.');
    return;
  }
  const [year, month] = monthVal.split('-');

  clearResults();
  btnRun.disabled = true;
  btnRun.textContent = 'Procesando...';

  const formData = new FormData();
  formData.append('month', month);
  formData.append('year', year);
  // Enviar credenciales Odoo guardadas (Configuración → Odoo)
  if (typeof odooCredentials !== 'undefined' && odooCredentials) {
    formData.append('credentials', odooCredentials);
  }

  try {
    const res = await fetch(`${API_URL}/api/run-ai-checklist`, {
      method: 'POST',
      body: formData
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Error ${res.status}`);
    }
    const data = await res.json();
    if (data.success) {
      statusMsg.textContent = `Periodo evaluado: ${data.periodo}. Facturas analizadas: ${data.total_facturas_analizadas}.`;
      resultsContainer.parentElement.insertBefore(statusMsg, resultsContainer);
      renderAnomalies(data.anomalias);
    } else {
      statusMsg.textContent = 'Error inesperado al ejecutar la auditoría.';
    }
  } catch (e) {
    console.error(e);
    if (e.message.includes('Credenciales') || e.message.includes('GROQ_API_KEY')) {
      statusMsg.textContent = `⚠ ${e.message}`;
    } else {
      statusMsg.textContent = 'Falló la comunicación con el servidor.';
    }
  } finally {
    btnRun.disabled = false;
    btnRun.innerHTML = `
      <svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" style="width:14px; height:14px; margin-right:5px; vertical-align:middle; display:inline;">
        <path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/>
      </svg>
      EJECUTAR AUDITORÍA IA`;
  }
}

if (btnRun) btnRun.addEventListener('click', runChecklist);
