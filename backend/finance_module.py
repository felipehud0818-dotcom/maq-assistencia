"""Módulo Financeiro (base: projeto finance-maq) integrado às OSs reais (Supabase) e usuários/técnicos reais (Mongo)."""
import os
import time
import hmac
import uuid
import asyncio
import re
import logging
import calendar
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

log = logging.getLogger("finance")
router = APIRouter()
M = None  # Mongo (lançamentos, categorias, usuários)
S = None  # Supabase (ordens de serviço)
_verify = None
_bearer = HTTPBearer()
TZ = ZoneInfo("America/Sao_Paulo")
NO_ID = {"_id": 0}
METHODS = ("debito", "credito", "pix", "dinheiro")
CREDIT_MODES = ("avista", "parcelado")
STATUSES = ("previsto", "pago", "atrasado")
GROUPS = {"entrada": ("servico", "peca", "taxa", "outros"), "saida": ("fixa", "variavel", "outra")}
DONE_STATUSES = ("Finalizada", "Visita Realizada")
OS_FIELDS = {"_id": 0, "os_number": 1, "customer_name": 1, "technician": 1, "status": 1, "visit_fee": 1,
             "visit_fee_paid": 1, "source": 1, "updated_at": 1, "product": 1}
DEFAULT_CATEGORIES = [
    ("entrada", "servico", "Serviço", "#2563EB", "servico"),
    ("entrada", "peca", "Peças", "#16A34A", "peca"),
    ("entrada", "taxa", "Taxa de visita", "#D97706", "taxa"),
    ("entrada", "outros", "Outras entradas", "#9333EA", None),
    ("saida", "fixa", "Aluguel", "#E11D48", None),
    ("saida", "fixa", "Salários e comissões", "#4F46E5", "comissao"),
    ("saida", "variavel", "Combustível", "#0891B2", None),
    ("saida", "variavel", "Peças compradas", "#059669", None),
    ("saida", "outra", "Outras saídas", "#64748B", None),
]


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def month_day(year: int, month: int, day: int) -> str:
    return date(year, month, min(day, calendar.monthrange(year, month)[1])).isoformat()


def iso_date(v) -> str:
    """'AAAA-MM-DD...' ou 'DD/MM/AAAA' → 'AAAA-MM-DD' ('' se inválida)."""
    s = str(v or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        s = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        return ""


def with_status(t: dict) -> dict:
    if t.get("status") == "previsto" and t["date"] < today():
        t["status"] = "atrasado"
    return t


def status_filter(status: str) -> dict:
    if status == "atrasado":
        return {"$or": [{"status": "atrasado"}, {"status": "previsto", "date": {"$lt": today()}}]}
    if status == "previsto":
        return {"status": "previsto", "date": {"$gte": today()}}
    return {"status": status}


# ---------- Autenticação (login real do sistema) ----------
async def current_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    p = await _verify(credentials)
    email = (p.get("email") or "").strip().lower()
    u = await M.users.find_one({"email": email}, {"_id": 0, "password_hash": 0, "password_enc": 0}) or {}
    return {"id": u.get("id") or p.get("user_id") or email, "email": email, "name": u.get("name") or p.get("name") or "",
            "role": "admin" if p.get("role") == "admin" else "tecnico",
            "finance_access": u.get("finance_access") or "own", "commission_pct": u.get("commission_pct", 0)}


FIN_UNLOCK_TTL = 8 * 3600
_unlock_fails: dict = {}


def _unlock_sig(uid: str, exp: str) -> str:
    return hmac.new(os.environ["JWT_SECRET"].encode(), f"fin:{uid}:{exp}".encode(), "sha256").hexdigest()


def check_unlocked(request: Request, user: dict):
    """Financeiro exige a senha própria (token curto no header X-Finance-Unlock), mesmo para admin."""
    exp, _, sig = (request.headers.get("x-finance-unlock") or "").partition(".")
    if not (exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, _unlock_sig(user["id"], exp))):
        raise HTTPException(423, "Financeiro bloqueado: digite a senha")


async def require_finance(request: Request, user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin" and user.get("finance_access") != "own":
        raise HTTPException(403, "Sem acesso ao financeiro")
    check_unlocked(request, user)
    return user


async def require_admin(request: Request, user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Apenas administradores")
    check_unlocked(request, user)
    return user


def scope(user: dict) -> dict:
    return {} if user["role"] == "admin" else {"technician_id": user["id"]}


# ---------- Técnicos reais ----------
def _tech_public(u: dict) -> dict:
    return {"id": u["id"], "name": u.get("name", ""), "email": u.get("email"), "role": "tecnico",
            "commission_pct": u.get("commission_pct", 0), "finance_access": u.get("finance_access") or "own"}


async def technicians() -> list:
    rows = await M.users.find({"role": "technician"}, {"_id": 0, "password_hash": 0, "password_enc": 0}).sort("name", 1).to_list(500)
    return [_tech_public(u) for u in rows if u.get("id")]


def match_tech(name: Optional[str], techs: list) -> Optional[str]:
    n = (name or "").strip().lower()
    if not n:
        return None
    exact = next((t["id"] for t in techs if (t["name"] or "").strip().lower() == n), None)
    if exact:
        return exact
    first = n.split()[0]
    return next((t["id"] for t in techs if (t["name"] or "").strip().lower().split()[:1] == [first]), None)


# ---------- Ordens de serviço reais ----------
def parts_total(parts: list) -> float:
    return sum(float(p.get("qty") or 0) * float(p.get("unit_price") or 0) for p in parts or [])


def values_total(v: dict) -> float:
    return float(v.get("service_value") or 0) + float(v.get("fee_value") or 0) + parts_total(v.get("parts"))


async def os_rows(q: Optional[str] = None, ids: Optional[List[str]] = None, limit: int = 30) -> list:
    if ids is not None:
        if not ids:
            return []
        query = {"os_number": {"$in": ids}}
        limit = max(limit, len(ids))
    elif q:
        query = {"$or": [{"os_number": {"$regex": q}}, {"customer_name": {"$regex": q}}]}
    else:
        query = {}
    rows = await S.service_orders.find(query, OS_FIELDS).sort("updated_at", -1).limit(limit).to_list(limit)
    techs = await technicians()
    nums = [r["os_number"] for r in rows]
    vals = {v["os_number"]: v async for v in M.fin_os_values.find({"os_number": {"$in": nums}}, NO_ID)}
    out = []
    for r in rows:
        v = vals.get(r["os_number"])
        out.append({"id": r["os_number"], "number": str(r["os_number"]).strip(), "client": r.get("customer_name") or "",
                    "technician": r.get("technician") or "", "technician_id": match_tech(r.get("technician"), techs),
                    "status": r.get("status") or "", "source": r.get("source") or "", "product": r.get("product") or "",
                    "visit_fee": float(r.get("visit_fee") or 0), "visit_fee_paid": bool(r.get("visit_fee_paid")),
                    "values": v, "total": round(values_total(v) if v else float(r.get("visit_fee") or 0), 2)})
    return out


async def check_os_ids(ids: List[str]):
    ids = list(dict.fromkeys(ids))
    if ids and len(await os_rows(ids=ids)) != len(ids):
        raise HTTPException(400, "OS inválida")
    return ids


async def system_cats() -> dict:
    return {c["system_key"]: c["id"] async for c in M.fin_categories.find({"system_key": {"$ne": None}}, NO_ID)}


def credit_fields(method: str, mode: Optional[str], installments: Optional[int]) -> dict:
    """Cartão de crédito exige à vista/parcelado; parcelado exige nº de parcelas (2-48)."""
    if method != "credito":
        return {"credit_mode": None, "installments": None}
    if mode not in CREDIT_MODES:
        raise HTTPException(400, "Cartão de crédito: escolha À vista ou Parcelado")
    if mode == "parcelado" and not (installments and 2 <= installments <= 48):
        raise HTTPException(400, "Informe o número de parcelas (2 a 48)")
    return {"credit_mode": mode, "installments": installments if mode == "parcelado" else 1}


async def os_entries(o: dict, vals: dict, method: str, status: str, when: str, credit: Optional[dict] = None):
    cats = await system_cats()
    parts = vals.get("parts") or []
    rows = [("servico", float(vals.get("service_value") or 0), f"Serviço OS #{o['number']} - {o['client']}", None),
            ("taxa", float(vals.get("fee_value") or 0), f"Taxa OS #{o['number']}", None),
            ("peca", parts_total(parts), f"Peças OS #{o['number']}", ", ".join(p["name"] for p in parts if p.get("name")) or None)]
    docs = [{"id": new_id(), "type": "entrada", "date": when, "amount": round(v, 2), "description": desc,
             "category_id": cats.get(key), "payment_method": method, "status": status, "os_ids": [o["id"]],
             "technician_id": o.get("technician_id"), "part_name": part, "reconciled": False, "source": "os",
             "os_key": key, "recurring_id": None, "created_at": now_iso(), **(credit or {})} for key, v, desc, part in rows if v > 0]
    if docs:
        await M.fin_transactions.insert_many(docs)
    return len(docs)


async def on_os_updated(order: dict, techs: Optional[list] = None):
    """OS com taxa de visita: paga vira entrada recebida; não paga vira valor a receber."""
    try:
        fee = float(order.get("visit_fee") or 0)
        num = order.get("os_number")
        if not num:
            return
        existing = await M.fin_transactions.find_one({"os_ids": num, "source": "os", "os_key": "taxa"}, NO_ID)
        cancelled = "cancel" in str(order.get("status") or "").lower()
        reg = iso_date(order.get("registration_date") or order.get("created_at"))
        since = (await get_settings()).get("fee_since")
        # Taxa não paga de OS anterior à data de corte não entra como a receber.
        before_cut = bool(since and reg and reg < since and not order.get("visit_fee_paid"))
        if fee <= 0 or cancelled or before_cut:
            if existing and existing.get("auto") and not existing.get("reconciled") and existing["status"] != "pago":
                await M.fin_transactions.delete_one({"id": existing["id"]})
            return
        status = "pago" if order.get("visit_fee_paid") else "previsto"
        # Paga: entra na data em que foi marcada como paga. A receber: data de cadastro da OS.
        paid_on = iso_date(order.get("updated_at")) or today()
        if existing:
            if existing.get("reconciled") or not existing.get("auto"):
                return
            if status == "pago":
                keep = existing["status"] == "pago" and existing["date"] != reg and iso_date(existing["date"]) == existing["date"]
                when = existing["date"] if keep else paid_on
            else:
                when = reg or existing["date"]
            if existing["amount"] != round(fee, 2) or existing["status"] != status or existing["date"] != when:
                await M.fin_transactions.update_one({"id": existing["id"]}, {"$set": {"amount": round(fee, 2), "status": status, "date": when}})
            return
        when = paid_on if status == "pago" else (reg or today())
        # A taxa é um componente independente: lançamentos de serviço/peça da mesma OS não bloqueiam a criação.
        cats = await system_cats()
        tech = match_tech(order.get("technician"), techs if techs is not None else await technicians())
        await M.fin_transactions.insert_one({
            "id": new_id(), "type": "entrada", "date": when, "amount": round(fee, 2),
            "description": f"Taxa OS #{str(num).strip()} - {order.get('customer_name') or ''}", "category_id": cats.get("taxa"),
            "payment_method": "pix", "status": status, "os_ids": [num], "technician_id": tech, "part_name": None,
            "reconciled": False, "source": "os", "os_key": "taxa", "auto": True, "recurring_id": None, "created_at": now_iso()})
    except Exception as exc:
        log.error("on_os_updated %s: %s", order.get("os_number"), exc)


_fee_sync = {"at": 0.0, "running": False}
_settings = {"at": 0.0, "data": {}}


async def get_settings() -> dict:
    if time.time() - _settings["at"] > 30:
        _settings["data"] = await M.fin_settings.find_one({"id": "main"}, NO_ID) or {}
        _settings["at"] = time.time()
    return _settings["data"]


AUDIT_FIELDS = ("type", "date", "amount", "description", "category_id", "payment_method", "status", "os_ids",
                "technician_id", "reconciled", "bank_tx_id", "credit_mode", "installments", "period_start", "period_end")


def _slim(d: Optional[dict]) -> Optional[dict]:
    return {k: d.get(k) for k in AUDIT_FIELDS if k in d} if d else None


async def audit(user: Optional[dict], entity: str, entity_id: str, action: str, before=None, after=None, reason: str = ""):
    """Trilha imutável: quem, quando, antes/depois. Nunca é editada pela API."""
    try:
        if isinstance(before, list) or isinstance(after, list):
            b, a = [_slim(x) for x in before or []], [_slim(x) for x in after or []]
        else:
            b, a = _slim(before), _slim(after)
        await M.fin_audit_logs.insert_one({
            "id": new_id(), "entity": entity, "entity_id": entity_id, "action": action, "before": b, "after": a,
            "reason": reason, "actor_id": (user or {}).get("id"), "actor_name": (user or {}).get("name") or (user or {}).get("email") or "sistema",
            "created_at": now_iso()})
    except Exception as exc:
        log.error("audit %s %s: %s", entity, entity_id, exc)


async def mark_fee_receipt(num: str, paid_at: str, method: str, receipt_url: str):
    """Ajusta a entrada da taxa paga com a data/forma do comprovante e o link dele."""
    tx = await M.fin_transactions.find_one({"os_ids": num, "source": "os", "os_key": "taxa"}, NO_ID)
    if tx and not tx.get("reconciled"):
        await M.fin_transactions.update_one({"id": tx["id"]}, {"$set": {
            "status": "pago", "date": paid_at, "payment_method": method, "receipt_url": receipt_url or tx.get("receipt_url")}})


async def _sync_fee_entries():
    try:
        techs = await technicians()
        fields = {**OS_FIELDS, "registration_date": 1, "created_at": 1}
        total, skip = 0, 0
        while True:  # Supabase devolve no máx. 1000 linhas por consulta
            rows = await S.service_orders.find({"visit_fee": {"$gte": 0.01}}, fields).sort("os_number", 1).skip(skip).limit(1000).to_list(1000)
            for r in rows:
                await on_os_updated(r, techs)
            total += len(rows)
            skip += 1000
            if len(rows) < 1000:
                break
        log.warning("sync taxas: %s OS verificadas", total)
    except Exception as exc:
        log.error("sync taxas: %s", exc)
    finally:
        _fee_sync["running"] = False


def sync_fee_entries():
    """Garante entrada (paga) / a receber (não paga) para toda OS com taxa de visita — no máx. a cada 10 min."""
    if _fee_sync["running"] or time.time() - _fee_sync["at"] < 600:
        return
    _fee_sync.update(at=time.time(), running=True)
    asyncio.create_task(_sync_fee_entries())


async def os_finance(o: dict) -> dict:
    txs = await M.fin_transactions.find({"os_ids": o["id"]}, NO_ID).to_list(500)
    cobrado = recebido = custo_saidas = 0.0
    for t in txs:
        share = t["amount"] / max(len(t["os_ids"]), 1)
        if t["type"] == "entrada":
            cobrado += share
            if t["status"] == "pago":
                recebido += share
        else:
            custo_saidas += share
    vals = o.get("values") or {}
    custo_pecas = sum(float(p.get("qty") or 0) * float(p.get("unit_cost") or 0) for p in vals.get("parts") or [])
    custo = custo_saidas + custo_pecas
    return {"os": o, "valor_os": o["total"], "cobrado": round(cobrado, 2), "recebido": round(recebido, 2),
            "a_receber": round(cobrado - recebido, 2), "custo": round(custo, 2), "custo_pecas": round(custo_pecas, 2),
            "custo_saidas": round(custo_saidas, 2), "lucro": round(cobrado - custo, 2),
            "lancamentos": [with_status(t) for t in txs]}


class PartIn(BaseModel):
    name: str
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    unit_cost: float = Field(0, ge=0)


class LaunchIn(BaseModel):
    service_value: float = Field(0, ge=0)
    fee_value: float = Field(0, ge=0)
    parts: List[PartIn] = []
    payment_method: str = "pix"
    credit_mode: Optional[str] = None
    installments: Optional[int] = None
    status: str = "previsto"
    date: Optional[str] = None


class ImportIn(BaseModel):
    start: str
    end: str


async def get_os(os_number: str, user: dict) -> dict:
    rows = await os_rows(ids=[os_number])
    if not rows or (user["role"] != "admin" and rows[0]["technician_id"] != user["id"]):
        raise HTTPException(404, "OS não encontrada")
    return rows[0]


@router.get("/finance/os")
async def list_os(q: Optional[str] = None, ids: Optional[str] = None, user: dict = Depends(require_finance)):
    rows = await os_rows(q=(q or "").strip() or None, ids=[i for i in ids.split("|") if i] if ids is not None else None,
                         limit=30 if user["role"] == "admin" else 500)
    return rows if user["role"] == "admin" else [r for r in rows if r["technician_id"] == user["id"]][:30]


@router.get("/finance/os/{os_number}/finance")
async def get_os_finance(os_number: str, user: dict = Depends(require_finance)):
    return await os_finance(await get_os(os_number, user))


@router.post("/finance/os/{os_number}/launch")
async def launch_os(os_number: str, body: LaunchIn, user: dict = Depends(require_admin)):
    if body.payment_method not in METHODS or body.status not in ("previsto", "pago"):
        raise HTTPException(400, "Forma de pagamento ou status inválido")
    credit = credit_fields(body.payment_method, body.credit_mode, body.installments)
    o = await get_os(os_number, user)
    if await M.fin_transactions.find_one({"os_ids": os_number, "source": "os", "reconciled": True}):
        raise HTTPException(409, "Esta OS já tem lançamentos conferidos com o extrato")
    vals = {"os_number": os_number, "service_value": body.service_value, "fee_value": body.fee_value,
            "parts": [p.model_dump() for p in body.parts], "date": body.date or today(), "updated_at": now_iso()}
    if values_total(vals) <= 0:
        raise HTTPException(400, "Informe ao menos um valor")
    await M.fin_os_values.update_one({"os_number": os_number}, {"$set": vals}, upsert=True)
    # Cria os novos antes de remover os antigos: uma falha no meio nunca deixa a OS sem lançamentos.
    old = await M.fin_transactions.find({"os_ids": os_number, "source": "os"}, NO_ID).to_list(50)
    n = await os_entries(o, vals, body.payment_method, body.status, vals["date"], credit)
    if old:
        await M.fin_transactions.delete_many({"id": {"$in": [t["id"] for t in old]}})
    new = await M.fin_transactions.find({"os_ids": os_number, "source": "os"}, NO_ID).to_list(50)
    await audit(user, "os_launch", os_number, "replaced" if old else "created", old, new)
    return {"ok": True, "created": n}


@router.post("/finance/os/import-done")
async def import_done(body: ImportIn, _: dict = Depends(require_admin)):
    rows = await S.service_orders.find({"status": {"$in": list(DONE_STATUSES)}, "visit_fee": {"$gte": 0.01},
                                        "updated_at": {"$gte": body.start, "$lte": body.end + "T23:59:59"}},
                                       OS_FIELDS).limit(3000).to_list(3000)
    before = await M.fin_transactions.count_documents({"source": "os"})
    for r in rows:
        await on_os_updated(r)
    return {"ok": True, "checked": len(rows), "created": await M.fin_transactions.count_documents({"source": "os"}) - before}


# ---------- Categorias ----------
class CategoryIn(BaseModel):
    name: str
    type: str
    group: str
    color: str = "#2563EB"


class MoveIn(BaseModel):
    direction: str


def validate_category(body: CategoryIn):
    if body.type not in GROUPS or body.group not in GROUPS[body.type]:
        raise HTTPException(400, "Tipo ou grupo inválido")


class UnlockIn(BaseModel):
    password: str


@router.post("/finance/unlock")
async def finance_unlock(body: UnlockIn, user: dict = Depends(current_user)):
    fails = [t for t in _unlock_fails.get(user["id"], []) if time.time() - t < 300]
    if len(fails) >= 5:
        raise HTTPException(429, "Muitas tentativas. Aguarde 5 minutos.")
    if not hmac.compare_digest(body.password.encode(), os.environ["FINANCE_PASSWORD"].encode()):
        _unlock_fails[user["id"]] = fails + [time.time()]
        raise HTTPException(401, "Senha incorreta")
    _unlock_fails.pop(user["id"], None)
    exp = str(int(time.time()) + FIN_UNLOCK_TTL)
    return {"token": f"{exp}.{_unlock_sig(user['id'], exp)}", "expires_at": int(exp)}


@router.get("/finance/me")
async def me(user: dict = Depends(current_user)):
    return {**user, "is_admin": user["role"] == "admin", "has_finance": user["role"] == "admin" or user["finance_access"] == "own"}


@router.get("/finance/techs")
async def list_techs(_: dict = Depends(require_admin)):
    return await technicians()


@router.get("/finance/categories")
async def list_categories(_: dict = Depends(require_finance)):
    return await M.fin_categories.find({}, NO_ID).sort("order", 1).to_list(500)


@router.post("/finance/categories")
async def create_category(body: CategoryIn, _: dict = Depends(require_admin)):
    validate_category(body)
    doc = {"id": new_id(), **body.model_dump(), "system_key": None, "order": await M.fin_categories.count_documents({})}
    await M.fin_categories.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/finance/categories/{cat_id}")
async def update_category(cat_id: str, body: CategoryIn, _: dict = Depends(require_admin)):
    validate_category(body)
    cat = await M.fin_categories.find_one({"id": cat_id}, NO_ID)
    if not cat:
        raise HTTPException(404, "Categoria não encontrada")
    if cat.get("system_key") and body.type != cat["type"]:
        raise HTTPException(400, "Categoria do sistema não pode mudar de tipo")
    await M.fin_categories.update_one({"id": cat_id}, {"$set": body.model_dump()})
    return await M.fin_categories.find_one({"id": cat_id}, NO_ID)


@router.post("/finance/categories/{cat_id}/move")
async def move_category(cat_id: str, body: MoveIn, _: dict = Depends(require_admin)):
    cats = await M.fin_categories.find({}, NO_ID).sort("order", 1).to_list(500)
    idx = next((i for i, c in enumerate(cats) if c["id"] == cat_id), None)
    if idx is None:
        raise HTTPException(404, "Categoria não encontrada")
    j = idx - 1 if body.direction == "up" else idx + 1
    if 0 <= j < len(cats):
        cats[idx], cats[j] = cats[j], cats[idx]
        for i, c in enumerate(cats):
            await M.fin_categories.update_one({"id": c["id"]}, {"$set": {"order": i}})
    return {"ok": True}


@router.delete("/finance/categories/{cat_id}")
async def delete_category(cat_id: str, _: dict = Depends(require_admin)):
    cat = await M.fin_categories.find_one({"id": cat_id}, NO_ID)
    if not cat:
        raise HTTPException(404, "Categoria não encontrada")
    if cat.get("system_key"):
        raise HTTPException(400, "Categoria usada pelo sistema não pode ser excluída")
    if await M.fin_transactions.count_documents({"category_id": cat_id}) or await M.fin_recurring.count_documents({"category_id": cat_id}):
        raise HTTPException(409, "Categoria em uso por lançamentos")
    await M.fin_categories.delete_one({"id": cat_id})
    await M.fin_budgets.delete_many({"category_id": cat_id})
    return {"ok": True}


# ---------- Lançamentos ----------
class TxIn(BaseModel):
    type: str
    date: str
    amount: float = Field(gt=0)
    description: str
    category_id: str
    payment_method: str
    credit_mode: Optional[str] = None
    installments: Optional[int] = None
    status: str
    os_ids: List[str] = []
    technician_id: Optional[str] = None
    part_name: Optional[str] = None


class LinksIn(BaseModel):
    os_ids: List[str]


class ReconcileIn(BaseModel):
    reconciled: bool


async def validate_tx(body: TxIn) -> dict:
    if body.type not in GROUPS or body.payment_method not in METHODS or body.status not in STATUSES:
        raise HTTPException(400, "Tipo, forma de pagamento ou status inválido")
    try:
        parse_date(body.date)
    except ValueError:
        raise HTTPException(400, "Data inválida")
    cat = await M.fin_categories.find_one({"id": body.category_id}, NO_ID)
    if not cat or cat["type"] != body.type:
        raise HTTPException(400, "Categoria inválida para este tipo")
    data = {**body.model_dump(), **credit_fields(body.payment_method, body.credit_mode, body.installments)}
    data["os_ids"] = await check_os_ids(body.os_ids)
    return data


async def generate_recurring() -> int:
    t = datetime.now(TZ).date()
    ym = t.strftime("%Y-%m")
    created = 0
    async for r in M.fin_recurring.find({"active": True}, NO_ID):
        if await M.fin_transactions.find_one({"recurring_id": r["id"], "date": {"$regex": f"^{ym}"}}):
            continue
        try:
            # recurring_key único (índice) impede duplicar a competência em execuções simultâneas.
            await M.fin_transactions.insert_one({
                "id": new_id(), "type": "saida", "date": month_day(t.year, t.month, r["day_of_month"]),
                "amount": r["amount"], "description": r["description"], "category_id": r["category_id"],
                "payment_method": r.get("payment_method", "pix"), "status": "previsto", "os_ids": [],
                "technician_id": None, "part_name": None, "reconciled": False, "source": "recorrente",
                "recurring_id": r["id"], "recurring_key": f"{r['id']}:{ym}", "created_at": now_iso()})
        except DuplicateKeyError:
            continue
        created += 1
    return created


@router.get("/finance/transactions")
async def list_transactions(
    user: dict = Depends(require_finance),
    type: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None,
    category_id: Optional[str] = None, technician_id: Optional[str] = None, status: Optional[str] = None,
    payment_method: Optional[str] = None, os_id: Optional[str] = None, limit: int = Query(2000, le=5000),
):
    await generate_recurring()
    sync_fee_entries()
    q = {**scope(user)}
    if type:
        q["type"] = type
    if start or end:
        q["date"] = {**({"$gte": start} if start else {}), **({"$lte": end} if end else {})}
    if category_id:
        q["category_id"] = category_id
    if technician_id and user["role"] == "admin":
        q["technician_id"] = None if technician_id == "none" else technician_id
    if payment_method:
        q["payment_method"] = payment_method
    if os_id:
        q["os_ids"] = os_id
    if status:
        q = {"$and": [q, status_filter(status)]}
    txs = await M.fin_transactions.find(q, NO_ID).sort("date", -1).to_list(limit)
    return [with_status(t) for t in txs]


@router.post("/finance/transactions")
async def create_transaction(body: TxIn, user: dict = Depends(require_admin)):
    doc = {"id": new_id(), **(await validate_tx(body)), "reconciled": False, "source": "manual", "recurring_id": None,
           "created_at": now_iso()}
    await M.fin_transactions.insert_one(doc)
    doc.pop("_id", None)
    await audit(user, "transaction", doc["id"], "created", None, doc)
    return doc


@router.put("/finance/transactions/{tx_id}")
async def update_transaction(tx_id: str, body: TxIn, user: dict = Depends(require_admin)):
    before = await M.fin_transactions.find_one({"id": tx_id}, NO_ID)
    if not before:
        raise HTTPException(404, "Lançamento não encontrado")
    data = await validate_tx(body)
    if before.get("reconciled") and (round(data["amount"], 2) != round(before["amount"], 2) or data["type"] != before["type"]):
        raise HTTPException(409, "Lançamento conferido com o extrato: desfaça a conferência antes de mudar valor ou tipo")
    data["auto"] = False
    await M.fin_transactions.update_one({"id": tx_id}, {"$set": data})
    after = await M.fin_transactions.find_one({"id": tx_id}, NO_ID)
    await audit(user, "transaction", tx_id, "updated", before, after)
    return with_status(after)


@router.delete("/finance/transactions/{tx_id}")
async def delete_transaction(tx_id: str, user: dict = Depends(require_admin)):
    before = await M.fin_transactions.find_one({"id": tx_id}, NO_ID)
    if not before:
        raise HTTPException(404, "Lançamento não encontrado")
    if before.get("reconciled") or before.get("bank_tx_id"):
        raise HTTPException(409, "Lançamento conferido com o extrato não pode ser excluído. Desfaça a conferência primeiro.")
    if before.get("source") == "repasse":
        raise HTTPException(409, "Repasse de comissão: estorne pela aba Técnicos")
    await M.fin_transactions.delete_one({"id": tx_id})
    await audit(user, "transaction", tx_id, "deleted", before, None)
    return {"ok": True}


@router.put("/finance/transactions/{tx_id}/links")
async def set_links(tx_id: str, body: LinksIn, user: dict = Depends(require_admin)):
    ids = await check_os_ids(body.os_ids)
    before = await M.fin_transactions.find_one({"id": tx_id}, NO_ID)
    if not before:
        raise HTTPException(404, "Lançamento não encontrado")
    await M.fin_transactions.update_one({"id": tx_id}, {"$set": {"os_ids": ids}})
    await audit(user, "transaction", tx_id, "links", before, {**before, "os_ids": ids})
    return {"ok": True, "os_ids": ids}


@router.patch("/finance/transactions/{tx_id}/reconcile")
async def reconcile(tx_id: str, body: ReconcileIn, user: dict = Depends(require_admin)):
    before = await M.fin_transactions.find_one({"id": tx_id}, NO_ID)
    if not before:
        raise HTTPException(404, "Lançamento não encontrado")
    upd = {"reconciled": body.reconciled}
    if not body.reconciled and before.get("bank_tx_id"):
        upd["bank_tx_id"] = None
        await M.fin_bank_transactions.update_one({"id": before["bank_tx_id"]}, {"$set": {"handled": None, "linked_tx_id": None}})
    await M.fin_transactions.update_one({"id": tx_id}, {"$set": upd})
    await audit(user, "transaction", tx_id, "reconciled" if body.reconciled else "unreconciled", before, {**before, **upd})
    return {"ok": True}


# ---------- Visão geral ----------
def summarize(txs: list) -> dict:
    s = {"entradas_pagas": 0.0, "saidas_pagas": 0.0, "entradas_total": 0.0, "saidas_total": 0.0,
         "a_receber": 0.0, "a_pagar": 0.0, "atrasado_receber": 0.0}
    for t in txs:
        e = t["type"] == "entrada"
        s["entradas_total" if e else "saidas_total"] += t["amount"]
        if t["status"] == "pago":
            s["entradas_pagas" if e else "saidas_pagas"] += t["amount"]
        else:
            s["a_receber" if e else "a_pagar"] += t["amount"]
            if e and t["status"] == "atrasado":
                s["atrasado_receber"] += t["amount"]
    s["saldo"] = s["entradas_pagas"] - s["saidas_pagas"]
    s["saldo_previsto"] = s["entradas_total"] - s["saidas_total"]
    return {k: round(v, 2) for k, v in s.items()}


@router.get("/finance/overview")
async def overview(start: str, end: str, technician_id: Optional[str] = None, user: dict = Depends(require_finance)):
    await generate_recurring()
    sync_fee_entries()
    q = {**scope(user)}
    if technician_id and user["role"] == "admin":
        q["technician_id"] = technician_id
    s, e = parse_date(start), parse_date(end)
    days = (e - s).days + 1
    ps, pe = (s - timedelta(days=days)).isoformat(), (s - timedelta(days=1)).isoformat()
    cur = [with_status(t) for t in await M.fin_transactions.find({**q, "date": {"$gte": start, "$lte": end}}, NO_ID).to_list(20000)]
    prev = [with_status(t) for t in await M.fin_transactions.find({**q, "date": {"$gte": ps, "$lte": pe}}, NO_ID).to_list(20000)]
    cats = {c["id"]: c async for c in M.fin_categories.find({}, NO_ID)}
    names = {t["id"]: t["name"] for t in await technicians()}
    by_cat, ranking, series = {}, {}, {}
    for t in cur:
        c = cats.get(t["category_id"], {"name": "Sem categoria", "color": "#94A3B8"})
        key = (t["type"], t["category_id"])
        by_cat.setdefault(key, {"type": t["type"], "category_id": t["category_id"], "name": c["name"], "color": c["color"], "total": 0})
        by_cat[key]["total"] += t["amount"]
        if t["type"] == "entrada" and t.get("technician_id"):
            r = ranking.setdefault(t["technician_id"], {"technician_id": t["technician_id"],
                                                        "name": names.get(t["technician_id"], "?"), "total": 0, "recebido": 0, "os": set()})
            r["total"] += t["amount"]
            if t["status"] == "pago":
                r["recebido"] += t["amount"]
            r["os"].update(t.get("os_ids", []))
        day = series.setdefault(t["date"], {"date": t["date"], "entradas": 0, "saidas": 0})
        day["entradas" if t["type"] == "entrada" else "saidas"] += t["amount"]
    rank = sorted(({**r, "os": len(r["os"]), "total": round(r["total"], 2), "recebido": round(r["recebido"], 2)}
                   for r in ranking.values()), key=lambda r: -r["total"])
    if user["role"] != "admin":
        rank = [r for r in rank if r["technician_id"] == user["id"]]
    alerts, due = [], []
    if user["role"] == "admin":
        alerts = [b for b in await budget_status() if b["pct"] >= 80]
        limit_day = (parse_date(today()) + timedelta(days=7)).isoformat()
        due = [with_status(t) for t in await M.fin_transactions.find(
            {"type": "saida", "status": {"$ne": "pago"}, "date": {"$lte": limit_day}}, NO_ID).sort("date", 1).to_list(50)]
    return {"current": summarize(cur), "previous": summarize(prev), "previous_range": {"start": ps, "end": pe},
            "by_category": sorted(by_cat.values(), key=lambda x: -x["total"]), "ranking": rank,
            "series": sorted(series.values(), key=lambda x: x["date"]), "budget_alerts": alerts, "due_soon": due}


AGING = [("a_vencer", "A vencer", None, 0), ("d1_7", "1–7 dias", 1, 7), ("d8_15", "8–15 dias", 8, 15),
         ("d16_30", "16–30 dias", 16, 30), ("d31_60", "31–60 dias", 31, 60), ("d60", "+60 dias", 61, 10 ** 6)]


@router.get("/finance/command-center")
async def command_center(start: str, end: str, user: dict = Depends(require_finance)):
    """Painel executivo: caixa, a receber/pagar, lucro, fluxo semanal (realizado + projetado), atraso e central de aten\u00e7\u00e3o."""
    await generate_recurring()
    sync_fee_entries()
    admin = user["role"] == "admin"
    q = {**scope(user)}
    td = parse_date(today())
    horizon = (td + timedelta(days=28)).isoformat()
    in7 = (td + timedelta(days=7)).isoformat()
    open_txs = [with_status(t) for t in await M.fin_transactions.find({**q, "status": {"$ne": "pago"}}, NO_ID).to_list(50000)]
    paid_all = await M.fin_transactions.find({**q, "status": "pago"}, {"_id": 0, "type": 1, "amount": 1, "date": 1}).to_list(100000)
    period = [with_status(t) for t in await M.fin_transactions.find({**q, "date": {"$gte": start, "$lte": end}}, NO_ID).to_list(50000)]

    def side(kind):
        rows = [t for t in open_txs if t["type"] == kind]
        late = [t for t in rows if t["status"] == "atrasado"]
        nxt = [t for t in rows if today() <= t["date"] <= in7]
        return {"total": round(sum(t["amount"] for t in rows), 2), "count": len(rows),
                "atrasado": round(sum(t["amount"] for t in late), 2), "atrasado_count": len(late),
                "prox7": round(sum(t["amount"] for t in nxt), 2), "prox7_count": len(nxt)}

    receber, pagar = side("entrada"), (side("saida") if admin else None)

    # Caixa: saldo banc\u00e1rio (Open Finance) quando houver conta conectada; sen\u00e3o, realizado acumulado.
    accounts = await M.fin_bank_accounts.find({}, NO_ID).to_list(100) if admin else []
    bank_balance = sum(float(a.get("balance") or 0) for a in accounts if a.get("type") != "CREDIT")
    realized = sum(t["amount"] if t["type"] == "entrada" else -t["amount"] for t in paid_all)
    caixa = {"valor": round(bank_balance if accounts else realized, 2), "fonte": "bancos" if accounts else "realizado", "contas": len(accounts)}

    s = summarize(period)
    lucro = {"valor": s["saldo"], "receita": s["entradas_pagas"], "despesa": s["saidas_pagas"],
             "margem_pct": round(s["saldo"] / s["entradas_pagas"] * 100, 1) if s["entradas_pagas"] else None}

    # Fluxo semanal: semanas do per\u00edodo (realizado) + pr\u00f3ximas 4 (projetado), saldo acumulado a partir do caixa.
    wk0 = parse_date(start) - timedelta(days=parse_date(start).weekday())
    weeks, w = [], wk0
    while w.isoformat() <= max(end, horizon):
        weeks.append({"start": w.isoformat(), "end": (w + timedelta(days=6)).isoformat(),
                      "entradas": 0.0, "saidas": 0.0, "entradas_prev": 0.0, "saidas_prev": 0.0})
        w += timedelta(days=7)
    first = weeks[0]["start"]
    seen = set()
    for t in period + open_txs:
        if t["id"] in seen or t["date"] < first:
            continue
        seen.add(t["id"])
        idx = (parse_date(t["date"]) - wk0).days // 7
        if idx >= len(weeks):
            continue
        paid = t["status"] == "pago"
        # Em aberto e j\u00e1 vencido entra como previsto na semana atual.
        if not paid and t["date"] < today():
            idx = max(0, (td - wk0).days // 7)
            if idx >= len(weeks):
                continue
        key = ("entradas" if t["type"] == "entrada" else "saidas") + ("" if paid else "_prev")
        weeks[idx][key] += t["amount"]
    bal = caixa["valor"]
    for wk in weeks:
        for k in ("entradas", "saidas", "entradas_prev", "saidas_prev"):
            wk[k] = round(wk[k], 2)
        wk["futura"] = wk["start"] > today()
        if wk["end"] >= today():
            bal += wk["entradas_prev"] - wk["saidas_prev"]
            wk["saldo_projetado"] = round(bal, 2)

    aging = []
    for key, label, lo, hi in AGING:
        rows = [t for t in open_txs if t["type"] == "entrada" and (
            t["date"] >= today() if lo is None else lo <= (td - parse_date(t["date"])).days <= hi)]
        aging.append({"key": key, "label": label, "total": round(sum(t["amount"] for t in rows), 2), "count": len(rows)})

    attention = []
    if receber["atrasado_count"]:
        attention.append({"level": "critico", "title": f"{receber['atrasado_count']} recebimento(s) vencido(s)",
                          "detail": f"{brl_py(receber['atrasado'])} precisam de cobran\u00e7a ou revis\u00e3o.", "tab": "entradas"})
    if admin and pagar["atrasado_count"]:
        attention.append({"level": "critico", "title": f"{pagar['atrasado_count']} conta(s) a pagar vencida(s)",
                          "detail": f"{brl_py(pagar['atrasado'])} em atraso.", "tab": "saidas"})
    neg = next((wk for wk in weeks if wk.get("saldo_projetado") is not None and wk["saldo_projetado"] < 0), None)
    if admin and neg:
        attention.append({"level": "critico", "title": "Saldo projetado negativo",
                          "detail": f"Semana de {neg['start'][8:10]}/{neg['start'][5:7]}: {brl_py(neg['saldo_projetado'])} se nada mudar.", "tab": "visao-geral"})
    if admin:
        for b in await budget_status():
            if b["pct"] >= 80:
                attention.append({"level": "critico" if b["pct"] >= 100 else "atencao",
                                  "title": f"Or\u00e7amento de {b['category_name']} em {b['pct']}%",
                                  "detail": f"{brl_py(b['spent'])} de {brl_py(b['monthly_limit'])} no m\u00eas.", "tab": "metas-limites"})
        if pagar["prox7_count"]:
            attention.append({"level": "atencao", "title": f"{pagar['prox7_count']} conta(s) vencem em 7 dias",
                              "detail": f"{brl_py(pagar['prox7'])} a pagar at\u00e9 {in7[8:10]}/{in7[5:7]}.", "tab": "saidas"})
        pend = await M.fin_bank_transactions.count_documents({"handled": None})
        if pend:
            attention.append({"level": "info", "title": f"{pend} movimenta\u00e7\u00e3o(\u00f5es) banc\u00e1ria(s) sem concilia\u00e7\u00e3o",
                              "detail": "H\u00e1 pend\u00eancias na confer\u00eancia banc\u00e1ria.", "tab": "bancos"})
        for it in await M.fin_bank_items.find({}, NO_ID).to_list(50):
            ls = str(it.get("last_sync") or "")[:10]
            if ls and (td - parse_date(ls)).days >= 3:
                attention.append({"level": "atencao", "title": f"Banco {it.get('connector') or ''} sem sincronizar",
                                  "detail": f"\u00daltima sincroniza\u00e7\u00e3o em {ls[8:10]}/{ls[5:7]}.", "tab": "bancos"})
    fees = [t for t in open_txs if t.get("source") == "os" and t.get("os_key") == "taxa"]
    if fees:
        attention.append({"level": "info", "title": f"{len(fees)} taxa(s) de visita a receber",
                          "detail": f"{brl_py(sum(t['amount'] for t in fees))} em taxas n\u00e3o pagas.", "tab": "entradas"})
    order = {"critico": 0, "atencao": 1, "info": 2}
    attention.sort(key=lambda a: order[a["level"]])
    return {"caixa": caixa, "receber": receber, "pagar": pagar, "lucro": lucro, "weeks": weeks,
            "aging": aging, "attention": attention, "today": today()}


def brl_py(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------- Configurações, auditoria, DRE e rentabilidade ----------
class SettingsIn(BaseModel):
    fee_since: Optional[str] = None


@router.get("/finance/settings")
async def read_settings(_: dict = Depends(require_admin)):
    s = await M.fin_settings.find_one({"id": "main"}, NO_ID) or {}
    return {"fee_since": s.get("fee_since")}


@router.put("/finance/settings")
async def save_settings(body: SettingsIn, user: dict = Depends(require_admin)):
    since = (body.fee_since or "").strip() or None
    if since and iso_date(since) != since:
        raise HTTPException(400, "Data inválida")
    before = await M.fin_settings.find_one({"id": "main"}, NO_ID) or {}
    await M.fin_settings.update_one({"id": "main"}, {"$set": {"id": "main", "fee_since": since, "updated_at": now_iso()}}, upsert=True)
    await M.fin_audit_logs.insert_one({"id": new_id(), "entity": "settings", "entity_id": "main", "action": "updated",
                                       "before": {"fee_since": before.get("fee_since")}, "after": {"fee_since": since}, "reason": "",
                                       "actor_id": user.get("id"), "actor_name": user.get("name") or user.get("email"), "created_at": now_iso()})
    _settings["at"] = 0
    _fee_sync["at"] = 0
    sync_fee_entries()  # reaplica a regra das taxas em segundo plano
    return {"fee_since": since}


@router.get("/finance/audit")
async def list_audit(entity: Optional[str] = None, entity_id: Optional[str] = None, limit: int = Query(300, le=2000),
                     _: dict = Depends(require_admin)):
    q = {}
    if entity:
        q["entity"] = entity
    if entity_id:
        q["entity_id"] = entity_id
    return await M.fin_audit_logs.find(q, NO_ID).sort("created_at", -1).to_list(limit)


async def _dre_block(start: str, end: str, regime: str) -> dict:
    q = {"date": {"$gte": start, "$lte": end}}
    if regime == "caixa":
        q["status"] = "pago"
    txs = await M.fin_transactions.find(q, NO_ID).to_list(100000)
    cats = {c["id"]: c async for c in M.fin_categories.find({}, NO_ID)}
    groups, detail = {}, {}
    for t in txs:
        c = cats.get(t.get("category_id"), {"group": "outros" if t["type"] == "entrada" else "outra", "name": "Sem categoria"})
        g = f"{t['type']}:{c['group']}"
        groups[g] = groups.get(g, 0) + t["amount"]
        detail.setdefault(g, {}).setdefault(c["name"], 0)
        detail[g][c["name"]] += t["amount"]
    gv = lambda k: round(groups.get(k, 0), 2)  # noqa: E731
    receita = round(sum(gv(f"entrada:{g}") for g in GROUPS["entrada"]), 2)
    custos = gv("saida:variavel")
    lucro_bruto = round(receita - custos, 2)
    fixas, outras = gv("saida:fixa"), gv("saida:outra")
    resultado = round(lucro_bruto - fixas - outras, 2)
    pct = lambda v: round(v / receita * 100, 1) if receita else None  # noqa: E731
    det = lambda k: [{"name": n, "total": round(v, 2)} for n, v in sorted(detail.get(k, {}).items(), key=lambda x: -x[1])]  # noqa: E731
    lines = [
        {"key": "receita", "label": "Receita bruta", "value": receita, "pct": pct(receita), "level": 0,
         "children": [{"name": {"servico": "Serviços", "peca": "Peças", "taxa": "Taxas de visita", "outros": "Outras receitas"}[g],
                       "total": gv(f"entrada:{g}")} for g in GROUPS["entrada"]]},
        {"key": "custos", "label": "(−) Custos variáveis / diretos", "value": -custos, "pct": pct(custos), "level": 1, "children": det("saida:variavel")},
        {"key": "lucro_bruto", "label": "= Margem de contribuição", "value": lucro_bruto, "pct": pct(lucro_bruto), "level": 0, "strong": True},
        {"key": "fixas", "label": "(−) Despesas fixas", "value": -fixas, "pct": pct(fixas), "level": 1, "children": det("saida:fixa")},
        {"key": "outras", "label": "(−) Outras despesas", "value": -outras, "pct": pct(outras), "level": 1, "children": det("saida:outra")},
        {"key": "resultado", "label": "= Resultado operacional", "value": resultado, "pct": pct(resultado), "level": 0, "strong": True},
    ]
    return {"lines": lines, "receita": receita, "resultado": resultado, "count": len(txs)}


@router.get("/finance/dre")
async def dre(start: str, end: str, regime: str = "caixa", _: dict = Depends(require_admin)):
    """DRE gerencial (não substitui a demonstração contábil oficial). Caixa = só pago; competência = tudo pela data."""
    if regime not in ("caixa", "competencia"):
        raise HTTPException(400, "Regime inválido")
    s, e = parse_date(start), parse_date(end)
    days = (e - s).days + 1
    ps, pe = (s - timedelta(days=days)).isoformat(), (s - timedelta(days=1)).isoformat()
    cur, prev = await _dre_block(start, end, regime), await _dre_block(ps, pe, regime)
    pmap = {ln["key"]: ln["value"] for ln in prev["lines"]}
    for ln in cur["lines"]:
        ln["previous"] = pmap.get(ln["key"], 0)
    return {**cur, "previous_range": {"start": ps, "end": pe}, "regime": regime}


@router.get("/finance/profitability")
async def profitability(start: str, end: str, _: dict = Depends(require_admin)):
    """Rentabilidade por OS e por técnico: receita − custos ligados à OS − comissão estimada."""
    txs = await M.fin_transactions.find({"date": {"$gte": start, "$lte": end}, "os_ids.0": {"$exists": True},
                                         "source": {"$ne": "repasse"}}, NO_ID).to_list(100000)
    techs = {t["id"]: t for t in await technicians()}
    service_cats = {c["id"] async for c in M.fin_categories.find({"type": "entrada", "group": "servico"}, {"_id": 0, "id": 1})}
    by_os = {}
    for t in txs:
        share = t["amount"] / len(t["os_ids"])
        for num in t["os_ids"]:
            r = by_os.setdefault(num, {"os_number": str(num).strip(), "receita": 0.0, "servico": 0.0, "custos": 0.0,
                                       "recebido": 0.0, "technician_id": None})
            if t["type"] == "entrada":
                r["receita"] += share
                r["recebido"] += share if t.get("status") == "pago" else 0
                r["servico"] += share if t.get("category_id") in service_cats else 0
                r["technician_id"] = r["technician_id"] or t.get("technician_id")
            else:
                r["custos"] += share
    rows, by_tech = [], {}
    for r in by_os.values():
        tech = techs.get(r["technician_id"]) or {}
        com = r["servico"] * (tech.get("commission_pct") or 0) / 100
        margem = r["receita"] - r["custos"] - com
        row = {**{k: round(v, 2) if isinstance(v, float) else v for k, v in r.items()}, "comissao": round(com, 2),
               "margem": round(margem, 2), "margem_pct": round(margem / r["receita"] * 100, 1) if r["receita"] else None,
               "technician": tech.get("name") or ""}
        rows.append(row)
        bt = by_tech.setdefault(r["technician_id"] or "-", {"technician": tech.get("name") or "Sem técnico", "os": 0,
                                                             "receita": 0.0, "margem": 0.0})
        bt["os"] += 1
        bt["receita"] += r["receita"]
        bt["margem"] += margem
    rows.sort(key=lambda x: x["margem"])
    receita = sum(r["receita"] for r in rows)
    margem = sum(r["margem"] for r in rows)
    techs_out = sorted(({**v, "receita": round(v["receita"], 2), "margem": round(v["margem"], 2),
                         "ticket": round(v["receita"] / v["os"], 2) if v["os"] else 0,
                         "margem_pct": round(v["margem"] / v["receita"] * 100, 1) if v["receita"] else None}
                        for v in by_tech.values()), key=lambda x: -x["margem"])
    return {"summary": {"os": len(rows), "receita": round(receita, 2), "margem": round(margem, 2),
                        "ticket": round(receita / len(rows), 2) if rows else 0,
                        "margem_pct": round(margem / receita * 100, 1) if receita else None,
                        "deficitarias": sum(1 for r in rows if r["margem"] < 0)},
            "os": rows[:500], "technicians": techs_out}


# ---------- Despesas fixas ----------
class RecurringIn(BaseModel):
    description: str
    amount: float = Field(gt=0)
    category_id: str
    day_of_month: int = Field(ge=1, le=31)
    payment_method: str = "pix"
    active: bool = True


class BudgetIn(BaseModel):
    category_id: str
    monthly_limit: float = Field(gt=0)


class PayoutIn(BaseModel):
    technician_id: str
    amount: float = Field(gt=0)
    period_start: str
    period_end: str
    payment_method: str = "pix"
    note: Optional[str] = None


class TechSettingsIn(BaseModel):
    commission_pct: Optional[float] = Field(None, ge=0, le=100)
    finance_access: Optional[str] = None


async def check_expense_category(cat_id: str):
    cat = await M.fin_categories.find_one({"id": cat_id}, NO_ID)
    if not cat or cat["type"] != "saida":
        raise HTTPException(400, "Escolha uma categoria de saída")


@router.get("/finance/recurring")
async def list_recurring(_: dict = Depends(require_admin)):
    await generate_recurring()
    items = await M.fin_recurring.find({}, NO_ID).sort("day_of_month", 1).to_list(500)
    ym = datetime.now(TZ).strftime("%Y-%m")
    for r in items:
        t = await M.fin_transactions.find_one({"recurring_id": r["id"], "date": {"$regex": f"^{ym}"}}, NO_ID)
        r["current"] = with_status(t) if t else None
    return items


@router.post("/finance/recurring")
async def create_recurring(body: RecurringIn, _: dict = Depends(require_admin)):
    await check_expense_category(body.category_id)
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await M.fin_recurring.insert_one(doc)
    doc.pop("_id", None)
    await generate_recurring()
    return doc


@router.put("/finance/recurring/{rid}")
async def update_recurring(rid: str, body: RecurringIn, _: dict = Depends(require_admin)):
    await check_expense_category(body.category_id)
    if (await M.fin_recurring.update_one({"id": rid}, {"$set": body.model_dump()})).matched_count == 0:
        raise HTTPException(404, "Despesa fixa não encontrada")
    await generate_recurring()
    return {"ok": True}


@router.delete("/finance/recurring/{rid}")
async def delete_recurring(rid: str, _: dict = Depends(require_admin)):
    if (await M.fin_recurring.delete_one({"id": rid})).deleted_count == 0:
        raise HTTPException(404, "Despesa fixa não encontrada")
    await M.fin_transactions.delete_many({"recurring_id": rid, "status": {"$ne": "pago"}, "date": {"$gte": today()}})
    return {"ok": True}


# ---------- Metas e limites ----------
async def budget_status() -> list:
    ym = datetime.now(TZ).strftime("%Y-%m")
    cats = {c["id"]: c async for c in M.fin_categories.find({}, NO_ID)}
    out = []
    async for b in M.fin_budgets.find({}, NO_ID):
        spent = 0.0
        async for t in M.fin_transactions.find({"category_id": b["category_id"], "type": "saida", "date": {"$regex": f"^{ym}"}}, {"_id": 0, "amount": 1}):
            spent += t["amount"]
        c = cats.get(b["category_id"], {})
        pct = round(spent / b["monthly_limit"] * 100, 1)
        out.append({**b, "category_name": c.get("name", "?"), "color": c.get("color", "#64748B"), "spent": round(spent, 2),
                    "pct": pct, "level": "estourado" if pct >= 100 else "alerta" if pct >= 80 else "ok", "month": ym})
    return sorted(out, key=lambda x: -x["pct"])


@router.get("/finance/budgets")
async def list_budgets(_: dict = Depends(require_admin)):
    return await budget_status()


@router.post("/finance/budgets")
async def upsert_budget(body: BudgetIn, _: dict = Depends(require_admin)):
    await check_expense_category(body.category_id)
    await M.fin_budgets.update_one({"category_id": body.category_id},
                                   {"$set": {"monthly_limit": body.monthly_limit}, "$setOnInsert": {"id": new_id()}}, upsert=True)
    return {"ok": True}


@router.delete("/finance/budgets/{bid}")
async def delete_budget(bid: str, _: dict = Depends(require_admin)):
    if (await M.fin_budgets.delete_one({"id": bid})).deleted_count == 0:
        raise HTTPException(404, "Limite não encontrado")
    return {"ok": True}


# ---------- Técnicos: fechamento ----------
async def routed_os(start: str, end: str, techs: list) -> dict:
    """OSs dos roteiros do período agrupadas por técnico (id) — a contagem real de atendimentos."""
    routes = await S.routes.find({}, NO_ID).to_list(5000)
    out = {}
    for r in routes:
        d = str(r.get("date") or "")[:10]
        if not d or d < start or d > end:
            continue
        # "Felipe/Tulio", "Felipe e Pedro": conta para cada técnico do roteiro.
        parts = re.split(r"\s*(?:/|,|&|\be\b)\s*", str(r.get("technician") or ""))
        tids = {t for t in (match_tech(p, techs) for p in parts) if t}
        for o in (r.get("orders") or []) if tids else []:
            num = str(o.get("os_number") or "").strip()
            if not num:
                continue
            done = o.get("stop_status") == "completed" or bool((o.get("conclusion") or "").strip())
            for tid in tids:
                bucket = out.setdefault(tid, {})
                prev = bucket.get(num)
                if prev and (prev["concluida"] or not done):
                    continue
                bucket[num] = {"os_number": num, "customer_name": o.get("customer_name") or "", "neighborhood": o.get("neighborhood") or "",
                           "city": o.get("city") or "", "route_name": r.get("name") or "", "route_date": d,
                           "route_status": r.get("status") or "", "conclusion": o.get("conclusion") or "", "concluida": done}
    return {k: sorted(v.values(), key=lambda x: (x["route_date"], x["os_number"])) for k, v in out.items()}


@router.get("/finance/technicians/{tech_id}/os")
async def technician_routed_os(tech_id: str, start: str, end: str, user: dict = Depends(require_finance)):
    if user["role"] != "admin" and user["id"] != tech_id:
        raise HTTPException(403, "Sem acesso")
    return (await routed_os(start, end, await technicians())).get(tech_id, [])


@router.get("/finance/technicians")
async def technicians_closing(start: str, end: str, user: dict = Depends(require_finance)):
    techs = await technicians()
    routed = await routed_os(start, end, techs)
    if user["role"] != "admin":
        techs = [t for t in techs if t["id"] == user["id"]]
    service_cats = [c["id"] async for c in M.fin_categories.find({"type": "entrada", "group": "servico"}, {"_id": 0, "id": 1})]
    out = []
    for tech in techs:
        txs = await M.fin_transactions.find({"technician_id": tech["id"], "type": "entrada",
                                             "date": {"$gte": start, "$lte": end}}, NO_ID).to_list(5000)
        txs = [with_status(t) for t in txs]
        servico_pago = sum(t["amount"] for t in txs if t["status"] == "pago" and t["category_id"] in service_cats)
        commission = servico_pago * (tech.get("commission_pct") or 0) / 100
        # Repasse conta só no fechamento cujo período o contém inteiro (nunca em dois fechamentos).
        payouts = await M.fin_payouts.find({"technician_id": tech["id"], "period_start": {"$gte": start},
                                            "period_end": {"$lte": end}, "reversed": {"$ne": True}}, NO_ID).to_list(500)
        repassado = sum(p["amount"] for p in payouts)
        out.append({"technician_id": tech["id"], "name": tech["name"], "commission_pct": tech.get("commission_pct", 0),
                    "finance_access": tech.get("finance_access"), "os_count": len({o for t in txs for o in t.get("os_ids", [])}),
                    "os_routed": len(routed.get(tech["id"], [])),
                    "os_routed_done": sum(1 for x in routed.get(tech["id"], []) if x["concluida"]),
                    "gerado": round(sum(t["amount"] for t in txs), 2),
                    "recebido": round(sum(t["amount"] for t in txs if t["status"] == "pago"), 2),
                    "servico_total": round(sum(t["amount"] for t in txs if t["category_id"] in service_cats), 2),
                    "servico_pago": round(servico_pago, 2), "comissao": round(commission, 2),
                    "repassado": round(repassado, 2), "a_repassar": round(max(commission - repassado, 0), 2)})
    return sorted(out, key=lambda x: -x["gerado"])


@router.put("/finance/technicians/{tech_id}/settings")
async def tech_settings(tech_id: str, body: TechSettingsIn, _: dict = Depends(require_admin)):
    data = body.model_dump(exclude_none=True)
    if "finance_access" in data and data["finance_access"] not in ("own", "none"):
        raise HTTPException(400, "Acesso inválido")
    if (await M.users.update_one({"id": tech_id, "role": "technician"}, {"$set": data})).matched_count == 0:
        raise HTTPException(404, "Técnico não encontrado")
    return {"ok": True}


@router.get("/finance/payouts")
async def list_payouts(technician_id: Optional[str] = None, user: dict = Depends(require_finance)):
    q = {}
    if user["role"] != "admin":
        q["technician_id"] = user["id"]
    elif technician_id:
        q["technician_id"] = technician_id
    return await M.fin_payouts.find(q, NO_ID).sort("paid_at", -1).to_list(500)


@router.post("/finance/payouts/{pid}/reverse")
async def reverse_payout(pid: str, user: dict = Depends(require_admin)):
    p = await M.fin_payouts.find_one({"id": pid, "reversed": {"$ne": True}}, NO_ID)
    if not p:
        raise HTTPException(404, "Repasse não encontrado ou já estornado")
    tx = await M.fin_transactions.find_one({"id": p.get("transaction_id")}, NO_ID)
    if tx and tx.get("reconciled"):
        raise HTTPException(409, "A saída do repasse já foi conferida com o extrato")
    await M.fin_payouts.update_one({"id": pid}, {"$set": {"reversed": True, "reversed_at": now_iso()}})
    if tx:
        await M.fin_transactions.delete_one({"id": tx["id"]})
    await audit(user, "payout", pid, "reversed", {**p, **(tx or {})}, None)
    return {"ok": True}


@router.post("/finance/payouts")
async def create_payout(body: PayoutIn, user: dict = Depends(require_admin)):
    if body.payment_method not in METHODS:
        raise HTTPException(400, "Forma de pagamento inválida")
    if body.period_start > body.period_end:
        raise HTTPException(400, "Período inválido")
    tech = next((t for t in await technicians() if t["id"] == body.technician_id), None)
    if not tech:
        raise HTTPException(404, "Técnico não encontrado")
    overlap = await M.fin_payouts.find_one({"technician_id": tech["id"], "reversed": {"$ne": True},
                                            "period_start": {"$lte": body.period_end}, "period_end": {"$gte": body.period_start},
                                            "$nor": [{"period_start": body.period_start, "period_end": body.period_end}]}, NO_ID)
    if overlap:
        raise HTTPException(409, f"Já existe repasse no período {fmt_br(overlap['period_start'])} a {fmt_br(overlap['period_end'])}, "
                                 "que se sobrepõe a este. Use o mesmo período ou um período sem sobreposição.")
    row = next((r for r in await technicians_closing(body.period_start, body.period_end, user) if r["technician_id"] == tech["id"]), None)
    devido = row["a_repassar"] if row else 0
    if body.amount > devido + 0.009:
        raise HTTPException(400, f"Valor acima do saldo a repassar no período ({brl_py(devido)})")
    cats = await system_cats()
    tx_id, pid = new_id(), new_id()
    tx = {
        "id": tx_id, "type": "saida", "date": today(), "amount": body.amount,
        "description": f"Repasse de comissão - {tech['name']} ({fmt_br(body.period_start)} a {fmt_br(body.period_end)})",
        "category_id": cats.get("comissao"), "payment_method": body.payment_method, "status": "pago", "os_ids": [],
        "technician_id": tech["id"], "part_name": None, "reconciled": False, "source": "repasse", "payout_id": pid,
        "recurring_id": None, "created_at": now_iso()}
    doc = {"id": pid, **body.model_dump(), "technician_name": tech["name"], "transaction_id": tx_id, "paid_at": today(),
           "commission_snapshot": row, "created_by": user.get("id")}
    await M.fin_transactions.insert_one(tx)
    try:
        await M.fin_payouts.insert_one(doc)
    except Exception:
        await M.fin_transactions.delete_one({"id": tx_id})
        raise
    doc.pop("_id", None)
    await audit(user, "payout", pid, "created", None, {**doc, "amount": body.amount})
    return doc


def fmt_br(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else iso


# ---------- Bancos (Pluggy) ----------
PLUGGY_BASE = "https://api.pluggy.ai"
_pluggy_cache = {"key": None, "exp": 0}


async def pluggy_key() -> str:
    if _pluggy_cache["key"] and _pluggy_cache["exp"] > time.time():
        return _pluggy_cache["key"]
    cid, secret = os.environ.get("PLUGGY_ID"), os.environ.get("PLUGGY_SECRET")
    if not cid or not secret:
        raise HTTPException(503, "Pluggy não configurado (PLUGGY_ID / PLUGGY_SECRET)")
    async with httpx.AsyncClient(base_url=PLUGGY_BASE, timeout=20) as c:
        r = await c.post("/auth", json={"clientId": cid, "clientSecret": secret})
    if r.is_error:
        raise HTTPException(424, f"Pluggy recusou as credenciais ({r.status_code})")
    _pluggy_cache.update(key=r.json()["apiKey"], exp=time.time() + 100 * 60)
    return _pluggy_cache["key"]


async def pluggy(method: str, path: str, **kw) -> dict:
    key = await pluggy_key()
    async with httpx.AsyncClient(base_url=PLUGGY_BASE, timeout=30) as c:
        r = await c.request(method, path, headers={"X-API-KEY": key}, **kw)
    if r.is_error:
        log.error("Pluggy %s %s -> %s %s", method, path, r.status_code, r.text[:200])
        raise HTTPException(424, f"Erro no Pluggy ({r.status_code})")
    return r.json()


class ItemIn(BaseModel):
    item_id: str


class LinkBankIn(BaseModel):
    transaction_id: str
    allow_diff: bool = False


class CreateFromBankIn(BaseModel):
    category_id: str
    description: Optional[str] = None
    os_ids: List[str] = []
    payment_method: str = "pix"


async def import_item(item_id: str) -> dict:
    accounts = (await pluggy("GET", "/accounts", params={"itemId": item_id})).get("results", [])
    count = 0
    for acc in accounts:
        await M.fin_bank_accounts.update_one({"id": acc["id"]}, {"$set": {
            "id": acc["id"], "item_id": item_id, "name": acc.get("name"), "number": acc.get("number"),
            "type": acc.get("type"), "balance": acc.get("balance"), "currency": acc.get("currencyCode")}}, upsert=True)
        params = {"accountId": acc["id"], "pageSize": 500}
        while True:
            page = await pluggy("GET", "/v2/transactions", params=params)
            for tx in page.get("results", []):
                amount = float(tx.get("amount") or 0)
                kind = "entrada" if (tx.get("type") == "CREDIT" or (tx.get("type") is None and amount > 0)) else "saida"
                await M.fin_bank_transactions.update_one({"id": tx["id"]}, {
                    "$set": {"id": tx["id"], "item_id": item_id, "account_id": acc["id"], "account_name": acc.get("name"),
                             "date": (tx.get("date") or "")[:10], "amount": round(abs(amount), 2), "type": kind,
                             "description": tx.get("description") or ""},
                    "$setOnInsert": {"handled": None, "linked_tx_id": None, "imported_at": now_iso()}}, upsert=True)
                count += 1
            nxt = page.get("next")
            if not nxt or "after=" not in nxt:
                break
            params = {"accountId": acc["id"], "pageSize": 500, "after": nxt.split("after=", 1)[1].split("&")[0]}
    await M.fin_bank_items.update_one({"id": item_id}, {"$set": {"last_sync": now_iso()}})
    return {"accounts": len(accounts), "transactions": count}


@router.get("/banks/status")
async def banks_status(_: dict = Depends(require_admin)):
    return {"configured": bool(os.environ.get("PLUGGY_ID") and os.environ.get("PLUGGY_SECRET"))}


@router.post("/banks/connect-token")
async def connect_token(user: dict = Depends(require_admin)):
    data = await pluggy("POST", "/connect_token", json={"options": {"clientUserId": user["id"], "avoidDuplicates": True}})
    return {"accessToken": data["accessToken"]}


@router.post("/banks/items")
async def save_item(body: ItemIn, _: dict = Depends(require_admin)):
    item = await pluggy("GET", f"/items/{body.item_id}")
    await M.fin_bank_items.update_one({"id": body.item_id}, {"$set": {
        "id": body.item_id, "connector": (item.get("connector") or {}).get("name", "Banco"),
        "status": item.get("status"), "connected_at": now_iso()}}, upsert=True)
    return await import_item(body.item_id)


@router.post("/banks/items/{item_id}/sync")
async def sync_item(item_id: str, _: dict = Depends(require_admin)):
    if not await M.fin_bank_items.find_one({"id": item_id}):
        raise HTTPException(404, "Conexão não encontrada")
    return await import_item(item_id)


@router.delete("/banks/items/{item_id}")
async def remove_item(item_id: str, _: dict = Depends(require_admin)):
    await M.fin_bank_items.delete_one({"id": item_id})
    await M.fin_bank_accounts.delete_many({"item_id": item_id})
    await M.fin_bank_transactions.delete_many({"item_id": item_id, "handled": None})
    return {"ok": True}


@router.get("/banks/items")
async def list_items(_: dict = Depends(require_admin)):
    items = await M.fin_bank_items.find({}, NO_ID).to_list(50)
    for it in items:
        it["accounts"] = await M.fin_bank_accounts.find({"item_id": it["id"]}, NO_ID).to_list(50)
    return items


async def suggestions(bt: dict) -> list:
    lo, hi = bt["amount"] - 0.01, bt["amount"] + 0.01
    try:
        d = parse_date(bt["date"])
    except ValueError:
        return []
    rng = {"$gte": (d - timedelta(days=5)).isoformat(), "$lte": (d + timedelta(days=5)).isoformat()}
    out = []
    async for t in M.fin_transactions.find({"type": bt["type"], "amount": {"$gte": lo, "$lte": hi}, "date": rng,
                                            "bank_tx_id": {"$in": [None]}}, NO_ID).limit(3):
        out.append({"kind": "lancamento", "transaction_id": t["id"], "label": t["description"], "date": t["date"],
                    "amount": t["amount"], "os_ids": t.get("os_ids", [])})
    if bt["type"] == "entrada":
        async for v in M.fin_os_values.find({"date": rng}, NO_ID):
            if abs(values_total(v) - bt["amount"]) <= 0.01:
                out.append({"kind": "os", "os_id": v["os_number"], "label": f"OS #{str(v['os_number']).strip()}",
                            "date": v["date"], "amount": round(values_total(v), 2)})
    return out


@router.get("/banks/transactions")
async def bank_transactions(pending_only: bool = False, _: dict = Depends(require_admin)):
    txs = await M.fin_bank_transactions.find({"handled": None} if pending_only else {}, NO_ID).sort("date", -1).to_list(1000)
    for t in txs:
        t["suggestions"] = await suggestions(t) if not t.get("handled") else []
    return txs


async def get_bank_tx(bid: str) -> dict:
    bt = await M.fin_bank_transactions.find_one({"id": bid}, NO_ID)
    if not bt:
        raise HTTPException(404, "Movimento não encontrado")
    if bt.get("handled"):
        raise HTTPException(409, "Movimento já tratado")
    return bt


@router.post("/banks/transactions/{bid}/link")
async def link_bank(bid: str, body: LinkBankIn, user: dict = Depends(require_admin)):
    bt = await get_bank_tx(bid)
    tx = await M.fin_transactions.find_one({"id": body.transaction_id}, NO_ID)
    if not tx:
        raise HTTPException(404, "Lançamento não encontrado")
    if tx["type"] != bt["type"]:
        raise HTTPException(400, "Tipo diferente: o movimento só concilia com lançamento do mesmo tipo (entrada/saída)")
    if tx.get("bank_tx_id") and tx["bank_tx_id"] != bid:
        raise HTTPException(409, "Este lançamento já está conciliado com outro movimento bancário")
    diff = round(abs(float(tx["amount"]) - float(bt["amount"])), 2)
    if diff > 0.01 and not body.allow_diff:
        raise HTTPException(422, f"Valores diferentes: extrato {brl_py(bt['amount'])} x lançamento {brl_py(tx['amount'])}")
    # Trava otimista: só concilia se o movimento ainda estiver pendente.
    res = await M.fin_bank_transactions.update_one({"id": bid, "handled": None},
                                                   {"$set": {"handled": "vinculado", "linked_tx_id": tx["id"], "linked_by": user.get("id"), "linked_at": now_iso()}})
    if res.matched_count == 0:
        raise HTTPException(409, "Movimento bancário já foi tratado")
    await M.fin_transactions.update_one({"id": tx["id"]}, {"$set": {"bank_tx_id": bid, "reconciled": True, "status": "pago", "bank_diff": diff or None}})
    await audit(user, "reconciliation", bid, "linked", tx, {**tx, "bank_tx_id": bid, "reconciled": True, "status": "pago"},
                reason=f"diferença {brl_py(diff)}" if diff > 0.01 else "")
    return {"ok": True}


@router.post("/banks/transactions/{bid}/create")
async def create_from_bank(bid: str, body: CreateFromBankIn, user: dict = Depends(require_admin)):
    bt = await get_bank_tx(bid)
    if bt.get("handled"):
        raise HTTPException(409, "Movimento bancário já foi tratado")
    cat = await M.fin_categories.find_one({"id": body.category_id}, NO_ID)
    if not cat or cat["type"] != bt["type"]:
        raise HTTPException(400, "Categoria inválida para este movimento")
    ids = await check_os_ids(body.os_ids)
    tech = (await os_rows(ids=ids[:1]))[0]["technician_id"] if ids else None
    tx_id = new_id()
    await M.fin_transactions.insert_one({
        "id": tx_id, "type": bt["type"], "date": bt["date"], "amount": bt["amount"],
        "description": body.description or bt["description"], "category_id": body.category_id,
        "payment_method": body.payment_method, "status": "pago", "os_ids": ids, "technician_id": tech,
        "part_name": None, "reconciled": True, "source": "banco", "recurring_id": None, "bank_tx_id": bid,
        "created_at": now_iso()})
    await M.fin_bank_transactions.update_one({"id": bid}, {"$set": {"handled": "criado", "linked_tx_id": tx_id}})
    await audit(user, "reconciliation", bid, "created_tx", None, {"type": bt["type"], "amount": bt["amount"], "date": bt["date"], "bank_tx_id": bid})
    return {"ok": True, "transaction_id": tx_id}


@router.post("/banks/transactions/{bid}/ignore")
async def ignore_bank(bid: str, user: dict = Depends(require_admin)):
    await get_bank_tx(bid)
    if (await M.fin_bank_transactions.update_one({"id": bid, "handled": None}, {"$set": {"handled": "ignorado"}})).matched_count == 0:
        raise HTTPException(409, "Movimento bancário já foi tratado")
    await audit(user, "reconciliation", bid, "ignored")
    return {"ok": True}


_tasks = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def sync_all(trigger: str):
    results = []
    async for it in M.fin_bank_items.find({}, {"_id": 0, "id": 1}):
        try:
            results.append({"item_id": it["id"], **(await import_item(it["id"]))})
        except Exception as exc:
            log.error("sync item %s falhou: %s", it["id"], exc)
            results.append({"item_id": it["id"], "error": str(getattr(exc, "detail", exc))})
    await M.fin_sync_runs.insert_one({"trigger": trigger, "finished_at": now_iso(), "results": results})


@router.post("/finance/cron/bank-sync")
async def cron_bank_sync(request: Request):
    secret = os.environ.get("WEBHOOK_CRON_SECRET", "")
    auth = request.headers.get("authorization", "")
    if not secret or not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], secret):
        raise HTTPException(401, "Unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    run_id = request.headers.get("x-webhook-id") or (body.get("run_id") if isinstance(body, dict) else None)
    if run_id:
        res = await M.fin_cron_runs.update_one({"run_id": run_id}, {"$setOnInsert": {"run_id": run_id, "at": now_iso()}}, upsert=True)
        if res.upserted_id is None:
            return {"ok": True, "duplicate": True}
    spawn(sync_all("cron"))
    return {"ok": True}


async def handle_event(evt: dict):
    event, item_id = evt.get("event", ""), evt.get("itemId")
    if not item_id or not await M.fin_bank_items.find_one({"id": item_id}):
        return
    if event == "item/deleted":
        await M.fin_bank_items.delete_one({"id": item_id})
        await M.fin_bank_accounts.delete_many({"item_id": item_id})
        await M.fin_bank_transactions.delete_many({"item_id": item_id, "handled": None})
    elif event == "transactions/deleted":
        await M.fin_bank_transactions.delete_many({"id": {"$in": evt.get("transactionIds") or []}, "handled": None})
    elif event in ("item/updated", "transactions/created", "transactions/updated"):
        await import_item(item_id)
    if event.startswith("item/") and event != "item/deleted":
        try:
            item = await pluggy("GET", f"/items/{item_id}")
            await M.fin_bank_items.update_one({"id": item_id}, {"$set": {"status": item.get("status")}})
        except Exception as exc:
            log.warning("item status %s: %s", item_id, exc)


@router.post("/banks/pluggy-webhook")
async def pluggy_webhook(request: Request, token: str = ""):
    secret = os.environ.get("PLUGGY_WEBHOOK_SECRET", "")
    if not secret or not hmac.compare_digest(token, secret):
        raise HTTPException(401, "Unauthorized")
    try:
        evt = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid body")
    if not isinstance(evt, dict):
        raise HTTPException(400, "Invalid body")
    if evt.get("clientId") and evt["clientId"] != os.environ.get("PLUGGY_ID"):
        return JSONResponse({"ok": True, "ignored": True})
    event_id = evt.get("eventId")
    if event_id:
        res = await M.fin_webhook_events.update_one({"event_id": event_id}, {"$setOnInsert": {
            "event_id": event_id, "event": evt.get("event"), "item_id": evt.get("itemId"), "received_at": now_iso()}}, upsert=True)
        if res.upserted_id is None:
            return {"ok": True, "duplicate": True}
    spawn(handle_event(evt))
    return {"ok": True}


def webhook_url(request: Request) -> str:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host", "")).split(",")[0].strip()
    return f"https://{host}/api/banks/pluggy-webhook?token={os.environ.get('PLUGGY_WEBHOOK_SECRET', '')}"


@router.get("/banks/webhook")
async def webhook_info(request: Request, _: dict = Depends(require_admin)):
    url = webhook_url(request)
    registered = False
    try:
        registered = any(h.get("url") == url for h in (await pluggy("GET", "/webhooks")).get("results", []))
    except HTTPException:
        pass
    last_events = await M.fin_webhook_events.find({}, NO_ID).sort("received_at", -1).limit(5).to_list(5)
    last_sync = await M.fin_sync_runs.find_one({}, NO_ID, sort=[("finished_at", -1)])
    return {"url": url, "registered": registered, "last_events": last_events, "last_sync": last_sync}


@router.post("/banks/webhook/register")
async def register_webhook(request: Request, _: dict = Depends(require_admin)):
    url = webhook_url(request)
    if any(h.get("url") == url for h in (await pluggy("GET", "/webhooks")).get("results", [])):
        return {"ok": True, "already": True}
    await pluggy("POST", "/webhooks", json={"event": "all", "url": url})
    return {"ok": True}


@router.post("/banks/sync-all")
async def sync_all_now(_: dict = Depends(require_admin)):
    spawn(sync_all("manual"))
    return {"ok": True}


# ---------- Assistentes de IA ----------
FUNCTIONS = {"resumo_semanal": "Resumo semanal"}
SYSTEM = ("Você é um assistente financeiro de uma empresa de assistência técnica. Escreva em português do Brasil, "
          "em markdown simples, com: um parágrafo de resumo, 3 a 5 destaques em tópicos, alertas que exigem ação "
          "e uma recomendação para a próxima semana. Use apenas os números fornecidos, nunca invente dados.")


class AssistantIn(BaseModel):
    name: str
    function: str = "resumo_semanal"
    instructions: Optional[str] = ""
    active: bool = True


async def week_context() -> str:
    end = parse_date(today())
    start = end - timedelta(days=6)
    pstart, pend = start - timedelta(days=7), start - timedelta(days=1)
    cats = {c["id"]: c["name"] async for c in M.fin_categories.find({}, NO_ID)}
    names = {t["id"]: t["name"] for t in await technicians()}

    async def load(a, b):
        return [with_status(t) for t in await M.fin_transactions.find(
            {"date": {"$gte": a.isoformat(), "$lte": b.isoformat()}}, NO_ID).to_list(10000)]
    cur, prev = await load(start, end), await load(pstart, pend)

    def tot(txs, typ, paid=None):
        return round(sum(t["amount"] for t in txs if t["type"] == typ and (paid is None or (t["status"] == "pago") == paid)), 2)
    lines = [f"Período: {start} a {end}. Semana anterior: {pstart} a {pend}.",
             f"Entradas recebidas: R$ {tot(cur, 'entrada', True)} (anterior R$ {tot(prev, 'entrada', True)})",
             f"Entradas a receber: R$ {tot(cur, 'entrada', False)}",
             f"Saídas pagas: R$ {tot(cur, 'saida', True)} (anterior R$ {tot(prev, 'saida', True)})",
             f"Saídas previstas/em atraso: R$ {tot(cur, 'saida', False)}", "Por categoria:"]
    bycat = {}
    for t in cur:
        k = (t["type"], cats.get(t["category_id"], "Sem categoria"))
        bycat[k] = bycat.get(k, 0) + t["amount"]
    lines += [f"- {k[0]} / {k[1]}: R$ {round(v, 2)}" for k, v in sorted(bycat.items(), key=lambda x: -x[1])]
    tech = {}
    for t in cur:
        if t["type"] == "entrada" and t.get("technician_id"):
            tech[t["technician_id"]] = tech.get(t["technician_id"], 0) + t["amount"]
    lines.append("Técnicos (valor gerado): " + (", ".join(f"{names.get(k, '?')} R$ {round(v, 2)}" for k, v in tech.items()) or "nenhum"))
    late = await M.fin_transactions.find({"status": {"$ne": "pago"}, "date": {"$lt": today()}}, NO_ID).to_list(200)
    lines.append(f"Lançamentos em atraso (total geral): {len(late)}, somando R$ {round(sum(t['amount'] for t in late), 2)}")
    lines += [f"- {t['type']} {t['date']} {t['description']} R$ {t['amount']}" for t in late[:10]]
    return "\n".join(lines)


async def generate_text(prompt: str, instructions: str) -> tuple:
    system = SYSTEM + (f"\nInstruções do administrador: {instructions}" if instructions else "")
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(api_key=os.environ["EMERGENT_LLM_KEY"], session_id=new_id(), system_message=system) \
            .with_model("openai", "gpt-6-luna")
        text = await chat.send_message(UserMessage(text=prompt))
        if text and str(text).strip():
            return str(text), "gpt-6-luna"
    except Exception as exc:
        log.warning("GPT-6 Luna falhou, usando Ollama: %s", exc)
    key, base = os.environ.get("OLLAMA_API_KEY"), os.environ.get("OLLAMA_BASE_URL")
    if not key or not base:
        raise HTTPException(424, "GPT-6 Luna indisponível e Ollama não configurado")
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{base.rstrip('/')}/api/chat", headers={"Authorization": f"Bearer {key}"}, json={
            "model": os.environ.get("OLLAMA_MODEL", "gpt-oss:120b"), "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
    if r.is_error:
        raise HTTPException(424, f"Ollama falhou ({r.status_code})")
    return r.json()["message"]["content"], "ollama/" + os.environ.get("OLLAMA_MODEL", "gpt-oss:120b")


@router.get("/assistants")
async def list_assistants(_: dict = Depends(require_admin)):
    items = await M.fin_assistants.find({}, NO_ID).sort("created_at", 1).to_list(100)
    for a in items:
        a["last_run"] = await M.fin_assistant_runs.find_one({"assistant_id": a["id"]}, NO_ID, sort=[("created_at", -1)])
    return {"assistants": items, "functions": FUNCTIONS}


@router.post("/assistants")
async def create_assistant(body: AssistantIn, _: dict = Depends(require_admin)):
    if body.function not in FUNCTIONS:
        raise HTTPException(400, "Função não disponível")
    doc = {"id": new_id(), **body.model_dump(), "created_at": now_iso()}
    await M.fin_assistants.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.put("/assistants/{aid}")
async def update_assistant(aid: str, body: AssistantIn, _: dict = Depends(require_admin)):
    if body.function not in FUNCTIONS:
        raise HTTPException(400, "Função não disponível")
    if (await M.fin_assistants.update_one({"id": aid}, {"$set": body.model_dump()})).matched_count == 0:
        raise HTTPException(404, "Assistente não encontrado")
    return {"ok": True}


@router.delete("/assistants/{aid}")
async def delete_assistant(aid: str, _: dict = Depends(require_admin)):
    if (await M.fin_assistants.delete_one({"id": aid})).deleted_count == 0:
        raise HTTPException(404, "Assistente não encontrado")
    await M.fin_assistant_runs.delete_many({"assistant_id": aid})
    return {"ok": True}


@router.post("/assistants/{aid}/run")
async def run_assistant(aid: str, _: dict = Depends(require_admin)):
    a = await M.fin_assistants.find_one({"id": aid}, NO_ID)
    if not a:
        raise HTTPException(404, "Assistente não encontrado")
    context = await week_context()
    text, model = await generate_text(f"Dados da semana:\n{context}\n\nEscreva o resumo semanal.", a.get("instructions", ""))
    run = {"id": new_id(), "assistant_id": aid, "content": text, "model": model, "created_at": now_iso()}
    await M.fin_assistant_runs.insert_one(run)
    run.pop("_id", None)
    return run


@router.get("/assistants/{aid}/runs")
async def list_runs(aid: str, _: dict = Depends(require_admin)):
    return await M.fin_assistant_runs.find({"assistant_id": aid}, NO_ID).sort("created_at", -1).to_list(50)


# ---------- Inicialização ----------
async def startup():
    sync_fee_entries()
    if await M.fin_categories.count_documents({}) == 0:
        await M.fin_categories.insert_many([{"id": new_id(), "type": t, "group": g, "name": n, "color": c, "system_key": k, "order": i}
                                            for i, (t, g, n, c, k) in enumerate(DEFAULT_CATEGORIES)])
    if await M.fin_assistants.count_documents({}) == 0:
        await M.fin_assistants.insert_one({"id": new_id(), "name": "Resumo da semana", "function": "resumo_semanal",
                                           "instructions": "Seja direto, destaque o que exige ação.", "active": True,
                                           "created_at": now_iso()})
    for col, keys in {"fin_transactions": ["id", "date", "technician_id", "os_ids", "recurring_id"],
                      "fin_categories": ["id"], "fin_os_values": ["os_number"], "fin_bank_transactions": ["id"]}.items():
        for k in keys:
            try:
                await M[col].create_index(k, unique=k in ("id", "os_number"))
            except Exception as exc:
                log.error("index %s.%s: %s", col, k, exc)
    try:
        await M.fin_transactions.create_index("recurring_key", unique=True, partialFilterExpression={"recurring_key": {"$type": "string"}})
        await M.fin_audit_logs.create_index([("created_at", -1)])
        await M.fin_audit_logs.create_index("entity_id")
    except Exception as exc:
        log.error("index extra: %s", exc)
    await generate_recurring()


def setup_finance(api_router, mongo_db, supa_db, verify_token):
    global M, S, _verify
    M, S, _verify = mongo_db, supa_db, verify_token
    api_router.include_router(router)
