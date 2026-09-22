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
        'name': 'find_column',
        'description': '在业务字典内的表里，按字段名或字段中文注释搜字段，返回「表名 + 字段名 + 类型 + 注释 + 示例值」。'
                       '当你不确定某个数据在哪个字段/哪张表时先用它（例如搜「线损」「执行金额」「台区」）。',
        'parameters': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': ['keyword']}}},
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
    return json.dumps(res, ensure_ascii=False, default=str)[:4000]


def execute(name, args):
    if name == 'kb_search':
        return tools_kb.kb_search(args.get('query', ''), int(args.get('top_k') or 3))
    if name == 'find_column':
        return tools_db.find_column(args.get('keyword', ''))
    if name == 'run_sql':
        return tools_db.run_sql(args.get('sql', ''))
    return {'error': 'unknown tool ' + str(name)}


# 问题补全用的提示：本服务自己做的第一步（上游只补时间与地点）。
# 只写「怎么用规则」，不写任何业务规则 —— 补成什么样完全由知识库切片决定。
COMPLETE_SYSTEM = (
    '你在做「问题补全 + 类型判断」：先把用户的问法补成一条标准的完整查询要求，再判断这道题要不要做分析。\n'
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
    '   - 补完的问题里，时间与地点都要按【统计范围】写全。\n'
    '5. **判类型**（只判这一件事，不要因为类型去改上面的补全结果）：\n'
    '   判据只有一条：**回答前要不要一个判断标准（阈值 / 规则 / 口径）**。要就是「分析」，不要就是「查数」。\n'
    '   - 「分析」：原因、趋势、对比，以及一切「有没有 / 是不是 / 合不合理」的判定题 ——'
     '因为它们必须先有标准才能下结论。\n'
    '     例：「今天为什么比昨天多」「今天有没有项目逾期」「线损率是不是偏高」「这笔钱花得合不合理」→ 分析。\n'
    '     问「先办哪几张 / 该怎么处理 / 下一步做什么 / 给个建议」也算分析 —— 要给排序或行动建议就必须先有标准。\n'
    '   - 「查数」：一个数、一组数、清单、排名、占比、多少个、是什么；取到数就能回答，不依赖任何标准。\n'
    '     例：「线损率是多少」「有多少个项目」「排名前五的所」「全市台区总共有多少个」→ 查数。\n'
    '   - 拿不准时判「查数」（判成分析会多查一次规则库，代价更高）。\n'
    '6. 输出**必须是一个 JSON 对象**，不要有任何其它文字、不要包代码块：\n'
    '   {"completed": "补全后的问题", "type": "查数" 或 "分析", "topic": "要分析什么"}\n'
    '   - type 只能是「查数」或「分析」这两个字面值。\n'
    '   - topic 只在 type=分析 时写：一句话说清要分析的对象与角度（用来检索分析规则），'
     '例如「供电所线损率同比升高的原因」；type=查数 时给空字符串。'
)


# 知识库里这两篇文档的名字（都在检索面索引 r57xtq9ypm 内）：
#   问法归一 = 残缺问法 -> 标准口径；分析规则 = 判断标准。
# 只认这两个文档名的切片 —— 否则别的文档会被当成判断标准，等于自己编标准。
ANALYSIS_DOCS = ('问法归一', '分析规则')


def analysis_rules(topic, question, top_k=8, limit=1500):
    """取「分析规则」切片。**只给分析类问题用**，查数类不调它。

    检索词优先用判出来的 topic（挑明了要分析什么），没有才退回原问题。
    检索失败按「没规则」处理：宁可只呈现事实，也不许拿别的东西当标准。
    """
    q = (topic or question or '').strip()
    if not q:
        return []
    try:
        r = tools_kb.kb_search(q, top_k=top_k, limit=limit)
        return [n for n in (r.get('nodes') or [])
                if any(d in (n.get('doc_name') or '') for d in ANALYSIS_DOCS)]
    except Exception:
        return []


def _parse_complete(raw, question):
    """把补全那一次调用的产出解析成 {completed, qtype, topic}。

    模型偶发不按 JSON 输出（只回一句补全后的问题）：那时**退回「查数 + 原问题」** ——
    既不让一段自然语言混进 completed，也不在没判准时误走分析分支。
    """
    txt = (raw or '').strip()
    if txt.startswith('```'):
        txt = txt.strip('`').strip()
    i, j = txt.find('{'), txt.rfind('}')
    if i >= 0 and j > i:
        try:
            d = json.loads(txt[i:j + 1])
            completed = str(d.get('completed') or '').strip()
            qtype = '分析' if str(d.get('type') or '').strip() == '分析' else '查数'
            topic = str(d.get('topic') or '').strip() if qtype == '分析' else ''
            if completed:
                return {'completed': completed, 'qtype': qtype, 'topic': topic, 'parsed': True}
        except Exception:
            pass
    return {'completed': question, 'qtype': '查数', 'topic': '', 'parsed': False}


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
    ask_text += '\n用户问题：' + question + '\n\n请按要求输出那个 JSON 对象。'
    msgs = [{'role': 'system', 'content': COMPLETE_SYSTEM},
            {'role': 'user', 'content': ask_text}]
    out = ''
    try:
        resp = _chat(msgs, model, use_tools=False)
        out = (resp['choices'][0]['message'].get('content') or '').strip()
    except Exception:
        out = ''
    dec = _parse_complete(out, question)
    return {'input': question, 'completed': dec['completed'], 'ok': bool(dec['parsed']),
            'qtype': dec['qtype'], 'topic': dec['topic'], 'parsed': dec['parsed'], 'raw': out,
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


# 「分析页面」用的系统提示：**独立的一次模型调用**，只产出一个单页 HTML 数据大屏。
PAGE_SYSTEM = (
    '你是HTML数据大屏生成助手小智。根据用户提供的内容，生成一个可直接运行的单页 HTML 页面。\n'
    '\n## 特殊规则\n'
    '- 如果用户内容中包含“澄清话术”，直接返回空字符串，不输出任何内容。\n'
    '- 只有在用户要求“生成列表”，生成的html页面不能生成echarts图表，必须输出表格\n'
    '- 只有在用户要求“编写报告”，生成的html页面不能生成echarts图表，也不用输出表格\n'
    '- 其他情况都要输出echarts表\n'
    '\n## 输出规范\n'
    '- 只能输出 HTML 源码本身。\n- 不要解释。\n- 不要使用 Markdown。\n- 不要使用代码块。\n'
    '- 不要输出除 HTML 以外的任何内容。\n- `body` 可滚动，但必须隐藏滚动条。\n'
    '\n## 页面要求\n'
    '- 必须包含 `<!DOCTYPE html>`、`<html>`、`<head>`、`<body>`。\n'
    '- 页面宽度 `100%`，高度 `100%`。\n- 表格平铺全部展示，不能用固定高度\n- 不能有滚动条\n'
    '- 不要生成页面标题。\n- 整体风格为科技风数据大屏。\n'
    '- 页面背景使用：`linear-gradient(145deg,#1e293b,#0f172a)`。\n'
    '- 全局字体使用：`font-family: cursive;`\n- 全局字号不得小于 `24px`。\n'
    '- 表格当中字体大小必须是20px\n'
    '\n## 技术要求\n'
    '- 使用 TailwindCSS CDN。\n- 使用 ECharts CDN。\n'
    '- 所有图表必须在 DOM 加载完成后再初始化，例如放在 `DOMContentLoaded` 中执行。\n'
    '- ECharts 必须基于已存在的 DOM 元素初始化，避免获取不到容器。\n'
    '- 每个图表都必须放在固定高度容器中。\n- ECharts 图表必须自适应父容器宽高。\n'
    '- 必须监听窗口变化并调用图表 `resize`。\n- 图表样式要有区分，不要全部使用同一种图表风格。\n'
    '\n## 内容规则\n'
    '- 只根据用户提供的内容进行布局和展示。\n- 不新增任何模块。\n- 不补充不存在的信息。\n- 不模拟数据。\n'
    '- 如果用户内容不足以生成图表，则使用文本、表格、分区块等方式展示，但仍然不能虚构内容。\n'
    '- 如果用户内容中包含“澄清话术”，直接返回空字符串，不输出任何内容。\n'
    '- 如果用户要求“生成列表”，生成的html页面不能生成echarts图表，必须输出表格\n'
    '- 如果用户要求“生成报告”，生成的html页面不能生成echarts图表，必须输出表格\n'
    '\n## 代码约束\n'
    '- 优先使用 Tailwind 工具类，尽量少写自定义 CSS。\n- `style` 标签内容控制在 10 行以内。\n'
    '- HTML 总行数控制在 240 行以内。\n- 代码必须可直接保存并运行。\n'
    '\n## 生成策略\n'
    '- 优先保证结构清晰、渲染稳定、代码简洁。\n- 减少冗余标签、重复样式和无意义包装层。\n'
    '- 仅输出最终 HTML 结果。\n'
    '- 图必须有数据：某类图**没有数据时不要保留空图容器**，改用卡片或表格展示 —— 空图比没有图更糟。'
)


def _strip_html(out):
    """从模型输出里取出 HTML 本体：去掉 ``` 包裹，丢掉 HTML 之前的任何说明文字。

    取不到 HTML（模型没照做）就返回空串 —— 宁可不要页面，也不要往接口里塞一段散文。
    """
    t = (out or '').strip()
    if t.startswith('```'):
        t = t.strip('`').strip()
        if t[:4].lower() == 'html':
            t = t[4:].lstrip()
    low = t.lower()
    i = low.find('<!doctype')
    if i < 0:
        i = low.find('<html')
    if i < 0:
        return ''
    return t[i:].strip()


def render_page(content, extra='', model=None, max_tokens=6000):
    """分析结论 -> 单页 HTML 数据大屏。**独立的一次模型调用，与作答那次不共用。**

    它是附属产物：任何异常都只返回空串，绝不能因为它把回答搞丢。
    """
    text = (content or '').strip()
    if not text or '澄清话术' in text:
        return ''
    if extra:
        text += ('\n\n【这次查到的原始数据（可直接用来作图，数字不要改、不要新增）】\n' + extra)
    try:
        resp = _chat([{'role': 'system', 'content': PAGE_SYSTEM},
                      {'role': 'user', 'content': text}],
                     model or config.PAGE_MODEL, use_tools=False, max_tokens=max_tokens)
        out = (resp['choices'][0]['message'].get('content') or '')
    except Exception as e:
        print('[agent] 分析页面渲染失败（不影响回答）：%s: %s' % (type(e).__name__, e), flush=True)
        return ''
    return _strip_html(out)


def ask(question, model=None, max_steps=None, profile=None, want_page=False,
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
    # 分流：这一次调用顺带判出来的题型（查数 / 分析）。解析失败一律当「查数」，不走分析分支。
    qtype = done.get('qtype') or '查数'
    rules_nodes = []          # 分析分支命中的「分析规则」切片，结果里要报个数
    # 判断（我们这边唯一的职责）：补全有没有产出与原文不同的内容 —— 相同就视为“没找到对应规则、不采用”
    used = bool(completed and completed.strip() and completed.strip() != question.strip())
    trace.append({'seq': len(trace) + 1, 'kind': 'complete', 'tool': 'question_completion',
                  'ms': done['ms'], 'args': {'input': question, 'rules': done['rules']},
                  'result': {'completed': completed, 'ok': done['ok'], 'used': used, 'qtype': qtype,
                             'topic': done.get('topic') or '', 'parsed': bool(done.get('parsed')),
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
    if qtype == '分析':
        rules_nodes = analysis_rules(done.get('topic'), question)
        ref = '\n\n'.join('【%s】%s' % (n.get('title') or '', n.get('content') or '') for n in rules_nodes)
        user_content += ('\n\n【分析规则】（这是本题的判断标准，**必须按它判断**）\n'
                         + (ref or '（没检索到对应规则：只呈现查得到的事实，不要自己编判断标准）')
                         + '\n规则里没写的标准不许自己编；查不到就写「未查得」，不许估算、不许补 0；'
                           '结论、依据（数字）、标准、建议四段都要有。')
        trace.append({'seq': len(trace) + 1, 'kind': 'analysis_rules', 'tool': 'kb_search',
                      'args': {'query': done.get('topic') or question},
                      'result': {'hit': len(rules_nodes),
                                 'docs': [{'doc_name': n.get('doc_name'), 'score': n.get('score')} for n in rules_nodes]}})
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
    loop_end = time.time()
    # 分析题的附加产物：**另一次模型调用**把结论渲染成单页 HTML 数据大屏。
    # 与作答那次不共用；同步生成、同步随本响应返回。页面失败只返回空串，回答照旧。
    page_html = ''
    if answer and config.PAGE_ON and (qtype == '分析' or want_page):
        # 页面只按「回答文字」生成时，文字里没有明细就画不出图（会出现空图区）。
        # 所以把这次 run_sql 查到的行一起给页面模型 —— 它有数据可画，才画得出。
        rows_ctx = []
        for _t in trace:
            if _t.get('kind') == 'tool' and _t.get('tool') == 'run_sql':
                _res = _t.get('result') or {}
                if _res.get('rows'):
                    rows_ctx.append({'rows': _res['rows'][:40]})
        extra = json.dumps(rows_ctx, ensure_ascii=False, default=str)[:6000] if rows_ctx else ''
        t_page = time.time()
        page_html = render_page(answer, extra=extra, model=model)
        trace.append({'seq': len(trace) + 1, 'kind': 'page', 'tool': 'render_page',
                      'ms': int((time.time() - t_page) * 1000),
                      'result': {'ok': bool(page_html), 'chars': len(page_html),
                                 'reason': '已生成分析页面' if page_html else '未生成（模型未给出 HTML 或调用失败）'}})
    DB_TOOLS = ('run_sql',)
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
        'qtype': qtype,
        'analysis_rules_hit': (len(rules_nodes) if qtype == '分析' else 0),
        'page_html': page_html,
        'completed_question': completed,
        'answer': answer,
        'trace': trace,
        'model': model,
        'elapsed_ms': int((time.time() - t_all) * 1000),
        'timings': {
            'namefix_ms': namefix_ms,
            'completion_ms': done['ms'],
            'loop_ms': int((loop_end - t_loop) * 1000),
            'page_ms': int((time.time() - loop_end) * 1000) if page_html else 0,
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
