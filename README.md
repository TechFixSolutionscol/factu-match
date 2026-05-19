# 📊 Comparador DIAN vs Siesa

Sistema para detectar facturas recibidas que están en la DIAN pero no están registradas en Siesa.

---

## Estructura del proyecto

```
factura-comparador/
├── backend/
│   ├── main.py              # Servidor FastAPI
│   ├── comparador.py        # Lógica de comparación
│   ├── requirements.txt     # Dependencias Python
│   └── .env.example         # Variables de entorno
└── frontend/
    └── index.html           # Interfaz web
```

---

## Despliegue en Render (backend)

1. Ve a https://render.com y crea cuenta con GitHub
2. Clic en **New → Web Service**
3. Conecta tu repositorio
4. Configura así:

| Campo | Valor |
|---|---|
| Root Directory | `backend` |
| Runtime | `Python 3` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |

5. En **Environment Variables** agrega:
   - `GROQ_API_KEY` → tu key de https://console.groq.com

6. Deploy. Render te da una URL como:
   `https://comparador-facturas.onrender.com`

---

## Configurar el frontend con la URL de Render

Abre `frontend/index.html` con cualquier editor y cambia esta línea:

```javascript
const API_URL = "REEMPLAZA_CON_TU_URL_DE_RENDER";
```

Por la URL que te dio Render:

```javascript
const API_URL = "https://comparador-facturas.onrender.com";
```

---

## Despliegue en Vercel (frontend)

1. Ve a https://vercel.com y crea cuenta con GitHub
2. **New Project** → selecciona tu repositorio
3. En **Root Directory** pon `frontend`
4. Clic en **Deploy**

Vercel te da una URL como:
`https://comparador-facturas.vercel.app`

Esa URL la compartes con todo el equipo.

---

## Columnas requeridas

### Archivo DIAN
| Columna | Descripción |
|---|---|
| `Folio` | Número de la factura |
| `Prefijo` | Prefijo (BOG, FE, FEMQ...) |
| `NIT Emisor` | NIT del proveedor |
| `Nombre Emisor` | Nombre del proveedor |

### Archivo Siesa
| Columna | Descripción |
|---|---|
| `Proveedor` | NIT del proveedor |
| `Docto. proveedor` | Número de factura (ej: FEMQ-00004465) |
| `Razón social proveedor` | Nombre del proveedor |

---

## Nota sobre Render plan gratuito

El plan gratuito de Render suspende el servidor después de 15 minutos de inactividad. La primera solicitud después de un período inactivo puede tardar 30-60 segundos en responder. Esto es normal.
