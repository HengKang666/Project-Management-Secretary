# -*- coding: utf-8 -*-
import json, ssl, sys, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
import config
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(config.LLM_BASE + '/models', headers={'Authorization': 'Bearer ' + config.LLM_KEY})
d = json.loads(urllib.request.urlopen(req, timeout=60, context=ctx).read().decode('utf-8', 'replace'))
ids = sorted(x.get('id') for x in d.get('data', []))
PAT = ('qwen3.8', 'qwen3.7', 'qwen3.6', 'qwen3.5', 'qwen3-max', 'qwen3-coder', 'qwen3-vl',
       'deepseek-v4', 'deepseek-v3', 'kimi-k', 'glm-5', 'MiniMax/MiniMax', 'kimi/')
keep = [i for i in ids if any(i.startswith(p) or i.lower().startswith(p.lower()) for p in PAT)]
print('候选聊天模型 =', len(keep))
for i in keep:
    print(' ', i)
