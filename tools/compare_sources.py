# -*- coding: utf-8 -*-
"""双源对比：确认「读 CSV」和「读数据库」两种词典来源给出**完全一致**的纠错结果。

【为什么需要它】
    词典搬家最怕的不是报错，而是**静默变样**：少一条、编码坏一个字符、
    预计算列没搬全 —— 都不会抛异常，只是某些问题纠正得不一样了。
    所以除了「行数对得上」「逐字段全等」（tools/verify_lexicon.py），
    还要在**行为层面**比一遍：同一批输入，两个引擎的输出必须一个字都不差。

【怎么跑】
    python tools/compare_sources.py            # 抽样 500（默认）
    python tools/compare_sources.py 2000       # 抽样 2000

【实现说明】
    两种模式在**两个独立子进程**里跑，各自 dump 成 JSON，最后比对。
    不放在同一进程是因为拼音表是模块级全局缓存，两个引擎会互相干扰 ——
    分进程才能干净地隔离。
"""
from __future__ import annotations

import csv
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                                  # deploy_server2.0/
SEC = os.path.join(ROOT, 'secretary')
LIB = os.path.join(SEC, 'libs')
SEED_DIR = os.path.normpath(os.path.join(ROOT, '..', 'lexicon_seed'))

# ---------------------------------------------------------------- 固定用例
# 覆盖：错字纠正、名称归一、简称补全、单位识别、台区召回、歧义反问、以及「一个字都不许改」的安全用例
FIXED = [
    # A 该改的
    '公家棚', '公家彭', '白鹤', '白鹤变压器', '白鹤2组台区线损',
    '环谈供电所的线损', '凉水的线损', '厉山的线损', '量水供电所',
    '随县供电公司', '随县供电公司的平均停电时长',
    '九颗松台区', '九棵松台区情况', '九颗松', '九颗松的线损', '9颗松',
    '曾都区淅河镇九颗松台区', '公加朋村',
    # B 不该改的（必须原样返回）
    '全市整体情况', '全市政协台区情况', '售电量和线损率是多少',
    '各区县整体情况', '结合意见工单分析', '情况怎么样',
    '今年的线损率', '那售电量呢', '高新公司整体情况',
]


def build_cases(n):
    """固定用例 + 从真实台区名派生的用例（固定随机种子，两个子进程生成同一批）。"""
    path = os.path.join(SEED_DIR, 'catalog.csv')
    if not os.path.exists(path):
        raise SystemExit('[对比] 找不到种子目录的 catalog.csv：%s' % path)
    with open(path, encoding='utf-8-sig', newline='') as f:
        cat = list(csv.DictReader(f))

    areas = [r['名称'] for r in cat if r.get('类别') == '台区' and r.get('名称')]
    random.seed(20260920)                      # 固定种子：两次抽样必须一样
    picked = random.sample(areas, min(n, len(areas)))

    cases = list(FIXED)
    cases.extend(picked)                       # 完整台区名
    for nm in picked[:max(1, n // 2)]:
        # 剥掉尾部编号/类型后缀，模拟用户只说地名（这是纠错最容易出岔子的地方）
        core = nm
        for suf in ('台区', '公变', '专变', '变压器', '配电室'):
            if core.endswith(suf):
                core = core[:-len(suf)]
                break
        core = re.sub(r'[0-9#＃]+$', '', core)
        if len(core) >= 3:
            cases.append(core)
            cases.append(core + '的线损')
    return cases


# ---------------------------------------------------------------- 子进程：跑一种模式
def child(mode, out_path, n):
    sys.path.insert(0, SEC)
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    if mode == 'db':
        import lex_source
        from name_correction_lib import use_source
        src = lex_source.MysqlSource()
        use_source(src)
        print('[子进程] 数据源 = 数据库')
    else:
        from name_correction_lib import use_source
        use_source(None)
        print('[子进程] 数据源 = CSV')

    from name_correction_lib import Corrector, describe_data
    t0 = time.time()
    c = Corrector()
    load_ms = int((time.time() - t0) * 1000)
    print('[子进程] 加载耗时 %d ms；%s' % (load_ms, describe_data()))
    if mode == 'db':
        src.close()

    cases = build_cases(n)
    t0 = time.time()
    out = []
    for text in cases:
        row = {'in': text}
        try:
            row['correct'] = c.correct(text).to_dict()
        except Exception as e:                                  # noqa: BLE001
            row['correct'] = {'__error__': '%s: %s' % (type(e).__name__, e)}
        try:
            row['resolve'] = c.resolve(text).to_dict()
        except Exception as e:                                  # noqa: BLE001
            row['resolve'] = {'__error__': '%s: %s' % (type(e).__name__, e)}
        out.append(row)
    run_ms = int((time.time() - t0) * 1000)
    print('[子进程] %d 条用例，共 %d ms（平均 %.2f ms/条）'
          % (len(cases), run_ms, run_ms / max(1, len(cases))))

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'mode': mode, 'load_ms': load_ms, 'run_ms': run_ms, 'rows': out},
                  f, ensure_ascii=False, sort_keys=True)
    return 0


# ---------------------------------------------------------------- 主进程：调度 + 比对
def main():
    if len(sys.argv) > 1 and sys.argv[1] == '_child':
        return child(sys.argv[2], sys.argv[3], int(sys.argv[4]))

    tmp = tempfile.mkdtemp(prefix='lexcmp_')
    a_path = os.path.join(tmp, 'csv.json')
    b_path = os.path.join(tmp, 'db.json')

    n = int(sys.argv[1]) if len(sys.argv) > 1 and str(sys.argv[1]).isdigit() else 500
    print('抽样 %d 条真实台区（另加 %d 条固定用例）' % (n, len(FIXED)))
    py = sys.executable
    me = os.path.abspath(__file__)
    for mode, path in (('csv', a_path), ('db', b_path)):
        print('\n===== 跑 %s 模式 =====' % mode)
        r = subprocess.run([py, me, '_child', mode, path, str(n)], cwd=SEC)
        if r.returncode != 0:
            raise SystemExit('[对比] %s 模式失败（退出码 %d）' % (mode, r.returncode))

    a = json.load(open(a_path, encoding='utf-8'))
    b = json.load(open(b_path, encoding='utf-8'))
    ra, rb = a['rows'], b['rows']
    print('\n===== 比对 =====')
    print('  CSV 模式：加载 %d ms，纠错 %d ms' % (a['load_ms'], a['run_ms']))
    print('  DB  模式：加载 %d ms，纠错 %d ms' % (b['load_ms'], b['run_ms']))
    print('  用例数：%d / %d' % (len(ra), len(rb)))

    if len(ra) != len(rb):
        print('  [FAIL] 用例数不一致')
        return 1

    diff = []
    for x, y in zip(ra, rb):
        if x['in'] != y['in']:
            diff.append(('用例错位', x['in'], y['in']))
            continue
        for key in ('correct', 'resolve'):
            if json.dumps(x[key], ensure_ascii=False, sort_keys=True) != \
               json.dumps(y[key], ensure_ascii=False, sort_keys=True):
                diff.append((key, x['in'], x[key], y[key]))

    if not diff:
        print('\n  全部 %d 条用例、correct + resolve 两个输出**逐字段完全一致** [OK]' % len(ra))
        return 0

    print('\n  [FAIL] 有 %d 处不一致：' % len(diff))
    for d in diff[:15]:
        if d[0] == '用例错位':
            print('    用例错位: %r vs %r' % (d[1], d[2]))
        else:
            print('    [%s] 输入 %r' % (d[0], d[1]))
            print('        CSV: %s' % json.dumps(d[2], ensure_ascii=False, sort_keys=True))
            print('        DB : %s' % json.dumps(d[3], ensure_ascii=False, sort_keys=True))
    return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
