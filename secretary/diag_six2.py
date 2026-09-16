# -*- coding: utf-8 -*-

import sys, io
sys.path.insert(0, r'D:\秘书智能体\secretary')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import agent
QS = ['截至2026年9月，随州市全市，情况怎么样',
      '截至2026年9月，随州市全市，情况怎么样',
      '2026年9月随州市全市，六项指标的同比分别是多少？',
      '2026年9月随县供电公司，六项指标分别是多少？']
for q in QS:
    d = agent.ask(q, max_steps=0)
    tools = [t for t in d['trace'] if t.get('kind') == 'tool']
    sqls = [t for t in tools if t['tool'] == 'run_sql']
    rounds = sorted(set(t.get('round') for t in tools))
    print('### %s' % q)
    print('  补全：%s' % str(d.get('completed_question'))[:110])
    print('  run_sql %d 次 / 工具轮 %d：%s' % (len(sqls), len(rounds), rounds))
    for t in sqls:
        print('    轮%-2s %s' % (t.get('round'), str((t.get('args') or {}).get('sql'))[:130]))
    print('  循环 %.1fs' % (d['timings']['loop_ms']/1000))
    print()
