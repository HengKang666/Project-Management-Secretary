# -*- coding: utf-8 -*-
"""批量跑标准问法，结果落盘，对就是对错就是错。"""
import json
import pathlib
import time

import agent

QUESTIONS = [
    '截至2026年9月，全市的预算总额是多少？',
    '截至2026年9月，全市累计支出多少？预算执行率是多少？',
    '截至2026年9月，全市相关项目的整体执行率是多少？已完成和在建项目各多少？',
    '2026年9月全市的线损率是多少？',
    '截至2026年9月，全市的治理成效如何？主要绩效指标完成情况怎样？',
    '全市有多少个供电所？',
    '截至2026年9月，全市已完工项目的验收通过率是多少？',
    '全市各区域中项目执行表现最好的是哪个？',
    '随州市2026年酒店入住率是多少？',
]

OUT = pathlib.Path(r'D:\秘书智能体\output')
OUT.mkdir(parents=True, exist_ok=True)
rows = []
for i, q in enumerate(QUESTIONS, 1):
    t0 = time.time()
    try:
        r = agent.ask(q)
    except Exception as e:
        r = {'answer': 'EXC ' + type(e).__name__ + ' ' + str(e), 'trace': []}
    dt = round(time.time() - t0, 1)
    sqls = [t['args'].get('sql') for t in r.get('trace', []) if t['tool'] == 'run_sql']
    rows.append({'no': i, 'question': q, 'answer': r.get('answer', ''), 'seconds': dt,
                 'tool_calls': len(r.get('trace', [])), 'sqls': sqls, 'trace': r.get('trace', [])})
    print('%d. %s' % (i, q))
    print('   答：%s' % r.get('answer', '').replace('\n', ' '))
    print('   %.1fs / 工具 %d 次 / SQL %d 条' % (dt, len(r.get('trace', [])), len(sqls)))
    print()

(OUT / 'verify_batch.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
lines = ['# 流程验证 · 模型自主查库回答（qwen3.8-max，无任何业务规则）', '',
         '题目共 %d 条，结果落盘 output/verify_batch.json。' % len(rows), '']
for r in rows:
    lines += ['## %d. %s' % (r['no'], r['question']), '',
              '**答**：%s' % r['answer'].replace('\n', '  '), '',
              '耗时 %.1fs ｜ 工具调用 %d 次 ｜ SQL %d 条' % (r['seconds'], r['tool_calls'], len(r['sqls'])), '']
    for s in r['sqls']:
        lines.append('- ' + str(s))
    lines.append('')
(OUT / 'verify_batch.md').write_text('\n'.join(lines), encoding='utf-8')
print('saved: output/verify_batch.json / verify_batch.md')
