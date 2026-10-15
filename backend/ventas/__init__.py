"""
FactuMatch Ventas — conciliación de documentos emitidos (DIAN) contra Odoo.

Módulo aislado del comparador de compras: tiene su propio lector del
contenedor DIAN, su extracción de Odoo en solo lectura, su motor de
conciliación (individual y global), su reporte y su agente investigador.

Reglas de negocio y decisiones documentadas en docs/VENTAS.md.
"""

__all__ = ["config"]
