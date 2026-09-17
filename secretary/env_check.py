# -*- coding: utf-8 -*-
"""环境自检：换一台机器部署后先跑这个，一眼看出缺哪一层。

只打印「有没有 / 多少条 / 多长」，不打印任何凭据值。
"""
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

print('=' * 60)
print('1. 读的是哪个 .env')
print('=' * 60)
for lv in ('SECRETARY_ENV',):
    print('  环境变量 %s = %r' % (lv, os.environ.get(lv)))
try:
    import config
    p = config.ENV_PATH
    print('  实际生效路径 : %s' % p)
    print('  文件存在     : %s' % os.path.exists(p))
    if os.path.exists(p):
        b = open(p, 'rb').read()
        print('  指纹         : %d 字节  sha256=%s' % (len(b), hashlib.sha256(b).hexdigest()[:16]))
except Exception as e:
    print('  !! config 导入失败：%s: %s' % (type(e).__name__, e))
    print('  （多半是 .env 没建或路径不对：把 .env.example 复制成 .env 填好）')
    raise SystemExit(1)

print()
print('=' * 60)
print('2. 连的是哪个库（只看非敏感项）')
print('=' * 60)
print('  host=%s port=%s user=%s db=%s' % (config.DB.get('host'), config.DB.get('port'), config.DB.get('user'), config.DB.get('db')))
print('  DB_PASSWORD 长度 = %s' % len(str(config.DB.get('password') or '')))
print('  LLM_BASE_URL 长度 = %s   LLM_KEY 长度 = %s' % (len(config.LLM_BASE), len(config.LLM_KEY)))

import semantic
import tools_db

print()
print('=' * 60)
print('3. 提示词（决定回答规则/业务规则/查询规则）')
print('=' * 60)
print('  代码要注入的 key: %s' % config.PROMPTS)
try:
    pool = semantic.prompts()
    for k in (config.PROMPTS or []):
        v = pool.get(k)
        print('    %-18s %s' % (k, ('有, %d 字' % len(v)) if v else '**缺失** -> 会用兜底提示，回答会明显变差'))
    print('  库里一共有 %d 个提示词 key' % len(pool))
except Exception as e:
    print('  !! 查不到提示词：%s: %s' % (type(e).__name__, str(e)[:150]))
try:
    import agent
    s = agent._system()
    print('  最终系统提示长度 = %d 字 %s' % (len(s), '（正常）' if len(s) > 3000 else '（**偏短，提示词很可能没注入**）'))
except Exception as e:
    print('  !! 拼系统提示失败：%s: %s' % (type(e).__name__, str(e)[:150]))

print()
print('=' * 60)
print('4. 业务字典（决定模型知不知道查哪张表）')
print('=' * 60)
for tb in ('ai_table_metadata', 'ai_column_metadata', 'ai_metric_metadata', 'ai_column_synonym', 'ai_table_relation', 'ai_prompt'):
    try:
        r = tools_db._exec('SELECT COUNT(*) AS c FROM ai_data.' + tb)
        print('    %-22s %s 行' % (tb, r['rows'][0]['c']))
    except Exception as e:
        print('    %-22s !! %s' % (tb, str(e)[:80]))
try:
    print('  可查的表 %d 张' % len(semantic.allowed_tables()))
except Exception as e:
    print('  可查的表 !! %s' % str(e)[:80])

print()
print('=' * 60)
print('5. 知识库检索（决定术语与问题补全）')
print('=' * 60)
try:
    import tools_kb
    r = tools_kb.kb_search('线损率', top_k=2)
    nodes = r.get('nodes') or []
    print('  检索「线损率」命中 %d 条 %s' % (len(nodes), '（正常）' if nodes else '（**检索不到，补全与术语会失效）**'))
    for n in nodes[:2]:
        print('    %s / %s' % (n.get('doc_name'), n.get('title')))
except Exception as e:
    print('  !! 检索失败：%s: %s' % (type(e).__name__, str(e)[:150]))

print()
print('=' * 60)
print('6. 真库连通')
print('=' * 60)
try:
    print('  SELECT 1 -> %s' % (tools_db.run_sql('SELECT 1 AS ok').get('rows') or [{}])[0])
except Exception as e:
    print('  !! 连不上：%s: %s' % (type(e).__name__, str(e)[:150]))
print()
