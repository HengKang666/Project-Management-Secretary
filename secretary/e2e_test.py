# -*- coding: utf-8 -*-
"""端到端全流程测试：口语问法 → 补全 → 模型自主查库 → 回答。逐条落盘，供人工核对正确度。"""
import json
import pathlib
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

QUESTIONS = [
    ('A综合', '情况怎么样'),
    ('A综合', '截至2026年9月，全市的预算和治理成效分别是什么？'),
    ('B预算', '全市预算总额是多少？'),
    ('B预算', '今年预算最高和最低的供电所分别是哪个？'),
    ('B预算', '4个供电公司今年的预算分别是多少？'),
    ('B预算', '预算执行率是多少？'),
    ('C项目', '今年一共有多少个项目？已完成多少个？'),
    ('C项目', '施工中的工单有多少个？'),
    ('C项目', '被驳回的工单有多少个？'),
    ('C项目', '有多少项目逾期还没完工？'),
    ('D治理', '2026年9月全市线损率是多少？'),
    ('D治理', '全市售电量是多少？'),
    ('D治理', '今年意见工单和故障报修工单各多少件？'),
    ('D治理', '用户平均停电时长是多少？'),
    ('E主数据', '全市有多少个供电所？'),
    ('E主数据', '全市有多少个台区？'),
    ('E主数据', '全市有多少条线路？'),
    ('F对比', '四个供电公司里线损率最低的是哪个？'),
    ('F对比', '哪个供电所的售电量最高？'),
    ('F对比', '哪个公司的意见工单最多？'),
    ('G陷阱', '2026年9月全市线损率的当月值是多少？'),
    ('G陷阱', '把预算表所有记录加起来，全市预算总额是多少？'),
    ('G陷阱', '台区的线损率最高是多少？'),
    ('H拒答', '随州市2026年的GDP是多少？'),
    ('H拒答', '三公经费花了多少？'),
    ('H拒答', '全市的酒店入住率是多少？'),
    ('H拒答', '按台区统计，全市预算总额是多少？'),
]

OUT = pathlib.Path(r'D:\秘书智能体\output')
OUT.mkdir(parents=True, exist_ok=True)
rows = []
for i, (cat, q) in enumerate(QUESTIONS, 1):
    t0 = time.time()
    try:
        body = json.dumps({'question': q, 'complete': True}).encode('utf-8')
        req = urllib.request.Request('http://127.0.0.1:8200/api/ask', data=body,
                                     headers={'Content-Type': 'application/json'})
        d = json.loads(urllib.request.urlopen(req, timeout=600).read().decode('utf-8'))
    except Exception as e:
        d = {'answer': 'EXC ' + type(e).__name__ + ' ' + str(e), 'trace': [], 'timings': {},
             'db_query_count': 0, 'kb_query_count': 0, 'unverified': False, 'steps': 0}
    d['category'] = cat
    d['seconds'] = round(time.time() - t0, 1)
    rows.append(d)
    print('[%2d/%d] %s | %s' % (i, len(QUESTIONS), cat, q))
    print('   补全：%s' % (d.get('completed_question') or '（未补全）')[:110])
    print('   回答：%s' % (d.get('answer') or '').replace(chr(10), ' ')[:220])
    print('   %.1fs 工具%s 查库%s 知识库%s %s' % (d['seconds'], d.get('steps', 0), d.get('db_query_count', 0),
          d.get('kb_query_count', 0), '【拦下】' if d.get('unverified') else ''))
    sqls = [t['args'].get('sql') for t in d.get('trace', []) if t.get('tool') == 'run_sql']
    for s in sqls:
        print('      SQL: %s' % str(s)[:190])
    print()

(OUT / 'e2e_v1.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
print('saved output/e2e_v1.json')
