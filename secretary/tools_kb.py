# -*- coding: utf-8 -*-
"""百炼知识检索客户端：只取切片，不取它生成的答案。"""
import json
import ssl
import urllib.request

import config

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


_CACHE = {}
_CACHE_MAX = 200


def kb_search(query, top_k=3, kb_ids=None, limit=450):
    kb_ids = kb_ids or config.KB_IDS
    ck = (str(query), int(top_k), tuple(kb_ids or []), int(limit))
    if ck in _CACHE:
        hit = dict(_CACHE[ck])
        hit['cached'] = True
        return hit
    body = {'agent_id': config.KB_AGENT_ID, 'query': query, 'images': []}
    if kb_ids:
        body['kb_search_configs'] = [{'id': i} for i in kb_ids]
    req = urllib.request.Request(config.KB_SEARCH_URL, data=json.dumps(body).encode('utf-8'),
                                 headers={'Authorization': 'Bearer ' + config.LLM_KEY,
                                          'Content-Type': 'application/json'})
    try:
        raw = urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT, context=_ctx).read().decode('utf-8', 'replace')
        d = json.loads(raw)
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200], 'nodes': [], 'total': 0}
    if not d.get('success'):
        return {'error': d.get('code', '') + ' ' + str(d.get('message', ''))[:200], 'nodes': [], 'total': 0}
    data = d.get('data') or {}
    nodes = []
    for n in (data.get('nodes') or [])[:top_k]:
        m = n.get('metadata') or {}
        nodes.append({
            'score': round(float(n.get('score') or 0), 4),
            'doc_name': m.get('doc_name') or m.get('title') or '',
            'title': (m.get('title') or '')[:120],
            'content': (m.get('content') or '')[:limit],
        })
    out = {'total': data.get('total'), 'cost_time': data.get('cost_time'), 'nodes': nodes}
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[ck] = out
    return out
