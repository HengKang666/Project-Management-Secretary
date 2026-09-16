# -*- coding: utf-8 -*-
"""业务库只读访问 + SQL 安全闸。除只读外不含任何业务判断。"""
import re
import threading

import pymysql

import config

_local = threading.local()
FORBID = re.compile(r'(;|--|/\*|\*/)')
NAME_OK = re.compile(r'^[A-Za-z0-9_]+$')


def _new_conn():
    return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, autocommit=True,
                           connect_timeout=8, read_timeout=30, charset='utf8mb4', **config.DB)


def _drop_conn():
    c = getattr(_local, 'conn', None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _local.conn = None


def _get_conn():
    """每个线程一条连接；用前先 ping 探活，死了就重连。"""
    c = getattr(_local, 'conn', None)
    if c is None:
        c = _new_conn()
        _local.conn = c
        return c
    try:
        c.ping(reconnect=True)
    except Exception:
        _drop_conn()
        c = _new_conn()
        _local.conn = c
    return c


def _exec_once(sql, params, limit_rows):
    cur = _get_conn().cursor()
    try:
        cur.execute('SET SESSION MAX_EXECUTION_TIME=%d' % config.SQL_TIMEOUT_MS)
    except Exception:
        pass
    # 注意：必须传 None 而不是 []。传了参数（哪怕是空列表）pymysql 就会对 SQL 做 % 格式化，
    # 任何 LIKE '%关键字%' 都会报 unsupported format character，逼模型把 % 写成 %% 再重试。
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(limit_rows) if cur.description else []
    cur.close()
    return {'columns': cols, 'rows': rows, 'row_count': len(rows)}


# 只有这几种错误才值得重连重试（连接断了）；SQL 语法/字段名错误重试没有意义
_RETRY_ERRNOS = (2006, 2013, 2055)


def _exec(sql, params=None, limit_rows=config.SQL_MAX_ROWS):
    """所有取数都走这里；只允许 SELECT / WITH —— 本服务对业务库与字典表都是只读。"""
    low = ' '.join(str(sql).split()).lower()
    if not (low.startswith('select') or low.startswith('with')):
        raise ValueError('只读：不允许执行非 SELECT 语句')
    try:
        return _exec_once(sql, params, limit_rows)
    except pymysql.err.InterfaceError:
        _drop_conn()
        return _exec_once(sql, params, limit_rows)
    except pymysql.err.OperationalError as e:
        if e.args and e.args[0] in _RETRY_ERRNOS:
            _drop_conn()
            return _exec_once(sql, params, limit_rows)
        raise


def safe_select(sql):
    s = ' '.join(str(sql).split())
    low = s.lower()
    if not (low.startswith('select') or low.startswith('with')):
        raise ValueError('只允许 SELECT 查询')
    if FORBID.search(s):
        raise ValueError('不允许分号或注释')
    if not re.search(r'\blimit\b', low):
        s += ' LIMIT %d' % config.SQL_MAX_ROWS
    return s


def run_sql(sql):
    """只读 SELECT；表名必须在业务字典里，否则拒绝并回可用表名。"""
    s = safe_select(sql)
    import semantic
    bad = semantic.unknown_tables(s)
    if bad:
        return {'error': '这些表不在业务字典里，不能查：' + '、'.join(sorted(bad)) + '。' + semantic.hint(),
                'sql': s, 'columns': [], 'rows': [], 'row_count': 0}
    try:
        r = _exec(s)
    except Exception as e:
        # 把该表的可用字段一起回给模型，省掉一轮“猜字段名”的试错
        hint = ''
        for t in sorted(semantic.tables_in_sql(s))[:1]:
            cols = list(semantic.table_cols(t).keys())
            if cols:
                hint = '表 %s 在业务字典里登记的字段：%s' % (t, '、'.join(cols[:40]))
        return {'error': str(e)[:300], 'hint': hint, 'sql': s,
                'columns': [], 'rows': [], 'row_count': 0}
    r['sql'] = s
    return r

def find_column(keyword):
    """只在业务字典内的表里搜字段；字典命中的排前面并补中文名与示例值。"""
    import semantic
    allowed = sorted(semantic.allowed_tables())
    if not allowed:
        return {'columns': [], 'rows': [], 'row_count': 0, 'error': '业务字典为空'}
    kw = '%' + str(keyword).strip() + '%'
    marks = ','.join(['%s'] * len(allowed))
    r = _exec('SELECT table_name AS tbl_name, column_name AS col_name, column_type AS col_type, '
              'column_comment AS col_comment FROM information_schema.columns '
              'WHERE table_schema=%s AND table_name IN (' + marks + ') '
              'AND (column_name LIKE %s OR column_comment LIKE %s) '
              'ORDER BY table_name, ordinal_position',
              [config.SCHEMA] + allowed + [kw, kw], limit_rows=80)
    hit = {}
    for t, c in semantic.find_columns(str(keyword).strip()):
        hit[(t, c['column_name'])] = c
    for row in r['rows']:
        c = hit.get((row['tbl_name'], row['col_name']))
        if c:
            row['col_cn_name'] = c['column_cn_name']
            row['col_example'] = c['example_value']
            row['in_dict'] = 1
    r['rows'].sort(key=lambda x: (0 if x.get('in_dict') else 1, x['tbl_name'], x['col_name']))
    return r


def list_tables(keyword=''):
    """只列业务字典里登记的表（白名单）。行数报 0 的用 COUNT 核实，避免把空表当数据源。"""
    import semantic
    info = {}
    try:
        rows = _exec('SELECT table_name AS tbl_name, table_rows AS tbl_rows, update_time AS tbl_update '
                     'FROM information_schema.tables WHERE table_schema=%s',
                     [config.SCHEMA], limit_rows=500)['rows']
        for row in rows:
            info[row['tbl_name']] = row
    except Exception:
        pass
    kw = str(keyword or '').strip().lower()
    out = []
    for t in semantic.table_list():
        name = t['table_name']
        if kw and kw not in (name + ' ' + (t['table_cn_name'] or '') + ' ' + t['business_desc']).lower():
            continue
        src = info.get(name) or {}
        n = src.get('tbl_rows') or 0
        if not n:
            try:
                n = _exec('SELECT COUNT(*) AS c FROM ' + name, limit_rows=1)['rows'][0]['c']
            except Exception:
                n = None
        row = {'tbl_name': name, 'tbl_cn_name': t['table_cn_name'],
               'tbl_comment': t['business_desc'][:150], 'tbl_rows': n,
               'tbl_update': src.get('tbl_update')}
        if t['note']:
            row['note'] = t['note']
        if not n:
            row['note'] = ((row.get('note') or '') + ' ').strip() + '本表当前 0 行，不要用它取数'
        out.append(row)
    return {'columns': ['tbl_name', 'tbl_cn_name', 'tbl_comment', 'tbl_rows', 'tbl_update', 'note'],
            'rows': out, 'row_count': len(out)}


def describe_table(table):
    """看一张表的结构：真库字段 + 业务字典里的中文名与示例值。表必须在字典里。"""
    import semantic
    name = semantic.table_name(table)
    if not NAME_OK.match(name):
        raise ValueError('表名不合法：' + str(table))
    if not semantic.is_allowed(name):
        return {'error': '表 %s 不在业务字典里，不能查。%s' % (name, semantic.hint())}
    r = _exec('SELECT column_name AS col_name, column_type AS col_type, '
              'is_nullable AS is_nullable, column_comment AS col_comment '
              'FROM information_schema.columns WHERE table_schema=%s AND table_name=%s '
              'ORDER BY ordinal_position', [config.SCHEMA, name], limit_rows=300)
    cur = semantic.table_cols(name)
    for row in r['rows']:
        c = cur.get(row['col_name'])
        if not c:
            continue
        row['col_cn_name'] = c['column_cn_name']
        row['col_example'] = c['example_value']
        if c['is_dimension']:
            row['col_role'] = '维度'
        elif c['is_metric']:
            row['col_role'] = '指标' + (('(' + c['aggregation_type'] + ')') if c['aggregation_type'] else '')
    r['dict_fields'] = len(cur)
    r['all_fields'] = len(r['rows'])
    try:
        s = _exec('SELECT * FROM ' + name + ' LIMIT 2')
        r['sample_rows'] = s['rows']
        if not s['rows']:
            r['note'] = '本表当前 0 行，不要用它取数'
    except Exception as e:
        r['sample_error'] = str(e)[:120]
    return r
