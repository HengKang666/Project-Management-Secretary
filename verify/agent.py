# -*- coding: utf-8 -*-
"""模型自主查库回答：给工具，不给流程。"""
import json
import ssl
import urllib.request

import db

BASE = db.ENV['LLM_BASE_URL'].rstrip('/')
KEY = db.ENV['LLM_API_KEY']
MODEL = 'qwen3.8-max'
MAX_STEPS = 8

SYSTEM = (
    '你是随州供电公司的项目管理秘书。你可以调用工具查询业务数据库（只读）。\n'
    '规则：\n'
    '1. 先自己判断该查什么：需要哪些表、字段怎么算，用工具去查表结构和数据，不要凭猜测下结论。\n'
    '2. 所有数字必须来自工具返回的结果。查不到、算不出、库里没有，就直说；不许编造，也不许拿别的年份或别的范围顶替。\n'
    '3. 最终回答用中文，120 字以内：先给结论，再给关键数字，数字后面注明口径（时间范围、统计范围）。'
)

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_tables',
        'description': '列出业务库里的表（表名、注释、估算行数）。keyword 可选，按表名或注释模糊筛选。',
        'parameters': {'type': 'object', 'properties': {'keyword': {'type': 'string'}}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'describe_table',
        'description': '列出某张表的字段（字段名、类型、是否可空、注释）。',
        'parameters': {'type': 'object', 'properties': {'table': {'type': 'string'}}, 'required': ['table']}}},
    {'type': 'function', 'function': {
        'name': 'run_sql',
        'description': '在只读业务库执行一条 SELECT（不接受分号与注释，最多返回 200 行）。',
        'parameters': {'type': 'object', 'properties': {'sql': {'type': 'string'}}, 'required': ['sql']}}},
]


def _post(messages):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps({'model': MODEL, 'messages': messages, 'tools': TOOLS,
                       'enable_thinking': False, 'temperature': 0, 'max_tokens': 1200}).encode('utf-8')
    req = urllib.request.Request(BASE + '/chat/completions', data=body, headers={
        'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    raw = urllib.request.urlopen(req, timeout=120, context=ctx).read().decode('utf-8', 'replace')
    return json.loads(raw)


def execute(name, args):
    if name == 'list_tables':
        return db.list_tables(args.get('keyword', '') or '')
    if name == 'describe_table':
        return db.describe_table(args.get('table', ''))
    if name == 'run_sql':
        return db.run_sql(args.get('sql', ''))
    return {'error': 'unknown tool ' + str(name)}


def ask(question):
    messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': question}]
    trace = []
    for step in range(1, MAX_STEPS + 1):
        r = _post(messages)
        msg = r['choices'][0]['message']
        calls = msg.get('tool_calls') or []
        keep = {'role': 'assistant', 'content': msg.get('content') or ''}
        if calls:
            keep['tool_calls'] = calls
        messages.append(keep)
        if not calls:
            return {'answer': (msg.get('content') or '').strip(), 'trace': trace, 'steps': step}
        for tc in calls:
            name = tc['function']['name']
            try:
                args = json.loads(tc['function'].get('arguments') or '{}')
            except Exception:
                args = {}
            try:
                res = execute(name, args)
            except Exception as e:
                res = {'error': type(e).__name__ + ': ' + str(e)}
            trace.append({'step': step, 'tool': name, 'args': args, 'result': res})
            messages.append({'role': 'tool', 'tool_call_id': tc['id'],
                             'content': json.dumps(res, ensure_ascii=False, default=str)[:8000]})
    return {'answer': '（达到最大步骤仍未给出结论）', 'trace': trace, 'steps': MAX_STEPS}
