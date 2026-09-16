# -*- coding: utf-8 -*-
"""原话测试：不预设标准问法，走完整流程（上游补全 → 模型自主查库 → 回答）。"""
import json, pathlib, sys, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r'D:\秘书智能体\secretary')
import agent

RAW = [
    ('口语', '情况怎么样'),
    ('口语', '今年预算花了多少'),
    ('口语', '预算都分给谁了'),
    ('口语', '线损率咋样'),
    ('口语', '哪个所线损最高'),
    ('口语', '工单现在什么进度'),
    ('口语', '有多少工单还没完工'),
    ('口语', '停电时间长不长'),
    ('口语', '售电量多少'),
    ('口语', '意见工单多不多'),
    ('口语', '今年干了多少活'),
    ('口语', '哪个公司表现最好'),
    ('口语', '台区情况看看'),
    ('口语', '上个月线损是多少'),
    ('口语', '驳回的工单都为啥驳回'),
    ('口语', '随州GDP多少'),
]

COMPLEX = [
    ('补全后', '查询截至 2026 年 9 月全市的整体情况，需包含：①预算安排（今年精益预算分配总额、供电公司的预算金额及占比）；②执行进度（缺陷治理工单的审批与施工进度）；③治理成效（供电所 6 项成效指标的近期变化情况，需给出售电量、线损率、意见工单、故障报修工单、用户平均停电时长、故障跳闸次数的本期数值与变化方向）。'),
]


def measure(d, seconds):
    from collections import Counter
    tr = d.get('trace') or []
    tools = [t for t in tr if t.get('kind') == 'tool']
    rounds = {}
    for t in tools:
        rounds[t.get('round') or 0] = rounds.get(t.get('round') or 0, 0) + 1
    tm = d.get('timings') or {}
    return {
        'tools': len(tools), 'by_tool': dict(Counter(t.get('tool') for t in tools)),
        'budget_calls': sum(1 for t in tools if t.get('tool') in ('list_tables', 'find_column', 'describe_table')),
        'rounds': len(rounds), 'max_parallel': max(rounds.values()) if rounds else 0,
        'seconds': round(seconds, 1), 'completion_ms': tm.get('completion_ms'),
        'loop_ms': tm.get('loop_ms'), 'unverified': d.get('unverified'),
    }


def run(q, complete):
    t0 = time.time()
    try:
        d = agent.ask(q, complete=complete, max_steps=0)
    except Exception as e:
        d = {'answer': 'EXC ' + type(e).__name__ + ' ' + str(e), 'trace': [], 'timings': {}}
    m = measure(d, time.time() - t0)
    m.update({'question': q, 'answer': d.get('answer'), 'completed_question': d.get('completed_question'),
              'model': d.get('model'), 'trace': d.get('trace')})
    return m


rows = []
print('=== 原话（走完整流程：补全 → 查库 → 回答）===')
for i, (cat, q) in enumerate(RAW, 1):
    m = run(q, True)
    m['category'] = cat
    rows.append(m)
    print('[%2d/%d] %s' % (i, len(RAW), q))
    print('   补全：%s' % (m.get('completed_question') or '（未补全）')[:130])
    print('   答：%s' % str(m.get('answer')).replace(chr(10), ' ')[:220])
    print('   %.1fs（补全 %.1fs + 循环 %.1fs）工具%d 摸底%d 工具轮%d 并发max%d' % (
        m['seconds'], (m['completion_ms'] or 0)/1000, (m['loop_ms'] or 0)/1000,
        m['tools'], m['budget_calls'], m['rounds'], m['max_parallel']))
    print()
print('=== 补全后的问题（只跑我们这侧）===')
for cat, q in COMPLEX:
    m = run(q, False)
    m['category'] = cat
    rows.append(m)
    print('   答：%s' % str(m.get('answer')).replace(chr(10), ' ')[:400])
    print('   %.1fs 工具%d 工具轮%d 并发max%d' % (m['seconds'], m['tools'], m['rounds'], m['max_parallel']))
out = pathlib.Path(r'D:\秘书智能体\output')
(out / 'e2e_raw_v1.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
n = len(rows)
print()
print('=== 汇总（%d 题）===' % n)
print('平均 补全 %.1fs + 循环 %.1fs = %.1fs（最长 %.1fs）' % (
    sum((r['completion_ms'] or 0) for r in rows)/1000/n, sum((r['loop_ms'] or 0) for r in rows)/1000/n,
    sum(r['seconds'] for r in rows)/n, max(r['seconds'] for r in rows)))
print('平均工具 %.1f 次；摸底 %d 次；出现并发>=2 的题 %d 道' % (
    sum(r['tools'] for r in rows)/n, sum(r['budget_calls'] for r in rows),
    sum(1 for r in rows if r['max_parallel'] >= 2)))
print('saved output/e2e_raw_v1.json')
