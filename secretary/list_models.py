# -*- coding: utf-8 -*-
import json, ssl, sys, urllib.request
sys.stdout.reconfigure(encoding='utf-8')
import config
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(config.LLM_BASE + '/models',
                             headers={'Authorization': 'Bearer ' + config.LLM_KEY})
try:
    d = json.loads(urllib.request.urlopen(req, timeout=60, context=ctx).read().decode('utf-8', 'replace'))
    ids = sorted(x.get('id') for x in d.get('data', []))
    print('模型数 =', len(ids))
    for i in ids:
        print(' ', i)
except Exception as e:
    print('FAIL', type(e).__name__, str(e)[:300])
