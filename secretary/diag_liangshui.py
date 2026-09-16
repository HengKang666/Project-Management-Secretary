# -*- coding: utf-8 -*-

import sys, io
sys.path.insert(0, r'D:\秘书智能体\secretary')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import agent
d = agent.ask('2026年 两水供电所的情况', max_steps=0)
print('补全：', str(d.get('completed_question'))[:180])
print()
n = 0
for t in d['trace']:
    if t.get('kind') != 'tool':
        continue
    if t['tool'] != 'run_sql':
        print('  轮%-2s %s' % (t.get('round'), t['tool'])); continue
    n += 1
    r = t.get('result') or {}
    print('  [%d] 轮%-2s %4sms 行%s' % (n, t.get('round'), t.get('ms'), r.get('row_count')))
    print('      SQL: %s' % str((t.get('args') or {}).get('sql'))[:180])
    rows = r.get('rows')
    if rows is not None:
        print('      返回: %s' % str(rows)[:150])
print()
print('run_sql 共 %d 次；工具轮数 %d；循环 %.1fs' % (n, len(set(t.get('round') for t in d['trace'] if t.get('kind')=='tool')), d['timings']['loop_ms']/1000))
print()
print('答：', str(d['answer']).replace(chr(10),' ')[:280])
