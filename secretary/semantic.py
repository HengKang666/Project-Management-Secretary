# -*- coding: utf-8 -*-
"""语义层：AI 能读哪些表、字段叫什么，全部来自他们在线上维护的 ai_data.* 表（只读）。

- 表级：ai_data.ai_table_metadata
- 字段级：ai_data.ai_column_metadata（只登记 AI 需要的字段）
- 口径/指标：ai_data.ai_metric_metadata（公式与单位）
- 同义词：ai_data.ai_column_synonym；表关联：ai_data.ai_table_relation

**不维护自己的口径表**：字典内容有缺，就报给维护方补行，代码不做补充。
本模块只读，不写数据库。
"""
import re
import threading

import config
import tools_db

_TICK = chr(96)
_lock = threading.Lock()
_cache = None

_TABLE_REF = re.compile(r'\b(?:from|join)\s+([^\s;(),]+(?:\s*,\s*[^\s;(),]+)*)', re.I)
_CTE = re.compile(r'\bwith\s+([A-Za-z0-9_]+)\s+as\s*\(|,\s*([A-Za-z0-9_]+)\s+as\s*\(', re.I)


def _rows(sql, limit=1000):
    return tools_db._exec(sql, limit_rows=limit)['rows']


def table_name(x):
    """把 dlj_data.t_power_x / 反引号 之类统一成裸表名。"""
    t = str(x).strip().strip(_TICK).split('.')[-1].strip()
    return t.strip(_TICK).strip()


def _build():
    tables = []
    index = {}
    for r in _rows('SELECT table_name AS t, table_cn_name AS cn, business_desc AS d, '
                   'importance_level AS lv FROM ai_data.ai_table_metadata ORDER BY id'):
        row = {'table_name': r['t'], 'table_cn_name': r['cn'] or '',
               'business_desc': (r['d'] or '').strip(), 'note': '', 'level': r['lv']}
        tables.append(row)
        index[r['t']] = row

    cols = {}
    for r in _rows('SELECT table_name AS t, column_name AS c, column_cn_name AS cn, data_type AS dt, '
                   'business_desc AS d, is_dimension AS dim, is_metric AS met, '
                   'aggregation_type AS agg, example_value AS ev '
                   'FROM ai_data.ai_column_metadata ORDER BY id'):
        cols.setdefault(r['t'], {})[r['c']] = {
            'column_name': r['c'], 'column_cn_name': r['cn'] or '', 'data_type': r['dt'] or '',
            'business_desc': (r['d'] or '').strip(), 'example_value': (r['ev'] or '').strip(),
            'is_dimension': r['dim'] or 0, 'is_metric': r['met'] or 0,
            'aggregation_type': r['agg'] or ''}

    metrics = []
    for r in _rows('SELECT metric_code AS code, metric_name AS name, business_desc AS d, '
                   'metric_formula AS f, unit AS u FROM ai_data.ai_metric_metadata ORDER BY id'):
        metrics.append({'code': r['code'], 'name': r['name'], 'desc': (r['d'] or '').strip(),
                        'formula': (r['f'] or '').strip(), 'unit': r['u'] or ''})

    syn = {}
    for r in _rows('SELECT table_name AS t, column_name AS c, synonym_word AS w FROM ai_data.ai_column_synonym'):
        syn.setdefault(r['w'], []).append('%s.%s' % (r['t'], r['c']))

    # 每张表的真实行数：让模型一眼看出哪张表有数、哪张是空的，不用去试
    counts = {}
    try:
        for r in _rows('SELECT table_name AS t, table_rows AS n FROM information_schema.tables '
                       'WHERE table_schema=%s', limit=500):
            counts[r['t']] = r['n'] or 0
    except Exception:
        pass
    for t in index:
        if not counts.get(t):
            try:
                counts[t] = tools_db._exec('SELECT COUNT(*) AS c FROM ' + t, limit_rows=1)['rows'][0]['c']
            except Exception:
                counts[t] = None

    # 示例值一律从真库取样（字典里原有的 example_value 不可信：写的是别省的公司名）
    samples = {}
    for t in index:
        try:
            rows = tools_db._exec('SELECT * FROM ' + t + ' LIMIT 3', limit_rows=3)['rows']
        except Exception:
            rows = []
        seen = {}
        for row in rows:
            for k, v in row.items():
                if v is None or str(v).strip() == '':
                    continue
                if k not in seen or len(str(v)) > len(seen[k]):
                    seen[k] = str(v)[:60]
        samples[t] = seen
    return {'tables': tables, 'index': index, 'cols': cols, 'metrics': metrics, 'syn': syn,
            'samples': samples, 'counts': counts}


def _ensure():
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = _build()
    return _cache


def reload():
    global _cache
    _cache = None
    return _ensure()['tables']


def prompts():
    """他们在 ai_prompt 里维护的提示词，取最新版本（IS_new=1 且未删）。"""
    out = {}
    try:
        for r in _rows('SELECT prompt_key AS k, prompt_content AS c FROM ai_data.ai_prompt '
                       'WHERE IS_new=1 AND deleted_flag=0 ORDER BY id'):
            if r['k'] and (r['c'] or '').strip():
                out[r['k']] = (r['c'] or '').strip()
    except Exception:
        pass
    return out


def _scope():
    """只允许的表；config.TABLES 为空时用字典里登记的全部。"""
    return set(config.TABLES) if getattr(config, 'TABLES', None) else None


def table_list():
    ts = _ensure()['tables']
    sc = _scope()
    return [t for t in ts if t['table_name'] in sc] if sc else ts


def allowed_tables():
    names = set(_ensure()['index'].keys())
    sc = _scope()
    return (names & sc) if sc else names


def is_allowed(table):
    return table_name(table) in allowed_tables()


def table_cols(table):
    return _ensure()['cols'].get(table_name(table)) or {}


def metrics():
    """限定表范围时不给旧指标目录（那些公式指向范围外的表，会误导）。"""
    return [] if _scope() else _ensure()['metrics']


def table_menu():
    """表目录 + 字段目录 + 指标口径，全部照搬他们表里的内容。"""
    data = _ensure()
    out = []
    for t in table_list():
        n = data.get('counts', {}).get(t['table_name'])
        line = '- %s ｜ %s ｜ 当前 %s 行 ｜ %s' % (t['table_name'], t['table_cn_name'] or '',
                                             '空' if n == 0 else (n if n is not None else '?'), t['business_desc'])
        if t['note']:
            line += ' ｜ 注意：' + t['note']
        cs = data['cols'].get(t['table_name']) or {}
        if cs:
            names = []
            for c in cs.values():
                nm = c['column_name'] + (c['column_cn_name'] or '')
                d = (c['business_desc'] or '').strip()
                # 字段说明按原文给足。原先截 40 字，把关键约束截掉了：
                # scope_key 的「station 是哈希，按名称筛选请用 scope_name」正好在第 41 字之后，
                # 模型只看到「district 为县公司名；stat…」→ 直接拿所名去等值匹配 scope_key。
                # 全库仅 30 个字段超过 40 字，放开后字典只增约 1K 字，不构成负担。
                if d and d != (c['column_cn_name'] or '').strip() and len(d) >= 6:
                    nm += '：' + d[:300]
                ev = (data['samples'].get(t['table_name']) or {}).get(c['column_name'])
                if ev:
                    nm += '（例:' + ev[:30] + '）'
                names.append(nm)
            line += '\n    字段: ' + '，'.join(names)
        out.append(line)
    ms = metrics()
    if ms:
        out.append('【他们登记的指标口径】')
        for m in ms:
            out.append('- %s(=%s) 单位%s ｜ %s ｜ %s' % (m['name'], m['code'], m['unit'], m['formula'], m['desc'][:70]))
    return '\n'.join(out)


def find_columns(keyword):
    kw = str(keyword).strip().lower()
    if not kw:
        return []
    hits = []
    for t, cs in _ensure()['cols'].items():
        for c in cs.values():
            blob = (c['column_name'] + ' ' + c['column_cn_name'] + ' ' + c['business_desc']).lower()
            if kw in blob:
                hits.append((t, c))
    return hits


def synonyms(keyword):
    return _ensure()['syn'].get(str(keyword).strip()) or []


def tables_in_sql(sql):
    """抽出 SQL 里引用的表名（含 FROM a, b 与 JOIN；排除 CTE 名）。"""
    names = set()
    for m in _TABLE_REF.finditer(str(sql)):
        for part in m.group(1).split(','):
            t = table_name(part)
            if re.match(r'^[A-Za-z0-9_]+$', t):
                names.add(t)
    for m in _CTE.finditer(str(sql)):
        for g in m.groups():
            if g:
                names.discard(g)
    return names


def unknown_tables(sql):
    return tables_in_sql(sql) - allowed_tables()


def hint():
    return '可用表：' + '、'.join(sorted(allowed_tables()))
