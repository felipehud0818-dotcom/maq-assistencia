"""Utilitário de migração Supabase antigo -> novo (uso único, via pooler)."""
import os
import psycopg2

OLD = dict(host="aws-1-sa-east-1.pooler.supabase.com", port=5432, user="postgres.vqzwgkevsaihlgmcswvi",
           password=os.environ["SUPABASE_SENHA"], dbname="postgres", connect_timeout=15)
NEW = dict(host="aws-1-us-east-1.pooler.supabase.com", port=5432, user="postgres.wzgckilxigyvzwhetnuk",
           password=os.environ["NEW_SUPABASE_DB_PASSWORD"], dbname="postgres", connect_timeout=15)


def conn(cfg):
    c = psycopg2.connect(**cfg)
    c.autocommit = True
    return c
