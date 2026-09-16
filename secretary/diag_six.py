# -*- coding: utf-8 -*-

import sys, io
sys.path.insert(0, r'D:\秘书智能体\secretary')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import agent
Q = '截至2026年9月，随州市全市，情况怎么样'
d = agent.ask(Q, max_steps=0)
print('补全：', str(d.get('completed_question'))[:200])
print()
print('工具序列（轮次 | 工具 | ms | SQL/参数）：')
for t in d['trace']:
    if t.get('kind') != 'tool':
        continue
    a = t.get('args') or {}
    r = t.get('result') or {}
    if t['tool'] == 'run_sql':
        print('  轮%-2s | run_sql | %5sms | 行%s' % (t.get('round'), t.get('ms'), r.get('row_count')))
        print('        %s' % str(a.get('sql'))[:150])
    else:
        print('  轮%-2s | %s | %sms' % (t.get('round'), t['tool'], t.get('ms')))
print()
print('工具总数：%d ；工具轮数：%d' % (
    len([t for t in d['trace'] if t.get('kind') == 'tool']),
    len(set(t.get('round') for t in d['trace'] if t.get('kind') == 'tool'))))
print('耗时 补全%.1fs + 循环%.1fs' % (d['timings']['completion_ms']/1000, d['timings']['loop_ms']/1000))
print()
print('答：', str(d['answer']).replace(chr(10), ' ')[:300])
