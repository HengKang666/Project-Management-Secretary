# -*- coding: utf-8 -*-
"""校验 agent_data 里的词典表与种子 CSV 是否逐字段一致。

【用途】导入后跑一遍，确认迁移无损。**只读，不改任何数据。**
    python tools/verify_lexicon.py
    python tools/verify_lexicon.py <csv目录>

【为什么不能只看行数】
    行数相同不代表内容相同：编码问题会导致中文变问号、列宽不足会被截断，
    这两种情况行数都不变。所以这里做的是**逐行逐字段全量比对**。
"""
from __future__ import annotations

import csv
import os
import sys

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def default_seed():
    for c in (os.path.join(ROOT, 'lexicon_seed'),
              os.path.normpath(os.path.join(ROOT, '..', 'lexicon_seed'))):
        if os.path.isdir(c):
            return c
    return os.path.normpath(os.path.join(ROOT, '..', 'lexicon_seed'))


def read_env(path=None):
    import re
    path = path or os.path.join(ROOT, '.env')
    d = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            d[k.strip()] = re.split(r'\s+#', v, maxsplit=1)[0].strip()
    return d


def rd(folder, name):
    with open(os.path.join(folder, name), encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


# (表名, CSV名, [(库列, CSV列)...])  其中 core_code 特殊处理，见 main
SPEC = [
    ('t_nc_typo', 'typo.csv',
     [('org', '适用组织'), ('wrong', '错误写法'), ('right_word', '正确写法'),
      ('freq_level', '频率等级'), ('reason', '错因类型'), ('sample', '纠正示例')]),
    ('t_nc_term', 'terms.csv',
     [('term', '术语'), ('definition', '定义'), ('alias', '常见表达'), ('org', '适用组织')]),
    ('t_nc_ambiguous', 'ambiguous.csv',
     [('py_code', '拼音码'), ('name_a', '名称A'), ('kind_a', '类别A'),
      ('name_b', '名称B'), ('kind_b', '类别B'), ('action', '处理方式')]),
    ('t_nc_wordlist', 'wordlist.csv',
     [('word', '标准词'), ('domain', '领域'), ('src', '来源'),
      ('py_full', '拼音码'), ('initials', '首字母')]),
    ('t_nc_pinyin', 'pinyin_table.csv',
     [('hanzi', '字'), ('initial', '声母'), ('final', '韵母')]),
    ('t_nc_catalog', 'catalog.csv',
     [('name', '名称'), ('kind', '类别'), ('owner', '所属'), ('py_full', '拼音码'),
      ('py_strict', '拼音码_严'), ('initials', '首字母'), ('source_id', '来源ID')]),
]


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else default_seed()
    if not os.path.isdir(folder):
        raise SystemExit('[校验] 种子目录不存在：%s' % folder)

    env = read_env()
    dbname = env.get('SECRETARY_AGENT_DB') or 'agent_data'
    conn = pymysql.connect(host=env['DB_HOST'], port=int(env['DB_PORT']),
                           user=env['DB_USER'], password=env['DB_PASSWORD'],
                           database=dbname, charset='utf8mb4', connect_timeout=10,
                           cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    print('[校验] 种子目录 %s' % folder)
    print('[校验] 目标 %s/%s\n' % (env['DB_HOST'], dbname))

    fails = []
    for table, csvname, pairs in SPEC:
        cols = ', '.join('`%s`' % c for c, _ in pairs)
        cur.execute('SELECT %s FROM %s' % (cols, table))
        db_rows = cur.fetchall()
        csv_rows = rd(folder, csvname)
        a = sorted(tuple(str(r[c]) for c, _ in pairs) for r in db_rows)
        b = sorted(tuple(str(r.get(c, '')) for _, c in pairs) for r in csv_rows)
        if a == b:
            print('  %-16s %6d 行  逐字段全等 [OK]' % (table, len(a)))
        else:
            fails.append(table)
            only_db = set(a) - set(b)
            only_csv = set(b) - set(a)
            print('  %-16s [FAIL] 库 %d 行 / CSV %d 行；仅库有 %d，仅 CSV 有 %d'
                  % (table, len(a), len(b), len(only_db), len(only_csv)))
            for x in list(only_db)[:3]:
                print('       仅库 :', x)
            for x in list(only_csv)[:3]:
                print('       仅CSV:', x)

    # core_code 是从 area_core.csv 合并进来的，单独比
    cur.execute('SELECT name, core_code FROM t_nc_catalog')
    db_core = sorted((r['name'], r['core_code']) for r in cur.fetchall())
    core = {r['名称']: r.get('核心码', '') for r in rd(folder, 'area_core.csv')}
    csv_core = sorted((r['名称'], core.get(r['名称'], '')) for r in rd(folder, 'catalog.csv'))
    if db_core == csv_core:
        print('  %-16s %6d 行  core_code 合并全等 [OK]' % ('catalog.core_code', len(db_core)))
    else:
        fails.append('catalog.core_code')
        print('  %-16s [FAIL] core_code 与 area_core.csv 不一致' % 'catalog.core_code')

    conn.close()
    print('\n结论：%s' % ('全部通过，迁移无损' if not fails else '存在不一致 -> %s' % fails))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
