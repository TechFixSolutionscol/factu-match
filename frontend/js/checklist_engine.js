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
    card.innerHTML = `
      <strong>${a.title || 'Anomalía'}</strong><br/>
      <small>${a.description || ''}</small>
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
  const groqKey = localStorage.getItem('groq_api_key') || '';
  if (!groqKey) {
    alert('Configura la clave API de Groq en la pantalla de Configuración.');
    return;
  }

  clearResults();
  btnRun.disabled = true;
  btnRun.textContent = 'Procesando...';

  const formData = new FormData();
  formData.append('month', month);
  formData.append('year', year);
  formData.append('groq_key', groqKey);

  try {
    const res = await fetch(`${API_URL}/api/run-ai-checklist`, {
      method: 'POST',
      body: formData
    });
    if (!res.ok) throw new Error(`Error ${res.status}`);
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
    statusMsg.textContent = 'Falló la comunicación con el servidor.';
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
