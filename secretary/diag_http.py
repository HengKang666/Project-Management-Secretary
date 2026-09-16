# -*- coding: utf-8 -*-
"""打本地服务，把 trace 里的工具错误原文打出来。"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')
q = sys.argv[1] if len(sys.argv) > 1 else '全市有多少个供电所？'
body = json.dumps({'question': q}).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8200/api/ask', data=body,
                             headers={'Content-Type': 'application/json'})
r = json.loads(urllib.request.urlopen(req, timeout=300).read().decode('utf-8'))
print('ANSWER:', r['answer'][:200])
print('db=%s kb=%s steps=%s no_db=%s' % (r['db_query_count'], r['kb_query_count'], r['steps'], r['no_db_query']))
for s in r['trace']:
    if s.get('kind') == 'tool':
        res = s.get('result') or {}
        err = res.get('error') if isinstance(res, dict) else None
        print('  - %-14s %5dms %s' % (s['tool'], s.get('ms', 0), ('ERR ' + str(err)[:160]) if err else 'ok'))
