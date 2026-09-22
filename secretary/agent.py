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


# 回答契约：**固定在提示词里，不靠知识库检索**。
#
# 为什么固定：检索按相关度取切片，可能只命中「阈值」而漏掉「怎么答」。
# 所以「从哪些角度答、答到什么粒度、多长」永远随请求带上；
# 知识库只负责「判断标准是多少、口径怎么写、取数用哪张表」。
ANALYSIS_FRAME = (
    '【回答契约】（每次都按它走，不依赖检索到的内容）\n'
    '1. 先定性：这道题要的是「有哪些 / 有多少 / 为什么 / 怎么办 / 合不合理」中的哪一种。\n'
    '2. 问「有哪些 / 哪些 / 是哪些」→ **必须逐条把对象列出来**（一行一条），'
    '每条给题目问到的字段（名字、所属、金额、状态、天数…）；**只回一个数就是答非所问**。\n'
    '3. 问「有多少 / 几个」→ 给数，并说明这个数是怎么算的（范围 + 状态 + 时间口径）。\n'
    '4. 问「为什么 / 原因」→ 业务库里通常**没有原因字段**：先把能确定的事实给全'
    '（状态、停留天数、金额、卡在哪个节点、责任人），再明确写「原因需业务核实，系统里没有原因字段」。'
    '**不许编原因，也不许回一句「未找到相关信息」。**\n'
    '5. 问「怎么办 / 先办哪几个 / 给建议」→ 给排序 + 排序依据 + 该谁动。\n'
    '6. 只问一个数 → 只给数，**不要附加「偏高/偏低/异常」这类判断**。\n'
    '7. 判断标准只认下面【判断标准】里给的；没给标准就不下判断、不编标准。\n'
    '8. 长度：先结论后依据，正文控制在 300 字内（逐条清单可略长，一行一条、不要客套）。\n'
    '9. 数量必须与清单一致：说「N 条」，清单就得正好 N 条；概览与明细不能互相矛盾。\n'
)


# 检索回来的切片里，只挡掉这些明显是测试/噪声的文档，其余业务文档一律采用。
KB_SKIP_DOCS = ('测试',)


def analysis_rules(topic, question, top_k=4, limit=700):
    """检索知识库里的判断标准（判据）。**只取判据，不取生成答案**。

    这一步同时兼作分流：**命中判据 = 按判据判断；没命中 = 只呈现事实**。
    检索失败按「没判据」处理：宁可只报事实，也不许拿别的东西当标准。
    """
    q = (topic or question or '').strip()
    if not q:
        return []
    try:
        r = tools_kb.kb_search(q, top_k=top_k * 2, limit=limit)
        out, seen = [], set()
        for n in (r.get('nodes') or []):
            doc = n.get('doc_name') or ''
            if any(d in doc for d in KB_SKIP_DOCS) or doc in seen:
                continue          # 同一篇文档只取分数最高的一条，别把切片堆满提示词
            seen.add(doc)
            out.append(n)
        return out[:top_k]
    except Exception:
        return []


# 兜底：字典/提示词读不到时才用（正常情况下系统提示全部来自数据库）
_FALLBACK = '你是随州供电公司的项目管理秘书，负责回答业务问题。'


def _system(today=None):
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
    # 必须给「今天」：停留天数、同比、"截至今天"全靠它。不给，同一条单两次会算出不同天数。
    head += ('\n\n今天的日期是 %s。所有「停留天数 / 截至今天」一律按这一天算，'
             '不要拿数据里的时间当今天。' % (today or time.strftime('%Y-%m-%d')))
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


def ask(question, model=None, max_steps=None, profile=None, want_page=False, today=None, scope=None,
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
    scope = time_scope.describe(question, fixed, prev=prev_scope, today=today)
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
    # **不预检索、不预注入判据**：按问题检索判据挑"该用哪条标准"实测不稳（会把相邻技能的标准当成本题的标准，
    # 例如把「项目执行进度异常」当成「审批积压」）。判据改成让模型在循环里用 kb_search 工具自己查 ——
    # 需要判断就去查，查不到就不下判断（见回答契约第 7 条）。
    # 问题也不改写：补全只补地名/名称/时间（上面的 name_fix 与 time_scope）。
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
    # 角度固定（ANALYSIS_FRAME 永远带），标准可检索 —— 两层分开，互不依赖
    user_content += '\n\n' + ANALYSIS_FRAME
    user_content += ('\n【判断标准从哪来】要做判断（是否异常/逾期/超阈/合理、该先办哪个）之前，'
                     '**先用 kb_search 查判断标准**（查《判据规则》或对应技能文档）；'
                     '查到了就按它判，查不到就只报事实并说明「系统里没有这类判定标准」。'
                     '**不许拿相邻技能的阈值套到本题上，也不许自己编标准。**')
    if profile:
        user_content += ('\n\n【提问人身份与数据范围】（判断标准与关注重点按它来，不要因此增减问题里已经要求的必答项）：\n' + profile)
    if scope:
        # 身份范围必须**压过**【统计范围】里的默认「全市」——否则上游传了范围，模型还是按全市答。
        user_content += ('\n\n【本题数据范围（强制）】%s\n'
                         '上面【统计范围】里的地点若是「全市」，那只是默认值；'
                         '**本题一律只取 %s 范围内的数据**，并在回答里写明这个范围。' % (scope, scope))
    messages = [{'role': 'system', 'content': _system(today)}, {'role': 'user', 'content': user_content}]
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
    # qtype 只作参考，且按**实际发生的事**判定：循环里查过判据（kb_search）才算分析题（不再猜题型）。
    # 必须在页面判断之前算出来 —— 页面默认只给分析题。
    qtype = '分析' if any(t.get('tool') == 'kb_search' and t.get('kind') == 'tool' for t in trace) else '查数'
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
        'today': today or time.strftime('%Y-%m-%d'),
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
        'completion_used': False,      # 已取消「问题改写」，这个字段保留只为兼容历史记录
        'qtype': qtype,
        'analysis_rules_hit': len([t for t in trace if t.get('tool') == 'kb_search' and t.get('kind') == 'tool']),
        'page_html': page_html,
        'completed_question': question,   # 不再改写问题：就是用户原话（只做过地名/时间补全）
        'answer': answer,
        'trace': trace,
        'model': model,
        'elapsed_ms': int((time.time() - t_all) * 1000),
        'timings': {
            'namefix_ms': namefix_ms,
            'completion_ms': 0,          # 问题改写已取消；判据检索的时间在 trace 的 rules 步里
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
            gaps.record(question, question, answer, model=model,
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
