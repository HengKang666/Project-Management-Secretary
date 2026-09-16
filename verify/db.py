# -*- coding: utf-8 -*-
"""只读业务库访问 + SQL 安全闸。除了只读，这里不固化成任何业务规则。"""
import re
import threading
import pymysql

ENV_PATH = r'D:\skill回答后端\.env'


def load_env(path=ENV_PATH):
    d = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        d[k.strip()] = v.strip()
    return d


ENV = load_env()
SCHEMA = 'dlj_data'
MAX_ROWS = 200
_lock = threading.RLock()
_conn = None

FORBID = re.compile(r'(;|--|/\*|\*/)')


def _conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = pymysql.connect(host=ENV['DB_HOST'], port=int(ENV['DB_PORT']), user=ENV['DB_USER'],
                                    password=ENV['DB_PASSWORD'], db=SCHEMA, charset='utf8mb4',
                                    cursorclass=pymysql.cursors.DictCursor, autocommit=True,
                                    connect_timeout=8, read_timeout=25)
        return _conn


def _exec(sql, params=None, limit_rows=MAX_ROWS):
    with _lock:
        cur = _conn().cursor()
        try:
            cur.execute('SET SESSION MAX_EXECUTION_TIME=15000')
        except Exception:
            pass
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit_rows) if cur.description else []
        cur.close()
    return {'columns': cols, 'rows': rows, 'row_count': len(rows)}


def safe_select(sql):
    s = ' '.join(str(sql).split())
    if not s.lower().startswith('select') and not s.lower().startswith('with'):
        raise ValueError('只允许 SELECT 查询')
    if FORBID.search(s):
        raise ValueError('不允许分号或注释')
    if not re.search(r'\blimit\b', s, re.I):
        s = s + ' LIMIT ' + str(MAX_ROWS)
    return s


def run_sql(sql):
    s = safe_select(sql)
    r = _exec(s)
    r['sql'] = s
    return r


def list_tables(keyword=''):
    sql = ("SELECT table_name, table_comment, table_rows FROM information_schema.tables "
           "WHERE table_schema=%s")
    params = [SCHEMA]
    if keyword:
        sql += " AND (table_name LIKE %s OR table_comment LIKE %s)"
        params += ['%' + keyword + '%', '%' + keyword + '%']
    sql += " ORDER BY table_name"
    return _exec(sql, params, limit_rows=400)


NAME_OK = re.compile(r'^[A-Za-z0-9_]+$')


def describe_table(table):
    if not NAME_OK.match(str(table)):
        raise ValueError('表名不合法')
    return _exec("SELECT column_name, column_type, is_nullable, column_comment "
                 "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                 "ORDER BY ordinal_position", [SCHEMA, table], limit_rows=300)
