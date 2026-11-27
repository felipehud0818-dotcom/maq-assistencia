"""
Routes module - Backend CRUD for route planning + geocoding + Google Routes API proxy

Geolocalização (fonte única de verdade = backend):
- Endereço estruturado e normalizado (sem UF inventada), com hash (versão) do endereço.
- Cada OS guarda `geolocation` {status, lat, lng, precision, source, place_id,
  formatted_address, address_hash, reasons, verified_at, verified_by}.
- Coordenada só é usada se status == "approved" E address_hash == hash do endereço ATUAL.
  Coordenada antiga/solta (legado, endereço alterado, vinda do frontend) é invalidada.
- Geocodificação valida rua, número, bairro, CEP, cidade e UF, e classifica em
  approved / review / blocked. Só "approved" entra automaticamente no roteiro.
- Cache de geocode por hash completo (rua, nº, bairro, cidade, UF, CEP) + metadados do Google.
- Backend não confia em `precision` do frontend; pino manual exige source="manual_confirmed".
- Auditoria de cada chamada ao Google em `geocode_audit`.
- Cliente httpx compartilhado.
"""
import asyncio
import difflib
import hashlib
import logging
import math
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel, Field
from typing import List, Optional
import httpx
import jwt

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get('JWT_SECRET')
if not JWT_SECRET or JWT_SECRET in ('default_secret', 'change-me', ''):
    raise RuntimeError("JWT_SECRET ausente ou inseguro em backend/.env")

# Local copy of token verification to avoid circular imports with server.py.
async def verify_token(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("iss") == "mastermaq-tracking":
        raise HTTPException(status_code=401, detail="Invalid token")
    import security
    return await security.check_session(payload)


router = APIRouter(prefix="/routes", tags=["routes"], dependencies=[Depends(verify_token)])


class RouteOS(BaseModel):
    os_number: str
    customer_name: Optional[str] = None
    neighborhood: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    number: Optional[str] = None
    phone1: Optional[str] = None
    complement: Optional[str] = None
    technician: Optional[str] = None
    conclusion: Optional[str] = None
    observation: Optional[str] = None
    sequence: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    attendance_date: Optional[str] = None
    status: Optional[str] = None

class RouteCreate(BaseModel):
    name: Optional[str] = None
    technician: str
    date: str
    orders: List[RouteOS]
    total_distance: Optional[str] = None
    total_duration: Optional[str] = None
    status: str = "Pendente"

class RouteUpdate(BaseModel):
    name: Optional[str] = None
    technician: Optional[str] = None
    date: Optional[str] = None
    orders: Optional[List[RouteOS]] = None
    total_distance: Optional[str] = None
    total_duration: Optional[str] = None
    status: Optional[str] = None
    needs_recalculation: Optional[bool] = None


def get_db():
    from server import db
    return db


# ─── HTTP client compartilhado ─────────────────────────────────────────
_http_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


# ─── Normalização de endereço ──────────────────────────────────────────
UF_BY_NAME = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA", "ceara": "CE",
    "distrito federal": "DF", "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
    "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG", "para": "PA",
    "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
}
UFS = set(UF_BY_NAME.values())

_ABBR = {
    "r": "rua", "av": "avenida", "avda": "avenida", "al": "alameda", "tv": "travessa",
    "trav": "travessa", "pc": "praca", "pca": "praca", "rod": "rodovia", "estr": "estrada",
    "bc": "beco", "lg": "largo", "dr": "doutor", "dra": "doutora", "prof": "professor",
    "profa": "professora", "pe": "padre", "cel": "coronel", "gal": "general",
    "eng": "engenheiro", "cap": "capitao", "maj": "major", "sen": "senador",
    "dep": "deputado", "pres": "presidente", "sta": "santa", "sto": "santo",
    "vl": "vila", "jd": "jardim",
}
_STREET_TYPES = {"rua", "avenida", "alameda", "travessa", "praca", "rodovia", "estrada",
                 "beco", "largo", "viela", "via"}
_STOPWORDS = {"de", "da", "do", "dos", "das", "e"}


def _norm(s) -> str:
    """Minúsculas, sem acentos, sem pontuação, espaços colapsados."""
    txt = "".join(c for c in unicodedata.normalize("NFD", str(s or "").lower()) if unicodedata.category(c) != "Mn")
    txt = re.sub(r"[^\w\s]", " ", txt)
    return " ".join(txt.split())


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))

def _num(s) -> str:
    """
    Normaliza o número do imóvel preservando letras de identificação.
    Exemplos:
      00150  -> 150
      150A   -> 150a
      150-B  -> 150b
      150 A  -> 150a
    """
    raw = _norm(s).replace(" ", "")

    match = re.fullmatch(r"0*(\d+)[-]?([a-z]?)", raw)

    if match:
        numeric = match.group(1).lstrip("0") or "0"
        suffix = match.group(2)
        return numeric + suffix

    return raw

def _norm_uf(state) -> str:
    n = _norm(state)
    if n.upper() in UFS:
        return n.upper()
    return UF_BY_NAME.get(n, "")


def _norm_street(s) -> str:
    return " ".join(_ABBR.get(t, t) for t in _norm(s).split())


def _street_core(s) -> str:
    toks = _norm_street(s).split()
    if toks and toks[0] in _STREET_TYPES:
        toks = toks[1:]
    return " ".join(t for t in toks if t not in _STOPWORDS)


def _similar(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _address_parts(o: dict) -> dict:
    """Endereço estruturado. NUNCA inventa UF: usa a cadastrada (ou a de 'Cidade/UF')."""
    city = str(o.get("city") or "")
    state = o.get("state") or ""
    if "/" in city:
        city, _, uf = city.partition("/")
        if not state:
            state = uf
    return {
        "street": str(o.get("address") or "").strip(),
        "number": str(o.get("number") or "").strip(),
        "neighborhood": str(o.get("neighborhood") or "").strip(),
        "city": city.strip(),
        "state": _norm_uf(state),
        "cep": _digits(o.get("cep")),
    }


def _missing_fields(parts: dict) -> List[str]:
    missing = []
    if not parts["street"]:
        missing.append("logradouro")
    if not _num(parts["number"]):
        missing.append("numero")
    if not parts["city"]:
        missing.append("cidade")
    if not parts["state"]:
        missing.append("uf")
    return missing


def _address_hash(parts: dict) -> str:
    """Versão do endereço: muda se rua, número, bairro, cidade, UF ou CEP mudarem.
    Complemento/referência ficam de fora (não alteram a posição do imóvel)."""
    key = "|".join([
        _norm_street(parts["street"]), _norm(parts["number"]), _norm(parts["neighborhood"]),
        _norm(parts["city"]), parts["state"], parts["cep"],
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _os_address(o: dict) -> str:
    """Texto legível do endereço (apenas exibição/auditoria; não é chave de cache)."""
    p = _address_parts(o)
    cep = f"{p['cep'][:5]}-{p['cep'][5:]}" if len(p["cep"]) == 8 else p["cep"]
    street = f"{p['street']} {p['number']}".strip()
    city_uf = " - ".join(x for x in [p["city"], p["state"]] if x)
    return ", ".join(x for x in [street, p["neighborhood"], city_uf, cep] if x)


def _valid_br(lat, lng) -> bool:
    """Dentro do Brasil (lat -34..6, lng -74..-28) — pega lat/lng invertidos ou zerados."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    return -34.0 <= lat <= 6.0 and -74.5 <= lng <= -28.0


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Geolocalização validada da OS ─────────────────────────────────────
_OS_GEO_PROJECTION = {
    "_id": 0, "os_number": 1, "address": 1, "number": 1, "neighborhood": 1, "city": 1,
    "state": 1, "cep": 1, "complement": 1, "lat": 1, "lng": 1, "geolocation": 1,
}


def _validated_coords(o: dict):
    """(lat, lng) só se a geolocalização está aprovada E vinculada ao endereço ATUAL da OS."""
    geo = (o or {}).get("geolocation") or {}
    if geo.get("status") != "approved":
        return None
    if geo.get("address_hash") != _address_hash(_address_parts(o)):
        return None
    if not _valid_br(geo.get("lat"), geo.get("lng")):
        return None
    return float(geo["lat"]), float(geo["lng"])


def _geo_doc(result: dict, source: str, user_id: Optional[str] = None) -> dict:
    st = result["status"]
    ok = st == "approved"
    doc = {
        "status": st,
        "lat": result.get("lat") if ok else None,
        "lng": result.get("lng") if ok else None,
        "precision": result.get("precision"),
        "source": source,
        "place_id": result.get("place_id"),
        "formatted_address": result.get("formatted_address"),
        "address_hash": result.get("address_hash"),
        "reasons": result.get("reasons") or [],
        "verified_at": _now(),
        "verified_by": user_id,
        "accuracy_meters": None,  # só preencher com medição real (GPS do aparelho)
    }
    if st == "review" and result.get("lat") is not None:
        doc["candidate"] = {"lat": result["lat"], "lng": result["lng"]}
    return doc


async def _persist_geolocation(db, os_number: str, result: dict, source: str, user_id: Optional[str] = None):
    """Grava o resultado na OS. lat/lng de nível superior só espelham coordenada APROVADA."""
    if result["status"] == "error":
        return  # falha transitória: não sobrescreve nada
    ok = result["status"] == "approved"
    await db.service_orders.update_one(
        {"os_number": os_number},
        {"$set": {
            "lat": result["lat"] if ok else None,
            "lng": result["lng"] if ok else None,
            "geolocation": _geo_doc(result, source, user_id),
        }},
    )


async def _invalidate_os_geolocation(db, os_number: str, address_hash: str):
    """Coordenada antiga/sem vínculo com o endereço atual deixa de valer até nova validação."""
    await db.service_orders.update_one(
        {"os_number": os_number},
        {"$set": {
            "lat": None,
            "lng": None,
            "geolocation": {"status": "pending", "address_hash": address_hash, "lat": None, "lng": None,
                            "reasons": ["endereco_alterado_ou_coordenada_sem_validacao"],
                            "verified_at": None, "verified_by": None},
        }},
    )


async def invalidate_os_geolocation(db, os_number: str):
    """Para outros módulos: chame ao editar rua/número/bairro/cidade/UF/CEP de uma OS."""
    so = await db.service_orders.find_one({"os_number": str(os_number)}, _OS_GEO_PROJECTION)
    if so:
        await _invalidate_os_geolocation(db, so["os_number"], _address_hash(_address_parts(so)))



async def _sync_routes_coords(db, routes: list) -> list:
    """
    Sincroniza as coordenadas de todas as OSs de vários roteiros
    em uma única consulta ao banco.
    """
    nums = list({
        str(o.get("os_number"))
        for route in routes
        for o in (route.get("orders") or [])
        if o.get("os_number")
    })

    if not nums:
        return routes

    sos = await db.service_orders.find(
        {"os_number": {"$in": nums}},
        _OS_GEO_PROJECTION
    ).to_list(len(nums))

    by_num = {
        str(so.get("os_number")): so
        for so in sos
    }

    for route in routes:
        for order in route.get("orders") or []:
            so = by_num.get(str(order.get("os_number")))
            coords = _validated_coords(so) if so else None

            order["lat"], order["lng"] = (
                coords if coords else (None, None)
            )

            order["geo_status"] = (
                "approved"
                if coords
                else ((so or {}).get("geolocation") or {}).get(
                    "status", "pending"
                )
            )

    return routes


# ─── Cache de geocode ──────────────────────────────────────────────────
async def _get_cached_geocode(db, addr_hash: str):
    """Busca no cache pela chave completa do endereço (inclui CEP, bairro, cidade, UF)."""
    cached = await db.geocode_cache.find_one(
        {"address_hash": addr_hash},
        {"_id": 0, "lat": 1, "lng": 1, "source": 1, "precision": 1, "formatted_address": 1, "place_id": 1},
    )
    if cached and _valid_br(cached.get("lat"), cached.get("lng")):
        return cached
    return None


async def _store_geocode_cache(db, parts: dict, result: dict, source: str):
    """Guarda só resultado APROVADO, com metadados do que o Google realmente encontrou."""
    addr_hash = result["address_hash"]
    doc = {
        "address_hash": addr_hash,
        "address": _os_address({"address": parts["street"], "number": parts["number"],
                                "neighborhood": parts["neighborhood"], "city": parts["city"],
                                "state": parts["state"], "cep": parts["cep"]})[:500],
        "lat": result["lat"], "lng": result["lng"],
        "precision": result.get("precision"), "source": source,
        "formatted_address": (result.get("formatted_address") or "")[:500],
        "place_id": result.get("place_id"),
        "cached_at": _now(),
    }
    if await db.geocode_cache.find_one({"address_hash": addr_hash}, {"address_hash": 1}):
        await db.geocode_cache.update_one({"address_hash": addr_hash}, {"$set": doc})
    else:
        await db.geocode_cache.insert_one({"id": str(uuid.uuid4()), **doc})


async def _audit(db, o: dict, parts: dict, result: dict, source: str):
    """Registra cada tentativa para diagnosticar 'endereço errado' depois."""
    try:
        await db.geocode_audit.insert_one({
            "id": str(uuid.uuid4()),
            "os_number": o.get("os_number"),
            "input_address": _os_address(o),
            "address_hash": result.get("address_hash"),
            "requested_cep": parts.get("cep"),
            "returned_cep": (result.get("got") or {}).get("cep"),
            "requested_city": parts.get("city"),
            "returned_city": (result.get("got") or {}).get("city"),
            "returned_address": result.get("formatted_address"),
            "lat": result.get("lat"), "lng": result.get("lng"),
            "precision": result.get("precision"),
            "confidence": result.get("status"),
            "reasons": result.get("reasons") or [],
            "source": source,
            "created_at": _now(),
        })
    except Exception as e:
        logger.warning(f"audit geocode falhou: {e!r}")


def _is_own(technician: str, payload: dict) -> bool:
    """Admin/outros perfis: sempre. Técnico: só se o roteiro é dele (nome completo ou primeiro nome)."""
    if (payload or {}).get("role") != "technician":
        return True
    name = (payload.get("name") or "").strip().lower()
    rt = (technician or "").strip().lower()
    if not name or not rt:
        return False
    first = name.split()[0]
    return rt == name or rt == first or rt.startswith(first + " ")


async def _own_route(db, route_id: str, payload: dict) -> dict:
    route = await db.routes.find_one({"id": route_id}, {"_id": 0})
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    if not _is_own(route.get("technician"), payload):
        raise HTTPException(status_code=403, detail="Acesso restrito a roteiros do próprio técnico")
    return route

@router.get("/")
async def list_routes(payload: dict = Depends(verify_token)):
    db = get_db()

    routes = await db.routes.find(
        {},
        {"_id": 0}
    ).sort("created_at", -1).to_list(5000)

    # Mantém a restrição de acesso dos técnicos.
    routes = [
        r for r in routes
        if _is_own(r.get("technician"), payload)
    ]

    # Nunca devolver coordenadas antigas armazenadas no documento do roteiro.
    routes = await _sync_routes_coords(db, routes)

    return routes


@router.get("/{route_id}")
async def get_route_by_id(route_id: str, payload: dict = Depends(verify_token)):
    db = get_db()
    route = await _own_route(db, route_id, payload)
    # Complemento atual da OS é só exibição. Coordenadas vêm SEMPRE da geolocalização validada da OS.
    route["orders"] = await _sync_orders_coords(db, route.get("orders") or [], with_complement=True)
    return route


@router.post("/")
async def create_route(route_data: RouteCreate, payload: dict = Depends(verify_token)):
    db = get_db()
    if not _is_own(route_data.technician, payload):
        raise HTTPException(status_code=403, detail="Técnico só pode criar roteiro para si mesmo")
    route_dict = route_data.dict()
    route_dict["id"] = str(uuid.uuid4())
    route_dict["created_at"] = datetime.now(timezone.utc).isoformat()
    route_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
    route_dict["orders"] = [o.dict() if hasattr(o, 'dict') else o for o in route_dict["orders"]]
    # Ignora lat/lng vindos do frontend: usa só a coordenada validada no backend.
    route_dict["orders"] = await _sync_orders_coords(db, route_dict["orders"])
    await db.routes.insert_one(route_dict)
    route_dict.pop("_id", None)
    # Mensagem de agendamento automática no WhatsApp para cada OS do roteiro.
    from whatsapp_module import send_route_schedule
    asyncio.create_task(send_route_schedule(db, dict(route_dict)))
    return route_dict


@router.put("/{route_id}")
async def update_route(route_id: str, route_update: RouteUpdate, payload: dict = Depends(verify_token)):
    db = get_db()
    old_route = await _own_route(db, route_id, payload)
    if route_update.technician is not None and not _is_own(route_update.technician, payload):
        raise HTTPException(status_code=403, detail="Técnico não pode passar o roteiro para outro técnico")
    if route_update.orders is not None and len(route_update.orders) == 0:
        raise HTTPException(status_code=400, detail="Lista de OSs vazia — roteiro não alterado")
    update_data = {k: v for k, v in route_update.dict().items() if v is not None}
    if "orders" in update_data:
        update_data["orders"] = [o.dict() if hasattr(o, 'dict') else o for o in update_data["orders"]]
        update_data["orders"] = await _sync_orders_coords(db, update_data["orders"])
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = await db.routes.update_one({"id": route_id}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Route not found")
    route = await db.routes.find_one({"id": route_id}, {"_id": 0})
    if "orders" in update_data:
        # OS (re)incluída no roteiro volta a aparecer no mapa mesmo se antes foi removida lá.
        old = {str(o.get("os_number")) for o in (old_route or {}).get("orders") or []}
        added = [f"os-{o.get('os_number')}" for o in update_data["orders"] if str(o.get("os_number")) not in old]
        if added:
            from server import mongo_db
            await mongo_db["route_map_state"].update_one({"route_id": route_id}, {"$pull": {"removed": {"$in": added}}})
    # Roteiro concluído → todas as OSs dele viram "Visita Realizada".
    if update_data.get("status") == "Concluído":
        for o in route.get("orders") or []:
            if o.get("os_number"):
                await db.service_orders.update_one(
                    {"os_number": o["os_number"]},
                    {"$set": {"status": "Visita Realizada", "updated_at": update_data["updated_at"]}},
                )
    return route


@router.delete("/{route_id}")
async def delete_route(route_id: str, payload: dict = Depends(verify_token)):
    db = get_db()
    await _own_route(db, route_id, payload)
    result = await db.routes.delete_one({"id": route_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Route not found")
    return {"message": "Route deleted"}


@router.post("/{route_id}/duplicate")
async def duplicate_route(route_id: str, payload: dict = Depends(verify_token)):
    db = get_db()
    route = await _own_route(db, route_id, payload)
    route["id"] = str(uuid.uuid4())
    route["name"] = f"{route.get('name', 'Roteiro')} (Copia)"
    route["status"] = "Pendente"
    route["created_at"] = datetime.now(timezone.utc).isoformat()
    route["updated_at"] = datetime.now(timezone.utc).isoformat()
    route["orders"] = await _sync_orders_coords(db, route.get("orders") or [])
    await db.routes.insert_one(route)
    route.pop("_id", None)
    return route


@router.put("/{route_id}/conclusion/{os_number}")
async def update_os_conclusion(route_id: str, os_number: str, body: dict, payload: dict = Depends(verify_token)):
    db = get_db()
    conclusion = body.get("conclusion", "")
    observation = body.get("observation", None)
    route = await _own_route(db, route_id, payload)
    orders = route.get("orders", [])
    for order in orders:
        if order["os_number"] == os_number:
            order["conclusion"] = conclusion
            if observation is not None:
                order["observation"] = observation
            break
    await db.routes.update_one({"id": route_id}, {"$set": {"orders": orders, "updated_at": datetime.now(timezone.utc).isoformat()}})
    if conclusion:
        await db.service_orders.update_one(
            {"os_number": os_number},
            {"$set": {"technical_diagnosis": conclusion, "updated_at": datetime.now(timezone.utc).isoformat()}}
        )
    return {"message": "Updated"}


# === GEOCODING (validação + confiança) ===
class GeocodeRequest(BaseModel):
    os_numbers: List[str]

class CacheCoordsItem(BaseModel):
    os_number: str
    lat: float
    lng: float
    precision: Optional[str] = None  # ignorado: o backend não confia na precisão declarada pelo frontend
    source: Optional[str] = None     # "manual_confirmed" = usuário posicionou/confirmou o pino no mapa

class CacheCoordsRequest(BaseModel):
    coords: List[CacheCoordsItem]


MAX_GEOCODE_PER_CALL = 30   # chamadas pagas por requisição; o resto volta como "pending"
_FAIL_TTL = {"blocked": 1800, "review": 600}
_geo_fail: dict = {}        # address_hash -> (resultado, expira_em). Só falhas conclusivas, com TTL.


def _fail_get(addr_hash: str):
    entry = _geo_fail.get(addr_hash)
    if not entry:
        return None
    if entry[1] < time.time():
        _geo_fail.pop(addr_hash, None)
        return None
    return entry[0]


def _fail_set(addr_hash: str, result: dict):
    if len(_geo_fail) > 5000:
        now = time.time()
        for k in [k for k, v in _geo_fail.items() if v[1] < now]:
            _geo_fail.pop(k, None)
    _geo_fail[addr_hash] = (result, time.time() + _FAIL_TTL.get(result["status"], 600))


def _evaluate(parts: dict, res: dict) -> dict:
    """Compara o resultado do Google com o endereço pedido. Retorna approved/review/blocked."""
    comps = res.get("address_components") or []

    def comp(*types, short=False):
        for c in comps:
            if set(types) & set(c.get("types", [])):
                return c.get("short_name" if short else "long_name") or ""
        return ""

    geom = res.get("geometry") or {}
    loc = geom.get("location") or {}
    ltype = geom.get("location_type")
    got = {
        "street": comp("route"),
        "number": comp("street_number"),
        "neighborhood": comp("sublocality_level_1", "sublocality", "neighborhood"),
        "state": comp("administrative_area_level_1", short=True).upper(),
        "cep": _digits(comp("postal_code")),
    }
    cities = {_norm(c.get("long_name")) for c in comps
              if {"administrative_area_level_2", "locality"} & set(c.get("types", []))}
    got["city"] = comp("administrative_area_level_2", "locality")

    block, review = [], []

    if not _valid_br(loc.get("lat"), loc.get("lng")):
        block.append("coordenada_invalida")
    if _norm(parts["city"]) not in cities:
        block.append("cidade_divergente")
    if got["state"] != parts["state"]:
        block.append("uf_divergente")
    if ltype not in ("ROOFTOP", "RANGE_INTERPOLATED"):
        block.append("precisao_aproximada")

    if not got["number"]:
        block.append("numero_nao_identificado")
    elif _num(got["number"]) != _num(parts["number"]):
        block.append("numero_divergente")

    if not got["street"]:
        block.append("logradouro_nao_identificado")
    else:
        ratio = difflib.SequenceMatcher(None, _street_core(parts["street"]), _street_core(got["street"])).ratio()
        if ratio < 0.6:
            block.append("logradouro_divergente")
        elif ratio < 0.85:
            review.append("logradouro_parcial")

    
    # CEP / bairro
    cep_ok = False

    requested_cep = _digits(parts.get("cep"))
    returned_cep = _digits(got.get("cep"))

    if requested_cep:
        if not returned_cep:
            review.append("cep_nao_retornado")
        elif requested_cep == returned_cep:
            cep_ok = True
        else:
            review.append("cep_divergente")

    neigh_known = bool(
        parts["neighborhood"] and got["neighborhood"]
    )

    neigh_ok = (
        neigh_known
        and _similar(
            parts["neighborhood"],
            got["neighborhood"]
        )
    )

    if not cep_ok:
        if neigh_known and not neigh_ok:
            review.append("bairro_divergente")
        elif not neigh_ok and "cep_nao_retornado" not in review:
            review.append("sem_confirmacao_cep_ou_bairro")

    if neigh_known and not neigh_ok and "bairro_divergente" not in review:
        review.append("bairro_divergente")


    if res.get("partial_match"):
        review.append("correspondencia_parcial")
    if ltype == "RANGE_INTERPOLATED" and not cep_ok:
        review.append("numero_interpolado_sem_cep")

    status = "blocked" if block else ("review" if review else "approved")
    return {
        "status": status,
        "lat": loc.get("lat"), "lng": loc.get("lng"),
        "precision": ltype,
        "formatted_address": res.get("formatted_address"),
        "place_id": res.get("place_id"),
        "reasons": block + review,
        "got": got,
    }


async def _geocode_os(db, o: dict, use_cache: bool = True) -> dict:
    """Geocodifica o endereço estruturado da OS e classifica a confiança.
    status: approved | review | blocked | incomplete | error (error = falha transitória, não é guardada)."""
    parts = _address_parts(o)
    addr_hash = _address_hash(parts)
    base = {"status": "incomplete", "lat": None, "lng": None, "address_hash": addr_hash,
            "precision": None, "formatted_address": None, "place_id": None, "reasons": [], "source": None}

    missing = _missing_fields(parts)
    if missing:
        return {**base, "status": "incomplete", "reasons": [f"faltando_{m}" for m in missing]}

    if use_cache:
        cached = await _get_cached_geocode(db, addr_hash)
        if cached:
            return {**base, "status": "approved", "lat": float(cached["lat"]), "lng": float(cached["lng"]),
                    "precision": cached.get("precision"), "formatted_address": cached.get("formatted_address"),
                    "place_id": cached.get("place_id"), "source": cached.get("source") or "google_geocoding",
                    "from_cache": True}

    failed = _fail_get(addr_hash)
    if failed:
        return failed

    key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
    if not key:
        return {**base, "status": "error", "reasons": ["api_key_ausente"]}

    cep = parts["cep"]
    cep_fmt = f"{cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep
    query = ", ".join(p for p in [
        f"{parts['street']} {parts['number']}".strip(),
        parts["neighborhood"],
        f"{parts['city']} - {parts['state']}",
        cep_fmt,
        "Brasil",
    ] if p)
    try:
        r = await _http().get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "components": f"country:BR|administrative_area:{parts['state']}",
                    "region": "br", "language": "pt-BR", "key": key},
            timeout=10.0,
        )
        data = r.json()
    except Exception as e:
        logger.warning(f"geocode {o.get('os_number')}: {e!r}")
        return {**base, "status": "error", "reasons": ["falha_de_rede"]}

    gstatus = data.get("status")
    if gstatus == "ZERO_RESULTS" or (gstatus == "OK" and not data.get("results")):
        result = {**base, "status": "blocked", "reasons": ["sem_resultado"], "source": "google_geocoding"}
        _fail_set(addr_hash, result)
        await _audit(db, o, parts, result, "google_geocoding")
        return result
    if gstatus != "OK":
        logger.warning(f"geocode {o.get('os_number')}: status Google {gstatus}")
        return {**base, "status": "error", "reasons": [f"google_{(gstatus or 'sem_status').lower()}"]}

    evals = [_evaluate(parts, res) for res in data["results"][:3]]
    rank = {"approved": 0, "review": 1, "blocked": 2}
    evals.sort(key=lambda e: rank[e["status"]])
    best = evals[0]
    if best["status"] == "approved":
        # Dois resultados "aprovados" em pontos distantes = endereço ambíguo → confirmação humana.
        for other in evals[1:]:
            if other["status"] == "approved" and _haversine_m(best["lat"], best["lng"], other["lat"], other["lng"]) > 150:
                best["status"] = "review"
                best["reasons"].append("resultado_ambiguo")
                break

    result = {**base, **best, "address_hash": addr_hash, "source": "google_geocoding"}
    await _audit(db, o, parts, result, "google_geocoding")
    if result["status"] == "approved":
        await _store_geocode_cache(db, parts, result, "google_geocoding")
    else:
        _fail_set(addr_hash, result)
    return result


def _legacy_precision(result: dict) -> str:
    """Compatibilidade com o frontend: exact | cached | review | approximate | not_found | incomplete | error."""
    st = result["status"]
    if st == "approved":
        return "cached" if result.get("from_cache") else "exact"
    if st == "review":
        return "review"
    if st == "blocked":
        return "not_found" if "sem_resultado" in (result.get("reasons") or []) else "approximate"
    return st


@router.post("/geocode")
async def get_cached_coords(req: GeocodeRequest, payload: dict = Depends(verify_token)):
    """
    Retorna lat/lng VALIDADOS para as OSs. Uma coordenada só é devolvida se estiver aprovada
    e vinculada ao endereço atual da OS. Coordenada antiga/sem vínculo é invalidada e a OS
    é geocodificada de novo (cache por endereço completo → Google, máx. MAX_GEOCODE_PER_CALL pagas).
    Cada item traz `status` (approved/review/blocked/incomplete/pending/error) e `reasons`.
    Itens em `review` trazem `candidate` para confirmação visual do pino.
    """
    db = get_db()
    nums = list(dict.fromkeys(str(n) for n in req.os_numbers))
    if not nums:
        return {"results": [], "total": 0, "cached": 0, "uncached": 0, "pending": 0, "needs_review": 0}
    user_id = (payload or {}).get("sub") or (payload or {}).get("id") or (payload or {}).get("user_id")

    orders = await db.service_orders.find({"os_number": {"$in": nums}}, _OS_GEO_PROJECTION).to_list(len(nums))

    sem = asyncio.Semaphore(5)
    results = []
    missing = []
    for o in orders:
        item = {"os_number": o["os_number"], "lat": None, "lng": None, "cached": False,
                "precision": None, "status": "pending", "reasons": []}
        results.append(item)

        coords = _validated_coords(o)
        if coords:
            item.update(lat=coords[0], lng=coords[1], cached=True, precision="cached", status="approved")
            continue

        parts = _address_parts(o)
        addr_hash = _address_hash(parts)
        geo = o.get("geolocation") or {}
        # Coordenada legada, de outro endereço ou sem aprovação: deixa de valer.
        stale = (o.get("lat") is not None or o.get("lng") is not None
                 or (geo.get("status") == "approved" and geo.get("address_hash") != addr_hash)
                 or (geo.get("address_hash") not in (None, addr_hash)))
        if stale:
            await _invalidate_os_geolocation(db, o["os_number"], addr_hash)

        if not _missing_fields(parts):
            cached = await _get_cached_geocode(db, addr_hash)
            if cached:
                res = {"status": "approved", "lat": float(cached["lat"]), "lng": float(cached["lng"]),
                       "precision": cached.get("precision"), "formatted_address": cached.get("formatted_address"),
                       "place_id": cached.get("place_id"), "address_hash": addr_hash, "reasons": [],
                       "from_cache": True}
                await _persist_geolocation(db, o["os_number"], res, cached.get("source") or "google_geocoding", user_id)
                item.update(lat=res["lat"], lng=res["lng"], cached=True, precision="cached", status="approved")
                continue
        missing.append((o, item))

    async def _retry(o, item):
        async with sem:
            res = await _geocode_os(db, o, use_cache=False)
        item["status"] = res["status"]
        item["reasons"] = res.get("reasons") or []
        item["precision"] = _legacy_precision(res)
        if res["status"] == "approved":
            item.update(lat=res["lat"], lng=res["lng"], cached=True)
        elif res["status"] == "review" and res.get("lat") is not None:
            item["candidate"] = {"lat": res["lat"], "lng": res["lng"],
                                 "formatted_address": res.get("formatted_address")}
        await _persist_geolocation(db, o["os_number"], res, res.get("source") or "google_geocoding", user_id)

    batch = missing[:MAX_GEOCODE_PER_CALL]
    await asyncio.gather(*[_retry(o, it) for o, it in batch])
    pending = len(missing) - len(batch)
    for _, it in missing[MAX_GEOCODE_PER_CALL:]:
        it["precision"] = "pending"

    found_nums = {o["os_number"] for o in orders}
    for os_num in nums:
        if os_num not in found_nums:
            results.append({"os_number": os_num, "lat": None, "lng": None, "cached": False,
                            "precision": "not_found", "status": "not_found", "reasons": ["os_nao_encontrada"]})

    cached_count = sum(1 for r in results if r["cached"])
    return {
        "results": results,
        "total": len(results),
        "cached": cached_count,
        "uncached": len(results) - cached_count,
        "pending": pending,  # >0: chame de novo para continuar geocodificando
        "needs_review": sum(1 for r in results if r.get("status") == "review"),
    }


@router.post("/cache-coords")
async def cache_coords(req: CacheCoordsRequest, payload: dict = Depends(verify_token)):
    """
    Recebe coordenadas obtidas no frontend, mas NÃO confia na precisão declarada por ele.
    - source="manual_confirmed": usuário confirmou/arrastou o pino → gravado como confirmação manual.
    - Qualquer outro caso: o backend geocodifica o endereço por conta própria e só aceita se a
      coordenada do frontend estiver a ≤100 m do resultado aprovado (grava a coordenada do backend).
    """
    db = get_db()
    user_id = (payload or {}).get("sub") or (payload or {}).get("id") or (payload or {}).get("user_id")
    updated = 0
    rejected = []
    for item in req.coords:
        if not _valid_br(item.lat, item.lng):
            rejected.append({"os_number": item.os_number, "reason": "coordenada_invalida"})
            continue
        o = await db.service_orders.find_one({"os_number": item.os_number}, _OS_GEO_PROJECTION)
        if not o:
            rejected.append({"os_number": item.os_number, "reason": "os_nao_encontrada"})
            continue
        parts = _address_parts(o)
        addr_hash = _address_hash(parts)

        
    if item.source == "manual_confirmed":
        # Confirmação manual só pode ser feita por administrador.
        if (payload or {}).get("role") not in {"admin", "master_admin"}:
            raise HTTPException(
                status_code=403,
                detail="Somente administradores podem confirmar localização manualmente."
            )

        if not parts["city"] or not parts["state"]:
            rejected.append({
                "os_number": item.os_number,
                "reason": "faltando_cidade_ou_uf"
            })
            continue

        if not parts["street"] or not parts["number"]:
            rejected.append({
                "os_number": item.os_number,
                "reason": "faltando_logradouro_ou_numero"
            })
            continue

        # Confere se o ponto está em uma região geograficamente
        # coerente com a cidade retornada pelo geocodificador reverso.
        api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail="GOOGLE_MAPS_API_KEY não configurada."
            )

        try:
            response = await _http().get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={
                    "latlng": f"{item.lat},{item.lng}",
                    "key": api_key,
                    "language": "pt-BR"
                },
                timeout=15
            )
            response.raise_for_status()
            reverse_data = response.json()

        except Exception as e:
            logger.error(f"Reverse geocode manual falhou: {e!r}")
            raise HTTPException(
                status_code=502,
                detail="Não foi possível validar o ponto no mapa."
            )

        reverse_results = reverse_data.get("results") or []
        if not reverse_results:
            rejected.append({
                "os_number": item.os_number,
                "reason": "ponto_sem_endereco_reverso"
            })
            continue

        reverse = reverse_results[0]
        reverse_comps = reverse.get("address_components") or []

        reverse_city = _norm(next(
            (
                c.get("long_name", "")
                for c in reverse_comps
                if {"administrative_area_level_2", "locality"} & set(c.get("types", []))
            ),
            ""
        ))

        reverse_state = next(
            (
                c.get("short_name", "").upper()
                for c in reverse_comps
                if "administrative_area_level_1" in c.get("types", [])
            ),
            ""
        )

        if reverse_city != _norm(parts["city"]) or reverse_state != parts["state"]:
            rejected.append({
                "os_number": item.os_number,
                "reason": "ponto_fora_da_cidade_ou_uf_informada",
                "returned_city": reverse_city,
                "returned_state": reverse_state
            })
            continue

        res = {
            "status": "approved",
            "lat": item.lat,
            "lng": item.lng,
            "precision": "MANUAL",
            "formatted_address": reverse.get("formatted_address"),
            "place_id": reverse.get("place_id"),
            "address_hash": addr_hash,
            "reasons": [],
            "source": "manual_confirmed"
        }

        await _persist_geolocation(
            db, item.os_number, res, "manual_confirmed", user_id
        )
        await _store_geocode_cache(db, parts, res, "manual_confirmed")
        await _audit(db, o, parts, res, "manual_confirmed")
        updated += 1
        continue

        res = await _geocode_os(db, o)
        if res["status"] != "approved":
            rejected.append({"os_number": item.os_number, "reason": res["status"], "details": res.get("reasons")})
            continue
        if _haversine_m(item.lat, item.lng, res["lat"], res["lng"]) > 100:
            rejected.append({"os_number": item.os_number, "reason": "coordenada_nao_confere_com_endereco"})
            continue
        await _persist_geolocation(db, item.os_number, res, res.get("source") or "google_geocoding", user_id)
        updated += 1

    return {"updated": updated, "total": len(req.coords), "rejected": rejected}


@router.get("/geocode/pending")
async def list_pending_geolocation(limit: int = 2000, payload: dict = Depends(verify_token)):
    if (payload or {}).get("role") not in {"admin", "master_admin"}:
        raise HTTPException(
            status_code=403,
            detail="Apenas administradores podem consultar pendências de geolocalização."
        )
    """OSs sem localização aprovada (pending/review/blocked/incomplete) para o filtro 'Precisa revisar'."""
    db = get_db()
    limit = max(1, min(limit, 5000))
    docs = await db.service_orders.find({}, _OS_GEO_PROJECTION).to_list(limit)
    out = []
    for o in docs:
        if _validated_coords(o):
            continue
        geo = o.get("geolocation") or {}
        out.append({
            "os_number": o.get("os_number"),
            "address": _os_address(o),
            "status": geo.get("status") or "pending",
            "reasons": geo.get("reasons") or [],
            "candidate": geo.get("candidate"),
        })
    return {"total": len(out), "results": out}


@router.post("/geocode/reconcile")
async def reconcile_geolocation(clear_legacy: bool = False, limit: int = 5000,
                                payload: dict = Depends(verify_token)):
    """Reconciliação (admin): invalida aprovações cujo endereço mudou depois de aprovadas.
    Com clear_legacy=true também limpa lat/lng antigos sem validação (serão re-geocodificados)."""
    if (payload or {}).get("role") == "technician":
        raise HTTPException(status_code=403, detail="Restrito a administradores")
    db = get_db()
    docs = await db.service_orders.find({}, _OS_GEO_PROJECTION).to_list(max(1, min(limit, 20000)))
    changed_addr = legacy = 0
    for o in docs:
        if _validated_coords(o):
            continue
        geo = o.get("geolocation") or {}
        has_coords = o.get("lat") is not None or o.get("lng") is not None
        addr_hash = _address_hash(_address_parts(o))
        if geo.get("status") == "approved":
            await _invalidate_os_geolocation(db, o["os_number"], addr_hash)
            changed_addr += 1
        elif has_coords and clear_legacy:
            await _invalidate_os_geolocation(db, o["os_number"], addr_hash)
            legacy += 1
    return {"scanned": len(docs), "invalidated_address_changed": changed_addr, "invalidated_legacy": legacy}



# === GOOGLE ROUTES API PROXY ===

class Waypoint(BaseModel):
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    # Preferido para paradas de OS: o backend usa a coordenada VALIDADA da OS (ignora lat/lng enviados).
    os_number: Optional[str] = None
    # "gps" (posição do técnico) ou "base": lat/lng enviados são usados como estão.
    kind: Optional[str] = None

class ComputeRouteRequest(BaseModel):
    origin: Waypoint
    destination: Optional[Waypoint] = None
    intermediates: List[Waypoint] = []
    optimize_waypoint_order: bool = False
    round_trip: bool = False


def _latlng_waypoint(lat: float, lng: float):
    return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


async def _resolve_waypoint(db, wp: Waypoint):
    """Coordenada só prevalece quando comprovadamente vinculada ao endereço atual."""
    if wp.os_number:
        so = await db.service_orders.find_one({"os_number": str(wp.os_number)}, _OS_GEO_PROJECTION)
        if not so:
            raise HTTPException(status_code=404, detail=f"OS {wp.os_number} não encontrada")
        coords = _validated_coords(so)
        if not coords:
            raise HTTPException(status_code=409,
                                detail=f"OS {wp.os_number} sem localização validada. Confirme o endereço antes de roteirizar.")
        return _latlng_waypoint(*coords)
    if wp.lat is not None and wp.lng is not None:
        # lat/lng + endereço sem os_number: não há como provar o vínculo → vale o endereço.
        if (wp.address or "").strip() and wp.kind not in ("gps", "base"):
            return {"address": wp.address}
        return _latlng_waypoint(wp.lat, wp.lng)
    return {"address": wp.address}


@router.post("/compute-route")
async def compute_route(req: ComputeRouteRequest):
    """
    Proxy to Google Routes API (computeRoutes) to avoid frontend API key referrer issues.
    Returns polyline, legs, distances, durations, and optimized waypoint order.
    """
    api_key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY not configured")
    db = get_db()

    inter = list(req.intermediates)
    for wp in [req.origin, req.destination, *inter]:
        if wp is None or wp.os_number:
            continue
        if (wp.lat is None) != (wp.lng is None) or (wp.lat is not None and not _valid_br(wp.lat, wp.lng)):
            raise HTTPException(status_code=400, detail=f"Coordenada inválida: {wp.lat},{wp.lng}")
        if wp.lat is None and not (wp.address or "").strip():
            raise HTTPException(status_code=400, detail="Parada sem coordenada e sem endereço")
    dest = req.destination
    same = (dest is not None and dest.lat is not None and dest.lat == req.origin.lat
            and dest.lng == req.origin.lng and not dest.os_number)
    # Sem round_trip não assume ida e volta: a última parada vira o destino.
    if not req.round_trip and (dest is None or same) and inter:
        dest = inter.pop()
    if dest is None:
        raise HTTPException(status_code=400, detail="Informe o destino ou ao menos uma parada")

    body = {
        "origin": await _resolve_waypoint(db, req.origin),
        "destination": await _resolve_waypoint(db, dest),
        "intermediates": [await _resolve_waypoint(db, wp) for wp in inter],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "optimizeWaypointOrder": req.optimize_waypoint_order and len(inter) > 1,
        "languageCode": "pt-BR",
        "units": "METRIC",
    }

    field_mask = ",".join([
        "routes.duration",
        "routes.distanceMeters",
        "routes.polyline.encodedPolyline",
        "routes.legs.distanceMeters",
        "routes.legs.duration",
        "routes.legs.polyline.encodedPolyline",
        "routes.legs.startLocation",
        "routes.legs.endLocation",
        "routes.legs.steps.distanceMeters",
        "routes.legs.steps.staticDuration",
        "routes.legs.steps.polyline.encodedPolyline",
        "routes.legs.steps.startLocation",
        "routes.legs.steps.endLocation",
        "routes.legs.steps.navigationInstruction",
        "routes.optimizedIntermediateWaypointIndex",
    ])

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }

    try:
        response = await _http().post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            json=body,
            headers=headers,
            timeout=30.0,
        )

        if response.status_code != 200:
            logger.error(f"Routes API error {response.status_code}: {response.text}")
            raise HTTPException(status_code=response.status_code, detail=response.text)

        data = response.json()
        if not data.get("routes"):
            raise HTTPException(status_code=400, detail="No routes found for the given waypoints")

        return data

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Google Routes API timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Routes API unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# === NAV-LEG (live navigation, per-segment traffic) ===

class NavLegRequest(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None
    # Preferido: destino = localização validada da OS (ignora dest_lat/dest_lng).
    dest_os_number: Optional[str] = None
    origin_heading: Optional[int] = None  # 0-360, sentido do veículo


@router.post("/nav-leg")
async def nav_leg(req: NavLegRequest):
    """
    Single origin→destination route used by the live-navigation map.
    Returns the encoded polyline plus `speedReadingIntervals` so the
    frontend can color each road segment by traffic level (NORMAL,
    SLOW, TRAFFIC_JAM). This mirrors Google Maps' UX where only the
    congested portion is colored, not the entire route.
    """
    api_key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY not configured")

    if not _valid_br(req.origin_lat, req.origin_lng):
        raise HTTPException(status_code=400, detail="Origem inválida")
    if req.dest_os_number:
        so = await get_db().service_orders.find_one({"os_number": str(req.dest_os_number)}, _OS_GEO_PROJECTION)
        coords = _validated_coords(so) if so else None
        if not coords:
            raise HTTPException(status_code=409,
                                detail=f"OS {req.dest_os_number} sem localização validada. Confirme o endereço antes de navegar.")
        dest_lat, dest_lng = coords
    else:
        dest_lat, dest_lng = req.dest_lat, req.dest_lng
        if dest_lat is None or dest_lng is None or not _valid_br(dest_lat, dest_lng):
            raise HTTPException(status_code=400, detail="Destino inválido")

    origin = {"location": {"latLng": {"latitude": req.origin_lat, "longitude": req.origin_lng}}}
    if req.origin_heading is not None:
        origin["location"]["heading"] = req.origin_heading % 360
    body = {
        "origin": origin,
        "destination": {"location": {"latLng": {"latitude": dest_lat, "longitude": dest_lng}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "extraComputations": ["TRAFFIC_ON_POLYLINE"],
        "polylineQuality": "HIGH_QUALITY",
        "languageCode": "pt-BR",
        "units": "METRIC",
    }
    field_mask = ",".join([
        "routes.duration",
        "routes.staticDuration",
        "routes.distanceMeters",
        "routes.polyline.encodedPolyline",
        "routes.travelAdvisory.speedReadingIntervals",
        "routes.legs.distanceMeters",
        "routes.legs.duration",
        "routes.legs.staticDuration",
    ])
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }
    try:
        response = await _http().post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            json=body, headers=headers, timeout=15.0,
        )
        if response.status_code != 200:
            logger.error(f"Nav-leg Routes API error {response.status_code}: {response.text}")
            raise HTTPException(status_code=response.status_code, detail=response.text)
        data = response.json()
        if not data.get("routes"):
            raise HTTPException(status_code=400, detail="No nav route found")
        route0 = data["routes"][0]
        return {
            "encoded_polyline": (route0.get("polyline") or {}).get("encodedPolyline", ""),
            "distance_meters": route0.get("distanceMeters", 0),
            "duration_seconds": int(str(route0.get("duration", "0s")).rstrip("s") or 0),
            # TRAFFIC_AWARE_OPTIMAL: `duration` já considera o trânsito.
            "duration_in_traffic_seconds": int(str(route0.get("duration", "0s")).rstrip("s") or 0),
            "static_duration_seconds": int(str(route0.get("staticDuration", "0s")).rstrip("s") or 0),
            "speed_reading_intervals": (route0.get("travelAdvisory") or {}).get("speedReadingIntervals", []),
        }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Routes API timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nav-leg unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── LLM-powered route suggestion analysis ─────────────────────────────
# Uses the Emergent LLM Key (OpenAI) to enrich the client-side proximity
# groupings with a human-friendly rationale. NEVER exposes the LLM key to
# the frontend — the frontend just sends the pre-computed groups.

class RouteAnalyzeOS(BaseModel):
    os_number: str
    customer_name: Optional[str] = None
    city: Optional[str] = None
    neighborhood: Optional[str] = None
    service_type: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[bool] = False
    days_open: Optional[int] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


class RouteAnalyzeGroup(BaseModel):
    name: str
    orders: List[RouteAnalyzeOS]
    avg_dist_km: Optional[float] = None


class RouteAnalyzeRequest(BaseModel):
    groups: List[RouteAnalyzeGroup]
    base_lat: Optional[float] = -19.8881334
    base_lng: Optional[float] = -43.9420748


@router.post("/analyze-suggestions")
async def analyze_route_suggestions(body: RouteAnalyzeRequest):
    """Analisa as sugestões de rota pré-computadas e retorna um resumo/
    rationale para cada grupo, considerando: Região, Proximidade da Base,
    Tempo em aberto (>10 dias) e Prioridade. Usa OpenAI via Emergent LLM Key.

    Retorno: { groups: [ { name, rationale, priority_score, tags, top_reason } ] }
    Se a LLM falhar, retorna um fallback determinístico (sem quebrar a UX).
    """
    import json as _json
    emergent_key = os.environ.get("EMERGENT_LLM_KEY")

    # Fallback determinístico caso a LLM falhe / não haja chave.
    def _deterministic_fallback():
        out = []
        for g in body.groups:
            os_list = g.orders or []
            urgentes = [o for o in os_list if (o.days_open or 0) > 10]
            prioritarias = [o for o in os_list if o.priority]
            cities = {}
            for o in os_list:
                c = (o.city or "").strip() or "?"
                cities[c] = cities.get(c, 0) + 1
            top_city = max(cities.items(), key=lambda x: x[1])[0] if cities else "-"
            tags = []
            if g.avg_dist_km is not None and g.avg_dist_km < 8:
                tags.append("perto-da-base")
            if len(urgentes) >= max(1, len(os_list) // 3):
                tags.append("atraso-alto")
            if prioritarias:
                tags.append("prioridade")
            score = (
                (0 if g.avg_dist_km is None else max(0, 40 - min(g.avg_dist_km, 40))) * 0.4
                + min(len(urgentes) * 5, 30) * 0.4
                + min(len(prioritarias) * 8, 30) * 0.2
            )
            rationale = (
                f"{len(os_list)} OSs concentradas em {top_city}. "
                f"{len(urgentes)} OS em atraso (>10 dias), {len(prioritarias)} marcada(s) como prioridade. "
                f"Distância média até a base: {g.avg_dist_km:.1f} km."
                if g.avg_dist_km is not None
                else f"{len(os_list)} OSs em {top_city}. {len(urgentes)} em atraso (>10 dias)."
            )
            out.append({
                "name": g.name,
                "rationale": rationale,
                "priority_score": round(score, 1),
                "tags": tags,
                "top_reason": tags[0] if tags else "distribuicao-equilibrada",
            })
        # Ordena por score desc
        out.sort(key=lambda x: -x["priority_score"])
        return {"groups": out, "source": "fallback"}

    if not emergent_key:
        logger.warning("EMERGENT_LLM_KEY ausente — usando fallback determinístico")
        return _deterministic_fallback()

    # Compact payload — evita estourar tokens quando houver muitas OSs.
    compact_groups = []
    for g in body.groups:
        compact_groups.append({
            "name": g.name,
            "avg_dist_km": round(g.avg_dist_km, 2) if g.avg_dist_km is not None else None,
            "os_count": len(g.orders or []),
            "os": [
                {
                    "n": o.os_number,
                    "cli": (o.customer_name or "")[:40],
                    "city": o.city or "",
                    "bairro": o.neighborhood or "",
                    "tipo": o.service_type or "",
                    "prio": bool(o.priority),
                    "dias_aberto": o.days_open or 0,
                }
                for o in (g.orders or [])[:25]  # cap por grupo
            ],
        })

    system_msg = (
        "Você é um planejador de rotas para técnicos de campo em Belo Horizonte/MG. "
        "Analise cada grupo de OSs (Ordens de Serviço) e devolva um JSON válido com a chave "
        "'groups', uma lista onde cada item contém: "
        "'name' (mantenha o nome enviado), "
        "'rationale' (1-2 frases em pt-BR explicando POR QUE atender esse grupo primeiro), "
        "'priority_score' (0-100, quanto maior mais urgente), "
        "'tags' (lista curta de strings: 'perto-da-base', 'atraso-alto', 'prioridade', 'regiao-densa', 'alternativa'), "
        "'top_reason' (um único motivo principal). "
        "Considere: 1) Região/proximidade (avg_dist_km), 2) Tempo em aberto (dias_aberto > 10 = urgente), "
        "3) Prioridade (prio=true = crítico), 4) Concentração geográfica (mesma cidade/bairro). "
        "Retorne APENAS o JSON, sem markdown."
    )
    user_text = _json.dumps({"base": {"lat": body.base_lat, "lng": body.base_lng}, "groups": compact_groups}, ensure_ascii=False)

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=emergent_key,
            session_id=f"route-analyze-{uuid.uuid4()}",
            system_message=system_msg,
        ).with_model("openai", "gpt-5.4-mini")
        response = await chat.send_message(UserMessage(text=user_text))
        text = (response or "").strip()
        # Remove possíveis code fences
        if text.startswith("```"):
            text = text.strip("`")
            # remove leading "json\n"
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        parsed = _json.loads(text)
        groups_out = parsed.get("groups") or []
        if not isinstance(groups_out, list) or not groups_out:
            raise ValueError("LLM retornou payload inesperado")
        # Ordena por score desc
        try:
            groups_out.sort(key=lambda x: -(x.get("priority_score") or 0))
        except Exception:
            pass
        return {"groups": groups_out, "source": "llm"}
    except Exception as e:
        logger.warning(f"[analyze-suggestions] LLM falhou, usando fallback: {e}")
        return _deterministic_fallback()