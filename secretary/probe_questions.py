# -*- coding: utf-8 -*-
"""多样化问题探针：换着花样问，看它怎么选表、怎么答。"""
import json
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import agent

QUESTIONS = [
    '随州市4个供电公司今年各自的售电量和线损率分别是多少？',
    '今年线损率最高的台区是哪个？属于哪条线路？',
    '今年预算金额最高的供电所是哪个？预算多少？',
    '全市用户平均停电时长最高的供电所是哪个？请把另外5项指标也列出来。',
    '截至2026年9月，审批通过但还没开始施工的工单有多少个？',
    '今年有哪些工单被驳回了？主要驳回原因是什么？',
    '全市台区总数是多少？其中线损率高于5%的有多少个？',
    '今年意见工单最多的供电所是哪个？有多少件？',
    '公司资质相关的材料在哪张表里？',
    '随州市2026年的GDP是多少？',
]

OUT = pathlib.Path(r'D:\秘书智能体\output')
OUT.mkdir(parents=True, exist_ok=True)
rows = []
for i, q in enumerate(QUESTIONS, 1):
    t0 = time.time()
    try:
        r = agent.ask(q)
    except Exception as e:
        r = {'answer': 'EXC ' + type(e).__name__ + ' ' + str(e), 'trace': [], 'db_query_count': 0,
             'kb_query_count': 0, 'no_db_query': True, 'unverified': False, 'steps': 0}
    r['seconds'] = round(time.time() - t0, 1)
    rows.append(r)
    tools_used = [t.get('tool') for t in r.get('trace', []) if t.get('kind') == 'tool']
    print('[%d] %s' % (i, q))
    print('    >> %s' % r['answer'].replace(chr(10), ' ')[:230])
    print('    %.1fs 工具%d 查库%d 知识库%d %s' % (r['seconds'], r.get('steps', 0), r.get('db_query_count', 0),
          r.get('kb_query_count', 0), ('【已拦下】' if r.get('unverified') else '')))
    print('    used: %s' % (','.join(tools_used) or '（无）'))
    print()
(OUT / 'probe_v1.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
print('saved output/probe_v1.json')
