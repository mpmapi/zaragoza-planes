# Zaragoza Planes · Servidor MCP

Servidor MCP que conecta un agente de IA con los **datos abiertos del Ayuntamiento de Zaragoza**, para que puedas preguntarle en lenguaje natural *"¿qué plan hay hoy?"* y te responda con eventos reales.

> 🎬 _Aquí irá el GIF de la demo_

## Herramientas

| Herramienta | Qué hace |
|---|---|
| `buscar_eventos` | Busca actividades de la agenda municipal por fecha, palabra clave y precio |
| `eventos_cerca_de` | Actividades cerca de un barrio, lugar conocido, estación Bizi o coordenadas |
| `estado_bizi` | Bicis y anclajes libres, en tiempo real, en las estaciones Bizi cercanas |

## Instalación

Necesitas Python 3.10 o superior.

```bash
python -m venv .venv
source .venv/bin/activate        # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> `certifi` aporta los certificados raíz. Sin él, el Python de python.org en macOS falla con `CERTIFICATE_VERIFY_FAILED` al llamar a la API.

## Probarlo

**1. Ver cómo vienen los datos reales**
```bash
python explorar_api.py
```

**2. Probar las herramientas con MCP Inspector** (necesita Node.js)
```bash
npx @modelcontextprotocol/inspector python server.py
```

**3. Conectarlo a Claude Desktop**

Abre *Ajustes → Desarrollador → Editar configuración* y añade:

```json
{
  "mcpServers": {
    "zaragoza-planes": {
      "command": "/RUTA/COMPLETA/.venv/bin/python",
      "args": ["/RUTA/COMPLETA/server.py"]
    }
  }
}
```

Reinicia Claude Desktop y pregúntale: *"¿Qué exposiciones gratis hay este finde en Zaragoza?"*

## Decisiones de diseño

- **Las descripciones de las herramientas son diseño conversacional:** son lo que el agente lee para decidir cuándo y cómo usarlas.
- **Datos limpios para el agente:** la API devuelve muchos campos; el servidor los reduce a lo que importa en una conversación (qué, dónde, cuándo, cuánto cuesta).
- **Fechas en lenguaje natural:** acepta "hoy", "mañana", "finde" o "jueves", como hablaría una persona.
- **Sin servicios externos para ubicar sitios:** "cerca de Plaza España" se resuelve con los propios datos abiertos (barrios, lugares de la agenda y estaciones Bizi). Lo que escribe la persona no sale hacia terceros.
- **Errores que el agente entiende:** los fallos previstos (fecha no válida, sitio desconocido, API caída) se devuelven con un mensaje que explica qué probar.

## Fuente de datos

- [Agenda de Zaragoza](https://www.zaragoza.es/sede/portal/datos-abiertos/servicio/catalogo/282) · Datos abiertos del Ayuntamiento de Zaragoza.
- [Bizi Zaragoza (GBFS 3.0)](https://zaragoza.publicbikesystem.net/customer/gbfs/v3.0/gbfs.json) · Estado de las estaciones en tiempo real (el antiguo dataset municipal de Bizi ya no devuelve datos).
