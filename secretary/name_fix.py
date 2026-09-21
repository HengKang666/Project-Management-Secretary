# -*- coding: utf-8 -*-
"""阶段①：字面纠错与名称归一（上游信息补全的「字面」部分）。

本服务的五段流程里，①「上游信息补全」是外置的、只补**统计时间与地点**。
这一步补的是**字面**：把说错的字改对、把只说半截的名称补成库里的标准名。

为什么必须排在 complete_question() 之前：
    「环谈供电所的台区线损」
      → 不改字：拿「环谈供电所」去检索知识库，检索不到 → 补全规则命中不了
      → 改对字：检索「环潭供电所」→ 命中补全规则 → 才可能补成标准问题
    字都没改对，后面的检索、补全、写 SQL 全是白费。

纠错库本身零第三方依赖（只用标准库，拼音查离线表），但它默认**从数据库取词典**：
    SECRETARY_LEXICON=db            读 agent_data 的 6 张 t_nc_* 表（默认）
    SECRETARY_LEXICON=file          读 libs/name_correction_lib/data/ 下的 CSV（零依赖，供回退）
    SECRETARY_LEXICON_RECHECK=60    词典热更新探测间隔（秒）；0 = 只在启动时加载一次

★ 数据库只在「加载词典」时用到一次：全量读进内存建好索引后就不再查库，
  之后每次纠错都是纯内存运算（毫秒级）。
  数据库是词典**存放的地方**，不是纠错时要去问的对象。

开关：环境变量 SECRETARY_NAMEFIX=0 可整体关闭本步。
"""
from __future__ import annotations

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, 'libs')

try:
    import config                       # 同目录；服务运行时必然已经加载过
except Exception:                       # noqa: BLE001
    config = None

_CORRECTOR = None
_FAILED = False
_FAILED_AT = 0.0                        # 上次加载失败的时间（隔一会儿允许重试）
_VER = None                             # 词典版本指纹（db 模式），用于热更新比对
_VER_AT = 0.0                           # 上次探测版本的时间
# ★ 保护首次加载：服务是 ThreadingHTTPServer，每个请求一个线程。不加锁的话，
#   服务刚起来时几个并发请求会同时去建数据库连接读词典，pymysql 报
#   「Packet sequence number wrong - got 232 expected 1」，然后整个纠错步被
#   判成永久降级。这个坑很隐蔽 —— 单进程直接跑 name_fix.py 永远复现不了。
_LOAD_LOCK = threading.Lock()
_RELOAD_LOCK = threading.Lock()         # 保护热重载
_RETRY_SEC = 60                         # 加载失败后多久允许再试一次（数据库恢复能自愈）

# 词典数据源：db = 读 agent_data 的 t_nc_* 表（默认）；file = 读 libs 下的 CSV
_SOURCE_MODE = (getattr(config, 'LEXICON_SOURCE', None)
                or os.environ.get('SECRETARY_LEXICON', 'db')).strip().lower()
_DB_MODE = _SOURCE_MODE == 'db'

# 热更新探测间隔（秒）；0 = 关闭（只在启动时加载一次）
_RECHECK_SEC = getattr(config, 'LEXICON_RECHECK_SEC', None)
if _RECHECK_SEC is None:
    try:
        _RECHECK_SEC = int(os.environ.get('SECRETARY_LEXICON_RECHECK', '60'))
    except (TypeError, ValueError):
        _RECHECK_SEC = 60


def _source_label():
    """数据源标签，供 /health 显示（file / db:agent_data）。"""
    if not _DB_MODE:
        return 'file'
    return 'db:%s' % (getattr(config, 'LEXICON_DB_NAME', None) or 'agent_data')


def _enabled():
    return os.environ.get('SECRETARY_NAMEFIX', '1') not in ('0', 'false', 'False', 'off')


def _build_source():
    """造一个词典数据源。file 模式返回 None —— 让纠错库自己去读同目录的 CSV。"""
    if not _DB_MODE:
        return None
    import lex_source               # 同目录；只有 db 模式才碰数据库
    return lex_source.MysqlSource()


def _load_corrector():
    """构造一个全新的 Corrector（含数据源）。"""
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    from name_correction_lib import Corrector, use_source

    src = _build_source()
    use_source(src)                 # ★ 必须在构造之前：所有索引都是 __init__ 里建的
    try:
        return Corrector()
    finally:
        if src is not None:
            src.close()             # ★ 加载完立刻关连接，不做长连接


def _peek_version():
    """查一次词典版本指纹；查不到返回 None（不抛）。"""
    try:
        import lex_source
        return lex_source.MysqlSource.current_version()
    except Exception as e:          # noqa: BLE001
        print('[namefix] 词典版本探测失败：%s: %s' % (type(e).__name__, e), flush=True)
        return None


def _corrector():
    """懒加载；加载失败降级，但**过一会儿允许重试**（数据库恢复后能自愈）。

    构造要加载四万多行词典：读 CSV 约 0.3 秒，读数据库（公网）约 7 秒。
    只做一次，之后常驻复用。

    ★ 必须加锁，原因见 _LOAD_LOCK 的注释（并发首次加载会把数据库连接搞坏）。
    """
    global _CORRECTOR, _FAILED, _FAILED_AT, _VER, _VER_AT
    if _CORRECTOR is not None:
        return _CORRECTOR
    if _FAILED and (time.time() - _FAILED_AT) < _RETRY_SEC:
        return None                 # 刚失败过，先别再试（避免每个请求都白等一次）
    with _LOAD_LOCK:
        if _CORRECTOR is not None:                  # 双检：别的线程可能刚加载完
            return _CORRECTOR
        if _FAILED and (time.time() - _FAILED_AT) < _RETRY_SEC:
            return None
        try:
            _CORRECTOR = _load_corrector()
            if _DB_MODE:
                _VER = _peek_version()              # 记下版本，供后面比对新旧
            _VER_AT = time.time()
            _FAILED = False
        except Exception as e:                      # noqa: BLE001
            _FAILED = True
            _FAILED_AT = time.time()
            # flush：这个提示日志重定向时若被缓冲就永远看不到
            print('[namefix] 纠错库加载失败，本步降级（不影响主流程）：%s: %s'
                  % (type(e).__name__, e), flush=True)
    return _CORRECTOR


def _maybe_reload():
    """每隔 _RECHECK_SEC 秒比对一次词典版本，变了就把纠错引擎整体重建。

    目的：人工改完词典**不用重启服务**就生效（默认最多延迟 60 秒）。

    ★ 必须整体重建，不能原地改索引 —— Corrector 里还有 _cache / _best_cache /
      _scache 三个实例级缓存，原地改会出现「新词典 + 旧缓存」的脏读。
      Python 给全局名赋值是原子的，正在处理的请求继续持有旧实例（不可变、跑得完），
      所以新旧切换天然安全，不必额外加读锁。
    """
    global _CORRECTOR, _VER, _VER_AT
    if _CORRECTOR is None or not _DB_MODE or _RECHECK_SEC <= 0:
        return
    if time.time() - _VER_AT < _RECHECK_SEC:
        return
    with _RELOAD_LOCK:
        if time.time() - _VER_AT < _RECHECK_SEC:      # 双检：并发时别的线程可能刚查过
            return
        _VER_AT = time.time()
        v = _peek_version()
        if not v or v == _VER:
            return
        try:
            new = _load_corrector()
        except Exception as e:                        # noqa: BLE001
            print('[namefix] 词典已更新但重载失败，继续用旧词典：%s: %s'
                  % (type(e).__name__, e), flush=True)
            return
        _CORRECTOR, _VER = new, v                     # 原子替换
        print('[namefix] 检测到词典更新，已重载（版本 %s）' % v, flush=True)


def available():
    """本步当前是否可用（页面/自测可以据此显示状态）。"""
    return bool(_enabled() and _corrector() is not None)


def status():
    """本步状态与原因，供 /health 与页面显示。

    词典可能**静默失效**，所以要能一眼看出来：
      · file 模式：libs/name_correction_lib/data/ 不在（clone 下来默认是缺的）
      · db   模式：agent_data 的 t_nc_* 表没建 / 表是空的 / 连接不上
    不报出来就只能靠比对 name_fix_used 才发现了。
    """
    if not _enabled():
        return {'available': False, 'reason': '已通过 SECRETARY_NAMEFIX=0 关闭',
                'source': _source_label()}
    if _corrector() is None:
        reason = ('纠错词典未就绪（数据源 db：agent_data 的 t_nc_* 表没建、为空或连不上）'
                  if _DB_MODE else
                  '纠错词典未就绪（libs/name_correction_lib/data/ 缺失）')
        return {'available': False, 'reason': reason + '，本步已降级',
                'source': _source_label()}
    return {'available': True, 'reason': '', 'source': _source_label()}


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
    _maybe_reload()                    # 词典被人改过就整体重建（默认每 60 秒探一次版本）
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
