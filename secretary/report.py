# -*- coding: utf-8 -*-
"""年度缺陷治理计划报告：把「技能文档 + 通知数据」交给模型，让它用现有工具自己查、自己分析。

这里不写死任何 SQL、不写死步骤：流程与判断方法在 secretary/skills/*.md 里，
换一年、换一个所、换一个分析主题都只改文档，不改代码。
"""
import os
import time

import agent
import semantic
import tools_db

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(os.path.dirname(HERE), 'skills')   # 技能文档放在仓库根 skills/，跨服务复用
SKILL_DOC = os.path.join(SKILL_DIR, '年度缺陷治理计划分析.md')
TABLE_DOC = os.path.join(SKILL_DIR, '数据表说明书.md')

BASE_SYSTEM = (
    '你是随州供电公司的项目管理秘书，现在要写一份可以上报的分析报告。\n'
    '【下面是约束你查数的，不是写进报告的】\n'
    '1. 报告里每个数字都要有出处：通知，或你用 run_sql 查到的结果；追不到出处的数字不许写。\n'
    '2. 项目清单与金额、缺陷类型、本所得分、六项指标现状，都必须先查过才能写；'
    '一次工具都不调就想出报告会被直接拦下。凭印象编项目名和数字，是这份工作里最严重的错误。\n'
    '3. 只能查业务字典里登记的表，只允许 SELECT；查询要把已作废的记录排除。\n'
    '4. 表名与字段名靠字典查（list_tables / find_column / describe_table），不要凭猜测写 SQL。\n'
    '5. 库里没有的就写「暂无数据」，不许估算、不许补 0、不许拿别的年份顶。\n'
    '【下面是报告文字的硬要求】\n'
    '6. 报告是所里写给上级看的正式汇报，不是技术说明：正文里一律不许出现数据库表名、字段名、英文标识、'
    'SQL、查询条件、返回行数、期次编码。\n'
    '7. 不许出现「数据来源」「数据溯源」「口径声明」「字段」「表」这类技术性说明段落，'
    '也不许写「本报告所有数字均可追溯」这类自我声明。\n'
    '8. 时间用业务说法（「截至2026年9月」「去年同期」「上年度」）；不要提 AI、不要提工具、不要提查询过程。\n'
    '9. 缺陷类型、工单状态这类编号必须翻成中文再写。\n'
    '10. 金额一律万元、两位小数；比率与时长两位小数。\n'
    '11. 先结论后依据，不要客套话，不要输出思考过程。'
)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _system(doc_text=None):
    try:
        skill = doc_text if doc_text is not None else _read(SKILL_DOC)
    except Exception as e:
        skill = '（技能文档读取失败：%s）' % e
    try:
        menu = semantic.table_menu()
    except Exception:
        menu = ''
    bar = '=' * 40
    return '\n\n'.join([
        BASE_SYSTEM,
        bar + '\n【技能文档：必须按它来分析】\n' + bar + '\n' + skill,
        bar + '\n【可查的表（表名与字段以字典为准，用 list_tables / find_column / describe_table 查）】\n' + bar + '\n' + menu,
    ])


def run_skill(skill_id, model=None, work_name='', frm='', to='', questions=None, role='', scope='', use_doc=True, inputs=None):
    """触发一个技能：把它的整组问题 + 技能文档一起交给模型，一次跑完出一份结果。"""
    t0 = time.time()
    cfg = list_triggers()
    sk = None
    for s in cfg.get('skills') or []:
        if s.get('id') == skill_id:
            sk = s
            break
    if not sk:
        return {'skill': str(skill_id), 'answer': '没有这个技能。', 'trace': [], 'tool_calls': 0}
    doc = (sk.get('doc_text') or '') if use_doc else ''
    qs = [str(x) for x in questions if str(x).strip()] if questions else [q.get('q') for q in (sk.get('questions') or [])]
    if not qs:
        return {'skill': sk.get('name'), 'answer': '这个技能没有勾选任何问题。', 'trace': [], 'tool_calls': 0}
    head = ''
    if role or scope:
        head += ('你的身份：%s。数据权限范围：%s。\n'
                 '**所有数据只能取这个范围内的**，并且要说清是在这个范围下的结论；范围外的不要给。\n'
                 % (role or (sk.get('role') or ''), scope or '全部'))
    head += '你的角色：%s。\n' % (role or sk.get('role') or '')
    if inputs:
        head += ('上游系统传过来的数据（业务系统推过来的原文，直接采用，不要改动；如果与库里的数据不一致，以这份为准并说明）：\n')
        for it in inputs:
            head += '  - %s：%s\n' % (it.get('label') or '', it.get('value') or '')
    if work_name:
        head += ('刚刚发生了一次状态变更：工单「%s」从「%s」变为「%s」。请结合这次变更来答。\n' % (work_name, frm, to))
    head += ('下面是这个技能要你回答的 %d 个问题，**请合并成一份回答**（不要一问一答地重复），'
             '按技能文档要求的段落与口径来组织：\n' % len(qs))
    user = head + '\n'.join('%d. %s' % (i + 1, q) for i, q in enumerate(qs))
    if not doc:
        user += '\n\n（本技能暂无技能文档，请按常识与已有提示词作答，并说明你依据了什么。）'
    answer, trace, blocked = _loop(_system(doc), user, model or agent.config.MODEL)
    calls = [t for t in trace if t.get('kind') == 'tool']
    return {'skill': sk.get('name'), 'skill_id': skill_id, 'answer': answer, 'trace': trace, 'use_doc': bool(use_doc),
            'questions': len(qs), 'doc': sk.get('doc'), 'doc_chars': len(doc), 'blocked': blocked,
            'tool_calls': len(calls), 'elapsed_ms': int((time.time() - t0) * 1000)}


def state_change_sim():
    """状态变更触发台的素材：规则表 + 一批真实工单当假数据（含当前状态中文）。"""
    import json as _json
    p = os.path.join(SKILL_DIR, '状态变更规则.json')
    try:
        with open(p, encoding='utf-8') as f:
            cfg = _json.load(f)
    except Exception as e:
        return {'rules': [], 'orders': [], 'error': str(e)[:200]}
    sql = ("SELECT w.work_name, w.budget_total, w.work_status, d.data_label AS status_cn "
           "FROM t_power_work_order w JOIN t_dict t ON t.dict_code='WORK_STATUS' "
           "JOIN t_dict_data d ON d.dict_id=t.dict_id AND d.data_value=w.work_status "
           "WHERE w.deleted_flag=0 AND w.work_status IN ('1','2','3','6','7') ORDER BY w.id DESC LIMIT 8")
    try:
        rows = tools_db.run_sql(sql).get('rows') or []
    except Exception as e:
        rows, _ = [], None
    orders = [{'work_name': r.get('work_name'), 'budget': float(r.get('budget_total') or 0),
               'status_cn': r.get('status_cn')} for r in rows]
    cfg['orders'] = orders
    cfg['sql'] = sql
    return cfg


def list_triggers():
    """读 skills/技能触发配置.json，并把对应技能文档正文带上，供前端触发台渲染。"""
    import json as _json
    p = os.path.join(SKILL_DIR, '技能触发配置.json')
    try:
        with open(p, encoding='utf-8') as f:
            cfg = _json.load(f)
    except Exception as e:
        return {'trigger_types': {}, 'skills': [], 'error': str(e)[:200]}
    docs = {d['name']: d['content'] for d in list_skill_docs()}
    for s in cfg.get('skills') or []:
        s['doc_text'] = docs.get(s.get('doc') or '', '')
        s['has_doc'] = bool(s['doc_text'])
    return cfg


def list_skill_docs():
    """列出 skills/ 下的技能文档（名字 + 标题 + 正文），供页面选用。"""
    out = []
    try:
        for fn in sorted(os.listdir(SKILL_DIR)):
            if not fn.lower().endswith('.md'):
                continue
            try:
                txt = _read(os.path.join(SKILL_DIR, fn))
            except Exception:
                continue
            title = ''
            for ln in txt.splitlines():
                if ln.startswith('# '):
                    title = ln[2:].strip()
                    break
            out.append({'name': fn, 'title': title, 'chars': len(txt), 'content': txt})
    except Exception:
        pass
    return out


def _notice_text(notice):
    lines = ['【本次收到的预算分配通知】',
             '供电所：%s' % notice.get('station'),
             '所属公司：%s' % notice.get('company'),
             '数据基准年度（上期）：%s' % notice.get('base_year'),
             '计划年度（本期）：%s' % notice.get('plan_year'),
             '本期分配金额：%s 元' % notice.get('cur_amount'),
             '上期分配金额：%s 元' % notice.get('prev_amount'),
             '',
             '本所 16 项指标得分（标准分 0~100；越高代表这一项越需要资源，不是越高越好）：',
             '| 指标 | 方向 | 上期标准分 | 本期标准分 |',
             '|---|---|---|---|']
    for m in notice.get('metrics') or []:
        lines.append('| %s | %s | %s | %s |' % (m.get('name'), m.get('direction'), m.get('prev_score'), m.get('cur_score')))
    lines += ['', '请按技能文档的要求，写出完整的分析报告。']
    return '\n'.join(lines)


def _loop(system, user, model, max_steps=26):
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    trace = []
    answer = ''
    nudged = blocked = False
    for step in range(1, max_steps + 1):
        t0 = time.time()
        resp = agent._chat(messages, model, use_tools=True, max_tokens=3000)
        llm_ms = int((time.time() - t0) * 1000)
        msg = resp['choices'][0]['message']
        calls = msg.get('tool_calls') or []
        if not calls:
            answer = (msg.get('content') or '').strip()
            used_tool = any(t.get('kind') == 'tool' for t in trace)
            if not used_tool and not nudged:
                nudged = True
                trace.append({'seq': len(trace) + 1, 'kind': 'nudge', 'ms': llm_ms,
                              'content': '模型未调用任何工具就想出报告，已要求其先查数'})
                messages.append({'role': 'assistant', 'content': answer})
                messages.append({'role': 'user', 'content':
                                 '你这一轮没有调用任何工具。报告里的项目清单、金额、缺陷类型、指标现状都还没有核实过，不许凭印象写。'
                                 '请先用 run_sql 把这些查出来（本所项目清单与金额、缺陷类型中文、本所得分明细、六项指标现状），再写报告。'
                                 '不要在回答里提到本条提示。'})
                continue
            if not used_tool:
                blocked = True
                trace.append({'seq': len(trace) + 1, 'kind': 'blocked', 'ms': llm_ms,
                              'content': '第二次仍未调用工具，已拦下不予出报告'})
                answer = ('【未核实，不予出报告】两次机会里都没有查询数据库，报告里的项目和数字无法追溯，已拦下。请重试或换更强的模型。')
                break
            trace.append({'seq': len(trace) + 1, 'kind': 'answer', 'ms': llm_ms, 'content': answer[:600]})
            break
        messages.append({'role': 'assistant', 'content': msg.get('content') or '', 'tool_calls': calls})
        outs = agent._exec_calls(calls)
        for tc, o in zip(calls, outs):
            trace.append({'seq': len(trace) + 1, 'kind': 'tool', 'tool': o['name'], 'args': o['args'],
                          'ms': o['ms'], 'round': step, 'result': o['result']})
            messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': agent._trim(o['name'], o['result'])})
        if step in (14, 20):
            messages.append({'role': 'user', 'content': '已经查了不少。够写报告就立刻按技能文档的 5 节输出报告，不要再翻表。'})
    if not answer:
        answer = '（模型到步数上限仍未给出报告，请重试或减少要分析的内容。）'
    return answer, trace, blocked


def _q(v):
    return chr(39) + str(v).replace(chr(39), chr(39) + chr(39)) + chr(39)


def latest_year():
    try:
        r = tools_db.run_sql('SELECT MAX(year) AS y FROM t_power_station_score_detail WHERE deleted_flag=0')
        rows = r.get('rows') or []
        if rows and rows[0].get('y'):
            return str(rows[0]['y'])
    except Exception:
        pass
    return str(time.localtime().tm_year)


def list_stations():
    try:
        sql = 'SELECT power_station_name AS n FROM t_power_company_budget_result WHERE deleted_flag=0 GROUP BY power_station_name ORDER BY power_station_name'
        r = tools_db.run_sql(sql)
        return [x['n'] for x in (r.get('rows') or []) if x.get('n')]
    except Exception:
        return []


def _fake(seed, i, lo, hi):
    x = (seed * 9301 + i * 49297 + 233280) % 233280
    return lo + int(x / 233280.0 * (hi - lo + 1))


def build_notice(station, base_year=None, plan_year=None, prev_amount=None, cur_amount=None,
                 scores=None, seed=None):
    base_year = str(base_year or latest_year())
    plan_year = str(plan_year or (int(base_year) + 1))
    sql1 = 'SELECT power_station_name, county_company_name, budget_amount FROM t_power_company_budget_result WHERE deleted_flag=0 AND year=%s AND power_station_name=%s LIMIT 1' % (_q(base_year), _q(station))
    rows = (tools_db.run_sql(sql1).get('rows') or [])
    company = rows[0].get('county_company_name') if rows else ''
    if prev_amount is None:
        prev_amount = float(rows[0]['budget_amount']) if rows else 0.0
    sql2 = 'SELECT metric_key, metric_name, direction, normalized_score, weighted_score, raw_value, weight FROM t_power_station_score_detail WHERE deleted_flag=0 AND year=%s AND power_station_name=%s ORDER BY metric_key' % (_q(base_year), _q(station))
    metrics, total_prev, total_cur = [], 0.0, 0.0
    sd = seed if seed is not None else sum(ord(c) for c in str(station))
    for i, r in enumerate(tools_db.run_sql(sql2).get('rows') or []):
        p = float(r.get('normalized_score') or 0)
        w = float(r.get('weight') or 0)
        if scores and r['metric_key'] in scores:
            cur = float(scores[r['metric_key']])
        elif scores:
            cur = p
        else:
            cur = max(0.0, min(100.0, p + _fake(sd, i, -9, 12)))
        metrics.append({'key': r['metric_key'], 'name': r['metric_name'], 'direction': r.get('direction'),
                        'raw_value': float(r['raw_value']) if r.get('raw_value') is not None else None,
                        'prev_score': round(p, 2), 'cur_score': round(cur, 2)})
        total_prev += p * w
        total_cur += cur * w
    if cur_amount is None:
        ratio = (total_cur / total_prev) if total_prev else 1.0
        cur_amount = round(prev_amount * ratio, 2)
    return {'station': station, 'company': company, 'base_year': base_year, 'plan_year': plan_year,
            'prev_amount': round(float(prev_amount), 2), 'cur_amount': round(float(cur_amount), 2), 'metrics': metrics}


def run_report(notice=None, model=None, **kw):
    t0 = time.time()
    if notice is None:
        notice = build_notice(kw.pop('station', '环潭供电所'), **kw)
    model = model or agent.config.MODEL
    answer, trace, blocked = _loop(_system(), _notice_text(notice), model)
    calls = [t for t in trace if t.get('kind') == 'tool']
    return {'title': '年度缺陷治理计划分析报告', 'notice': notice, 'report': answer, 'trace': trace,
            'model': model, 'elapsed_ms': int((time.time() - t0) * 1000), 'blocked': blocked,
            'tool_calls': len(calls),
            'sql_calls': len([t for t in calls if t.get('tool') == 'run_sql'])}