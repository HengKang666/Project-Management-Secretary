# -*- coding: utf-8 -*-
"""对话记录落库（agent_data 库）+ 历史查询。

与 tools_db 的分工（**别混**）：
    tools_db   读 dlj_data（业务库），**严格只读**（只放 SELECT/WITH + 表白名单）
    本模块     读写 agent_data（记录库），需要写权限，**独立连接**，不走 tools_db 的闸

三条纪律（与 name_fix / gaps 一致）：
  1. **失败只打日志** —— 落库坏了绝不影响问答。print 必须 flush，否则重定向到文件时看不到
  2. **一个开关能整体关掉** —— SECRETARY_QA_LOG=0
  3. **重活异步，关键同步** —— 见下面 save() 的说明

写哪些表：
    t_chat_session      本会话一行（不存在则建，标题取首问）
    t_chat_message      两条：用户那条 + AI 那条（AI 那条带 reply_to_id 指回用户那条）
    t_chat_query_trace  一行：这次问答的口径与质量（纠错命中 / 时间地点 / 查库次数 / 是否可信）
    t_chat_trace_step   N 行：每一步（纠错/补全/工具调用/作答/拦下）
"""
import json
import os
import threading
import uuid

import pymysql

import config

# 步骤明细的截断长度：一次问答 5~10 步，不截断会把库撑大
_TEXT_MAX = 2000


def _enabled():
    return os.environ.get('SECRETARY_QA_LOG', '1') not in ('0', 'false', 'False', 'off')


def _conn():
    """记录库的连接。**每次用完就关** —— 写入频率低（一次问答一次），
    长连接反而要处理探活/失效，不划算。

    这里显式写参数、不用 `**config.DB`：config.DB 里的键名是 `db`，
    而 pymysql 1.1+ 已把它标为废弃（要求用 `database`），传进去会刷 DeprecationWarning。
    """
    return pymysql.connect(host=config.DB['host'], port=config.DB['port'],
                           user=config.DB['user'], password=config.DB['password'],
                           database=config.AGENT_DB,
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True,
                           connect_timeout=5, read_timeout=15, write_timeout=15,
                           charset='utf8mb4')


def available():
    """记录库是否就绪（供 /health）。"""
    if not _enabled():
        return False
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute('SELECT COUNT(*) AS n FROM t_chat_session')
            cur.fetchone()
        finally:
            c.close()
        return True
    except Exception:
        return False


def status():
    if not _enabled():
        return {'available': False, 'reason': '已通过 SECRETARY_QA_LOG=0 关闭'}
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute('SELECT COUNT(*) AS n FROM t_chat_session')
            cur.fetchone()
        finally:
            c.close()
        return {'available': True, 'reason': ''}
    except Exception as e:                      # noqa: BLE001
        return {'available': False,
                'reason': '记录库 %s 不可用（%s: %s），本次问答不落库' %
                          (config.AGENT_DB, type(e).__name__, str(e)[:120])}


def new_session_id():
    """不传 session_id 时由服务端生成（UUID，对外暴露用这个，不暴露自增 id）。"""
    return 's_' + uuid.uuid4().hex


# ---------------------------------------------------------------- 写

def _json(v, cap=_TEXT_MAX):
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return s[:cap]


def _resolve_user(cur, user_id, user_code):
    """拿到 t_user.id。调用方可能只给业务用户ID（uid），这里换成主键。"""
    if user_id:
        return int(user_id), None
    code = (user_code or '').strip()
    if not code:
        return None, None
    cur.execute('SELECT id, uid FROM t_user WHERE uid=%s AND deleted_flag=0 LIMIT 1', (code,))
    r = cur.fetchone()
    return (r['id'], r['uid']) if r else (None, code)


def save(result, session_id=None, user_id=None, user_code=None,
         client_ip=None, channel=None):
    """把一次问答落库。返回 {'session_id', 'qa_id'}；失败抛异常（由调用方兜住）。

    **为什么关键部分同步、步骤异步**：
      - 主表（会话/消息/trace）同步写，因为响应里要回 `qa_id` 给前端做评价，
        异步就拿不到 id 了。这几条 INSERT 正常只要几十毫秒。
      - 步骤明细每次 5~10 行、每行最多 2KB，是最重的一块，放后台线程，
        不占用响应时间。
    """
    sid = session_id or new_session_id()
    if not _enabled():
        return {'session_id': sid, 'qa_id': None}
    info = _save_sync(result, sid, user_id, user_code, client_ip, channel)
    if info.get('steps'):
        t = threading.Thread(target=_save_steps, args=(info['qa_id'], info['steps']), daemon=True)
        t.start()
    return {'session_id': sid, 'qa_id': info['qa_id']}


def _save_sync(result, sid, user_id, user_code, client_ip, channel):
    """写会话 + 两条消息 + trace 主行。seq_no 冲突时重试一次。"""
    scope = result.get('scope') or {}
    timings = result.get('timings') or {}
    answer = result.get('answer') or ''
    question = result.get('input_question') or ''
    unverified = bool(result.get('unverified'))
    try:
        import gaps
        is_gap = bool(gaps.is_gap(answer, unverified))
    except Exception:
        is_gap = unverified

    last_err = None
    for _ in range(2):                          # 唯一键冲突重试一次
        c = _conn()
        try:
            cur = c.cursor()
            uid_pk, uid_code = _resolve_user(cur, user_id, user_code)
            cur.execute('SELECT id FROM t_chat_session WHERE session_id=%s', (sid,))
            row = cur.fetchone()
            if row:
                skey = row['id']
                cur.execute('SELECT MAX(seq_no) AS m FROM t_chat_message WHERE session_id=%s', (skey,))
                seq = int(cur.fetchone()['m'] or 0)
            else:
                cur.execute(
                    'INSERT INTO t_chat_session '
                    '(session_id, user_id, user_code, ai_model, title, channel, client_ip, '
                    ' msg_count, status, last_msg_at) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,0,1,NOW())',
                    (sid, uid_pk, uid_code, result.get('model'),
                     (question or '')[:50] or None, channel or 'webapi', client_ip))
                skey = cur.lastrowid
                seq = 0
            # 用户那条
            cur.execute(
                'INSERT INTO t_chat_message '
                '(session_id, chat_session_id, seq_no, direction, sender_type, sender_id, '
                ' msg_type, content, gen_status, send_status) '
                'VALUES (%s,%s,%s,1,1,%s,1,%s,1,3)',
                (skey, sid, seq + 1, uid_pk, question))
            user_msg_id = cur.lastrowid
            # AI 那条（带耗时与模型）
            cur.execute(
                'INSERT INTO t_chat_message '
                '(session_id, chat_session_id, seq_no, direction, sender_type, reply_to_id, '
                ' msg_type, content, model, latency_ms, gen_status, send_status, error_msg) '
                'VALUES (%s,%s,%s,2,2,%s,1,%s,%s,%s,%s,3,%s)',
                (skey, sid, seq + 2, user_msg_id, answer, result.get('model'),
                 int(result.get('elapsed_ms') or 0),
                 2 if unverified else 1,
                 '未调用工具即下结论，已拦下' if unverified else None))
            ai_msg_id = cur.lastrowid
            # trace 主行
            cur.execute(
                'INSERT INTO t_chat_query_trace '
                '(message_id, chat_session_id, raw_question, fixed_question, completed_question, '
                ' user_profile, name_fix_used, completion_used, '
                ' period_type, period_key, time_text, time_is_default, place, place_is_default, '
                ' model, db_query_count, sql_query_count, kb_query_count, steps, '
                ' no_db_query, unverified, is_gap, elapsed_ms, namefix_ms, completion_ms, loop_ms) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (ai_msg_id, sid,
                 question, result.get('fixed_question'), result.get('completed_question'),
                 (result.get('user_profile') or None),
                 1 if result.get('name_fix_used') else 0,
                 1 if result.get('completion_used') else 0,
                 scope.get('period_type'), scope.get('period_key'), scope.get('time'),
                 1 if scope.get('time_is_default') else 0,
                 scope.get('place'), 1 if scope.get('place_is_default') else 0,
                 result.get('model'),
                 int(result.get('db_query_count') or 0), int(result.get('sql_query_count') or 0),
                 int(result.get('kb_query_count') or 0), int(result.get('steps') or 0),
                 1 if result.get('no_db_query') else 0, 1 if unverified else 0, 1 if is_gap else 0,
                 int(result.get('elapsed_ms') or 0),
                 int(timings.get('namefix_ms') or 0), int(timings.get('completion_ms') or 0),
                 int(timings.get('loop_ms') or 0)))
            qa_id = cur.lastrowid
            cur.execute('UPDATE t_chat_session SET msg_count = msg_count + 2, last_msg_at = NOW(), '
                        'total_cost_ms = total_cost_ms + %s WHERE id=%s',
                        (int(result.get('elapsed_ms') or 0), skey))
            return {'session_id': sid, 'qa_id': qa_id,
                    'steps': result.get('trace') or [], 'ok': True}
        except pymysql.err.IntegrityError as e:
            last_err = e                          # 多半是 seq_no 撞了，重试一次
        except Exception as e:                    # noqa: BLE001
            raise
        finally:
            c.close()
    raise last_err or RuntimeError('落库失败')


def _save_steps(qa_id, trace):
    """步骤明细（后台线程）。失败只打日志。"""
    if not qa_id or not trace:
        return
    try:
        c = _conn()
        try:
            cur = c.cursor()
            for i, s in enumerate(trace, 1):
                if not isinstance(s, dict):
                    continue
                res = s.get('result') if isinstance(s.get('result'), dict) else None
                err = (res or {}).get('error')
                out = res if res else (s.get('content') or '')
                cur.execute(
                    'INSERT INTO t_chat_trace_step '
                    '(trace_id, step_no, step_kind, round_no, tool_name, input_text, output_text, '
                    ' sql_text, result_rows, cost_ms, model_ms, status, error_msg) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (qa_id, int(s.get('seq') or i), s.get('kind'), s.get('round'),
                     s.get('tool'), _json(s.get('args')), _json(out),
                     (res or {}).get('sql'), (res or {}).get('row_count'),
                     s.get('ms'), s.get('model_ms'),
                     1 if err else 0, (str(err)[:500] if err else None)))
        finally:
            c.close()
    except Exception as e:                        # noqa: BLE001
        print('[qa_log] 步骤明细写入失败（不影响回答）：%s: %s'
              % (type(e).__name__, e), flush=True)


# ---------------------------------------------------------------- 读（历史会话）

def load_context(session_id, limit=5, answer_chars=400):
    """取同一场会话最近的 N 轮问答，用作**多轮上下文**。返回按时间正序（旧 → 新）。

    只取「有 AI 回答」的轮次（靠 trace 关联），所以正在进行中的那轮不会混进来。
    用途有两个：
      ① 交给模型当上下文 —— 理解「那全市的呢」这类省略了主语的问法；
      ② 在**问题完全没提时间/地点**时，沿用上一轮的口径（见 time_scope.describe）。

    ★ 上游传 0 表示「本次不带上下文」（见 agent.ask），那时**不应该调用本函数** ——
      这里 max(1, ...) 会兜成 1 轮，语义就变了。
    """
    if not session_id:
        return []
    limit = max(1, min(int(limit or 5), 20))
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute(
            'SELECT m.seq_no, q.raw_question, q.fixed_question, q.place, q.place_is_default, '
            '       q.period_type, q.period_key, q.time_text, q.time_is_default, m.content AS answer '
            'FROM t_chat_message m '
            'JOIN t_chat_query_trace q ON q.message_id = m.id AND q.deleted_flag = 0 '
            'WHERE m.chat_session_id=%s AND m.deleted_flag=0 AND m.sender_type=2 '
            'ORDER BY m.seq_no DESC LIMIT %s', (session_id, limit))
        rows = cur.fetchall()[::-1]                     # 转成正序
        for r in rows:
            r['answer'] = (r.get('answer') or '')[:answer_chars]
        return rows
    finally:
        c.close()


def list_sessions(user_code=None, limit=20, offset=0):
    """历史对话列表：一行一场会话，按最后活动时间倒序。"""
    limit = max(1, min(int(limit or 20), 200))
    offset = max(0, int(offset or 0))
    sql = ('SELECT s.session_id, s.user_code, s.title, s.ai_model, s.channel, s.client_ip, '
           '       s.msg_count, s.status, s.create_time, s.last_msg_at, s.ended_at, '
           '       (SELECT COUNT(*) FROM t_chat_query_trace q '
           '         WHERE q.chat_session_id = s.session_id AND q.deleted_flag=0) AS ask_count, '
           '       (SELECT COUNT(*) FROM t_chat_query_trace q '
           '         WHERE q.chat_session_id = s.session_id AND q.is_gap=1 AND q.deleted_flag=0) AS gap_count '
           'FROM t_chat_session s WHERE s.deleted_flag=0')
    params = []
    if user_code:
        sql += ' AND s.user_code = %s'
        params.append(user_code)
    sql += ' ORDER BY COALESCE(s.last_msg_at, s.create_time) DESC, s.id DESC LIMIT %s OFFSET %s'
    params += [limit, offset]
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        for r in rows:                            # datetime → 字符串，便于 JSON 输出
            for k in ('create_time', 'last_msg_at', 'ended_at'):
                if r.get(k) is not None:
                    r[k] = str(r[k])
        return rows
    finally:
        c.close()


def get_history(session_id, limit=200, with_steps=False):
    """某场会话的历史对话记录：消息 + 每条 AI 回答对应的口径/质量信息。

    消息与 trace 是 **LEFT JOIN** —— 用户那条消息没有 trace，不能丢。
    """
    limit = max(1, min(int(limit or 200), 1000))
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('SELECT session_id, user_code, title, ai_model, channel, msg_count, '
                    'status, create_time, last_msg_at, ended_at '
                    'FROM t_chat_session WHERE session_id=%s AND deleted_flag=0', (session_id,))
        session = cur.fetchone()
        if not session:
            return None
        for k in ('create_time', 'last_msg_at', 'ended_at'):
            if session.get(k) is not None:
                session[k] = str(session[k])
        cur.execute(
            'SELECT m.id, m.seq_no, m.direction, m.sender_type, m.sender_id, m.reply_to_id, '
            '       m.msg_type, m.content, m.model, m.latency_ms, m.create_time, '
            '       q.id AS qa_id, q.fixed_question, q.completed_question, '
            '       q.name_fix_used, q.completion_used, q.period_type, q.period_key, '
            '       q.time_text, q.time_is_default, q.place, q.place_is_default, '
            '       q.db_query_count, q.sql_query_count, q.kb_query_count, q.steps, '
            '       q.no_db_query, q.unverified, q.is_gap, q.elapsed_ms, '
            '       q.feedback, q.feedback_note '
            'FROM t_chat_message m '
            'LEFT JOIN t_chat_query_trace q ON q.message_id = m.id AND q.deleted_flag = 0 '
            'WHERE m.chat_session_id=%s AND m.deleted_flag=0 '
            'ORDER BY m.seq_no LIMIT %s', (session_id, limit))
        msgs = cur.fetchall()
        for m in msgs:
            if m.get('create_time') is not None:
                m['create_time'] = str(m['create_time'])
        if with_steps:
            ids = [m['qa_id'] for m in msgs if m.get('qa_id')]
            steps = {}
            if ids:
                marks = ','.join(['%s'] * len(ids))
                cur.execute('SELECT trace_id, step_no, step_kind, round_no, tool_name, '
                            '       sql_text, result_rows, cost_ms, model_ms, status, error_msg '
                            'FROM t_chat_trace_step WHERE trace_id IN (' + marks + ') AND deleted_flag=0 '
                            'ORDER BY trace_id, step_no', ids)
                for s in cur.fetchall():
                    steps.setdefault(s['trace_id'], []).append(s)
            for m in msgs:
                if m.get('qa_id'):
                    m['steps'] = steps.get(m['qa_id'], [])
        return {'session': session, 'total': len(msgs), 'messages': msgs}
    finally:
        c.close()


def set_feedback(qa_id, feedback, note=None):
    """给某次回答打评价（1赞 / 0踩）。返回受影响行数。"""
    fb = 1 if int(feedback) == 1 else 0
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('UPDATE t_chat_query_trace SET feedback=%s, feedback_note=%s, feedback_time=NOW() '
                    'WHERE id=%s AND deleted_flag=0', (fb, (note or None), int(qa_id)))
        return cur.rowcount
    finally:
        c.close()


def stats():
    """记录库概览（供页面/运维看）。"""
    c = _conn()
    try:
        cur = c.cursor()
        out = {}
        for name, sql in [
            ('sessions', 'SELECT COUNT(*) AS n FROM t_chat_session WHERE deleted_flag=0'),
            ('messages', 'SELECT COUNT(*) AS n FROM t_chat_message WHERE deleted_flag=0'),
            ('questions', 'SELECT COUNT(*) AS n FROM t_chat_query_trace WHERE deleted_flag=0'),
            ('steps', 'SELECT COUNT(*) AS n FROM t_chat_trace_step WHERE deleted_flag=0'),
            ('gaps', 'SELECT COUNT(*) AS n FROM t_chat_query_trace WHERE is_gap=1 AND deleted_flag=0'),
            ('unverified', 'SELECT COUNT(*) AS n FROM t_chat_query_trace WHERE unverified=1 AND deleted_flag=0'),
            ('name_fix_hit', 'SELECT COUNT(*) AS n FROM t_chat_query_trace WHERE name_fix_used=1 AND deleted_flag=0'),
        ]:
            cur.execute(sql)
            out[name] = int(cur.fetchone()['n'])
        return out
    finally:
        c.close()


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print('SECRETARY_QA_LOG 开启：', _enabled())
    print('记录库：', config.AGENT_DB)
    print('状态：', status())
    if _enabled() and status()['available']:
        print('统计：', stats())
        print('\n最近 5 场会话：')
        for s in list_sessions(limit=5):
            print('   %s | %s | %s 问 | %s' % (s['session_id'], s['title'], s['ask_count'], s['last_msg_at']))
