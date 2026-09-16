# -*- coding: utf-8 -*-
import json, time
import tools_db, tools_kb, agent

print('--- list_tables(预算) ---')
r = tools_db.list_tables('预算')
print('rows=', r['row_count'])
for x in (r['rows'] or [])[:6]:
    print('  ', x['tbl_name'], '|', (x.get('tbl_comment') or '')[:30], '|', x.get('tbl_rows'))

print('--- describe_table(t_power_company_assessment) ---')
r = tools_db.describe_table('t_power_company_assessment')
print('cols=', r.get('row_count'), 'sample=', len(r.get('sample_rows') or []))
for c in (r.get('rows') or [])[:6]:
    print('  ', c['col_name'], c['col_type'], (c.get('col_comment') or '')[:26])

print('--- run_sql ---')
print(json.dumps(tools_db.run_sql('SELECT year, COUNT(*) c FROM t_power_company_assessment GROUP BY year'), ensure_ascii=False, default=str)[:500])

print('--- kb_search ---')
k = tools_kb.kb_search('预算执行率 口径')
print('total=', k.get('total'), 'nodes=', len(k.get('nodes') or []), 'err=', k.get('error'))
for n in (k.get('nodes') or [])[:2]:
    print('  score', n['score'], '|', n['title'][:50])

print('--- agent.ask 单条端到端 ---')
t0 = time.time()
r = agent.ask('全市有多少个供电所？', max_steps=8)
print('answer:', r['answer'])
print('steps:', r['steps'], 'db:', r['db_query_count'], 'kb:', r['kb_query_count'], 'no_db:', r['no_db_query'], '%.1fs' % (time.time() - t0))
for s in r['trace']:
    if s.get('kind') == 'tool':
        print('   -', s['tool'], str(s['args'])[:150], '->', str(s['result'])[:150])
