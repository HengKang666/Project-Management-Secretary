# -*- coding: utf-8 -*-
"""时间与统计范围补全：把「上个月」「今年」这类相对说法，换成库里认的具体期间。

**为什么用代码而不是让模型自己换算**
    「上个月」是哪一个月，取决于**今天是哪天**。模型不会去翻日历，
    稍不留神就把 8 月算成 9 月 —— 而且它算错了也看不出来。
    这种「确定性换算」交给代码：零成本、每次都一样、还能顺手把原词改掉。

**口径与库对齐**（`t_power_ai_metric_snapshot.period_type`）
    month       当月       period_key = YYYYMM
    year        全年       period_key = YYYY
    yearToDate  本年累计   period_key = YYYYMM（1 月累计到该月）

**「今年」为什么不给 year**
    今年还没过完。而且库里 station 层的当期数据只在 yearToDate 里
    （year 只到 2025，没有 2026）—— 给 `year=2026` 会一条都查不到。
    所以「今年」= yearToDate + 数据里最新的那个月。
"""
import datetime
import re

import tools_db

# ---------------------------------------------------------------- 中文数字
_CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
           '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12, '元': 1, '正': 1}
_CN_ALT = '|'.join(sorted(_CN_NUM, key=len, reverse=True))

# 相对月（长的排前面，「上个月」要先于「上月」命中）
_MONTH_REL = [('上个月', -1), ('上月', -1), ('前一个月', -1), ('前月', -1),
              ('本月份', 0), ('本月', 0), ('这个月', 0), ('当月', 0),
              ('下个月', 1), ('下月', 1)]

# 相对年
_YEAR_REL = [('今年以来', 0), ('年初至今', 0), ('本年度', 0),
             ('今年', 0), ('本年', 0),
             ('上一年', -1), ('去年', -1), ('上年', -1),
             ('前年', -2)]

# 数据最新月份缓存（进程级）—— 这个值一个进程里不会变，没必要反复查库
_LATEST = {'value': None}
# 「该月的当月值有没有」的缓存，按 YYYYMM 存（一个进程里数据不会变）
_HAS = {}
# 「最近一个有值的月份」缓存
_NEAR = {}
# 问题里出现这些词，就认为问的是「供电所/台区」这一层
_STATION_HINT = re.compile(r'供电所|供电服务站|台区|公变|专变|变压器|变电站|线路|台变')


def _latest_ym():
    """业务数据里最新的月份（YYYYMM）。查不到就用系统当月兜底。"""
    if _LATEST['value']:
        return _LATEST['value']
    now = datetime.date.today()
    fallback = '%04d%02d' % (now.year, now.month)
    try:
        rows = tools_db._exec(
            "SELECT MAX(period_key) AS k FROM t_power_ai_metric_snapshot "
            "WHERE deleted_flag=0 AND period_type='yearToDate'",
            limit_rows=1)['rows']
        k = str((rows[0].get('k') if rows else '') or '')
        _LATEST['value'] = k if re.fullmatch(r'\d{6}', k) else fallback
    except Exception:
        _LATEST['value'] = fallback
    return _LATEST['value']


def _month_has_data(ym):
    """该月份的「当月」口径有没有值（0 和 NULL 都算没值）。

    六项指标按月结算，当月往往要到月末才齐；历史上也有整月缺失的
    （全市层 2026 年 8 月连行都没有）。这两种情况都不能干巴巴回一句「查不到」。
    """
    if ym in _HAS:
        return _HAS[ym]
    ok = True
    try:
        rows = tools_db._exec(
            "SELECT COUNT(*) AS n FROM t_power_ai_metric_snapshot "
            "WHERE deleted_flag=0 AND period_type='month' AND period_key='%s' "
            "AND metric_value IS NOT NULL AND metric_value <> 0" % ym,
            limit_rows=1)['rows']
        ok = int((rows[0].get('n') if rows else 0) or 0) > 0
    except Exception:
        pass                            # 查不到就别下结论，按「有」处理
    _HAS[ym] = ok
    return ok


def _nearest_month(ym):
    """往回想，最近一个「当月值」有数据的月份（YYYYMM）。"""
    if ym in _NEAR:
        return _NEAR[ym]
    got = ''
    try:
        rows = tools_db._exec(
            "SELECT MAX(period_key) AS k FROM t_power_ai_metric_snapshot "
            "WHERE deleted_flag=0 AND period_type='month' AND period_key < '%s' "
            "AND metric_value IS NOT NULL AND metric_value <> 0" % ym,
            limit_rows=1)['rows']
        got = str((rows[0].get('k') if rows else '') or '')
    except Exception:
        pass
    _NEAR[ym] = got
    return got


def _ym_text(ym):
    """YYYYMM → 2026年8月（不合法就返回空串）。"""
    if re.fullmatch(r'\d{6}', str(ym or '')):
        return '%d年%d月' % (int(ym[:4]), int(ym[4:6]))
    return ''


def _is_station_level(question, fixed):
    """这次问的是不是「供电所/台区」这一层（快照表在这一层没有当月口径）。

    不能只看纠错结果：「环潭供电所」本来就是库里的标准名，纠错层不认为它需要改，
    names / areas 都是空的 —— 只看它就会漏判。所以在原问题上再按词面兜一道。
    """
    for n in ((fixed or {}).get('names') or []):
        if str(n.get('类别') or '') in ('供电所', '供电服务站', '台区'):
            return True
    if (fixed or {}).get('areas'):
        return True
    return bool(_STATION_HINT.search(str(question or '')))


def _to_int(s):
    s = (s or '').strip()
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s, 0)


def _mk(matched, text, ptype, pkey, is_default=False, note='', q=None):
    """组装结果；rewritten = 把问题里那个相对时间词换成具体时间的完整问题。"""
    rewritten = q
    if q and matched and matched != text:
        rewritten = q.replace(matched, text, 1)
    return {'matched': matched, 'time_text': text, 'period_type': ptype,
            'period_key': pkey, 'is_default': is_default, 'note': note,
            'rewritten': rewritten}


def resolve(question, today=None, prev=None):
    """解析问题里的时间说法。返回 dict（字段见 _mk）。

    优先级：具体年月 > 具体年 > 具体月 > 相对说法 > **沿用上一轮** > 默认（今年至今）。

    prev = 上一轮的统计范围（同一场会话）。**只在问题完全没提时间时才生效** ——
    用户这轮说了时间就必须用他说的。
    """
    q = str(question or '')
    today = today or datetime.date.today()
    ym = _latest_ym()
    cur_y, cur_m = int(ym[:4]), int(ym[4:6])

    # ① 具体年月：2026年8月 / 2026年08月 / 2026年八月
    m = re.search(r'(20\d{2})\s*年\s*(\d{1,2}|' + _CN_ALT + r')\s*月', q)
    if m:
        y, mo = int(m.group(1)), _to_int(m.group(2))
        if 1 <= mo <= 12:
            return _mk(m.group(0), '%d年%d月' % (y, mo), 'month',
                       '%04d%02d' % (y, mo), q=q)

    # ② 具体年：2026年（后面不再跟月份）
    m = re.search(r'(20\d{2})\s*年(?!\s*(?:\d{1,2}|' + _CN_ALT + r')\s*月)', q)
    if not m:
        # ②′ 裸年份：`2025` 不带「年」字（口语里很常见：「凉水2025售电量」）。
        #     放在这里、③（光说月份）之前；数字边界用 \d 卡死，免得切进长数字的中段。
        #     紧跟「月」的情况不进这一支（那是 ③ 的活），避免把「2025 8月」读成整年。
        m = re.search(r'(?<![\d.])(20\d{2})(?![\d.]|月)', q)
    if m:
        y = int(m.group(1))
        if y == cur_y:
            # 今年的年份说法，含义仍是「今年以来」
            return _mk(m.group(0), '%d年1-%d月' % (y, cur_m), 'yearToDate', ym, q=q)
        return _mk(m.group(0), '%d年' % y, 'year', str(y), q=q)

    # ③ 光说月份：8月（默认当年）
    m = re.search(r'(?<![\d年])(\d{1,2}|' + _CN_ALT + r')\s*月(?!份)', q)
    if m:
        mo = _to_int(m.group(1))
        if 1 <= mo <= 12 and mo <= cur_m:
            return _mk(m.group(0), '%d年%d月' % (cur_y, mo), 'month',
                       '%04d%02d' % (cur_y, mo), q=q)

    # ④ 相对月份：上月 / 本月 / 下月
    for word, off in sorted(_MONTH_REL, key=lambda x: -len(x[0])):
        if word in q:
            y, mo = cur_y, cur_m + off
            while mo <= 0:
                mo += 12
                y -= 1
            while mo > 12:
                mo -= 12
                y += 1
            return _mk(word, '%d年%d月' % (y, mo), 'month',
                       '%04d%02d' % (y, mo), q=q)

    # ⑤ 相对年份：今年 / 去年 / 前年
    for word, off in sorted(_YEAR_REL, key=lambda x: -len(x[0])):
        if word in q:
            y = cur_y + off
            if off == 0:
                return _mk(word, '%d年1-%d月' % (y, cur_m), 'yearToDate', ym, q=q)
            return _mk(word, '%d年' % y, 'year', str(y), q=q)

    # ⑥ 季度：库里没有季度口径，只能给区间 + 说明
    m = re.search(r'([1-4一二三四])\s*季度|本季度|这个季度', q)
    if m:
        head = m.group(0)
        if head in ('本季度', '这个季度'):
            qn = (cur_m - 1) // 3 + 1
        else:
            qn = _to_int(m.group(1)) or 1
        sm = (qn - 1) * 3 + 1
        em = sm + 2
        return _mk(head, '%d年%d-%d月' % (cur_y, sm, em), 'yearToDate', ym,
                   note='库里没有季度口径，只能按 %d 年 %d-%d 月取累计值，'
                        '回答时要说清这是区间累计而不是季度指标。' % (cur_y, sm, em), q=q)

    # ⑦ 什么都没说 → 优先沿用上一轮，其次默认今年至今
    if prev and prev.get('period_type') and prev.get('period_key'):
        return _mk('', prev.get('time_text') or str(prev.get('period_key')),
                   prev['period_type'], prev['period_key'], is_default=True,
                   note='问题里没说时间，沿用上一轮的「%s」（多轮会话）。'
                        % (prev.get('time_text') or prev.get('period_key')), q=q)
    return _mk('', '%d年1-%d月' % (cur_y, cur_m), 'yearToDate', ym, is_default=True,
               note='问题里没说时间，按「今年至今」理解。', q=q)


# 供电所/公司名单（从纠错库里取一次，按长度降序）
_UNITS = {'list': None}
# 简称表：把「国网随县供电公司」这类全名剥成核心「随县」，用于认出用户说的简称。
# 只认长度 >= 2 的核心，避免「所」「站」这类一个字的核心误命中。
_UNIT_CORES = {'list': None}
_STRIP_HEAD = ('国网', '湖北省', '湖北省电力', '随州市', '随州', '中国')
_STRIP_TAIL = ('供电服务站', '供电服务公司', '供电有限公司', '供电公司', '供电所', '服务站', '公司', '供电')


def _units():
    """库里的供电所/公司名，长的排前面（用于从问题里认出地点）。"""
    if _UNITS['list'] is None:
        out = []
        try:
            import name_fix
            c = name_fix._corrector()
            if c is not None:
                out = [(nm, k) for nm, k in c.kind.items()
                       if k in ('供电公司', '供电所', '供电服务站')]
                out.sort(key=lambda x: -len(x[0]))
        except Exception:
            out = []
        _UNITS['list'] = out
    return _UNITS['list']


def _strip_unit(name):
    """把全名剥成核心：国网随县供电公司 → 随县；环潭供电所 → 环潭。"""
    s = str(name or '').strip()
    changed = True
    while changed:
        changed = False
        for h in _STRIP_HEAD:
            if s.startswith(h) and len(s) > len(h) + 1:
                s = s[len(h):]
                changed = True
        for t in _STRIP_TAIL:
            if s.endswith(t) and len(s) > len(t) + 1:
                s = s[:-len(t)]
                changed = True
    return s.strip()


def _unit_cores():
    """核心 → 全名，长的核心排前面。"""
    if _UNIT_CORES['list'] is None:
        d = {}
        for nm, _k in _units():
            core = _strip_unit(nm)
            if len(core) >= 2 and core not in d:
                d[core] = nm
        _UNIT_CORES['list'] = sorted(d.items(), key=lambda x: -len(x[0]))
    return _UNIT_CORES['list']


# 地点层的类别（供电所 / 公司 / 服务站）。台区是**对象层**，单独处理，见 place_of。
_PLACE_KINDS = ('', '供电公司', '供电所', '供电服务站')


def place_of(fixed, question=''):
    """从纠错结果里取地点（= 本轮的统计范围）；一个都认不出就返回空串。

    纠错层只报告「被改过」的名字 —— 「环潭供电所」本来就在库里，
    它一个字都不改，names/areas 都是空的。所以还要拿名单在原问题上兜一道。

    ★ **台区也算地点**。库里的名字 99.7% 是台区（两万多条；供电所级只有几十条），
      只认供电所的话，「9颗松情况怎么样」的地点会被当成「全市」，
      下一句追问「情况怎么样，售电量，线损这些」就把「全市」沿用过去，整题答偏 ——
      用户问的是那个台区，答出来的却是全市的数。
      两级同时出现时取**台区**：更具体的那个才是本轮范围，
      否则「环潭供电所九棵松台区」会被退回整个供电所，数对不上。
    """
    names = (fixed or {}).get('names') or []
    areas = (fixed or {}).get('areas') or []
    unit_hit = ''
    area_hit = ''
    for src in (areas, names):
        for it in src:
            nm = str(it.get('标准名') or it.get('文本') or '').strip()
            if not nm:
                continue
            kd = str(it.get('类别') or '')
            if kd == '台区':
                area_hit = area_hit or nm
            elif kd in _PLACE_KINDS:
                unit_hit = unit_hit or nm
    if area_hit or unit_hit:
        return area_hit or unit_hit
    # 原问题和纠错后的问题都比一遍：「凉水供电所」在纠错后是「两水供电所」，
    # 名单里存的是后者，只比原问题就认不出来。
    blob = (question or '') + ' ' + str((fixed or {}).get('text') or '')
    for nm, _k in _units():
        if nm in blob:
            return nm
    # 只说了简称（「随县」而名单里是「国网随县供电公司」）时，用核心名兜一道。
    # 放在全名匹配之后：全名命中优先，避免简称把更具体的地名抢走。
    for core, full in _unit_cores():
        if core in blob:
            return full
    return ''


# 「明说是全市范围」的说法。place_of 对「明说全市」和「什么都没说」都返回空串，
# 所以多轮里判断"要不要沿用上一轮地点"时，必须靠这几个词把两者分开。
_WHOLE_CITY = ('全市', '整个市', '市局', '各区县', '各县区', '各个区县', '所有区县', '各县', '各所')


def _says_whole_city(question):
    """问题里是不是**明说**要全市范围（而不是"没说地点"）。"""
    q = str(question or '')
    return any(w in q for w in _WHOLE_CITY)


def describe(question, fixed=None, prev=None, today=None):
    """一步到位：返回 {time, place, scope_text, question, inherited}。

    scope_text 是给模型看的【统计范围】说明；question 是补全后的完整问题。
        · 相对时间词（上月/本月/今年…）会被换成具体时间，写回问题里；
        · 问题里没时间、没地点 → **优先沿用上一轮**（多轮会话），
          再没有才补默认值（今年至今 / 全市）；沿用与默认都**不改写原问题**，
          只附说明，免得把用户的原话改得面目全非。

    prev = 上一轮的统计范围（同一场会话）：{period_type, period_key, time_text, place}
    """
    t = resolve(question, today=today, prev=prev)
    place = place_of(fixed, question)
    place_default = False
    place_inherited = False
    if not place:
        # 没认出地点：上一轮有、且这轮没明说「全市」这类范围词 → 沿用上一轮。
        # 注意必须排除「全市」：place_of 对「明说全市」和「什么都没说」都返回空串，
        # 不加这一个判断，用户问「那全市的呢」会被错误地沿用到上一轮的供电所。
        if prev and prev.get('place') and not _says_whole_city(question):
            place = prev['place']
            place_inherited = True
        else:
            place = '全市'
            place_default = True
    ym = _latest_ym()
    cur_y, cur_m = int(ym[:4]), int(ym[4:6])

    lines = []
    lines.append('时间：%s（%s，库内字段 period_type=%s、period_key=%s）'
                 % (t['time_text'],
                    {'month': '当月', 'year': '全年', 'yearToDate': '本年累计'}.get(t['period_type'], ''),
                    t['period_type'], t['period_key']))
    # 「用的默认值 / 沿用上一轮」这类说明由 note 给出（resolve 里已写好），这里不重复
    if t['note']:
        lines.append('      ' + t['note'])
    if place_inherited:
        # 沿用的地点是**推断**出来的，不是用户这轮说的 —— 必须允许模型推翻，
        # 否则用户换成别的简称（我们没认出来的）就会被错误地按上一轮的口径查。
        lines.append('地点：%s（本轮问题里没认出地点，沿用上一轮。'
                     '**如果本轮问题里其实提到了别的地点，以问题里说的为准**）' % place)
    else:
        lines.append('地点：%s%s' % (place, '（问题里没说地点，默认全市）' if place_default else ''))

    # 口径可用性：提前说清，免得模型取不到数就干巴巴回一句「未找到」
    if t['period_type'] == 'month':
        if not _month_has_data(t['period_key']):
            near = _nearest_month(t['period_key'])
            if t['period_key'] == ym:
                lines.append('      ⚠️ 最新月份（%s）的「当月」值还没出齐，按这个口径取不到数 —— '
                             '请改用「%d年1-%d月」的本年累计值来答，并说明这是累计口径。'
                             % (t['time_text'], cur_y, cur_m))
            elif near:
                lines.append('      ⚠️ %s 的「当月」值在快照表里是空的（数据缺失），'
                             '最近有值的是 %s —— 请改用它取数，并在回答里说明改用了哪个月。'
                             % (t['time_text'], _ym_text(near)))
            else:
                lines.append('      ⚠️ %s 的「当月」值在快照表里是空的 —— '
                             '请改用本年累计来答，并说明口径。' % t['time_text'])
        if _is_station_level(question, fixed):
            lines.append('      ⚠️ 六项指标快照表在「供电所」粒度没有当月口径（只有本年累计和全年），'
                         '要供电所的数请改用本年累计。')

    return {'time': t, 'place': place, 'place_default': place_default,
            'inherited': {'time': bool(t['note'] and '沿用上一轮' in t['note']),
                          'place': place_inherited},
            'scope_text': '\n'.join(lines), 'question': t['rewritten'] or question}
