"""
Agente investigador de FactuMatch Ventas (Claude, API oficial de Anthropic).

- herramientas.py: herramientas de SOLO LECTURA sobre el resultado determinístico
  y, si hay credenciales, consultas acotadas a Odoo por OdooSoloLectura.
- prompts.py: instrucciones estáticas (cacheables) y contexto inicial minimizado.
- esquema.py: esquema de la respuesta estructurada y validación anti-invención.
- proveedor.py: bucle de herramientas con límites de llamadas y tiempo, manejo
  de errores y respaldo «Análisis IA no disponible».

El agente nunca escribe en Odoo y su caída no afecta la conciliación.
"""

from .proveedor import investigar  # noqa: F401
