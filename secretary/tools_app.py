# -*- coding: utf-8 -*-
"""上游「问题补全」应用客户端：把口语问法补成标准问题。

调用方式（bl app call 的 --verbose 实测）：
    POST https://dashscope.aliyuncs.com/api/v1/apps/<app_id>/completion
    Authorization: Bearer <DashScope API Key>
    {"input": {"prompt": "情况怎么样"}, "parameters": {}}
返回：
    {"output": {"finish_reason": "stop", "session_id": "...", "text": "查询截至2026年9月..."},
     "usage": {"models": [{"input_tokens": 4026, "model_id": "balanced", "output_tokens": 582}]},
     "request_id": "..."}
"""
import json
import ssl
import urllib.request

import config

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# 补全结果缓存：实测补全要 7–19 秒，是整条链路最慢的一段；同一问法重复问直接命中缓存。
_CACHE = {}
_CACHE_MAX = 200


def _cache_get(key):
    v = _CACHE.get(key)
    return dict(v, cached=True) if v else None


def _cache_put(key, val):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = val


def complete_question(raw, session_id=None):
    """把原始问法补成标准问题。失败时返回 error，调用方应退回原始问题。"""
    key = (raw or '').strip()
    if key and not session_id:
        hit = _cache_get(key)
        if hit:
            return hit
    body = {'input': {'prompt': raw}, 'parameters': {}}
    if session_id:
        body['input']['session_id'] = session_id
    req = urllib.request.Request(config.COMPLETION_URL, data=json.dumps(body).encode('utf-8'),
                                 headers={'Authorization': 'Bearer ' + config.LLM_KEY,
                                          'Content-Type': 'application/json'})
    try:
        raw_resp = urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT, context=_ctx).read().decode('utf-8', 'replace')
        d = json.loads(raw_resp)
    except Exception as e:
        return {'text': '', 'error': type(e).__name__ + ': ' + str(e)[:200]}
    out = d.get('output') or {}
    usage = (d.get('usage') or {}).get('models') or [{}]
    result = {
        'text': (out.get('text') or '').strip(),
        'session_id': out.get('session_id'),
        'model_id': usage[0].get('model_id'),
        'input_tokens': usage[0].get('input_tokens'),
        'output_tokens': usage[0].get('output_tokens'),
        'request_id': d.get('request_id'),
        'cached': False,
    }
    if key and result['text']:
        _cache_put(key, result)
    return result
