# -*- coding: utf-8 -*-

import os
import sys, io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import tools_db, agent
print('=== 1) 单 % 的 LIKE 现在能不能查 ===')
for sql in ["SELECT name FROM t_power_company WHERE name LIKE '%两水%'",
            "SELECT scope_name FROM t_power_ai_metric_snapshot WHERE scope_type='station' AND scope_name LIKE '%两水%' LIMIT 3"]:
    r = tools_db.run_sql(sql)
    if r.get('error'):
        print('  ERR %s | %s' % (sql[:60], str(r['error'])[:90]))
    else:
        print('  OK  %s -> %s' % (sql[:60], str(r.get('rows'))[:80]))
print()
print('=== 2) 参数化查询仍然正常（find_column 走参数）===')
r = tools_db.find_column('线损')
print('  find_column("线损") 行数', r.get('row_count'), '| 首行', str((r.get('rows') or [{}])[0])[:90])
print()
print('=== 3) 同一题再跑一遍，看查询次数 ===')
d = agent.ask('2026年 两水供电所的情况', max_steps=0)
n = 0
for t in d['trace']:
    if t.get('kind') == 'tool' and t['tool'] == 'run_sql':
        n += 1
        r = t.get('result') or {}
        err = r.get('error')
        print('  [%d] 轮%-2s 行%-4s %s' % (n, t.get('round'), r.get('row_count'), str((t.get('args') or {}).get('sql'))[:110]))
        if err:
            print('       出错: %s' % str(err)[:90])
print('  run_sql 共 %d 次；循环 %.1fs' % (n, d['timings']['loop_ms']/1000))
print('  答：%s' % str(d['answer']).replace(chr(10),' ')[:220])
