# -*- coding: utf-8 -*-
"""模型自主循环：只给工具与底线，不给流程。

分工：上游「信息补全」工作流负责机构名自动纠错与统计时间（本服务先调它、取出参）；
**问题补全由本服务负责** —— 用知识库里的补全规则与术语词典，把口语问法补成标准问题，再查口径、取数、作答。
全部在一个模型循环里由模型自己排顺序，代码不写业务流程。
"""
import concurrent.futures
import json
import re
import ssl
import time
import urllib.request

import config
import gaps
import semantic
import tools_app
import tools_db
import tools_kb

# 数字断言：出现「数字 + 单位」就认为它在给业务数字（这类答案必须有工具出处）
_CLAIM = re.compile(r'\d+(?:\.\d+)?\s*(?:万|亿|%|％|件|个|条|户|千瓦时|千瓦|kWh|元|次|小时|倍)')


TOOLS = [
    {'type': 'function', 'function': {
        'name': 'kb_search',
        'description': '检索业务知识库，用于弄清术语、指标口径、业务背景。返回按相关度排序的文档切片。',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'description': '检索意图'},
            'top_k': {'type': 'integer', 'description': '返回几条，默认 5'}},
            'required': ['query']}}},
    {'type': 'function', 'function': {
        'name': 'list_tables',
        'description': '列出业务字典里登记的表（表名、中文名、用途、行数、注意）。keyword 可选，按表名或用途模糊筛选。',
        'parameters': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'find_column',
        'description': '在业务字典内的表里，按字段名或字段中文注释搜字段，返回「表名 + 字段名 + 类型 + 注释 + 示例值」。'
                       '当你不确定某个数据在哪个字段/哪张表时先用它（例如搜「线损」「执行金额」「台区」）。',
        'parameters': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': ['keyword']}}},
    {'type': 'function', 'function': {
        'name': 'describe_table',
        'description': '列出某张表的字段（字段名、类型、注释、业务字典里的中文名与示例值）和前 2 行样本。表必须在业务字典里。',
        'parameters': {'type': 'object', 'properties': {'table': {'type': 'string'}}, 'required': ['table']}}},
    {'type': 'function', 'function': {
        'name': 'run_sql',
        'description': '在只读业务库执行一条 SELECT（只允许业务字典里的表；不支持分号与注释，最多返回 200 行）。',
        'parameters': {'type': 'object', 'properties': {'sql': {'type': 'string'}}, 'required': ['sql']}}},
]

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _chat(messages, model, use_tools=True, max_tokens=1500):
    body = {'model': model, 'messages': messages,
            'enable_thinking': False, 'temperature': 0, 'max_tokens': max_tokens}
    if use_tools:
        body['tools'] = TOOLS
    body = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(config.LLM_BASE + '/chat/completions', data=body, headers={
        'Authorization': 'Bearer ' + config.LLM_KEY, 'Content-Type': 'application/json'})
    raw = urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT, context=_ctx).read().decode('utf-8', 'replace')
    return json.loads(raw)


def _trim(name, res):
    if name == 'list_tables':
        res = dict(res)
        res['rows'] = (res.get('rows') or [])[:150]
    return json.dumps(res, ensure_ascii=False, default=str)[:4000]


def execute(name, args):
    if name == 'kb_search':
        return tools_kb.kb_search(args.get('query', ''), int(args.get('top_k') or 3))
    if name == 'list_tables':
        return tools_db.list_tables(args.get('keyword', '') or '')
    if name == 'find_column':
        return tools_db.find_column(args.get('keyword', ''))
    if name == 'describe_table':
        return tools_db.describe_table(args.get('table', ''))
    if name == 'run_sql':
        return tools_db.run_sql(args.get('sql', ''))
    return {'error': 'unknown tool ' + str(name)}


# 问题补全用的提示：本服务自己做的第一步（上游只补时间与地点）。
# 只写「怎么用规则」，不写任何业务规则 —— 补成什么样完全由知识库切片决定。
COMPLETE_SYSTEM = (
    '你在做「问题补全」：把用户的问法补成一条标准的完整查询要求。\n'
    '1. 只输出补全后的问题本身，不要回答、不要解释、不要加任何前后缀。\n'
    '2. 补全依据下面给出的规则库切片：切片怎么规定就怎么补，不要自己另立规则。\n'
    '3. 先判断切片里有没有与用户问题**对应**的补全规则。判断标准（三条必须全满足）：\n'
    '   - 切片里明确写着「用户问 X 时，补全为 …」，且 X 与用户问题说的是**同一件事**。\n'
    '   - 只是共享一两个词（例如都含「台区」「情况」）不算对应。\n'
    '   - 补全**不能改变问题的类型**：用户问的是某个具体指标或数量（如「台区线损率是多少」「低电压台区有多少个」），'
    '就不能套用「整体情况 / 台区情况说明」这类规则；反过来，用户问整体情况，也不要补成单个指标。\n'
    '   有对应规则 → 按它补全；没有对应规则 → **原样输出用户问题**，不要编造补全内容。\n'
    '4. 时间与范围已由上游补全确定，直接采用，不要反问。'
)


def complete_question(question, model=None):
    """本服务自己的问题补全：检索补全规则库，让模型按规则改写。只看规则库与问题，不带用户画像。"""
    model = model or config.MODEL
    t0 = time.time()
    nodes = []
    try:
        r = tools_kb.kb_search(question, top_k=6, limit=1200)
        nodes = r.get('nodes') or []
    except Exception:
        nodes = []
    ref = '\n\n'.join('【%s】%s' % (n.get('title') or '', n.get('content') or '') for n in nodes)
    ask_text = ('补全规则库切片：\n' + (ref or '（没检索到，按通用规则补全）') +
                '\n\n用户问题：' + question + '\n\n请输出补全后的问题。')
    msgs = [{'role': 'system', 'content': COMPLETE_SYSTEM},
            {'role': 'user', 'content': ask_text}]
    out = ''
    try:
        resp = _chat(msgs, model, use_tools=False)
        out = (resp['choices'][0]['message'].get('content') or '').strip()
    except Exception:
        out = ''
    return {'input': question, 'completed': out or question, 'ok': bool(out),
            'ms': int((time.time() - t0) * 1000),
            'rules': [{'doc_name': n.get('doc_name'), 'title': n.get('title'), 'score': n.get('score')}
                      for n in nodes]}


# 兜底：字典/提示词读不到时才用（正常情况下系统提示全部来自数据库）
_FALLBACK = '你是随州供电公司的项目管理秘书，负责回答业务问题。'


def _system():
    """系统提示 = 他们在 ai_prompt 里维护的提示词（config.PROMPTS 指定的 key）
    + 业务字典的表目录。**提示词不在代码里写死**。
    """
    parts = []
    try:
        pool = semantic.prompts()
        for k in (config.PROMPTS or []):
            if pool.get(k):
                parts.append('===== ' + k + ' =====\n' + pool[k])
    except Exception:
        pass
    head = '\n\n'.join(parts).strip() or _FALLBACK
    try:
        return head + '\n\n【可用数据表（只能查这些，字典外的表一律不可用）】\n' + semantic.table_menu()
    except Exception:
        return head


def _one_call(tc):
    name = tc['function']['name']
    try:
        args = json.loads(tc['function'].get('arguments') or '{}')
    except Exception:
        args = {}
    t1 = time.time()
    try:
        res = execute(name, args)
    except Exception as e:
        res = {'error': type(e).__name__ + ': ' + str(e)[:300]}
    return {'name': name, 'args': args, 'result': res, 'ms': int((time.time() - t1) * 1000)}


def _exec_calls(calls):
    """同一轮的多个工具调用并发执行；返回顺序与 calls 一一对应。"""
    if len(calls) == 1:
        return [_one_call(calls[0])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(calls))) as ex:
        return list(ex.map(_one_call, calls))


def ask(question, model=None, max_steps=None, profile=None):
    """question = 上游信息补全后的问题（时间与地点已明确）。

    profile = 用户画像（可选）：补全阶段不使用；与补全后的问题一起交给模型拆解任务。
    """
    model = model or config.MODEL
    max_steps = max_steps or config.MAX_STEPS
    t_all = time.time()
    trace = []
    # 第一步：上游「信息补全」工作流（机构名自动纠错 + 统计时间）。失败不影响后续，只是少一层参考。
    t_up = time.time()
    try:
        up = tools_app.complete_question(question)
    except Exception as e:
        up = {'text': '', 'error': type(e).__name__ + ': ' + str(e)[:200]}
    up_text = (up.get('text') or '').strip()
    up_ms = int((time.time() - t_up) * 1000)
    trace.append({'seq': 1, 'kind': 'upstream', 'tool': 'info_completion',
                  'ms': up_ms, 'args': {'input': question},
                  'result': {'completed': up_text, 'error': up.get('error'),
                             'request_id': up.get('request_id'), 'cached': up.get('cached')}})
    done = complete_question(question, model)
    completed = done['completed']
    # 判断（我们这边唯一的职责）：补全有没有产出与原文不同的内容 —— 相同就视为“没找到对应规则、不采用”
    used = bool(completed and completed.strip() and completed.strip() != question.strip())
    trace.append({'seq': 2, 'kind': 'complete', 'tool': 'question_completion',
                  'ms': done['ms'], 'args': {'input': question, 'rules': done['rules']},
                  'result': {'completed': completed, 'ok': done['ok'], 'used': used,
                             'reason': '按规则库补全' if used else '规则库没有对应问法，不采用补全'}})
    t_loop = time.time()
    # 问题为准；补全结果只作参考，由模型自己判断适不适用
    user_content = '原始问题：' + question
    if up_text:
        user_content += ('\n\n【上游信息补全】上游「信息补全」工作流的出参：里面的机构名、地点名已按标准名'
                         '纠正（例如把不存在的名称改成库里真实存在的名称），统计时间也已确定，'
                         '查库时请直接采用其中的机构名与时间范围；如果它改写了问题的指标或问法，以原始问题为准。\n'
                         + up_text)
    if used:
        user_content += ('\n\n【补全参考】按知识库的补全规则库，这个问法通常应补成下面这样。'
                         '它只是参考：如果与上面的问题不符，或它提到的指标/范围在当前数据里查不到，'
                         '以问题为准，忽略不适用的部分。\n' + completed)
    if profile:
        user_content += ('\n\n用户画像（用于判断统计范围与关注重点，不要因此增减问题里已经要求的必答项）：\n' + profile)
    messages = [{'role': 'system', 'content': _system()}, {'role': 'user', 'content': user_content}]
    answer = ''
    nudged = False
    blocked = False
    step = 0
    while True:
        step += 1
        if max_steps and step > max_steps:
            answer = '（已超过设定的最大步骤 %d，停止）' % max_steps
            break
        t0 = time.time()
        resp = _chat(messages, model)
        llm_ms = int((time.time() - t0) * 1000)
        msg = resp['choices'][0]['message']
        calls = msg.get('tool_calls') or []
        if not calls:
            answer = (msg.get('content') or '').strip()
            used_tool = any(t.get('kind') == 'tool' for t in trace)
            # 硬约束：一个工具都没调就下结论，先要求它核实一次；仍不调则直接拦下，不当答案返回。
            if not used_tool and not nudged:
                nudged = True
                trace.append({'seq': len(trace) + 1, 'kind': 'nudge', 'model_ms': llm_ms,
                              'content': '模型未调用任何工具就想作答，已要求其先核实'})
                messages.append({'role': 'assistant', 'content': answer})
                messages.append({'role': 'user', 'content':
                                 '你这一轮没有调用任何工具。请先用工具核实事实（查知识库口径 + 查业务库数据），再给出结论；'
                                 '如果确实不需要任何工具，也请先说明理由并至少调用一次工具确认。'
                                 '不要在回答里提到这条提示，也不要复述你的工具调用过程。'})
                continue
            if not used_tool:
                # 提醒过一次后仍不调工具：只要它给的不是业务数字（纯拒答），就放行；给了数字才拦。
                if nudged and not _CLAIM.search(answer):
                    note = '本轮未调用任何工具，以下为模型基于业务字典的判断'
                    answer = '（' + note + '）' + answer
                    trace.append({'seq': len(trace) + 1, 'kind': 'answer', 'model_ms': llm_ms,
                                  'content': answer[:600], 'note': note})
                    break
                blocked = True
                trace.append({'seq': len(trace) + 1, 'kind': 'blocked', 'model_ms': llm_ms,
                              'content': '未调用任何工具即下结论，已拦下。模型原文：' + answer[:300]})
                answer = ('【未核实，不予作答】这一轮没有调用任何工具去核对事实，结论不可信，已拦下。'
                          '请重试，或把问题问得更具体一些。')
                break
            trace.append({'seq': len(trace) + 1, 'kind': 'answer', 'model_ms': llm_ms,
                          'content': answer[:600]})
            break
        keep = {'role': 'assistant', 'content': msg.get('content') or '', 'tool_calls': calls}
        messages.append(keep)
        # 不设步数上限，改用周期性软提醒，防止一直翻表不收手
        if step >= 15 and step % 5 == 0:
            messages.append({'role': 'user', 'content':
                             '你已经调用 %d 次工具了。如果已有足够数据，请立刻给出结论；'
                             '还差关键数据就集中查最关键的那一项。不要在回答里提到本条提示。' % step})
        outs = _exec_calls(calls)
        for tc, o in zip(calls, outs):
            trace.append({'seq': len(trace) + 1, 'kind': 'tool', 'tool': o['name'], 'args': o['args'],
                          'ms': o['ms'], 'model_ms': llm_ms, 'round': step, 'result': o['result']})
            messages.append({'role': 'tool', 'tool_call_id': tc['id'], 'content': _trim(o['name'], o['result'])})
    DB_TOOLS = ('run_sql', 'list_tables', 'describe_table')
    db_calls = [t for t in trace if t.get('tool') in DB_TOOLS]
    sql_calls = [t for t in trace if t.get('tool') == 'run_sql']
    kb_calls = [t for t in trace if t.get('tool') == 'kb_search']
    result = {
        'input_question': question,
        'upstream_text': up_text,
        'upstream_error': up.get('error'),
        'user_profile': profile or '',
        'completion_used': used,
        'completed_question': completed,
        'answer': answer,
        'trace': trace,
        'model': model,
        'elapsed_ms': int((time.time() - t_all) * 1000),
        'timings': {
            'upstream_ms': up_ms,
            'completion_ms': done['ms'],
            'loop_ms': int((time.time() - t_loop) * 1000),
            'total_ms': int((time.time() - t_all) * 1000),
        },
        'db_query_count': len(db_calls),
        'sql_query_count': len(sql_calls),
        'kb_query_count': len(kb_calls),
        'no_db_query': len(db_calls) == 0,
        'unverified': blocked,
        'steps': len(trace),
    }
    try:
        if gaps.is_gap(answer, blocked):
            gaps.record(question, completed, answer, model=model,
                        db_count=result['db_query_count'], kb_count=result['kb_query_count'],
                        unverified=blocked)
            result['recorded_as_gap'] = True
    except Exception:
        pass
    return result
