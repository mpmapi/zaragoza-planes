"""
Paso 1 de la tarde: mira cómo vienen los datos reales antes de tocar el servidor.
Ejecuta:  python explorar_api.py
Guarda una muestra en muestra_eventos.json para que la abras y la revises.
"""

import json
from datetime import date

from server import _resumen, _sesiones, fetch_eventos

hoy = date.today()
eventos = fetch_eventos(hoy)
print(f"Eventos con fechas que incluyen hoy ({hoy}): {len(eventos)}\n")

if eventos:
    print("Campos del primer evento:")
    print(sorted(eventos[0].keys()), "\n")
    print("Así lo ve el agente:")
    de_hoy = [(ev, s) for ev in eventos if (s := _sesiones(ev, hoy, hoy))]
    print(f"Con sesión real hoy: {len(de_hoy)}\n")
    for ev, sesiones in de_hoy[:5]:
        print(json.dumps(_resumen(ev, sesiones), ensure_ascii=False, indent=2))

with open("muestra_eventos.json", "w", encoding="utf-8") as f:
    json.dump(eventos[:20], f, ensure_ascii=False, indent=2)
print("\nMuestra (20 eventos) guardada en muestra_eventos.json")
