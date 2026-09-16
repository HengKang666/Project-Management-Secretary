# -*- coding: utf-8 -*-
"""缺口清单：把"我们答不了"的问题记下来，供上游补知识 / 补指标。

触发条件：答案为"查不到/没有这个口径"，或被服务层拦下（未核实）。
落盘：output/gaps/gaps.jsonl（一行一条，可导出给上游同事）。
"""
import json
import os
import time

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'output', 'gaps')
_FILE = os.path.join(_DIR, 'gaps.jsonl')


def is_gap(answer, unverified=False):
    if unverified:
        return True
    a = (answer or '')[:60]
    return ('查不到' in a) or ('未核实' in a) or ('没有这个口径' in a) or ('暂无' in a and '数据' in a)


def record(raw_question, completed_question, answer, model=None, db_count=0, kb_count=0,
           unverified=False, kind='查不到'):
    os.makedirs(_DIR, exist_ok=True)
    row = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'kind': kind, 'raw': raw_question,
           'completed': completed_question, 'answer': (answer or '')[:300], 'model': model,
           'db_calls': db_count, 'kb_calls': kb_count, 'unverified': bool(unverified)}
    with open(_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def list_gaps(limit=200):
    if not os.path.exists(_FILE):
        return []
    rows = []
    with open(_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows[-limit:][::-1]
