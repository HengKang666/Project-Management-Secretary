# -*- coding: utf-8 -*-
"""把纠错词典 CSV 导入 agent_data 的 t_nc_* 表。

【什么时候跑】离线，只跑一次（或词典要重建时）。**服务运行时不调用本脚本。**

【用法】
    python tools/import_lexicon.py                    # 用默认种子目录
    python tools/import_lexicon.py <csv目录>           # 指定种子目录

【它会做什么】
    1. TRUNCATE 6 张词典表（清空重建）
    2. catalog.csv 与 area_core.csv 按「名称」合并成 t_nc_catalog
    3. 其余 4 个 CSV 各自进一张表；pinyin_table.csv 进 t_nc_pinyin
    4. 导入后逐表核对行数

【★ 注意事项】
    · **会清空人工修改**：TRUNCATE 后重导，之前手工改过的词典内容全部丢失。
      只想小改词典请直接用 SQL UPDATE，不要重跑本脚本。
    · **rules.csv 不导入**：它在代码里从未被读取（死数据），详见 lexicon_schema.sql 末尾说明。
    · **py_full / py_strict / initials / core_code 是离线编译产物**，
      由本脚本原样搬运，绝不在导入时重算 —— 重算口径一旦与 pinyin_table 不一致，
      同音匹配会静默失效。要重算必须连带重跑 tools 里生成这些列的脚本。
"""
from __future__ import annotations

import csv
import os
import re
import sys
import time

import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # deploy_server2.0/
ENV_PATH = os.path.join(ROOT, '.env')
CHUNK = 500                                      # 每批插入行数，公网往返下 500 行约 30 KB

TABLES = ('t_nc_typo', 't_nc_term', 't_nc_ambiguous',
          't_nc_wordlist', 't_nc_catalog', 't_nc_pinyin')


def read_env(path=ENV_PATH):
    """只取连接所需的 4 项 + 库名，不引 config.py（避免 import 路径依赖）。"""
    d = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            v = re.split(r'\s+#', v, maxsplit=1)[0]      # 剥行尾注释（对齐 config.py 的做法）
            d[k.strip()] = v.strip()
    return d


def load_csv(folder, name):
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        raise SystemExit('[导入] 找不到 %s' % path)
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def insert(cur, table, cols, data):
    """批量插入。data 是 list[list]，顺序与 cols 一致。"""
    if not data:
        return 0
    sql = 'INSERT INTO %s (%s) VALUES (%s)' % (
        table, ','.join('`%s`' % c for c in cols), ','.join(['%s'] * len(cols)))
    n = 0
    for i in range(0, len(data), CHUNK):
        batch = data[i:i + CHUNK]
        cur.executemany(sql, batch)
        n += len(batch)
    return n


def default_seed():
    """种子目录：**刻意放在 deploy_server2.0 之外**，这样部署包里不含业务数据。

    依次找：deploy_server2.0/lexicon_seed -> 工作区根/lexicon_seed
    （前者只是个方便，正常用后者。）
    """
    for c in (os.path.join(ROOT, 'lexicon_seed'),
              os.path.normpath(os.path.join(ROOT, '..', 'lexicon_seed'))):
        if os.path.isdir(c):
            return c
    return os.path.normpath(os.path.join(ROOT, '..', 'lexicon_seed'))


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else default_seed()
    if not os.path.isdir(folder):
        raise SystemExit('[导入] 种子目录不存在：%s' % folder)

    env = read_env()
    dbname = env.get('SECRETARY_AGENT_DB') or 'agent_data'
    print('[导入] 种子目录 %s' % folder)
    print('[导入] 目标 %s@%s:%s/%s' % (env['DB_USER'], env['DB_HOST'], env['DB_PORT'], dbname))

    t0 = time.time()
    conn = pymysql.connect(host=env['DB_HOST'], port=int(env['DB_PORT']),
                           user=env['DB_USER'], password=env['DB_PASSWORD'],
                           database=dbname, charset='utf8mb4',
                           connect_timeout=10, read_timeout=120, write_timeout=120,
                           autocommit=False, cursorclass=pymysql.cursors.DictCursor)
    cur = conn.cursor()
    try:
        print('\n--- 1. 清空 6 张词典表 ---')
        for t in TABLES:
            cur.execute('TRUNCATE TABLE %s' % t)
            print('  TRUNCATE', t)

        counts = {}

        # ---------- 2. 人工词典 ----------
        print('\n--- 2. 导入人工词典 ---')
        counts['t_nc_typo'] = insert(cur, 't_nc_typo',
            ['org', 'wrong', 'right_word', 'freq_level', 'reason', 'sample'],
            [[r.get('适用组织', ''), r.get('错误写法', ''), r.get('正确写法', ''),
              r.get('频率等级', ''), r.get('错因类型', ''), r.get('纠正示例', '')]
             for r in load_csv(folder, 'typo.csv')])
        print('  t_nc_typo      ', counts['t_nc_typo'])

        counts['t_nc_term'] = insert(cur, 't_nc_term',
            ['term', 'definition', 'alias', 'org'],
            [[r.get('术语', ''), r.get('定义', ''), r.get('常见表达', ''), r.get('适用组织', '')]
             for r in load_csv(folder, 'terms.csv')])
        print('  t_nc_term      ', counts['t_nc_term'])

        counts['t_nc_ambiguous'] = insert(cur, 't_nc_ambiguous',
            ['py_code', 'name_a', 'kind_a', 'name_b', 'kind_b', 'action'],
            [[r.get('拼音码', ''), r.get('名称A', ''), r.get('类别A', ''),
              r.get('名称B', ''), r.get('类别B', ''), r.get('处理方式', '')]
             for r in load_csv(folder, 'ambiguous.csv')])
        print('  t_nc_ambiguous ', counts['t_nc_ambiguous'])

        counts['t_nc_wordlist'] = insert(cur, 't_nc_wordlist',
            ['word', 'domain', 'src', 'py_full', 'initials'],
            [[r.get('标准词', ''), r.get('领域', ''), r.get('来源', ''),
              r.get('拼音码', ''), r.get('首字母', '')]
             for r in load_csv(folder, 'wordlist.csv')])
        print('  t_nc_wordlist  ', counts['t_nc_wordlist'])

        # ---------- 3. 名称目录（两个 CSV 合并成一张表）----------
        print('\n--- 3. 导入名称目录（catalog + area_core 合并）---')
        core = {r['名称']: r.get('核心码', '') for r in load_csv(folder, 'area_core.csv')}
        cat = load_csv(folder, 'catalog.csv')
        counts['t_nc_catalog'] = insert(cur, 't_nc_catalog',
            ['name', 'kind', 'owner', 'py_full', 'py_strict', 'initials', 'core_code', 'source_id'],
            [[r['名称'], r.get('类别', ''), r.get('所属', ''), r.get('拼音码', ''),
              r.get('拼音码_严', ''), r.get('首字母', ''), core.get(r['名称'], ''), r.get('来源ID', '')]
             for r in cat])
        print('  t_nc_catalog   ', counts['t_nc_catalog'],
              '（catalog %d 行，其中 %d 行配到了核心码）'
              % (len(cat), sum(1 for r in cat if core.get(r['名称']))))

        # ---------- 4. 拼音表 ----------
        print('\n--- 4. 导入拼音表 ---')
        counts['t_nc_pinyin'] = insert(cur, 't_nc_pinyin',
            ['hanzi', 'initial', 'final'],
            [[r.get('字', ''), r.get('声母', ''), r.get('韵母', '')]
             for r in load_csv(folder, 'pinyin_table.csv')])
        print('  t_nc_pinyin   ', counts['t_nc_pinyin'])

        conn.commit()
        print('\n提交完成，用时 %.2fs' % (time.time() - t0))

        # ---------- 5. 回读核对 ----------
        print('\n--- 5. 回读核对（数据库实际行数 vs 期望）---')
        expect = {'t_nc_typo': 'typo.csv', 't_nc_term': 'terms.csv',
                  't_nc_ambiguous': 'ambiguous.csv', 't_nc_wordlist': 'wordlist.csv',
                  't_nc_catalog': 'catalog.csv', 't_nc_pinyin': 'pinyin_table.csv'}
        all_ok = True
        for t, csvname in expect.items():
            cur.execute('SELECT COUNT(*) n FROM %s' % t)
            got = cur.fetchone()['n']
            want = len(load_csv(folder, csvname))
            flag = 'OK ' if got == want else 'MISMATCH'
            if got != want:
                all_ok = False
            print('  %-16s %6d / %6d  %s' % (t, got, want, flag))
        print('\n结果：%s' % ('全部一致' if all_ok else '存在不一致，请检查！'))
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
