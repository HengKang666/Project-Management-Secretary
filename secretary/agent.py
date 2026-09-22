# -*- coding: utf-8 -*-
"""模型自主循环：只给工具与底线，不给流程。

分工：**信息补全由本服务自己做**（字面纠错与名称归一 → 时间与地点补全），
问题补全走知识库的补全规则，再查口径、取数、作答。
上游那个补全应用已不在链路里，时间与地点同样由本服务补（见 time_scope.py）。
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
import name_fix
import semantic
import time_scope
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
        'description': '在只读业务库执行一条 SELECT（只允许业务字典里的表；不支持分号与注释，最多返回 200 行）。'
                       '★ 要查多个互不依赖的数时，请在**同一次回复里一次发出多条 run_sql**'
                       '（系统会并发执行，一轮就全拿到）；不要一条一条分多轮来 —— 每多一轮就多花几秒。',
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
    '4. **时间与地点不要自己定** —— 它们已经由前面的步骤算好了，就在下面给的【统计范围】里，'
    '直接照抄到补全后的问题里即可。\n'
    '   - **绝对不要**在问题缺时间/地点时自己补「今年」「今年至今」「全市」这类默认值：'
    '多轮会话里，缺的部分可能该**沿用上一轮**（上一轮问的是「环潭供电所」，这一轮只说'
    '「那售电量呢」，地点就该是环潭供电所而不是全市）。自己补默认值会和【统计范围】打架。\n'
    '   - 也不要自己推算日期（「上月」是几月）——【统计范围】里已经是具体年月。\n'
    '   - 补完的问题里，时间与地点都要按【统计范围】写全。'
)


def complete_question(question, model=None, scope_text=None, prev_question=None):
    """本服务自己的问题补全：检索补全规则库，让模型按规则改写。

    **必须把已定好的统计范围传进来**（scope_text）：
    补全的产出会作为【补全参考】交给主循环，若它自己另补一套时间/地点，
    就会与【统计范围】冲突 —— 多轮里表现为「说了沿用上一轮，模型却按全市答」。

    prev_question = 上一轮的问题，用于理解省略了主语的问法（「那售电量呢」）。
    """
    model = model or config.MODEL
    t0 = time.time()
    nodes = []
    try:
        r = tools_kb.kb_search(question, top_k=6, limit=1200)
        nodes = r.get('nodes') or []
    except Exception:
        nodes = []
    ref = '\n\n'.join('【%s】%s' % (n.get('title') or '', n.get('content') or '') for n in nodes)
    ask_text = '补全规则库切片：\n' + (ref or '（没检索到，按通用规则补全）') + '\n'
    if prev_question:
        ask_text += ('\n上一轮用户问的是：' + prev_question +
                     '\n（本轮可能省略了主语或指标，请结合上一轮理解它到底在问什么）\n')
    if scope_text:
        ask_text += '\n【统计范围】（时间与地点已确定，补全时直接采用，不要改成别的）\n' + scope_text + '\n'
    ask_text += '\n用户问题：' + question + '\n\n请输出补全后的问题。'
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


def ask(question, model=None, max_steps=None, profile=None,
        session_id=None, user_id=None, user_code=None, client_ip=None, channel=None,
        history_turns=None):
    """question = 用户的原话（不需要上游预处理）。

    本服务依次做：**载入多轮上下文** → 字面纠错与名称归一 → 时间与地点补全 →
    问题补全 → 模型循环查数作答 → 落库（问答/会话/消息/执行明细写进 agent_data）。

    profile = 用户画像（可选）：补全阶段不使用；与补全后的问题一起交给模型拆解任务。

    session_id / user_id / user_code / client_ip / channel = 记录用（可选）：
      - session_id 不传则服务端生成一个（UUID），无论落库成败都会回在返回值里
      - user_code 传业务用户ID（如 t_user.uid），落库时换成内部主键
      - **落库失败绝不影响问答** —— 只打日志

    多轮上下文：
      - 传了 session_id 就会自动读同一场会话最近的 N 轮（N = history_turns
        或 config.HISTORY_TURNS），既交给模型当上下文，也用于「本轮没说时间/地点时沿用上一轮」
      - history_turns=0 可单次关掉上下文；SECRETARY_HISTORY_TURNS=0 全局关掉
    """
    model = model or config.MODEL
    max_steps = max_steps or config.MAX_STEPS
    t_all = time.time()
    trace = []
    raw_question = question
    # ⓪ 字面纠错 + 名称归一（阶段①的「字面」部分，在问题补全之前）。
    #    必须先做：字没改对，后面检索知识库、补全、写 SQL 全是白费。
    t_fix = time.time()
    fixed = name_fix.fix(question)
    if fixed['used']:
        question = fixed['text']
        trace.append({'seq': len(trace) + 1, 'kind': 'namefix', 'tool': 'name_fix',
                      'ms': fixed['ms'],
                      'args': {'input': raw_question},
                      'result': {'fixed': fixed['text'],
                                 'word_fixes': fixed['word_fixes'],
                                 'name_fixes': fixed['name_fixes'],
                                 'names': fixed['names'],
                                 'areas': fixed['areas'],
                                 'unresolved': fixed.get('unresolved', []),
                                 'hint': fixed['hint']}})
    namefix_ms = int((time.time() - t_fix) * 1000)
    # ⓪·4 载入多轮上下文（同一场会话最近的几轮问答）。
    #      **必须放在时间地点补全之前** —— 本轮没提时间/地点时要沿用上一轮的口径。
    #      载入失败按「无上下文」处理，绝不影响本轮问答。
    turns = config.HISTORY_TURNS if history_turns is None else max(0, int(history_turns))
    history = []
    if turns and session_id:
        try:
            import qa_log
            history = qa_log.load_context(session_id, limit=turns)
        except Exception as e:                      # noqa: BLE001
            print('[agent] 读取多轮上下文失败（按无上下文继续）：%s: %s'
                  % (type(e).__name__, e), flush=True)
    prev_scope = None
    if history:
        last = history[-1]
        prev_scope = {'period_type': last.get('period_type'),
                      'period_key': last.get('period_key'),
                      'time_text': last.get('time_text'),
                      'place': last.get('place')}
    # ⓪·5 时间与范围补全：相对时间词换成具体期间；没说的优先沿用上一轮，再没有才补默认值。
    #       这种换算是确定性的（「上月」是几月取决于今天），交给代码，不留给模型去猜日期。
    t_scope = time.time()
    scope = time_scope.describe(question, fixed, prev=prev_scope)
    if scope['question'] and scope['question'] != question:
        question = scope['question']
    trace.append({'seq': len(trace) + 1, 'kind': 'scope', 'tool': 'time_scope',
                  'ms': int((time.time() - t_scope) * 1000),
                  'result': {'matched': scope['time']['matched'],
                             'time': scope['time']['time_text'],
                             'period_type': scope['time']['period_type'],
                             'period_key': scope['time']['period_key'],
                             'time_is_default': scope['time']['is_default'],
                             'place': scope['place'],
                             'place_is_default': scope['place_default'],
                             'history_turns': len(history),
                             'time_inherited': scope['inherited']['time'],
                             'place_inherited': scope['inherited']['place'],
                             'hint': scope['time']['note']}})
    done = complete_question(question, model,
                             scope_text=scope['scope_text'],
                             prev_question=(history[-1].get('raw_question') if history else None))
    completed = done['completed']
    # 判断（我们这边唯一的职责）：补全有没有产出与原文不同的内容 —— 相同就视为“没找到对应规则、不采用”
    used = bool(completed and completed.strip() and completed.strip() != question.strip())
    trace.append({'seq': len(trace) + 1, 'kind': 'complete', 'tool': 'question_completion',
                  'ms': done['ms'], 'args': {'input': question, 'rules': done['rules']},
                  'result': {'completed': completed, 'ok': done['ok'], 'used': used,
                             'reason': '按规则库补全' if used else '规则库没有对应问法，不采用补全'}})
    t_loop = time.time()
    # 问题为准；补全结果只作参考，由模型自己判断适不适用
    user_content = question
    if history:
        hl = []
        for i, h in enumerate(history, 1):
            hl.append('%d) 用户：%s' % (i, h.get('raw_question') or ''))
            hl.append('   系统：%s' % (h.get('answer') or '').replace('\n', ' '))
            hl.append('   当时口径：%s / %s' % (h.get('time_text') or '-', h.get('place') or '-'))
        user_content += (
            '\n\n【对话历史】（同一场会话最近的 %d 轮，**只用来判断本轮问题在问什么、省略了什么**）\n%s\n'
            '注意三点：① 历史里的数字是按**当时的口径**算出来的，不能当成本轮答案；'
            '本轮必须按下面【统计范围】重新查数。'
            '② 本轮的统计口径已在上面的【统计范围】里定好，**不要**因为历史而改变它。'
            '③ 不要在回答里复述或提及这段历史。' % (len(history), '\n'.join(hl)))
    # 统计范围必须明确交给模型：六项指标是按「范围 + 期间」预计算的，
    # 不给这一句，它就得自己猜该取 month 还是 yearToDate、该取哪个月。
    user_content += ('\n\n【统计范围】（已按今天日期算好，直接采用，不要自行推算日期）\n'
                     + scope['scope_text'])
    if fixed['hint']:
        # 名称被归到「地名层」时（库里没有这个精确名），必须告诉模型用前缀查，
        # 否则它很可能拿合成名做等值匹配，一条都查不到还以为是空数据。
        user_content += ('\n\n【名称归一说明】' + fixed['hint'])
    if used:
        user_content += ('\n\n【补全参考】按知识库的补全规则库，这个问法通常应补成下面这样。'
                         '它**只是参考**：如果与上面【统计范围】里的时间或地点不一致，'
                         '**一律以【统计范围】为准**；如果它提到的指标在当前数据里查不到，'
                         '也以问题为准，忽略不适用的部分。\n' + completed)
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
                                 '另外：需要查多个互不依赖的数时，请把要用到的 run_sql **一次发出**，'
                                 '不要一条一条分多轮来。'
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
        'input_question': raw_question,
        'fixed_question': question,
        'name_fix_used': fixed['used'],
        'scope': {
            'time': scope['time']['time_text'],
            'period_type': scope['time']['period_type'],
            'period_key': scope['time']['period_key'],
            'time_is_default': scope['time']['is_default'],
            'place': scope['place'],
            'place_is_default': scope['place_default'],
            # 这两个为 true 说明该值**不是用户这轮说的**，而是从上一轮沿用来的
            'time_inherited': scope['inherited']['time'],
            'place_inherited': scope['inherited']['place'],
        },
        'user_profile': profile or '',
        'completion_used': used,
        'completed_question': completed,
        'answer': answer,
        'trace': trace,
        'model': model,
        'elapsed_ms': int((time.time() - t_all) * 1000),
        'timings': {
            'namefix_ms': namefix_ms,
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
        'history_used': len(history),
    }
    try:
        if gaps.is_gap(answer, blocked):
            gaps.record(question, completed, answer, model=model,
                        db_count=result['db_query_count'], kb_count=result['kb_query_count'],
                        unverified=blocked)
            result['recorded_as_gap'] = True
    except Exception:
        pass
    # 落库：问答/会话/消息/执行明细写进 agent_data 库。
    # **这一段的任何异常都必须吞掉** —— 记录是附属能力，不能因为它把回答搞丢。
    try:
        import qa_log
        rec = qa_log.save(result, session_id=session_id, user_id=user_id, user_code=user_code,
                          client_ip=client_ip, channel=channel)
        result['session_id'] = rec['session_id']
        result['qa_id'] = rec['qa_id']
    except Exception as e:                          # noqa: BLE001
        result['session_id'] = session_id
        result['qa_id'] = None
        # flush：不加的话日志重定向到文件时会被缓冲，等于没有输出
        print('[qa_log] 落库失败（不影响回答）：%s: %s' % (type(e).__name__, e), flush=True)
    return result
