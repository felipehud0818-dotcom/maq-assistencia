"""
Supabase database adapter - provides MongoDB-like interface over Supabase REST API.
Used by all backend modules (server.py, routes_module.py, inventory_routes.py, etc.)
"""
import os
import re
import json
import logging
import httpx
from urllib.parse import quote as _url_quote

logger = logging.getLogger(__name__)


def _pgrst_encode(v):
    """URL-encoda um valor para uso seguro em query strings PostgREST.
    Necessário porque a col=eq.<value> vai direto na URL — se `value`
    contém `#`, `&`, `?`, espaço, etc., a URL quebra.

    Ex: os_number='#71693600 ' → '%2371693600%20'. Sem isso, o `#` era
    interpretado como fragment e o filtro chegava vazio ao Supabase.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return _url_quote(str(v), safe='')

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ---------- Política anti-estouro de egress ----------
# 1) Leituras idênticas repetidas em 30s saem do cache em memória (invalidado em qualquer escrita na tabela).
# 2) Listas de service_orders sem projeção nunca trazem as fotos base64 (photo_1..9 = ~80% do tamanho da tabela).
import time as _time
_READ_TTL = 30
_read_cache: dict = {}
SO_LIGHT_COLUMNS = (
    "id,os_number,customer_name,registration_date,attendance_date,status,sub_status,micro_status,service_type,source,"
    "brand,model,product,address,number,complement,neighborhood,city,state,cep,reference_point,phone1,phone2,landline,"
    "email,technician,serial_number,batch,reported_defect,technical_diagnosis,installation_correct,required_parts,"
    "voltage_f1f2,voltage_f1_terra,voltage_f2_terra,observations,priority,visit_fee,lat,lng,sort_date,status_orcamento,"
    "conclusao_orcamento,assinafy_status,assinafy_document_id,scheduling_history,print_count,share_token,created_at,"
    "updated_at,signature_client_name,signature_client_cpf,signature_date,signature_tech_name,visit_fee_paid,site_info")


def _cache_get(table: str, url: str):
    hit = _read_cache.get(table, {}).get(url)
    if hit and _time.time() - hit[0] < _READ_TTL:
        return hit[1]
    return None


def _cache_put(table: str, url: str, data):
    bucket = _read_cache.setdefault(table, {})
    if len(bucket) > 300:
        bucket.clear()
    bucket[url] = (_time.time(), data)


def _bust(table: str):
    _read_cache.pop(table, None)

# Shared connection pool - prevents ConnectTimeout under load
_http_client = None

def _get_client():
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _val(v):
    """Format a value for PostgREST filter"""
    if v is None:
        return "is.null"
    if isinstance(v, bool):
        return f"is.{str(v).lower()}"
    return str(v)


class SupabaseCollection:
    """Wraps a Supabase table with MongoDB-like find/insert/update/delete methods."""

    def __init__(self, table_name: str, field_map: dict = None):
        self.table = table_name
        self.url = f"{SUPABASE_URL}/rest/v1/{table_name}"
        # field_map: renames MongoDB field names to PostgreSQL column names
        self.field_map = field_map or {}
        self.reverse_map = {v: k for k, v in self.field_map.items()}

    def _map_field(self, f):
        return self.field_map.get(f, f)

    def _unmap_field(self, f):
        return self.reverse_map.get(f, f)

    def _map_doc(self, doc):
        """Rename doc keys from Mongo names to PG column names"""
        if not self.field_map:
            return doc
        return {self.field_map.get(k, k): v for k, v in doc.items()}

    def _unmap_doc(self, doc):
        """Rename doc keys from PG column names back to Mongo names"""
        if not self.field_map:
            return doc
        return {self.reverse_map.get(k, k): v for k, v in doc.items()}

    def _build_filters(self, query: dict) -> str:
        """Convert MongoDB-style query to PostgREST query string params"""
        parts = []
        or_conditions = []

        for key, val in query.items():
            if key == "_raw_or":
                # Direct PostgREST or clause (pre-built)
                or_conditions.append(f"or=({val})")
                continue
            if key == "$or":
                # Handle $or: list of conditions
                or_parts = []
                for cond in val:
                    for ck, cv in cond.items():
                        col = self._map_field(ck)
                        if isinstance(cv, dict):
                            if "$regex" in cv:
                                pattern = cv["$regex"]
                                or_parts.append(f"{col}.ilike.*{_pgrst_encode(pattern)}*")
                            elif "$in" in cv:
                                vals = ",".join(_pgrst_encode(x) for x in cv["$in"])
                                or_parts.append(f"{col}.in.({vals})")
                        else:
                            or_parts.append(f"{col}.eq.{_pgrst_encode(cv)}")
                if or_parts:
                    or_conditions.append(f'or=({",".join(or_parts)})')
                continue

            col = self._map_field(key)

            if isinstance(val, dict):
                if "$regex" in val:
                    pattern = val["$regex"]
                    if pattern.startswith("^") and pattern.endswith("$"):
                        # Exact match with regex anchors
                        clean = pattern[1:-1]
                        parts.append(f"{col}=ilike.{_pgrst_encode(clean)}")
                    elif pattern.startswith("^"):
                        clean = pattern[1:]
                        parts.append(f"{col}=ilike.{_pgrst_encode(clean)}%")
                    else:
                        parts.append(f"{col}=ilike.*{_pgrst_encode(pattern)}*")
                elif "$in" in val:
                    vals = ",".join(f'"{_pgrst_encode(x)}"' if isinstance(x, str) else str(x) for x in val["$in"])
                    parts.append(f"{col}=in.({vals})")
                elif "$nin" in val:
                    vals = ",".join(f'"{_pgrst_encode(x)}"' if isinstance(x, str) else str(x) for x in val["$nin"])
                    parts.append(f"{col}=not.in.({vals})")
                elif "$ne" in val:
                    parts.append(f"{col}=neq.{_pgrst_encode(val['$ne'])}")
                elif "$gte" in val and "$lte" in val:
                    parts.append(f"{col}=gte.{_pgrst_encode(val['$gte'])}")
                    parts.append(f"{col}=lte.{_pgrst_encode(val['$lte'])}")
                elif "$gte" in val:
                    parts.append(f"{col}=gte.{_pgrst_encode(val['$gte'])}")
                elif "$lte" in val:
                    parts.append(f"{col}=lte.{_pgrst_encode(val['$lte'])}")
            elif val is None:
                parts.append(f"{col}=is.null")
            else:
                parts.append(f"{col}=eq.{_pgrst_encode(val)}")

        return "&".join(parts + or_conditions)

    def _select_from_projection(self, projection: dict = None) -> str:
        if not projection:
            return "*"
        # Projection: {"_id": 0, "field1": 1, "field2": 1} -> "field1,field2"
        fields = [self._map_field(k) for k, v in projection.items() if v == 1 and k != "_id"]
        return ",".join(fields) if fields else "*"

    async def find_one(self, query: dict = None, projection: dict = None):
        query = query or {}
        select = self._select_from_projection(projection)
        filters = self._build_filters(query)
        url = f"{self.url}?select={select}&limit=1"
        if filters:
            url += f"&{filters}"

        cached = _cache_get(self.table, url)
        if cached is not None:
            return self._unmap_doc(cached[0]) if cached else None
        c = _get_client()
        r = await c.get(url, headers={**_headers(), "Accept": "application/json"}, timeout=15)

        if r.status_code not in (200, 206):
            return None
        rows = r.json()
        _cache_put(self.table, url, rows)
        if not rows:
            return None
        doc = rows[0]
        return self._unmap_doc(doc)

    def find(self, query: dict = None, projection: dict = None):
        """Returns a FindCursor for chaining .sort().skip().limit().to_list()"""
        return FindCursor(self, query or {}, projection)

    async def insert_one(self, doc: dict):
        _bust(self.table)
        doc = dict(doc)
        doc.pop("_id", None)
        mapped = self._map_doc(doc)
        c = _get_client()
        r = await c.post(self.url, json=mapped, headers={**_headers(), "Prefer": "return=minimal"}, timeout=15)
        if r.status_code not in (200, 201):
            logger.error(f"insert_one {self.table}: {r.status_code} - {r.text[:200]}")
            # Surface conflicts (409) and other errors so callers can react
            raise RuntimeError(f"insert_one {self.table} failed: {r.status_code} - {r.text[:200]}")

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        _bust(self.table)
        set_data = update.get("$set", update)
        set_data.pop("_id", None)
        mapped = self._map_doc(set_data)
        filters = self._build_filters(query)
        url = f"{self.url}?{filters}" if filters else self.url

        prefer = "return=minimal"
        if upsert:
            prefer = "return=minimal,resolution=merge-duplicates"

        c = _get_client()
        if upsert:
            # For upsert, merge query fields into the data
            merged = {**self._map_doc(query), **mapped}
            r = await c.post(self.url, json=merged, headers={**_headers(), "Prefer": prefer}, timeout=15)
        else:
            r = await c.patch(url + "&limit=1", json=mapped, headers={**_headers(), "Prefer": prefer}, timeout=15)

        if r.status_code not in (200, 201, 204):
            # Previously this only logged and returned matched_count=0, causing
            # silent data loss (endpoints returned 200 OK to clients while the
            # write never persisted). Fail loudly so the client gets a proper
            # error and the user knows to retry.
            logger.error(f"update_one {self.table}: {r.status_code} - {r.text[:400]}")
            raise RuntimeError(f"update_one {self.table} failed: {r.status_code} - {r.text[:200]}")

        class Result:
            matched_count = 1 if r.status_code in (200, 201, 204) else 0
            modified_count = matched_count
        return Result()

    async def update_many(self, query: dict, update: dict):
        _bust(self.table)
        set_data = update.get("$set", update)
        set_data.pop("_id", None)
        mapped = self._map_doc(set_data)
        filters = self._build_filters(query)
        url = f"{self.url}?{filters}" if filters else self.url

        c = _get_client()
        r = await c.patch(url, json=mapped, headers={**_headers(), "Prefer": "return=representation"}, timeout=30)

        count = 0
        if r.status_code in (200, 206):
            try:
                count = len(r.json())
            except Exception:
                count = 0

        class Result:
            modified_count = count
            matched_count = count
        return Result()

    async def delete_one(self, query: dict):
        _bust(self.table)
        filters = self._build_filters(query)
        url = f"{self.url}?{filters}&limit=1"
        c = _get_client()
        r = await c.delete(url, headers={**_headers(), "Prefer": "return=representation"}, timeout=15)

        count = 0
        if r.status_code in (200, 206):
            try:
                count = len(r.json())
            except Exception:
                count = 0

        class Result:
            deleted_count = count
        return Result()

    async def count_documents(self, query: dict = None):
        query = query or {}
        filters = self._build_filters(query)
        url = f"{self.url}?select=*&limit=0"
        if filters:
            url += f"&{filters}"
        c = _get_client()
        r = await c.get(url, headers={**_headers(), "Prefer": "count=exact"}, timeout=15)
        try:
            cr = r.headers.get("content-range", "*/0")
            return int(cr.split("/")[-1])
        except Exception:
            return 0

    async def distinct(self, field: str, query: dict = None):
        query = query or {}
        col = self._map_field(field)
        filters = self._build_filters(query)
        url = f"{self.url}?select={col}&limit=10000"
        if filters:
            url += f"&{filters}"
        c = _get_client()
        r = await c.get(url, headers=_headers(), timeout=15)
        if r.status_code not in (200, 206):
            return []
        values = set()
        for row in r.json():
            v = row.get(col)
            if v is not None:
                values.add(v)
        return list(values)

    async def aggregate(self, pipeline: list):
        """Basic aggregation support - returns AggCursor"""
        return AggCursor(self, pipeline)


class FindCursor:
    """Mimics Motor's find cursor with chaining"""

    def __init__(self, collection, query, projection):
        self.col = collection
        self.query = query
        self.projection = projection
        self._sort_field = None
        self._sort_dir = 1
        self._skip = 0
        self._limit_val = 1000
        self._collation = None

    def sort(self, field, direction=1):
        if isinstance(field, str):
            self._sort_field = self.col._map_field(field)
            self._sort_dir = direction
        elif isinstance(field, list):
            # List of (field, direction) tuples
            self._sort_field = self.col._map_field(field[0][0])
            self._sort_dir = field[0][1]
        return self

    def collation(self, col_spec):
        self._collation = col_spec
        return self

    def skip(self, n):
        self._skip = n
        return self

    def limit(self, n):
        self._limit_val = n
        return self

    async def to_list(self, length=None):
        if length:
            self._limit_val = length

        select = self.col._select_from_projection(self.projection)
        if select == "*" and self.col.table == "service_orders":
            select = SO_LIGHT_COLUMNS
        filters = self.col._build_filters(self.query)

        url = f"{self.col.url}?select={select}"
        if filters:
            url += f"&{filters}"

        if self._sort_field:
            order = "desc" if self._sort_dir == -1 else "asc"
            # Handle numeric ordering for os_number
            if self._collation and self._collation.get("numericOrdering"):
                url += f"&order={self._sort_field}.{order}"
            else:
                url += f"&order={self._sort_field}.{order}"

        url += f"&offset={self._skip}&limit={self._limit_val}"

        docs = _cache_get(self.col.table, url)
        if docs is None:
            c = _get_client()
            r = await c.get(url, headers=_headers(), timeout=30)
            if r.status_code not in (200, 206):
                logger.error(f"find {self.col.table}: {r.status_code} - {r.text[:200]}")
                return []
            docs = r.json()
            _cache_put(self.col.table, url, docs)
        docs = list(docs)
        # Handle numeric ordering for os_number in Python if needed
        if self._collation and self._collation.get("numericOrdering") and self._sort_field == "os_number":
            try:
                reverse = self._sort_dir == -1
                docs.sort(key=lambda d: int(d.get("os_number", "0")) if d.get("os_number", "").isdigit() else 0, reverse=reverse)
            except Exception:
                pass

        return [self.col._unmap_doc(d) for d in docs]

    def __aiter__(self):
        self._iter_offset = 0
        self._iter_buffer = []
        self._iter_done = False
        return self

    async def __anext__(self):
        if not self._iter_buffer and not self._iter_done:
            select = self.col._select_from_projection(self.projection)
            if select == "*" and self.col.table == "service_orders":
                select = SO_LIGHT_COLUMNS
            filters = self.col._build_filters(self.query)
            url = f"{self.col.url}?select={select}"
            if filters:
                url += f"&{filters}"
            url += f"&offset={self._iter_offset}&limit=500"
            c = _get_client()
            r = await c.get(url, headers=_headers(), timeout=30)
            if r.status_code not in (200, 206) or not r.json():
                self._iter_done = True
                raise StopAsyncIteration
            self._iter_buffer = [self.col._unmap_doc(d) for d in r.json()]
            self._iter_offset += len(self._iter_buffer)
            if len(self._iter_buffer) < 500:
                self._iter_done = True

        if not self._iter_buffer:
            raise StopAsyncIteration
        return self._iter_buffer.pop(0)


class AggCursor:
    """Placeholder for aggregate results"""
    def __init__(self, collection, pipeline):
        self.col = collection
        self.pipeline = pipeline
        self._data = None

    async def to_list(self, length=None):
        # Aggregations handled case-by-case in the calling code
        return self._data or []


class SupabaseDB:
    """MongoDB-like database accessor: db.collection_name"""

    def __init__(self):
        self._collections = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            return super().__getattribute__(name)
        if name not in self._collections:
            field_map = {}
            if name == "sync_logs":
                field_map = {"timestamp": "ts"}
            elif name == "whatsapp_messages":
                field_map = {"to": "to_number", "from": "from_number", "timestamp": "ts"}
            self._collections[name] = SupabaseCollection(name, field_map)
        return self._collections[name]


# Singleton
db = SupabaseDB()
