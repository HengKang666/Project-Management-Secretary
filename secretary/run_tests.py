# -*- coding: utf-8 -*-
"""批量测试：同一批题、可切模型，结果落盘 output/。"""
import argparse
import json
import pathlib
import time

import agent

QUESTIONS = [
    ('综合', '截至2026年9月，全市的总体情况如何？请分别说明预算安排、执行进度和治理成效。'),
    ('预算', '截至2026年9月，全市的预算总额是多少？'),
    ('预算', '截至2026年9月，全市累计支出多少？预算执行率是多少？'),
    ('预算', '全市2026年预算与2025年相比变化了多少？'),
    ('项目', '截至2026年9月，全市相关项目的整体执行率是多少？已完成和在建项目各多少？'),
    ('项目', '截至2026年9月，全市有哪些执行偏慢或存在风险的项目？'),
    ('项目', '截至2026年9月，全市已完工项目的验收情况如何？验收通过率是多少？'),
    ('治理', '截至2026年9月，全市的治理成效如何？主要绩效指标完成情况怎样？'),
    ('治理', '2026年9月全市的线损率是多少？'),
    ('治理', '各区域中治理表现最好的是哪个？关键指标数据是多少？'),
    ('主数据', '全市有多少个供电所？'),
    ('对比', '全市各区域中项目执行表现最好的是哪个？'),
    ('超范围', '随州市2026年酒店入住率是多少？'),
    ('超范围', '随州市2026年的全社会用电量是多少？'),
    ('超范围', '截至2026年9月，全市的财政总收入是多少？'),
]

OUT = pathlib.Path(pathlib.Path(__file__).resolve().parents[1] / 'output')
out = OUT
out.mkdir(parents=True, exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model', default='qwen3.8-max')
    ap.add_argument('--max-steps', type=int, default=0)  # 0 = 不限
    ap.add_argument('--tag', default='')
    args = ap.parse_args()
    tag = args.tag or args.model.replace('.', '')
    rows = []
    for i, (cat, q) in enumerate(QUESTIONS, 1):
        t0 = time.time()
        try:
            r = agent.ask(q, model=args.model, max_steps=args.max_steps)
        except Exception as e:
            r = {'answer': 'EXC ' + type(e).__name__ + ' ' + str(e), 'trace': [], 'elapsed_ms': 0,
                 'db_query_count': 0, 'kb_query_count': 0, 'no_db_query': True, 'steps': 0}
        r['category'] = cat
        r['seconds'] = round(time.time() - t0, 1)
        rows.append(r)
        print('[%2d/%d] %s  %s' % (i, len(QUESTIONS), cat, q))
        print('    %s' % r['answer'].replace('\n', ' ')[:200])
        print('    %.1fs 查库%d 知识库%d 工具%d%s' % (r['seconds'], r['db_query_count'],
              r['kb_query_count'], r['steps'], '  <<< 未查库' if r['no_db_query'] else ''))
        print()
    (out / ('test_raw_' + tag + '.json')).write_text(
        json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    n = len(rows)
    print('=== 汇总 %s ===' % args.model)
    print('题数 %d | 未查库就答 %d | 平均耗时 %.1fs | 平均工具调用 %.1f | 知识库调用 %d 条含' % (
        n, sum(1 for r in rows if r['no_db_query']), sum(r['seconds'] for r in rows) / n,
        sum(r['steps'] for r in rows) / n, sum(1 for r in rows if r['kb_query_count'] > 0)))


if __name__ == '__main__':
    main()
