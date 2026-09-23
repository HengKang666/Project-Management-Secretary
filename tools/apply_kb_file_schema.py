# -*- coding: utf-8 -*-
"""执行 kb_file_schema.sql（建知识库文件台账相关表）。

为什么不用 mysql 客户端：本项目的原则是**不依赖 MySQL 客户端**（用 pymysql 纯 Python 驱动），
服务器上通常也不装 mysql CLI，所以建表也走同一个驱动。

    python -X utf8 tools/apply_kb_file_schema.py            # 执行
    python -X utf8 tools/apply_kb_file_schema.py --check    # 只看现状，不建

幂等：SQL 里用的是 CREATE TABLE IF NOT EXISTS + information_schema 判断加列，
重复执行不会报错、不会清数据。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'secretary'))

import pymysql                                                    # noqa: E402
import config                                                     # noqa: E402

SQL_FILE = os.path.join(HERE, 'kb_file_schema.sql')

TABLES = ('t_kb_file', 't_kb_file_page', 't_kb_upload_task')
COLUMNS = ('summary', 'summary_upto_seq', 'summary_time')


def statements(path):
    """把 SQL 文件切成一条条语句。

    规则：丢掉整行注释（`--` 开头）与空行，再按 `;` 切。
    够用且可读 —— 本文件里没有「语句中间夹行注释」的写法，也没有存储过程。
    """
    lines = []
    for line in open(path, encoding='utf-8'):
        s = line.strip()
        if not s or s.startswith('--'):
            continue
        lines.append(line)
    out = []
    for raw in '\n'.join(lines).split(';'):
        s = raw.strip()
        if s:
            out.append(s)
    return out


def conn():
    return pymysql.connect(host=config.DB['host'], port=config.DB['port'],
                           user=config.DB['user'], password=config.DB['password'],
                           database=config.AGENT_DB, connect_timeout=15,
                           charset='utf8mb4', autocommit=True,
                           cursorclass=pymysql.cursors.DictCursor)


def check(cur):
    print('--- 现状 ---')
    marks = ', '.join(['%s'] * len(TABLES))
    cur.execute('SELECT TABLE_NAME FROM information_schema.TABLES '
                'WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (%s)' % ('%s', marks),
                (config.AGENT_DB,) + TABLES)
    got = {r['TABLE_NAME'] for r in cur.fetchall()}
    for t in TABLES:
        print('  %-18s %s' % (t, '存在' if t in got else '★ 缺失'))
    marks = ', '.join(['%s'] * len(COLUMNS))
    cur.execute('SELECT COLUMN_NAME FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME IN (%s)'
                % ('%s', '%s', marks),
                (config.AGENT_DB, 't_chat_session') + COLUMNS)
    got = {r['COLUMN_NAME'] for r in cur.fetchall()}
    for col in COLUMNS:
        print('  t_chat_session.%-22s %s' % (col, '存在' if col in got else '★ 缺失'))


def main():
    dry = '--check' in sys.argv
    print('库：%s@%s:%s/%s' % (config.DB['user'], config.DB['host'], config.DB['port'], config.AGENT_DB))
    print('SQL：%s' % SQL_FILE)
    c = conn()
    try:
        cur = c.cursor()
        if dry:
            check(cur)
            return
        stmts = statements(SQL_FILE)
        print('\n共 %d 条语句，开始执行…' % len(stmts))
        for i, s in enumerate(stmts, 1):
            head = ' '.join(s.split())[:70]
            try:
                cur.execute(s)
            except Exception as e:                                # noqa: BLE001
                print('  [%2d/%d] 失败：%s\n        %s' % (i, len(stmts), head, e))
                raise
            print('  [%2d/%d] OK   %s%s' % (i, len(stmts), head, ' …' if len(s) > 70 else ''))
        print()
        check(cur)
        print('\n✅ 建表完成')
    finally:
        c.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
