"""
Servidor MCP "Zaragoza Planes"
Conecta un agente de IA con los datos abiertos del Ayuntamiento de Zaragoza.

Ejecutar en local:   python server.py
Probar:              npx @modelcontextprotocol/inspector python server.py
"""

import html
import json
import math
import re
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

API_EVENTOS = "https://www.zaragoza.es/sede/servicio/actividades/evento/list.json"
URL_FICHA_EVENTO = "https://www.zaragoza.es/sede/servicio/cultura/evento/{id}"
FILAS_POR_PAGINA = 500        # máximo que admite la API de la agenda
CACHE_EVENTOS_SEG = 15 * 60   # la agenda cambia poco: 15 min de caché

mcp = MCPServer(
    name="zaragoza-planes",
    instructions=(
        "Herramientas para descubrir planes en Zaragoza a partir de los datos "
        "abiertos del Ayuntamiento. Responde siempre en español y menciona el "
        "lugar, la hora y el precio cuando estén disponibles. Cada evento trae "
        "su ficha oficial en el campo url: ponla siempre como enlace en el "
        "título del evento, por ejemplo [Título](url), para que la persona "
        "pueda abrirla. No digas que hay enlaces sin mostrarlos."
    ),
)


# ---------------------------------------------------------------------------
# Acceso a la API
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """Usa los certificados de certifi si está instalado.

    El Python de python.org en macOS no trae certificados raíz y falla con
    CERTIFICATE_VERIFY_FAILED; certifi lo soluciona sin desactivar la
    verificación.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


def _get_json(url: str, params: dict) -> dict:
    """Llama a la API y devuelve el JSON.

    Los fallos de red se convierten en ToolError: así el agente recibe un
    mensaje que entiende en vez de un genérico "Error executing tool".
    """
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        host = urllib.parse.urlparse(url).netloc
        raise ToolError(
            f"El servicio de datos abiertos ({host}) no responde bien ahora mismo "
            f"({exc.__class__.__name__}). Prueba de nuevo en unos minutos."
        ) from exc


_cache: dict = {}


def _cacheado(clave, ttl: float, cargar):
    """Caché en memoria muy simple: evita repetir descargas lentas."""
    ahora = time.monotonic()
    if clave in _cache and ahora - _cache[clave][0] < ttl:
        return _cache[clave][1]
    valor = cargar()
    _cache[clave] = (ahora, valor)
    return valor


def _descargar_eventos(params: dict) -> list[dict]:
    """Descarga todas las páginas de una consulta a la agenda (en paralelo)."""
    # Sin un orden fijo la API repite eventos entre páginas y se salta otros
    # (comprobado: ~680 de 2967 perdidos). Ordenar por id lo evita.
    base = {"terminadas": "false", "srsname": "wgs84", "rows": FILAS_POR_PAGINA,
            "sort": "id asc", **params}
    primera = _get_json(API_EVENTOS, {**base, "start": 0})
    total = primera.get("totalCount", 0)
    paginas = [primera]
    inicios = range(FILAS_POR_PAGINA, total, FILAS_POR_PAGINA)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paginas += pool.map(lambda s: _get_json(API_EVENTOS, {**base, "start": s}), inicios)

    # La paginación no es atómica: quitamos posibles duplicados entre páginas.
    eventos, vistos = [], set()
    for pagina in paginas:
        for ev in pagina.get("result") or []:
            if ev.get("id") not in vistos:
                vistos.add(ev.get("id"))
                eventos.append(ev)
    return eventos


def fetch_eventos(desde: date, hasta: date | None = None) -> list[dict]:
    """Eventos vigentes cuyas fechas se solapan con [desde, hasta].

    El filtro de la API es por fechas globales del evento; se amplía un día
    por cada lado para no perder nada por zonas horarias, y el filtrado fino
    por sesión se hace después con _sesiones().
    """
    hasta = hasta or desde
    q = (f"startDate=le={(hasta + timedelta(days=1)).isoformat()}T23:59:59Z;"
         f"endDate=ge={(desde - timedelta(days=1)).isoformat()}T00:00:00Z")
    return _cacheado(("eventos", desde, hasta), CACHE_EVENTOS_SEG,
                     lambda: _descargar_eventos({"q": q}))


# ---------------------------------------------------------------------------
# Utilidades para limpiar los datos (los datos públicos vienen "con sorpresas")
# ---------------------------------------------------------------------------

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


def _norm(texto) -> str:
    """Minúsculas, sin tildes ni guiones blandos: para comparar textos."""
    t = unicodedata.normalize("NFKD", str(texto or "")).replace("\xad", "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def _limpiar_html(valor) -> str:
    """Las descripciones vienen en HTML con entidades (&oacute;...)."""
    texto = re.sub(r"<[^>]+>", " ", str(valor or ""))
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def _parse_fecha(valor) -> date | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor)[:10]).date()
    except ValueError:
        return None


def _texto(valor) -> str:
    """Convierte str / dict / lista en texto legible."""
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor.strip()
    if isinstance(valor, dict):
        return (valor.get("title") or valor.get("name") or "").strip()
    if isinstance(valor, list):
        return ", ".join(filter(None, (_texto(v) for v in valor)))
    return str(valor)


def _sesiones(evento: dict, desde: date, hasta: date) -> list[tuple[dict, dict | None]]:
    """Sesiones (subEvent, horario) del evento que caen dentro de [desde, hasta].

    Un evento puede tener varias subEvent (fechas o lugares distintos) y cada
    una varios horarios por día de la semana en openingHours, p. ej. un curso
    de septiembre a diciembre que solo es los martes.
    """
    subeventos = evento.get("subEvent") or [{}]   # sin subEvent: fechas globales
    resultado = []
    for sub in subeventos:
        ini = _parse_fecha(sub.get("startDate")) or _parse_fecha(evento.get("startDate"))
        fin = _parse_fecha(sub.get("endDate")) or _parse_fecha(evento.get("endDate")) or ini
        if not ini:
            continue
        a, b = max(ini, desde), min(fin, hasta)
        if a > b:
            continue
        dias_en_rango = {DIAS[(a + timedelta(days=d)).weekday()]
                         for d in range(min((b - a).days + 1, 7))}
        horarios = [h for h in sub.get("openingHours") or [] if isinstance(h, dict)]
        validos = [h for h in horarios
                   if not h.get("dayOfWeek") or _norm(h["dayOfWeek"]) in dias_en_rango]
        if horarios and not validos:
            continue  # ese día de la semana no hay sesión
        resultado.extend((sub, h) for h in (validos or [None]))
    return resultado


def _es_puntual(sesiones) -> bool:
    """True si alguna sesión es de un solo día (concierto, charla...), no un curso largo."""
    return any(sub.get("startDate") and sub.get("startDate") == sub.get("endDate")
               for sub, _ in sesiones)


def _hora(horario: dict | None) -> str:
    if not horario or not horario.get("startTime"):
        return ""
    if horario.get("endTime"):
        return f"{horario['startTime']}-{horario['endTime']}"
    return horario["startTime"]


def _orden_sesion(sesion: tuple[dict, dict | None]):
    """Orden cronológico: fecha, día de la semana y hora."""
    sub, horario = sesion
    dia = _norm((horario or {}).get("dayOfWeek"))
    return (str(sub.get("startDate") or ""), DIAS.index(dia) if dia in DIAS else 7, _hora(horario))


def _describir_sesion(sub: dict, horario: dict | None) -> str:
    ini, fin = _parse_fecha(sub.get("startDate")), _parse_fecha(sub.get("endDate"))
    hora = _hora(horario)
    if ini and ini == fin:
        dia = DIAS[ini.weekday()].replace("miercoles", "miércoles").replace("sabado", "sábado")
        return f"{dia} {ini:%d/%m} {hora}".strip()
    if horario and horario.get("dayOfWeek"):
        return f"{horario['dayOfWeek'].lower()} {hora}".strip()
    return hora


def _lugar_sesion(evento: dict, sub: dict) -> dict:
    loc = sub.get("location") if isinstance(sub.get("location"), dict) else {}
    return {
        "lugar": _texto(loc) or _texto(evento.get("location")),
        "direccion": loc.get("streetAddress") or _limpiar_html(sub.get("comment")),
        "barrio": _barrio(loc.get("junta")),
    }


def _tarifas(evento: dict) -> list[dict]:
    precio = evento.get("price")
    return [p for p in precio if isinstance(p, dict)] if isinstance(precio, list) else []


def _es_gratis(evento: dict) -> bool:
    """Gratis solo si consta como tal. price=null significa "no indicado", no gratis."""
    tarifas = _tarifas(evento)
    return bool(tarifas) and all(
        p.get("hasCurrencyValue") in (0, 0.0) or "gratu" in _norm(p.get("fareGroup"))
        for p in tarifas
    )


def _precio(evento: dict) -> str:
    tarifas = _tarifas(evento)
    if not tarifas:
        return "No indicado"
    if _es_gratis(evento):
        return "Gratis"
    partes = []
    for p in tarifas:
        valor = p.get("hasCurrencyValue")
        importe = f"{valor:g} €".replace(".", ",") if isinstance(valor, (int, float)) else ""
        partes.append(((p.get("fareGroup") or "").strip(), importe))
    if len({importe for _, importe in partes}) == 1:   # "Taquilla 5 €, Anticipada 5 €" -> "5 €"
        return partes[0][1]
    return ", ".join(f"{grupo} {importe}".strip() for grupo, importe in partes)


def _categoria(evento: dict) -> str:
    return _texto(evento.get("category")) or _texto(evento.get("type"))


def _resumen(evento: dict, sesiones: list[tuple[dict, dict | None]] | None = None,
             lugar_de: dict | None = None) -> dict:
    """Lo que ve el agente: qué, dónde, cuándo y cuánto cuesta.

    lugar_de: sesión cuyo lugar se muestra (por defecto, la primera).
    """
    if sesiones is None:   # sin rango de fechas: todas las sesiones del evento
        sesiones = [(sub, h) for sub in evento.get("subEvent") or [{}]
                    for h in (sub.get("openingHours") or [None])]
    sesiones = sorted(sesiones, key=_orden_sesion)
    sub0 = sesiones[0][0] if sesiones else {}
    horarios =list(dict.fromkeys(filter(None, (_describir_sesion(s, h) for s, h in sesiones))))
    return {
        "titulo": _texto(evento.get("title")) or "Sin título",
        "categoria": _categoria(evento),
        **_lugar_sesion(evento, lugar_de or sub0),
        "inicio": str(evento.get("startDate", ""))[:10],
        "fin": str(evento.get("endDate", ""))[:10],
        "horario": "; ".join(horarios[:4]) + (" …" if len(horarios) > 4 else ""),
        "precio": _precio(evento),
        "dirigido_a": _texto(evento.get("population")),
        "url": URL_FICHA_EVENTO.format(id=evento.get("id", "")),
    }


def _pajar(evento: dict, sesiones) -> str:
    """Todo el texto donde buscar una palabra clave, normalizado."""
    partes = [_texto(evento.get("title")), _categoria(evento), _texto(evento.get("type")),
              _texto(evento.get("population")), _texto(evento.get("program")),
              _limpiar_html(evento.get("description"))]
    for sub, _ in sesiones:
        partes += list(_lugar_sesion(evento, sub).values())
    return _norm(" ".join(partes))


def _resolver_fechas(cuando: str) -> tuple[date, date]:
    """Traduce "hoy", "mañana", "finde", "jueves" o AAAA-MM-DD a un rango de días."""
    hoy = date.today()
    c = _norm(cuando or "hoy")
    if c == "hoy":
        return hoy, hoy
    if c in ("manana", "pasado manana"):
        d = hoy + timedelta(days=1 if c == "manana" else 2)
        return d, d
    if c in ("finde", "fin de semana", "este finde", "este fin de semana"):
        if hoy.weekday() == 6:          # domingo: lo que queda del finde es hoy
            return hoy, hoy
        sabado = hoy + timedelta(days=(5 - hoy.weekday()) % 7)
        return sabado, sabado + timedelta(days=1)
    if c in DIAS:                       # próximo día con ese nombre (hoy incluido)
        d = hoy + timedelta(days=(DIAS.index(c) - hoy.weekday()) % 7)
        return d, d
    parsed = _parse_fecha(c)
    if parsed:
        return parsed, parsed
    raise ToolError(
        f"No entiendo la fecha '{cuando}'. Usa 'hoy', 'mañana', 'finde', "
        "un día de la semana ('jueves') o una fecha AAAA-MM-DD."
    )




# ---------------------------------------------------------------------------
# Ubicaciones: convertir un "lugar" en coordenadas sin servicios externos.
# El diccionario de sitios sale de los propios datos abiertos: barrios,
# lugares de la agenda y estaciones Bizi.
# ---------------------------------------------------------------------------

GBFS_BIZI = "https://zaragoza.publicbikesystem.net/customer/gbfs/v3.0/"
CACHE_SITIOS_SEG = 24 * 3600   # lugares y estaciones casi no cambian

_ABREVIATURAS = {
    "pza": "plaza", "pl": "plaza", "avda": "avenida", "av": "avenida",
    "po": "paseo", "pte": "puente", "parq": "parque", "c": "calle",
    "cno": "camino", "s": "san", "sta": "santa", "sto": "santo",
}
_PALABRAS_VACIAS = {"de", "del", "la", "el", "los", "las", "y", "en", "a", "al"}


def _palabras(texto) -> set[str]:
    """Palabras significativas, sin tildes y con abreviaturas expandidas
    ("Pza. España" y "Plaza de España" dan lo mismo)."""
    tokens = re.findall(r"[a-z0-9]+", _norm(texto))
    return {_ABREVIATURAS.get(t, t) for t in tokens} - _PALABRAS_VACIAS


def _barrio(junta) -> str:
    """'Junta Municipal Actur - Rey Fernando' -> 'Actur - Rey Fernando'."""
    nombre = re.sub(r"^(junta\s+(municipal|vecinal)|distrito)\s+", "",
                    str(junta or "").strip(), flags=re.I).strip()
    return nombre.title() if nombre.isupper() else nombre


def _clave_barrio(junta) -> str:
    """Une variantes del mismo barrio ('Actur-Rey Fernando', 'DISTRITO SUR'...)."""
    return " ".join(sorted(_palabras(_barrio(junta))))


def _coords(evento: dict, sub: dict) -> tuple[float, float] | None:
    """(lat, lon) de una sesión: la de su lugar o, si no, la del evento."""
    loc = sub.get("location") if isinstance(sub.get("location"), dict) else {}
    for geo in (loc.get("geometry"), evento.get("geometry")):
        c = (geo or {}).get("coordinates") if isinstance(geo, dict) else None
        if isinstance(c, list) and len(c) == 2:
            return float(c[1]), float(c[0])   # GeoJSON viene como [lon, lat]
    return None


def _distancia_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distancia en línea recta (haversine), en metros."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _texto_gbfs(valor) -> str:
    """GBFS 3.0 da los textos como [{"text":..,"language":..}]; nos quedamos con el español."""
    if isinstance(valor, list):
        textos = {v.get("language"): v.get("text") for v in valor if isinstance(v, dict)}
        valor = textos.get("es") or next(iter(textos.values()), "")
    return str(valor or "").replace("\xad", "").strip()


def _sin_numero(nombre: str) -> str:
    """'16-Pza. España' -> 'Pza. España' (el número no le dice nada a nadie)."""
    return re.sub(r"^\d+\s*-\s*", "", nombre).strip()


def estaciones_bizi() -> list[dict]:
    """Datos fijos de las estaciones Bizi (nombre, dirección, coordenadas)."""
    def cargar():
        data = _get_json(GBFS_BIZI + "station_information", {})
        return [{
            "id": s.get("station_id"),
            "nombre": _sin_numero(_texto_gbfs(s.get("name"))),
            "direccion": _sin_numero(_texto_gbfs(s.get("address"))),
            "lat": float(s["lat"]),
            "lon": float(s["lon"]),
            "capacidad": s.get("capacity"),
        } for s in data["data"]["stations"] if s.get("lat") is not None]
    return _cacheado("bizi_info", CACHE_SITIOS_SEG, cargar)


def _eventos_toda_la_agenda() -> list[dict]:
    """Toda la agenda vigente (lento, ~10 s): solo para ampliar el diccionario de sitios."""
    return _cacheado("agenda_completa", CACHE_SITIOS_SEG, lambda: _descargar_eventos({}))


def _sitios(eventos: list[dict]) -> list[dict]:
    """Diccionario de sitios reconocibles: barrios, lugares de la agenda y estaciones Bizi."""
    lugares = {}
    for ev in eventos:
        for sub in ev.get("subEvent") or []:
            loc = sub.get("location") if isinstance(sub.get("location"), dict) else {}
            if loc.get("geometry") and loc.get("title"):
                lat, lon = _coords(ev, sub)
                lugares[loc.get("id") or loc["title"]] = {
                    "nombre": loc["title"].strip(), "tipo": "lugar de la agenda",
                    "detalle": loc.get("streetAddress") or "", "junta": loc.get("junta"),
                    "lat": lat, "lon": lon,
                }

    # Barrio = centro de los lugares de la agenda de esa junta.
    barrios: dict[str, list] = {}
    for l in lugares.values():
        if _clave_barrio(l["junta"]):
            barrios.setdefault(_clave_barrio(l["junta"]), []).append(l)
    sitios = [{
        "nombre": _barrio(ls[0]["junta"]), "tipo": "barrio", "detalle": "",
        "barrio": clave,
        "lat": sum(l["lat"] for l in ls) / len(ls),
        "lon": sum(l["lon"] for l in ls) / len(ls),
    } for clave, ls in barrios.items()]

    sitios += lugares.values()
    sitios += [{"nombre": e["nombre"], "tipo": "estación Bizi", "detalle": e["direccion"],
                "lat": e["lat"], "lon": e["lon"]} for e in estaciones_bizi()]
    return sitios


def _buscar_sitio(buscadas: set[str], sitios: list[dict]) -> list[dict]:
    """Sitios que contienen todas las palabras buscadas, del más al menos probable."""
    puntuados = []
    for s in sitios:
        en_nombre = _palabras(s["nombre"])
        if buscadas <= en_nombre:
            en_direccion = False
        elif buscadas <= en_nombre | _palabras(s["detalle"]):
            en_direccion = True
        else:
            continue
        # Mejor si coincide el nombre (no la dirección), si es un barrio
        # y si al nombre le sobran pocas palabras ("El Pilar" antes que
        # "Centro Cívico Valdefierro Pilar Adiego").
        puntuados.append(((en_direccion, s["tipo"] != "barrio", len(en_nombre - buscadas)), s))
    puntuados.sort(key=lambda p: p[0])
    return [s for _, s in puntuados]


def _resolver_lugar(lugar: str, eventos: list[dict]) -> dict:
    """Convierte el texto de la persona en un punto de referencia."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", lugar or "")
    if m:
        lat, lon = float(m[1]), float(m[2])
        return {"nombre": f"{lat:.5f}, {lon:.5f}", "tipo": "coordenadas", "lat": lat, "lon": lon}

    buscadas = _palabras(lugar)
    if not buscadas:
        raise ToolError("Indica un sitio: un barrio, un lugar conocido o coordenadas 'lat,lon'.")

    # Primero con los lugares de los eventos de esas fechas (ya descargados);
    # si no aparece, con toda la agenda.
    for cargar in (lambda: eventos, _eventos_toda_la_agenda):
        try:
            coincidencias = _buscar_sitio(buscadas, _sitios(cargar()))
        except ToolError:
            continue   # si la agenda completa no carga, mejor "no encuentro" que "API caída"
        if coincidencias:
            elegido = dict(coincidencias[0])
            elegido["otras"] = list(dict.fromkeys(
                f"{s['nombre']} ({s['tipo']})" for s in coincidencias[1:]
                if s["nombre"] != elegido["nombre"]
            ))[:3]
            return elegido

    raise ToolError(
        f"No encuentro '{lugar}' entre los barrios, lugares de la agenda y "
        "estaciones Bizi. Prueba con un barrio (Delicias, Actur, Casco Histórico...), "
        "un lugar conocido (Plaza España, Aljafería, Teatro Principal...) o "
        "coordenadas 'lat,lon'."
    )


# ---------------------------------------------------------------------------
# Filtrado común a las herramientas de eventos
# ---------------------------------------------------------------------------

def _candidatos(eventos, desde: date, hasta: date, texto: str, solo_gratis: bool):
    """(evento, sesiones) que tienen sesión en esas fechas y pasan los filtros."""
    palabras = _norm(texto).split()
    for ev in eventos:
        sesiones = _sesiones(ev, desde, hasta)
        if not sesiones:
            continue
        if solo_gratis and not _es_gratis(ev):
            continue
        if palabras:
            pajar = _pajar(ev, sesiones)
            if not all(p in pajar for p in palabras):
                continue
        yield ev, sesiones


def _primera_hora(sesiones) -> str:
    return min((_hora(h) for _, h in sesiones if _hora(h)), default="99:99")


# ---------------------------------------------------------------------------
# Herramientas MCP
# La descripción (docstring) y las de cada parámetro (Field) son lo que lee
# el agente para decidir cuándo y cómo usar cada herramienta: aquí está el
# diseño conversacional.
# ---------------------------------------------------------------------------

Cuando = Annotated[str, Field(
    description='"hoy", "mañana", "finde", un día de la semana ("jueves") o una fecha AAAA-MM-DD.'
)]
Texto = Annotated[str, Field(
    description='Palabra clave opcional para filtrar por título, categoría, descripción, '
                'lugar o barrio (p. ej. "concierto", "exposición", "Harinera", "niños").'
)]
SoloGratis = Annotated[bool, Field(
    description="True para devolver solo actividades marcadas como gratuitas "
                "(se excluyen las que no indican precio)."
)]
MaxResultados = Annotated[int, Field(ge=1, le=25, description="Número máximo de eventos a devolver (1-25).")]


@mcp.tool()
def buscar_eventos(
    cuando: Cuando = "hoy",
    texto: Texto = "",
    solo_gratis: SoloGratis = False,
    max_resultados: MaxResultados = 10,
) -> list[dict]:
    """Busca planes y actividades en la agenda municipal de Zaragoza.

    Úsala cuando la persona pregunte qué hacer, qué planes hay, conciertos,
    exposiciones, teatro, actividades infantiles, etc. en Zaragoza.

    Primero devuelve las actividades puntuales de ese día (conciertos,
    charlas, funciones) y después las de larga duración (cursos, exposiciones).

    Cada evento incluye url, su ficha oficial: al mencionarlo, enlázala en el
    título, por ejemplo [Título](url).
    """
    desde, hasta = _resolver_fechas(cuando)
    candidatos = list(_candidatos(fetch_eventos(desde, hasta), desde, hasta, texto, solo_gratis))
    # Puntuales primero, y dentro de cada grupo por hora de inicio.
    candidatos.sort(key=lambda c: (not _es_puntual(c[1]), _primera_hora(c[1])))
    return [_resumen(ev, ses) for ev, ses in candidatos[:max_resultados]]


@mcp.tool()
def eventos_cerca_de(
    lugar: Annotated[str, Field(
        description='Barrio, lugar conocido o coordenadas ("41.65,-0.88").'
    )],
    cuando: Cuando = "hoy",
    radio_km: Annotated[float, Field(
        ge=0.1, le=10,
        description="Distancia máxima en km (por defecto 1, unos 15 min andando).",
    )] = 1.0,
    texto: Texto = "",
    solo_gratis: SoloGratis = False,
    max_resultados: MaxResultados = 10,
) -> dict:
    """Busca planes de la agenda municipal de Zaragoza cerca de un sitio concreto.

    Úsala cuando la persona diga dónde está o adónde quiere ir: "cerca de Plaza
    España", "algo por el Actur", "estoy en la Aljafería, ¿qué hay cerca?". Si
    solo pregunta qué hacer, sin mencionar un sitio, usa buscar_eventos.

    Reconoce barrios (Delicias, Actur, Casco Histórico...), lugares de la agenda
    (centros cívicos, teatros, museos), nombres de estaciones Bizi y coordenadas
    "lat,lon". No entiende direcciones postales cualesquiera. La respuesta indica
    qué punto de referencia se ha usado: si no es el que la persona quería, pídele
    un sitio más concreto.

    Si el lugar es un barrio, devuelve los eventos de ese barrio. Si es un sitio
    concreto, devuelve los que están dentro del radio, con la distancia en metros
    en línea recta. Primero van los planes puntuales y luego los de larga
    duración, cada grupo ordenado por cercanía. Los eventos sin ubicación no
    aparecen.

    Cada evento incluye url, su ficha oficial: al mencionarlo, enlázala en el
    título, por ejemplo [Título](url).
    """
    desde, hasta = _resolver_fechas(cuando)
    eventos = fetch_eventos(desde, hasta)
    ref = _resolver_lugar(lugar, eventos)
    punto = (ref["lat"], ref["lon"])
    barrio = ref.get("barrio")

    encontrados = []
    for ev, sesiones in _candidatos(eventos, desde, hasta, texto, solo_gratis):
        mas_cerca = None   # (distancia, sesión) de la sesión más cercana
        for sesion in sesiones:
            sub = sesion[0]
            coords = _coords(ev, sub)
            if not coords:
                continue
            if barrio:
                loc = sub.get("location") if isinstance(sub.get("location"), dict) else {}
                if _clave_barrio(loc.get("junta")) != barrio:
                    continue
            d = _distancia_m(punto, coords)
            if not barrio and d > radio_km * 1000:
                continue
            if mas_cerca is None or d < mas_cerca[0]:
                mas_cerca = (d, sesion)
        if mas_cerca:
            encontrados.append((ev, sesiones, mas_cerca))

    if barrio:   # en un barrio la distancia al "centro" no aporta: orden por hora
        encontrados.sort(key=lambda e: (not _es_puntual(e[1]), _primera_hora(e[1])))
    else:
        encontrados.sort(key=lambda e: (not _es_puntual(e[1]), e[2][0]))

    resultado_eventos = []
    for ev, sesiones, (d, sesion) in encontrados[:max_resultados]:
        resumen = _resumen(ev, sesiones, lugar_de=sesion[0])
        if not barrio:
            resumen["distancia_m"] = int(round(d, -1))
        resultado_eventos.append(resumen)

    respuesta = {
        "referencia": {
            "nombre": ref["nombre"], "tipo": ref["tipo"],
            "lat": round(ref["lat"], 5), "lon": round(ref["lon"], 5),
        },
        "otras_coincidencias": ref.get("otras", []),
        "eventos": resultado_eventos,
    }
    if not resultado_eventos:
        respuesta["nota"] = (
            "No hay eventos en ese barrio para esas fechas." if barrio else
            f"No hay eventos a menos de {radio_km:g} km para esas fechas; "
            "se puede ampliar radio_km."
        )
    return respuesta


def _estado_estaciones() -> dict[str, dict]:
    """Estado en vivo de cada estación. Caché de 30 s: el feed cambia cada minuto."""
    def cargar():
        data = _get_json(GBFS_BIZI + "station_status", {})
        return {s["station_id"]: s for s in data["data"]["stations"]}
    return _cacheado("bizi_estado", 30, cargar)


def _minutos_desde(marca) -> int | None:
    try:
        momento = datetime.fromisoformat(str(marca).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(momento.tzinfo) - momento).total_seconds() // 60))


@mcp.tool()
def estado_bizi(
    zona: Annotated[str, Field(
        description='Barrio, lugar conocido, estación Bizi o coordenadas ("41.65,-0.88").'
    )],
    para: Annotated[Literal["coger", "dejar", "cualquiera"], Field(
        description='"coger" (solo estaciones con bicis), "dejar" (solo con anclajes '
                    'libres) o "cualquiera".'
    )] = "cualquiera",
    max_estaciones: Annotated[int, Field(
        ge=1, le=10, description="Número de estaciones a devolver (1-10)."
    )] = 3,
) -> dict:
    """Consulta en tiempo real las estaciones Bizi (la bici pública de Zaragoza) más
    cercanas a un sitio: cuántas bicis hay y cuántos anclajes libres.

    Úsala cuando la persona quiera moverse en bici, pregunte si hay Bizis cerca o
    dónde dejar la bici. También sirve para ver cómo llegar a un plan encontrado con
    buscar_eventos o eventos_cerca_de.

    Reconoce los mismos sitios que eventos_cerca_de: barrios, lugares de la agenda,
    estaciones Bizi y coordenadas "lat,lon". Para un barrio se toma como referencia
    su centro. La respuesta indica qué punto se ha usado.

    Devuelve las estaciones ordenadas por cercanía, con la distancia en metros en
    línea recta, y deja fuera las que están fuera de servicio. Los datos son del
    momento de la consulta: dilo así ("ahora mismo hay..."), porque pueden cambiar
    antes de que la persona llegue.
    """
    ref = _resolver_lugar(zona, fetch_eventos(date.today()))
    punto = (ref["lat"], ref["lon"])
    estados = _estado_estaciones()

    estaciones = []
    for est in estaciones_bizi():
        st = estados.get(est["id"])
        if not st or not st.get("is_installed") or not (st.get("is_renting") or st.get("is_returning")):
            continue   # fuera de servicio
        bicis = st.get("num_vehicles_available", 0) if st.get("is_renting") else 0
        anclajes = st.get("num_docks_available", 0) if st.get("is_returning") else 0
        if para == "coger" and bicis == 0:
            continue
        if para == "dejar" and anclajes == 0:
            continue
        estaciones.append({
            "nombre": est["nombre"],
            "direccion": est["direccion"],
            "distancia_m": int(round(_distancia_m(punto, (est["lat"], est["lon"])), -1)),
            "bicis_disponibles": bicis,
            "anclajes_libres": anclajes,
            "actualizado_hace_min": _minutos_desde(st.get("last_reported")),
        })

    estaciones.sort(key=lambda e: e["distancia_m"])
    respuesta = {
        "referencia": {
            "nombre": ref["nombre"], "tipo": ref["tipo"],
            "lat": round(ref["lat"], 5), "lon": round(ref["lon"], 5),
        },
        "otras_coincidencias": ref.get("otras", []),
        "estaciones": estaciones[:max_estaciones],
    }
    if not estaciones:
        respuesta["nota"] = "Ahora mismo no hay ninguna estación Bizi que cumpla eso."
    return respuesta


if __name__ == "__main__":
    mcp.run()  # transporte stdio, el que usa Claude Desktop
