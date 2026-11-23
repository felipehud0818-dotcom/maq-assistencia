"""Migração completa do Supabase antigo para o novo: schema, dados, sequências, RLS/policies, grants e Storage.
Uso: python migration/migrate.py [schema|data|storage|verify|all]"""
import io
import os
import sys
import requests
from pgconn import conn, OLD, NEW

TABLES_SQL = "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relkind='r' order by 1"


def tables(cur):
    cur.execute(TABLES_SQL)
    return [r[0] for r in cur.fetchall()]


def schema(o, n):
    oc, nc = o.cursor(), n.cursor()
    tbls = tables(oc)
    # Sequências usadas em defaults
    oc.execute("select sequence_name from information_schema.sequences where sequence_schema='public'")
    for (s,) in oc.fetchall():
        nc.execute(f'create sequence if not exists public."{s}"')
    for t in tbls:
        oc.execute("""select a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull, pg_get_expr(d.adbin,d.adrelid), a.attidentity
                      from pg_attribute a left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum
                      where a.attrelid=%s::regclass and a.attnum>0 and not a.attisdropped order by a.attnum""", (f'public."{t}"',))
        cols = []
        for name, typ, notnull, default, ident in oc.fetchall():
            c = f'"{name}" {typ}'
            if ident in ("a", "d"):
                c += f" generated {'always' if ident == 'a' else 'by default'} as identity"
            elif default is not None:
                c += f" default {default}"
            if notnull:
                c += " not null"
            cols.append(c)
        nc.execute(f'create table if not exists public."{t}" ({", ".join(cols)})')
    for kinds in (("p", "u", "c", "x"), ("f",)):
        for t in tbls:
            oc.execute("select conname, pg_get_constraintdef(oid) from pg_constraint where conrelid=%s::regclass and contype = any(%s)",
                       (f'public."{t}"', list(kinds)))
            for name, d in oc.fetchall():
                nc.execute("select 1 from pg_constraint where conname=%s and conrelid=%s::regclass", (name, f'public."{t}"'))
                if not nc.fetchone():
                    nc.execute(f'alter table public."{t}" add constraint "{name}" {d}')
    oc.execute("""select indexdef from pg_indexes i where schemaname='public'
                  and not exists (select 1 from pg_constraint c where c.conname=i.indexname)""")
    for (d,) in oc.fetchall():
        nc.execute(d.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS").replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS"))
    oc.execute("select pg_get_functiondef(p.oid) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public'")
    for (d,) in oc.fetchall():
        nc.execute(d)
    oc.execute("""select tgname, c.relname, pg_get_triggerdef(t.oid) from pg_trigger t join pg_class c on c.oid=t.tgrelid
                  join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and not t.tgisinternal""")
    for name, t, d in oc.fetchall():
        nc.execute(f'drop trigger if exists "{name}" on public."{t}"')
        nc.execute(d)
    for t in tbls:
        oc.execute("select relrowsecurity, relforcerowsecurity from pg_class where oid=%s::regclass", (f'public."{t}"',))
        rls, force = oc.fetchone()
        if rls:
            nc.execute(f'alter table public."{t}" enable row level security')
        if force:
            nc.execute(f'alter table public."{t}" force row level security')
        for role in ("anon", "authenticated"):
            nc.execute(f'revoke all on public."{t}" from {role}')
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                oc.execute("select has_table_privilege(%s, %s, %s)", (role, f'public."{t}"', priv))
                if oc.fetchone()[0]:
                    nc.execute(f'grant {priv} on public."{t}" to {role}')
    oc.execute("select policyname, tablename, permissive, roles, cmd, qual, with_check from pg_policies where schemaname='public'")
    for name, t, perm, roles, cmd, qual, chk in oc.fetchall():
        nc.execute(f'drop policy if exists "{name}" on public."{t}"')
        sql = f'create policy "{name}" on public."{t}" as {perm} for {cmd} to {", ".join(roles)}'
        if qual:
            sql += f" using ({qual})"
        if chk:
            sql += f" with check ({chk})"
        nc.execute(sql)
    print("schema ok:", len(tbls), "tabelas")


def data(o, n):
    oc, nc = o.cursor(), n.cursor()
    nc.execute("set session_replication_role = replica")  # sem triggers/FKs durante a carga
    for t in tables(oc):
        nc.execute(f'truncate public."{t}" cascade')
        buf = io.StringIO()
        oc.copy_expert(f'copy public."{t}" to stdout with (format csv)', buf)
        buf.seek(0)
        nc.copy_expert(f'copy public."{t}" from stdin with (format csv)', buf)
        print("dados", t, buf.tell())
    nc.execute("set session_replication_role = origin")
    oc.execute("select sequence_name from information_schema.sequences where sequence_schema='public'")
    for (s,) in oc.fetchall():
        oc.execute(f'select last_value, is_called from public."{s}"')
        v, called = oc.fetchone()
        nc.execute("select setval(%s, %s, %s)", (f'public."{s}"', v, called))


def storage(o):
    oc = o.cursor()
    old_url, old_key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    new_url, new_key = os.environ["NEW_SUPABASE_URL"], os.environ["NEW_SUPABASE_SECRET_KEY"]
    nh = {"apikey": new_key, "Authorization": f"Bearer {new_key}"}
    oh = {"apikey": old_key, "Authorization": f"Bearer {old_key}"}
    oc.execute("select id, public, file_size_limit, allowed_mime_types from storage.buckets")
    for bid, public, limit, mimes in oc.fetchall():
        r = requests.post(f"{new_url}/storage/v1/bucket", headers=nh, json={"id": bid, "name": bid, "public": public,
                          "file_size_limit": limit, "allowed_mime_types": mimes}, timeout=30)
        print("bucket", bid, r.status_code)
    oc.execute("select bucket_id, name, metadata->>'mimetype' from storage.objects order by 1, 2")
    ok = 0
    for bid, name, mime in oc.fetchall():
        h = requests.head(f"{new_url}/storage/v1/object/{bid}/{name}", headers=nh, timeout=30)
        if h.status_code == 200:
            ok += 1
            continue
        g = requests.get(f"{old_url}/storage/v1/object/{bid}/{name}", headers=oh, timeout=300)
        g.raise_for_status()
        p = requests.post(f"{new_url}/storage/v1/object/{bid}/{name}", headers={**nh, "Content-Type": mime or "application/octet-stream",
                          "x-upsert": "true"}, data=g.content, timeout=300)
        p.raise_for_status()
        ok += 1
    print("storage objetos:", ok)


def verify(o, n):
    oc, nc = o.cursor(), n.cursor()
    bad = 0
    for t in tables(oc):
        oc.execute(f'select count(*) from public."{t}"')
        nc.execute(f'select count(*) from public."{t}"')
        a, b = oc.fetchone()[0], nc.fetchone()[0]
        bad += a != b
        print(f"{t}: antigo={a} novo={b} {'OK' if a == b else 'DIFERENTE'}")
    oc.execute("select count(*) from storage.objects")
    nc.execute("select count(*) from storage.objects")
    print("storage.objects:", oc.fetchone()[0], nc.fetchone()[0])
    print("DIVERGENCIAS:", bad)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    o, n = conn(OLD), conn(NEW)
    if step in ("schema", "all"):
        schema(o, n)
    if step in ("data", "all"):
        data(o, n)
    if step in ("storage", "all"):
        storage(o)
    if step in ("verify", "all"):
        verify(o, n)
