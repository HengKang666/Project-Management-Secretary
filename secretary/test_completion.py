# -*- coding: utf-8 -*-
"""测上游补全：口语问法进去，标准问题出来。看它补得准不准、有没有补出库里没有的指标。"""
import sys, time
sys.stdout.reconfigure(encoding='utf-8')
import tools_app

QS = [
    '情况怎么样',
    '花了多少钱',
    '哪些项目慢了',
    '和去年比怎么样',
    '哪个所最好',
    '三公花了多少',
    '转移支付多少',
    '群众满意度多少',
    '台区线损率高于5%的有多少',
    '随县这个月停电多不多',
]
for q in QS:
    t0 = time.time()
    r = tools_app.complete_question(q)
    print('【%s】' % q)
    if r.get('text'):
        print('  → %s' % r['text'])
        print('    %.1fs  model=%s  tokens=%s/%s' % (time.time() - t0, r.get('model_id'),
              r.get('input_tokens'), r.get('output_tokens')))
    else:
        print('  → 失败：%s' % r.get('error'))
    print()
