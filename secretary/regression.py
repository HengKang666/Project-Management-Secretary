# -*- coding: utf-8 -*-
"""回归：语义层 + 并发改造后，量摸底类调用、往返次数、耗时。逐题落盘供人工核对。"""
import json
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r'D:\秘书智能体\secretary')
import agent
import semantic

COMPLEX = [
    ('Z复杂', '查询2026年1月至9月全市的整体情况，需包含：①预算安排（今年精益预算分配总额、供电公司的预算金额及占比）；②执行进度（缺陷治理工单的审批与施工进度）；③治理成效（售电量、线损率、意见工单、故障报修工单、用户平均停电时长、故障跳闸次数）。'),
    ('Z复杂', '四个供电公司今年各自的售电量、线损率、意见工单数、故障报修工单数分别是多少？'),
    ('Z复杂', '按供电所统计，售电量前五名分别是哪几个所、各多少？各自所属公司是什么？'),
]


def load_questions():
    src = pathlib.Path(r'D:\秘书智能体\secretary\e2e_test.py').read_text(encoding='utf-8')
    start = src.index('QUESTIONS = [')
    end = src.index(']', start) + 1
    ns = {}
    exec(src[start:end], ns)
    return ns['QUESTIONS']


BUDGET = ('list_tables', 'find_column', 'describe_table')


def measure(d):
    tr = d.get('trace') or []
    tools = [t for t in tr if t.get('kind') == 'tool']
    rounds = {}
    for t in tools:
        rounds[t.get('round') or 0] = rounds.get(t.get('round') or 0, 0) + 1
    r = d.get('timings') or {}
    bad = 0
    for t in tools:
        if t.get('tool') == 'run_sql':
            res = t.get('result') or {}
            if res.get('error') and '业务字典' in str(res['error']):
                bad += 1
    from collections import Counter
    by_tool = dict(Counter(t.get('tool') for t in tools))
    return {
        'tools': len(tools),
        'by_tool': by_tool,
        'budget_calls': sum(1 for t in tools if t.get('tool') in BUDGET),
        'kb': sum(1 for t in tools if t.get('tool') == 'kb_search'),
        'sql': sum(1 for t in tools if t.get('tool') == 'run_sql'),
        'rounds': len(rounds),
        'llm_rounds': len(rounds) + 1,
        'max_parallel': max(rounds.values()) if rounds else 0,
        'blocked_sql': bad,
        'loop_ms': r.get('loop_ms'), 'total_ms': r.get('total_ms') or d.get('elapsed_ms'),
    }


def main():
    qs = load_questions() + COMPLEX
    rows = []
    for i, (cat, q) in enumerate(qs, 1):
        t0 = time.time()
        try:
            d = agent.ask(q, max_steps=0)
        except Exception as e:
            d = {'answer': 'EXC ' + type(e).__name__ + ' ' + str(e), 'trace': [], 'timings': {}}
        m = measure(d)
        m.update({'category': cat, 'question': q, 'answer': d.get('answer'),
                  'seconds': round(time.time() - t0, 1), 'model': d.get('model'),
                  'unverified': d.get('unverified'), 'steps': d.get('steps'),
                  'completed_question': d.get('completed_question'),
                  'timings': d.get('timings'), 'trace': d.get('trace')})
        rows.append(m)
        print('[%2d/%d] %s | %s' % (i, len(qs), cat, q[:40]))
        print('   摸底%d 查库%d 知识库%d 工具%d | 工具轮%d LLM往返%d 并发max%d | %.1fs'
              % (m['budget_calls'], m['sql'], m['kb'], m['tools'], m['rounds'], m['llm_rounds'],
                 m['max_parallel'], m['seconds']))
        print('   答：%s' % (str(d.get('answer') or '')).replace(chr(10), ' ')[:200])
        if m['blocked_sql']:
            print('   拦下字典外表 %d 次' % m['blocked_sql'])
        print()
    out = pathlib.Path(r'D:\秘书智能体\output')
    (out / 'regression_v3.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    n = len(rows)
    tot = {k: sum(r[k] or 0 for r in rows) for k in ('tools', 'budget_calls', 'kb', 'sql', 'blocked_sql')}
    names = {}
    for r in rows:
        for k, v in (r.get('by_tool') or {}).items():
            names[k] = names.get(k, 0) + v
    print('工具名分布：%s' % names)
    print('=== 汇总（%d 题）===' % n)
    print('工具 %d 次（均 %.1f）；摸底 %d 次（占 %.0f%%）；run_sql %d；kb %d；拦下字典外表 %d'
          % (tot['tools'], tot['tools']/n, tot['budget_calls'], 100*tot['budget_calls']/max(tot['tools'],1),
             tot['sql'], tot['kb'], tot['blocked_sql']))
    print('平均 LLM 往返 %.1f 次；平均耗时 %.1fs' % (sum(r['llm_rounds'] for r in rows)/n, sum(r['seconds'] for r in rows)/n))
    par = [r for r in rows if r['max_parallel'] >= 2]
    print('出现同一轮并发 >=2 条的题：%d 道' % len(par))
    comp = [r for r in rows if r['category'] == 'Z复杂']
    if comp:
        print('复杂题：往返 %s；耗时 %s' % ([r['llm_rounds'] for r in comp], [r['seconds'] for r in comp]))
    print('saved output/regression_v3.json')


if __name__ == '__main__':
    main()
