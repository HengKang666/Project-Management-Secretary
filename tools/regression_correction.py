# -*- coding: utf-8 -*-
"""纠错链路自测 —— 改引擎 / 换词典源后必跑。

五组：
  A 该改的（功能）    输入必须给出期望结果
  B 不该改的（安全）  输入必须原样返回，一个字都不许动   ← 误改比漏改严重
  C 台区召回（抽样）  拿库内真实台区名的「地名部分」回查，看认不认得出来
  D 边界输入          空 / 纯空格 / 纯标点 / 纯数字 / 纯英文 / 超长 / emoji / 换行
  E 并发与性能        多线程同时纠错结果必须与单线程一致；统计耗时分布

用例集沿用 tools/19_regression.py（那份是从旧副本加载的，本脚本指向本服务的链路）。

用法：
    python tools/regression_correction.py              # 抽样 300
    python tools/regression_correction.py 1000
"""
from __future__ import annotations

import csv
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEC = os.path.join(ROOT, 'secretary')
SEED_DIR = os.path.normpath(os.path.join(ROOT, '..', 'lexicon_seed'))

sys.path.insert(0, SEC)
sys.path.insert(0, os.path.join(SEC, 'libs'))
sys.stdout.reconfigure(encoding='utf-8')

import lex_source                                        # noqa: E402
from name_correction_lib import Corrector, use_source    # noqa: E402

# ---------------------------------------------------------------- A 该改的
A_CASES = [
    ("公家棚", "龚家棚台区"),
    ("公家彭", "龚家棚台区"),
    ("白鹤", "白鹤台区"),
    ("白鹤变压器", "白鹤变压器"),
    ("环谈供电所的线损", "环潭供电所的线损"),
    ("凉水的线损", "两水供电所的线损"),
    ("厉山的线损", "厉山供电所的线损"),
    ("量水供电所", "两水供电所"),
    ("随县供电公司", "国网随县供电公司"),
    ("随县供电公司的平均停电时长", "国网随县供电公司的平均停电时长"),
    ("九颗松台区", "九棵松台区"),
    ("九棵松台区情况", "九棵松台区情况"),
    ("九颗松", "九棵松台区"),
    ("九颗松的线损", "九棵松台区的线损"),
    ("9颗松", "九棵松台区"),
    ("9颗松情况", "九棵松台区情况"),
]

# -------------------------------------------------------------- B 不该改的
B_CASES = [
    "平均停电时长", "停电时长是多少", "用户平均停电时间", "用户平均停电时长",
    "线损率", "售电量", "意见工单数", "故障报修工单数", "故障跳闸次数",
    "停电检修", "停电原因", "停电时间", "低电压线路长度", "业扩报装",
    "供电可靠性", "产生经济效益", "工单预算执行率", "全市的线损率是多少",
    "帮我查一下深农台区线损", "这个月的售电量是多少",
    "全市整体情况", "全市整体情况怎么样", "各区县整体情况",
    "结合意见工单中哪些事件和我们当前需要治理的缺陷工单有关系",
]

# ---------------------------------------------------------------- D 边界
EDGE = [
    ("", "空字符串"),
    ("   ", "纯空格"),
    ("?", "纯标点"),
    ("？？？", "全角标点"),
    ("1234567890", "纯数字"),
    ("abc DEF", "纯英文"),
    ("的的了和与是", "纯虚词"),
    ("线损", "两字业务词"),
    ("龚家棚", "三字地名"),
    ("龚" * 300, "超长重复单字"),
    ("龚家棚线损" * 60, "超长正常句（300字）"),
    ("曾都区淅河镇龚家棚村4#台区40129的线损是多少", "长句含编号"),
    ("龚家棚\n线损", "含换行"),
    ("龚家棚\t线损", "含制表符"),
    ("龚家棚😀线损", "含 emoji"),
    (" 龚家棚 线损 ", "首尾空格"),
    ("龚家棚,线损", "含英文逗号"),
    ("龚家棚，线损", "含中文逗号"),
    ("龚家棚线损。", "含句号"),
    ("\u3000龚家棚\u3000", "全角空格"),
]

SUFFIX_RE = re.compile("|".join([
    "配电变压器", "柱上变压器", "箱式变压器", "变压器", "柱上变", "箱变",
    "公变", "专变", "室变", "变台", "台区", "配变", "变"]))
DIGIT_RE = re.compile(r"[0-9]+|[#＃]|号|组")


def place_core(name):
    s = SUFFIX_RE.sub("", name)
    s = re.sub(r"[A-Za-z0-9#＃（）()\s]+", "", s)
    s = DIGIT_RE.sub("", s)
    for w in ("村", "小区", "还建房", "组", "号"):
        s = s.replace(w, "")
    return s


def load_areas():
    """从种子目录读台区名（不查库，保证与词典来源无关）。"""
    with open(os.path.join(SEED_DIR, 'catalog.csv'), encoding='utf-8-sig', newline='') as f:
        return [r['名称'] for r in csv.DictReader(f)
                if r.get('类别') == '台区' and r.get('名称')]


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    t0 = time.time()

    src = lex_source.MysqlSource()
    use_source(src)
    c = Corrector()
    src.close()

    fails = []

    # ---------------------------------------------------------- A
    print("=" * 66)
    print("A 该改的（功能）")
    bad_a = 0
    for s, want in A_CASES:
        got = c.resolve(s).resolved
        ok = got == want
        bad_a += (not ok)
        print("  %s %-26s -> %s" % ("[OK]" if ok else "[!!]", s, got))
        if not ok:
            print("        期望: %s" % want)
    if bad_a:
        fails.append('A 功能 %d 条' % bad_a)

    # ---------------------------------------------------------- B
    print("\nB 不该改的（安全）")
    bad_b = []
    for s in B_CASES:
        got = c.resolve(s).resolved
        if got != s:
            bad_b.append((s, got))
    for s, got in bad_b:
        print("  [!!] %-26s -> %s" % (s, got))
    print("  %s 误改 %d / %d" % ("[OK]" if not bad_b else "[!!]", len(bad_b), len(B_CASES)))
    if bad_b:
        fails.append('B 误改 %d 条' % len(bad_b))

    # ---------------------------------------------------------- C
    print("\nC 台区召回（抽样 %d）" % n_sample)
    areas = load_areas()
    random.seed(42)
    sample = random.sample(areas, min(n_sample, len(areas)))
    loose = strict = tried = 0
    misses = []
    for nm in sample:
        core = place_core(nm)
        if len(core) < 3:
            continue
        tried += 1
        try:
            res = c.resolve(core)
        except Exception as e:                              # noqa: BLE001
            print("  [!!] resolve 抛异常：%s -> %s: %s" % (core, type(e).__name__, e))
            continue
        names = res.names or []
        if names and names[0].kind == "台区":
            loose += 1
        blob = " ".join([names[0].std] + list(names[0].alts or [])) if names else ""
        if nm in blob or core in blob:
            strict += 1
        elif len(misses) < 5:
            misses.append((core, names[0].std if names else "(空)"))
    print("  宽（认出是台区）: %d / %d = %.1f%%" % (loose, tried, loose / max(1, tried) * 100))
    print("  严（指向原台区）: %d / %d = %.1f%%" % (strict, tried, strict / max(1, tried) * 100))
    for core, got in misses:
        print("      漏掉：%-12s -> %s" % (core, got))

    # ---------------------------------------------------------- D
    print("\nD 边界输入（关注：不许抛异常、不许把长句改坏）")
    bad_d = 0
    for s, note in EDGE:
        try:
            r = c.correct(s)
            rr = c.resolve(s)
            changed = (r.corrected != s) or (rr.resolved != s)
            print("  [OK] %-22s len=%-4d changed=%-5s -> %s"
                  % (note, len(s), changed, (rr.resolved or '')[:40]))
        except Exception as e:                              # noqa: BLE001
            bad_d += 1
            print("  [!!] %-22s 抛异常：%s: %s" % (note, type(e).__name__, e))
    if bad_d:
        fails.append('D 边界 %d 条异常' % bad_d)

    # ---------------------------------------------------------- E
    print("\nE 并发一致性 + 性能")
    inputs = [s for s, _ in A_CASES] + B_CASES
    single = [c.resolve(s).resolved for s in inputs]

    def one(s):
        return c.resolve(s).resolved

    with ThreadPoolExecutor(max_workers=8) as ex:
        multi = list(ex.map(one, inputs * 3))                # 每个输入跑 3 遍
    ref = single * 3
    mismatch = [(inputs[i % len(inputs)], a, b)
                for i, (a, b) in enumerate(zip(multi, ref)) if a != b]
    if mismatch:
        fails.append('E 并发不一致 %d 处' % len(mismatch))
        for s, a, b in mismatch[:5]:
            print("  [!!] %s: 并发=%s 单线程=%s" % (s, a, b))
    else:
        print("  [OK] 8 线程 x %d 次：结果与单线程逐条一致" % len(multi))

    times = []
    for _ in range(3):
        for s in inputs:
            t = time.time()
            c.resolve(s)
            times.append((time.time() - t) * 1000)
    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[int(len(times) * 0.95)]
    print("  耗时 %d 次：均值 %.1fms  中位 %.1fms  P95 %.1fms  最大 %.1fms"
          % (len(times), sum(times) / len(times), p50, p95, times[-1]))

    print("\n" + "=" * 66)
    print("结论：%s（%.1fs）" % ("全部通过" if not fails else "有问题 -> %s" % fails,
                             time.time() - t0))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main())
