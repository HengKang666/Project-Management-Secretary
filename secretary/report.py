# -*- coding: utf-8 -*-
"""年度缺陷治理计划（报告剧本）。

接到请求后：按 reports/annual-defect-plan.json 跑确定性 SQL 取数 -> 1 次模型做判断（归因/目标/优先级）
-> 1 次模型成文 -> 返回 6 段报告 + 6 步轨迹 + 对账结果。

本文件只做编排与对账；业务口径全部来自剧本 JSON 与业务字典，不写业务规则。
"""
import concurrent.futures
import json
import os
import re
import time

import agent
import tools_db

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYBOOK = os.path.join(HERE, 'reports', 'annual-defect-plan.json')

MODEL1_SYSTEM = (
    '你是随州供电公司的项目管理秘书，现在只做年度缺陷治理计划的【判断】，不写报告正文。\n'
    '输入是一份事实包（全部来自数据库查询）和判定规则。只看事实包里的数字，不许引入外部数据、不许自己编公式。\n'
    '只输出一个 JSON 对象，不要输出解释文字，不要用代码块包起来：\n'
    '{"attribution":"一段话（150字内），说明本范围的钱为什么这么分：预算结果与得分如何对应",'
    '"targets":[{"metric":"指标名","why":"依据（必须引用事实包里的数字）","direction":"想改善的方向"}],'
    '"projects":[{"priority":1,"name":"项目方向","reason":"为什么先做它"}]}\n'
    '硬规则：\n'
    '1) targets 只能从规则给出的候选指标里选，取 2-3 项；选哪几项由你依据"本范围 vs 全市"的对比和成效现状判断，理由写进 why；\n'
    '2) 每条 why / reason 必须引用事实包里的具体数字；\n'
    '3) 事实包里的金额单位已经是【万元】，直接用，不要换算、不要自己加小数位；\n'
    '4) 事实包里 incomplete 列出的供电所得分不完整，不得参与排名比较，提到它必须标注"数据缺失"；\n'
    '4.1) raw_value_all_zero 里的指标，其"原始值为 0"的供电所超过半数，说明该指标没采集到数据、得分高是假象，'
    '不得用它推出"资金缺口大/需求最迫切"，也不得选它当治理目标；\n'
    '5) 数据缺失就写"数据缺失"，不许估计、不许补 0。'
)

MODEL2_SYSTEM = (
    '你是随州供电公司的项目管理秘书。根据【事实包】和【已确定的判断】，写一份可以直接上报的年度缺陷治理计划（Markdown）。\n'
    '章节固定六节，标题必须是这六行：\n'
    '## 一、预算分配情况\n## 二、16 项指标得分分析\n## 三、今年缺陷治理目标\n## 四、缺陷治理项目计划\n## 五、预计成效\n## 六、计划上报\n'
    '硬规则：\n'
    '1) 报告里出现的每一个数字都必须能在事实包里找到，不许自己算、不许估计、不许引用外部数据；\n'
    '2) 金额一律用【万元】并保留两位小数（事实包里已经是万元）；成效指标的数值必须带事实包给的单位；'
    '比率、时长保留两位小数；不要出现一长串小数；\n'
    '3) 数据缺失就写"数据缺失"，不要补 0；incomplete 里的供电所必须点名标注；\n'
    '4) 引用评分规则时照抄事实包里的规则原文要点，不要自己编公式；\n'
    '5)【二、16 项指标得分分析】不要逐条照抄 16 条规则原文：按"基础信息 / 作业承载力 / 问题导向 / 历史预算执行"'
    '四类归纳，只为第三节选中的候选指标引用完整规则原文；\n'
    '6)【五、预计成效】必须给出具体的建议目标值：以现状均值为起点、以该指标"表现最好供电所值"为参照，'
    '给出建议目标并注明"建议值，需审定"；同时说明现状基线来自哪个统计期。\n'
    '7) 不要出现英文字段名或 JSON 键名（如 incomplete、avg_score），要用中文字段含义表达；\n'
    '8) 每节先给结论再给依据；不要客套话；不要写"本报告由 AI 生成"；不要输出思考过程。'
)


def load_playbook():
    with open(PLAYBOOK, encoding='utf-8') as f:
        return json.load(f)


def _q(sql):
    t0 = time.time()
    try:
        res = tools_db.run_sql(sql)
    except Exception as e:
        res = {'error': type(e).__name__ + ': ' + str(e)[:200]}
    if not isinstance(res, dict):
        res = {'rows': res}
    ms = int((time.time() - t0) * 1000)
    res.setdefault('rows', [])
    res.setdefault('columns', [])
    res['sql'] = sql
    res['ms'] = ms
    res['row_count'] = len(res.get('rows') or [])
    return res


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _fill(sql, ctx):
    for k, v in ctx.items():
        sql = sql.replace('{' + k + '}', str(v))
    return sql


def _run(pb, ctx, keys):
    """跑指定 key 的查询，返回 {key: [(label, 结果)]}，顺序与剧本一致。同一批并发。"""
    out, jobs, order = {}, [], {}
    for st in pb['steps']:
        if st['key'] not in keys:
            continue
        for i, q in enumerate(st['queries']):
            order[(st['key'], q['label'])] = i
            jobs.append((st, q))
    if not jobs:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
        futs = {ex.submit(_q, _fill(q['sql'], ctx)): (st, q) for st, q in jobs}
        for fu in concurrent.futures.as_completed(futs):
            st, q = futs[fu]
            out.setdefault(st['key'], []).append((q['label'], fu.result()))
    for k in out:
        out[k].sort(key=lambda x: order[(k, x[0])])
    return out


def _pick(block, label):
    for lab, res in block or []:
        if lab == label:
            return res
    return {'rows': [], 'row_count': 0, 'sql': '', 'ms': 0}


def _quote(v):
    return "'" + str(v).replace("'", "''") + "'"


def latest_year():
    r = _q("SELECT MAX(year) AS y FROM t_power_station_score_detail WHERE deleted_flag=0")
    rows = r.get('rows') or []
    if rows and rows[0].get('y'):
        return str(rows[0]['y'])
    return str(time.localtime().tm_year)


def list_scopes(year):
    r = _q("SELECT county_company_name AS c FROM t_power_company_budget_result "
           "WHERE deleted_flag=0 AND year=" + _quote(year) + " GROUP BY county_company_name ORDER BY c")
    return [x['c'] for x in (r.get('rows') or []) if x.get('c')]


def collect(year, scope):
    """按剧本取数，返回 (facts, trace, warnings)。"""
    pb = load_playbook()
    company = '' if (not scope or scope == 'city') else scope
    label = '全市' if not company else company
    trace, warnings = [], []

    ctx = {'year': year, 'next_year': str(int(year) + 1), 'company': company,
           'company_cmp': company, 'company_filter': '',
           'candidate_keys': ', '.join(_quote(k) for k in pb['rules']['candidate_metric_keys']),
           'period': '', 'effect_filter': '', 'effect_filter_cmp': ''}
    if company:
        ctx['company_filter'] = ' AND county_company_name=' + _quote(company)

    # 第一轮：不依赖 {period} / 供电所清单的查询
    phase1 = _run(pb, ctx, ('budget', 'score', 'projects'))
    # 统计期（第一轮之后取，后续查询要用它做占位替换）
    pres = _q(_fill(pb['steps'][2]['queries'][0]['sql'], ctx))
    period = ''
    if pres.get('rows'):
        period = str(pres['rows'][0].get('period') or '')
    ctx['period'] = period
    if not period:
        warnings.append('成效快照没有可用统计期，第五、六节将给出"数据缺失"。')

    # 公司口径的成效过滤
    if company:
        st_rows = _pick(phase1.get('budget'), '本范围各供电所预算').get('rows') or []
        names = sorted({str(r['power_station_name']) for r in st_rows if r.get('power_station_name')})
        if names:
            # 快照表与对比表都只有 scope_name（没有 power_station_id），用名称清单过滤
            flt = ' AND scope_name IN (' + ','.join(_quote(n) for n in names) + ')'
            ctx['effect_filter'] = flt
            ctx['effect_filter_cmp'] = flt

    blocks = dict(phase1)
    eff = _run(pb, ctx, ('effects',))
    for lab, res in eff.get('effects') or []:
        if lab == '最新统计期':
            continue
        blocks.setdefault('effects', []).append((lab, res))
    blocks.setdefault('effects', []).insert(0, ('最新统计期', pres))

    # ---- 事实包 ----
    b_total = _pick(blocks.get('budget'), '全市预算总额（allocation is_new=1）')
    b_comp = _pick(blocks.get('budget'), '各单位预算金额与供电所数（result deleted_flag=0）')
    b_stat = _pick(blocks.get('budget'), '本范围各供电所预算')
    s_std = _pick(blocks.get('score'), '16 项评分标准（含规则原文）')
    s_tot = _pick(blocks.get('score'), '各供电所加权总分与排名')
    s_avg = _pick(blocks.get('score'), '本范围 16 项平均得分（按加权分降序）')
    s_cmp = _pick(blocks.get('score'), '候选治理项：本范围 vs 全市均分')
    s_zero = _pick(blocks.get('score'), '候选治理项：原始值为 0 的供电所数（数据缺失识别）')
    e_cur = _pick(blocks.get('effects'), '本范围 6 项成效现状')
    e_yoy = _pick(blocks.get('effects'), '本范围 6 项成效同比（快照对比表）')
    p_st = _pick(blocks.get('projects'), '本年度工单按专业与状态')
    p_sum = _pick(blocks.get('projects'), '本年度工单总体进度')
    p_df = _pick(blocks.get('projects'), '本年度按缺陷类型')

    facts = {
        'year': year, 'next_year': str(int(year) + 1), 'scope': label,
        'budget': {
            'total': (b_total.get('rows') or [{}])[0],
            'companies': b_comp.get('rows') or [],
            'stations': b_stat.get('rows') or [],
        },
        'score': {
            'standards': [{k: r.get(k) for k in ('sort_no', 'metric_key', 'metric_name', 'direction',
                                                 'full_score_pct', 'pass_score_pct', 'remark')} for r in (s_std.get('rows') or [])],
            'station_totals': s_tot.get('rows') or [],
            'metrics': s_avg.get('rows') or [],
            'candidates': s_cmp.get('rows') or [],
        },
        'effects': {
            'period': period,
            'metrics': e_cur.get('rows') or [],
            'yoy': e_yoy.get('rows') or [],
            'metric_names': pb['rules']['effect_metric_names'],
        },
        'projects': {
            'summary': (p_sum.get('rows') or [{}])[0],
            'by_status': p_st.get('rows') or [],
            'by_defect': p_df.get('rows') or [],
        },
        'rules': pb['rules'],
    }

    if not company:
        for c in facts['score']['candidates']:
            c['scope_score'] = c.get('city_score')

    # 得分不完整的供电所不得参与排名（实测有 1 所只有 1 条得分）
    incomplete, rank = [], 0
    for r in facts['score']['station_totals']:
        r['complete'] = (r.get('metric_cnt') or 0) == 16
        if r['complete']:
            rank += 1
            r['rank'] = rank
        else:
            r['rank'] = None
            incomplete.append({'name': r.get('power_station_name'), 'metric_cnt': r.get('metric_cnt')})
    facts['score']['incomplete'] = incomplete

    # 基础额度按【全市】口径：各公司 station_cnt 之和（不受范围过滤影响）
    n_st = sum((c.get('station_cnt') or 0) for c in facts['budget']['companies']) or 1
    base = (_num((facts['budget']['total'] or {}).get('total')) or 0) / n_st
    facts['budget']['base_per_station'] = round(base, 2)

    # 原始值为 0 = 该项没采集，得分高是假象，必须标出来
    zmap = {r.get('metric_key'): r for r in (s_zero.get('rows') or [])}
    for c in facts['score']['candidates']:
        z = zmap.get(c.get('metric_key')) or {}
        zc, zn = _num(z.get('zero_cnt')), _num(z.get('n'))
        c['raw_zero_cnt'] = int(zc) if zc is not None else None
        c['raw_zero_pct'] = round(100.0 * (zc or 0) / (zn or 1), 1) if zn else None
    facts['score']['raw_zero_gap'] = [c for c in facts['score']['candidates'] if (c.get('raw_zero_pct') or 0) > 50]

    # ---- 轨迹（6 步）----
    seq = 0
    for st in pb['steps']:
        seq += 1
        qs = []
        for lab, res in blocks.get(st['key']) or []:
            qs.append({'label': lab, 'sql': res.get('sql', ''), 'ms': res.get('ms', 0),
                       'row_count': res.get('row_count', 0), 'error': res.get('error'),
                       'rows': (res.get('rows') or [])[:60]})
        trace.append({'seq': st['seq'], 'kind': 'sql', 'title': st['title'], 'queries': qs})
    return facts, trace, warnings


def _targets_fallback(facts):
    cands = facts['score']['candidates']
    out = []
    for c in cands:
        v = _num(c.get('scope_score'))
        if v is None:
            v = _num(c.get('city_score'))
        out.append((v if v is not None else -1, c))
    out.sort(key=lambda x: -x[0])
    ts = []
    for v, c in out[:3]:
        ts.append({'metric': c.get('metric_name'), 'why': '本范围该项平均得分 %.1f（全市平均 %.1f），是候选治理项中最高的一档' % (v, _num(c.get('city_score')) or 0),
                   'direction': '降低该项对应的问题程度'})
    return ts


def _view(facts):
    """给模型的显示口径：金额转万元、比率两位小数、列表截断，避免长尾小数写进报告。"""
    def r2(x):
        v = _num(x)
        return None if v is None else round(v, 2)

    def wan(x):
        v = _num(x)
        return None if v is None else round(v / 10000.0, 2)

    b = facts['budget']
    tot = _num((b['total'] or {}).get('total'))
    comps = []
    for c in b['companies']:
        amt = _num(c.get('amount')) or 0
        comps.append({'company': c.get('company'), 'station_cnt': c.get('station_cnt'),
                      'amount_wan': wan(amt), 'share_pct': round(amt * 100.0 / (tot or 1), 2)})
    st = [{'name': s.get('power_station_name'), 'company': s.get('company'),
           'amount_wan': wan(s.get('budget_amount'))} for s in b['stations']]
    sc = facts['score']
    cands = []
    for c in sc['candidates']:
        cands.append({'metric': c.get('metric_name'), 'scope_avg': r2(c.get('scope_score')),
                      'city_avg': r2(c.get('city_score')), 'n': c.get('n'),
                      'raw_zero_pct': c.get('raw_zero_pct')})
    eff = []
    for e in facts['effects']['metrics']:
        bd = e.get('better_direction')
        lo, hi = r2(e.get('min_value')), r2(e.get('max_value'))
        best, worst = (lo, hi) if bd == 'decrease_better' else (hi, lo)
        eff.append({'metric': facts['effects']['metric_names'].get(e.get('metric_code'), e.get('metric_code')),
                    'unit': (facts['rules'].get('effect_metric_units') or {}).get(e.get('metric_code')),
                    'better_direction': bd, 'n': e.get('n'),
                    'avg': r2(e.get('avg_value')), 'best_station': best, 'worst_station': worst})
    return {
        'year': facts['year'], 'next_year': facts['next_year'], 'scope': facts['scope'],
        'budget': {'total_wan': wan(tot), 'base_per_station_wan': wan(b.get('base_per_station')),
                   'companies': comps, 'stations_top5': st[:5], 'stations_bottom5': st[-5:]},
        'score': {
            'standards': [{'no': s.get('sort_no'), 'metric': s.get('metric_name'),
                           'direction': s.get('direction'), 'rule': (s.get('remark') or '')} for s in sc['standards']],
            'station_totals': [{'name': r.get('power_station_name'), 'total_score': r2(r.get('total_score')),
                                'rank': r.get('rank')} for r in sc['station_totals']],
            'metrics': [{'metric': m.get('metric_name'), 'avg_score': r2(m.get('avg_score')),
                         'avg_weighted': r2(m.get('avg_weighted'))} for m in sc['metrics']],
            'candidates': cands,
            'raw_value_all_zero': [{'metric': c.get('metric_name'), 'zero_pct': c.get('raw_zero_pct')}
                                   for c in (sc.get('raw_zero_gap') or [])],
            'incomplete': sc.get('incomplete') or [],
        },
        'effects': {'period': facts['effects']['period'], 'metrics': eff, 'yoy': facts['effects']['yoy']},
        'projects': {
            'summary': {'n': (facts['projects']['summary'] or {}).get('n'),
                        'amount_wan': wan((facts['projects']['summary'] or {}).get('amount')),
                        'finished': (facts['projects']['summary'] or {}).get('finished'),
                        'overdue': (facts['projects']['summary'] or {}).get('overdue')},
            'by_status': [{'specialty': x.get('specialty'), 'work_status': x.get('work_status'),
                           'n': x.get('n'), 'amount_wan': wan(x.get('amount'))} for x in facts['projects']['by_status']],
            'by_defect_top8': [{'defect_type': x.get('defect_type'), 'n': x.get('n'),
                                'amount_wan': wan(x.get('amount'))} for x in facts['projects']['by_defect'][:8]],
        },
        'rules': facts['rules'],
    }


def _parse_json(txt):
    if not txt:
        return None
    s = txt.strip()
    s = re.sub(r'^[\s]*[\`]{3}(?:json)?', '', s)
    s = re.sub(r'[\`]{3}[\s]*$', '', s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r'\{.*\}', s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def run_report(year=None, scope='', model=None):
    t0 = time.time()
    pb = load_playbook()
    year = str(year or latest_year())
    model = model or agent.config.MODEL
    warnings = []
    facts, trace, warns = collect(year, scope)
    collect_ms = int((time.time() - t0) * 1000)
    warnings.extend(warns)

    # --- 模型①：判断 ---
    t1 = time.time()
    plan, plan_raw, plan_err = None, '', None
    try:
        ctx = {'facts': _view(facts), 'rules': pb['rules'], 'outline': pb['outline']}
        resp = agent._chat([{'role': 'system', 'content': MODEL1_SYSTEM},
                            {'role': 'user', 'content': json.dumps(ctx, ensure_ascii=False, default=str)}],
                           model, use_tools=False, max_tokens=1600)
        plan_raw = (resp['choices'][0]['message'].get('content') or '').strip()
        plan = _parse_json(plan_raw)
    except Exception as e:
        plan_err = type(e).__name__ + ': ' + str(e)[:200]
    if not plan or not plan.get('targets'):
        warnings.append('模型判断失败或未给出目标，已退到确定性规则生成目标。' + (plan_err or ''))
        plan = {'attribution': '（模型判断失败，以下为按规则生成的确定性结论）',
                'targets': _targets_fallback(facts), 'projects': []}
        plan['fallback'] = True
    m1_ms = int((time.time() - t1) * 1000)
    trace.append({'seq': 5, 'kind': 'model', 'title': '模型判断：归因 / 治理目标 / 优先级',
                  'model': model, 'ms': m1_ms, 'output': plan, 'raw': plan_raw[:4000], 'error': plan_err})

    # --- 模型②：成文 ---
    t2 = time.time()
    report_md, rp_err = '', None
    try:
        resp = agent._chat([{'role': 'system', 'content': MODEL2_SYSTEM},
                            {'role': 'user', 'content': json.dumps({'facts': _view(facts), 'decision': plan,
                                                                    'outline': pb['outline']},
                                                                   ensure_ascii=False, default=str)}],
                           model, use_tools=False, max_tokens=3000)
        report_md = (resp['choices'][0]['message'].get('content') or '').strip()
    except Exception as e:
        rp_err = type(e).__name__ + ': ' + str(e)[:200]
    m2_ms = int((time.time() - t2) * 1000)
    if not report_md:
        warnings.append('模型成文失败，已退到确定性模板成文。' + (rp_err or ''))
        report_md = _template_report(facts, plan)
    trace.append({'seq': 6, 'kind': 'model', 'title': '模型成文：按六节骨架出报告',
                  'model': model, 'ms': m2_ms, 'output': report_md, 'error': rp_err})

    return {
        'playbook': pb['id'], 'title': pb['title'], 'year': year,
        'scope': facts['scope'], 'model': model,
        'report': report_md, 'sections': _split_sections(report_md),
        'decision': plan, 'facts': facts, 'trace': trace, 'warnings': warnings,
        'checks': _checks(facts, len(trace)),
        'elapsed_ms': int((time.time() - t0) * 1000),
        'timings': {'collect_ms': collect_ms, 'model1_ms': m1_ms, 'model2_ms': m2_ms},
    }


def _split_sections(md):
    out = []
    for part in re.split(r'^##\s+', md or '', flags=re.M):
        part = part.strip()
        if part:
            head = part.splitlines()[0]
            out.append({'title': head, 'body': part})
    return out


def _checks(facts, _n):
    ck = []
    total = _num((facts['budget']['total'] or {}).get('total'))
    comp_sum = sum(_num(c.get('amount')) or 0 for c in facts['budget']['companies'])
    diff = None if total is None else round(total - comp_sum, 2)
    ck.append({'name': '预算总额对账：allocation(is_new=1) vs result(deleted_flag=0) 汇总',
               'a': total, 'b': comp_sum, 'diff': diff,
               'pass': diff is not None and abs(diff) < 1.0})
    st_cnt = len({r.get('power_station_name') for r in facts['budget']['stations']})
    sc_cnt = len({r.get('power_station_name') for r in facts['score']['station_totals']})
    ck.append({'name': '供电所口径对齐：预算表 vs 得分表', 'a': st_cnt, 'b': sc_cnt, 'diff': st_cnt - sc_cnt,
               'pass': st_cnt == sc_cnt})
    inc = facts['score'].get('incomplete') or []
    ck.append({'name': '16 项完整性：每个供电所应有 16 条得分（不完整者不参与排名）',
               'bad_count': len(inc), 'bad': [x['name'] + '(' + str(x['metric_cnt']) + '条)' for x in inc[:5]],
               'pass': not inc})
    ps = sorted({round((_num(c.get('amount')) or 0) / max(1, c.get('station_cnt') or 1), 2)
                 for c in facts['budget']['companies']})
    ck.append({'name': '各单位「预算 ÷ 供电所数」是否一致（等额基础）', 'values': ps,
               'pass': len(ps) <= 1})
    eff_n = len(facts['effects']['metrics'])
    ck.append({'name': '6 项成效覆盖（deleted_flag=0 / station / yearToDate）', 'a': eff_n, 'pass': eff_n == 6})
    ck.append({'name': '候选治理项覆盖（问题导向 4 项 + 预算执行）',
               'a': len(facts['score']['candidates']), 'pass': len(facts['score']['candidates']) == 5})
    gap = facts['score'].get('raw_zero_gap') or []
    ck.append({'name': '候选指标原始值缺失识别（原始值为 0 占比 >50% 者不得当治理目标）',
               'items': [str(c.get('metric_name')) + ' ' + str(c.get('raw_zero_pct')) + '%' for c in gap],
               'pass': True})
    return ck


def _template_report(facts, plan):
    b = facts['budget']
    lines = ['## 一、预算分配情况', '%s %s 年度预算合计 %s 元。' % (facts['scope'], facts['year'], (b['total'] or {}).get('total'))]
    for c in b['companies']:
        lines.append('- %s：%s 元，%s 个供电所' % (c.get('company'), c.get('amount'), c.get('station_cnt')))
    lines.append('')
    lines.append('## 二、16 项指标得分分析')
    for m in facts['score']['metrics'][:16]:
        lines.append('- %s：本范围平均得分 %s（加权 %s）' % (m.get('metric_name'), m.get('avg_score'), m.get('avg_weighted')))
    lines.append('')
    lines.append('## 三、今年缺陷治理目标')
    for t in plan.get('targets') or []:
        lines.append('- %s：%s' % (t.get('metric'), t.get('why')))
    lines.append('')
    lines.append('## 四、缺陷治理项目计划')
    for p in plan.get('projects') or []:
        lines.append('- P%s %s：%s' % (p.get('priority'), p.get('name'), p.get('reason')))
    lines.append('')
    lines.append('## 五、预计成效')
    lines.append('统计期 %s。' % facts['effects']['period'])
    for m in facts['effects']['metrics']:
        lines.append('- %s：均值 %s（范围 %s - %s，n=%s）' % (facts['effects']['metric_names'].get(m.get('metric_code'), m.get('metric_code')),
                                                         m.get('avg_value'), m.get('min_value'), m.get('max_value'), m.get('n')))
    lines.append('')
    lines.append('## 六、计划上报')
    lines.append('（模型成文失败，本段由确定性模板生成，数据均来自本次查询。）')
    return '\n'.join(lines)
