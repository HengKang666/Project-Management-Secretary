# -*- coding: utf-8 -*-
"""阶段①：字面纠错与名称归一（上游信息补全的「字面」部分）。

本服务的五段流程里，①「上游信息补全」是外置的、只补**统计时间与地点**。
这一步补的是**字面**：把说错的字改对、把只说半截的名称补成库里的标准名。

为什么必须排在 complete_question() 之前：
    「环谈供电所的台区线损」
      → 不改字：拿「环谈供电所」去检索知识库，检索不到 → 补全规则命中不了
      → 改对字：检索「环潭供电所」→ 命中补全规则 → 才可能补成标准问题
    字都没改对，后面的检索、补全、写 SQL 全是白费。

零第三方依赖：库只用标准库（拼音来自 libs/name_correction_lib/data/pinyin_table.csv），
不引入 pypinyin。

开关：环境变量 SECRETARY_NAMEFIX=0 可整体关闭本步。
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, 'libs')

_CORRECTOR = None
_FAILED = False


def _enabled():
    return os.environ.get('SECRETARY_NAMEFIX', '1') not in ('0', 'false', 'False', 'off')


def _corrector():
    """懒加载，加载失败永久降级（不反复重试、不抛异常）。

    构造要加载两万条名录索引，约 0.3 秒 —— 只做一次，之后常驻复用。
    """
    global _CORRECTOR, _FAILED
    if _CORRECTOR is not None or _FAILED:
        return _CORRECTOR
    try:
        if _LIB not in sys.path:
            sys.path.insert(0, _LIB)
        from name_correction_lib import Corrector
        _CORRECTOR = Corrector()
    except Exception as e:                      # noqa: BLE001
        _FAILED = True
        # flush：这个提示只在首次调用时打一次，日志重定向时若被缓冲就永远看不到
        print('[namefix] 纠错库加载失败，本步降级（不影响主流程）：%s: %s'
              % (type(e).__name__, e), flush=True)
    return _CORRECTOR


def available():
    """本步当前是否可用（页面/自测可以据此显示状态）。"""
    return bool(_enabled() and _corrector() is not None)


def status():
    """本步状态与原因，供 /health 与页面显示。

    词典（libs/name_correction_lib/data/）不入库，clone 下来默认是缺的：
    服务照跑、问答照答，**但纠错与归一静默失效**。所以要能一眼看出来，
    否则只能靠比对 name_fix_used 才发现。
    """
    if not _enabled():
        return {'available': False, 'reason': '已通过 SECRETARY_NAMEFIX=0 关闭'}
    if _corrector() is None:
        return {'available': False,
                'reason': '纠错词典未就绪（libs/name_correction_lib/data/ 缺失），本步已降级'}
    return {'available': True, 'reason': ''}


def fix(question):
    """把问题里说错的字改对、名称补全。

    返回 dict；**任何异常都退化为「原文 + used=False」** ——
    纠错是增强能力，不该让提问链路挂掉。
    """
    original = (question or '').strip()
    out = {'original': original, 'text': original, 'used': False,
           'word_fixes': [], 'name_fixes': [], 'areas': [],
           'names': [], 'need_clarify': [], 'hint': '', 'ms': 0}
    if not original or not _enabled():
        return out
    c = _corrector()
    if c is None:
        return out

    t0 = time.time()
    try:
        r = c.resolve(original)                 # resolve 内部已包含 correct
        text = r.resolved or original
        out['text'] = text
        out['used'] = text != original

        base = c.correct(original)              # 再取一次明细（有缓存，几乎不花时间）
        out['word_fixes'] = [{'错误': w.get('错误'), '正确': w.get('正确'),
                              '错因': w.get('错因')} for w in (base.word_fixes or [])]
        out['name_fixes'] = [{'错误': w.get('错误'), '正确': w.get('正确')}
                             for w in (base.name_fixes or [])]
        out['areas'] = [{'文本': a.get('文本'), '标准名': a.get('标准名'),
                         '已改正': a.get('已改正')} for a in (base.areas or [])]
        out['need_clarify'] = [x.get('提示') for x in (base.need_clarify or [])]

        for n in (r.names or []):
            d = n.to_dict()
            out['names'].append(d)
        out['unresolved'] = [u.get('文本') for u in (r.unresolved or [])]

        # 有「地名层合成名」时给一句提示：库里没有这个精确名，要按前缀查。
        # 不提示的话，模型很可能拿它做等值匹配，一条都查不到还以为是空数据。
        synth = [d for d in out['names'] if d.get('库内精确名') is False]
        if synth:
            parts = ['%s（按前缀 %s%% 查，共 %s 个同名台区）'
                     % (d['标准名'], d.get('建议前缀', ''), d.get('同名候选数', '?'))
                     for d in synth]
            out['hint'] = ('下面的名称是按「地名层」归一的合成名，**库里没有这几个字的精确名**，'
                           '查库请用 LIKE 前缀匹配，不要用等号：' + '；'.join(parts))
    except Exception as e:                      # noqa: BLE001
        out['error'] = '%s: %s' % (type(e).__name__, e)
        out['text'] = original
        out['used'] = False
    out['ms'] = int((time.time() - t0) * 1000)
    return out


if __name__ == '__main__':
    # 自测：python name_fix.py
    sys.stdout.reconfigure(encoding='utf-8')
    print('可用：%s' % available())
    for q in ['帮我查一下环谈供电所上个月的台去线损',
              '公家棚线损', '公家彭', '白鹤变压器',
              '厉山的线损', '凉水的线损', '情况怎么样']:
        r = fix(q)
        print('  %-22s → %-28s (%d ms)' % (q, r['text'], r['ms']))
        if r['hint']:
            print('      ! %s' % r['hint'])
