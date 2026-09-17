# -*- coding: utf-8 -*-
"""上游「信息补全」工作流应用客户端：机构名自动纠错 + 补统计时间。

应用 id 见 config.COMPLETION_APP_ID。调用（本工作区 MaaS 端点实测可通，dashscope 公网端点也可）：
    POST <MaaS 域名>/api/v1/apps/<app_id>/completion
    Authorization: Bearer <API Key>
    {"input": {"prompt": "凉水供电所这个月线损率多少"}, "parameters": {}}
返回（实测）：
    {"output": {"finish_reason": "stop", "session_id": "...",
                "text": "两水供电所这个月仙台区损率多少\n本月：2026年9月，上月：2026年8月"},
     "usage": {}, "request_id": "..."}
第一行是纠正后的问题（「凉水供电所」库里不存在，被改成真实的「两水供电所」），
第二行是统计时间。耗时实测 0.5–0.6 秒（冷启动偶尔十几秒）。
注意：该工作流会重写问题文本，偶发把指标名改坏（如「台区线损率」→「仙台区损率」），
所以本服务只把它当参考，指标与问法仍以原始问题为准。
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
