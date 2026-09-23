# -*- coding: utf-8 -*-
"""L2 会话摘要：把一场会话的早期轮次压成一段话，续聊时注进提示。

**为什么需要**：现在的多轮上下文只带最近 5 轮（`config.HISTORY_TURNS`），
每轮回答还只带前 400 字。所以「翻出一场三个月前的会话继续聊」时，
模型只知道最后几轮说了什么，前面的全丢了。有了会话摘要，
续聊时能带上"这场会话之前大概聊过什么、用过什么口径"。

**它是怎么工作的**：

```
每轮问答落库后 → maybe_enqueue()：看未摘要轮数够不够阈值（默认 5）
   够了 → 入队 → 后台线程 _summarize()
       读「旧摘要 + seq_no > summary_upto_seq 的新轮次」
       ★ 但**末尾 SUMMARY_KEEP_RECENT 轮除外** —— 那几轮本来就原样进【对话历史】，
         重复写进摘要只会让同一件事出现两遍
       → 调模型压成 ≤300 字
       → 带乐观锁写回 t_chat_session.summary / summary_upto_seq
下次提问 → agent 把 summary 作为独立一段注入，并且【对话历史】只带
       seq_no > summary_upto_seq 的轮次（避免摘要与明细重复）
```

**三条设计红线**（每条都是踩过的坑）：

1. **异步**：一次问答已经 9~11 秒，摘要再同步做会雪上加霜。
2. **不是每轮都抽**：攒够 5 轮才做一次，token 消耗降一个数量级。
3. **摘要只写"还没过期的关注对象与固定叫法"**，且不超过 150 字。**三样东西一律不写**：
   ① 时间范围与统计主体（那是【统计范围】的活，摘要再写一份只会和它打架）；
   ② 任何结论、判断或状态（"未查到""未生成工单""处于草稿状态""数据缺失已改用累计"…）——
      这类话**没有数字**，所以躲得过"不许写数字"的纪律，却会被模型当成现成答案而不再查数；
   ③ 过程叙述（"后续轮次中又增加了…"）。
   ★ 2026-09-22 改版原因：旧版要求写"关注对象 + 已确认口径 + 结论性事实"，
     实测生成出来的摘要变成"对象清单 + 口径枚举 + 时效断言"的堆叠，
     例如"统计主体**先后使用过**全市和两水供电所""包括**两个**时间范围""工单**尚在**实施中"——
     续聊时和本轮的【统计范围】打架，把回答带偏。
"""
import json
import queue
import ssl
import threading
import time
import urllib.request

import config
import qa_log

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

_LOCK = threading.Lock()
_Q = queue.Queue()
_WORKER = {'thread': None}
_INFLIGHT = set()                                    # 正在摘要的会话，防重复入队

SUMMARIZE_SYSTEM = (
    '你在给一通「电力业务问答」会话做**上下文交接条**，用途只有一个：\n'
    '用户以后重新打开这场会话继续提问时，让模型知道你**之前在关注什么对象**。\n'
    '\n'
    '★ 判断标准只有一条：**「下一轮提问时，这句话还有用吗？」** 没用的一律不写。\n'
    '\n'
    '输出要求（逐条遵守）：\n'
    '1. 只输出正文。不要标题、不要前后缀、不要"以下是摘要"这类话。\n'
    '2. 用第三人称陈述，一件事一句话，总共不超过 150 字。**宁短勿全**。\n'
    '3. 只写这两类内容：\n'
    '   ① 当前在关注的对象（供电所 / 台区 / 部门 / 指标名）\n'
    '   ② 固定叫法（例如用户习惯把某个指标说成另一个词）\n'
    '4. ★ **不要写时间范围、不要写统计主体。**\n'
    '   本轮的统计口径由系统另行确定并直接告诉模型（就是【统计范围】），\n'
    '   你再写一份只会和它打架。历史曾经用过哪些时间、哪些主体，一律不要写。\n'
    '5. ★ **不要写任何结论、判断或状态**，包括但不限于：\n'
    '   「未查到」「未生成／未找到」「处于草稿或审核状态」「正在实施中」\n'
    '   「数据缺失，已改用累计口径」「与上年无法对比」「两个名称对应不同范围」。\n'
    '   —— 这类话一定会过期，而且模型会把它当成现成答案，不再去查数。\n'
    '   （业务数字当然也一律不写。）\n'
    '6. 不写寒暄、不写"用户提问了""系统回答了"，也不写\n'
    '   "后续轮次中…""在此基础上又增加了…"这类过程叙述。\n'
    '7. ★ 合并旧摘要时**以新的为准**：新内容与旧内容冲突就直接换掉，\n'
    '   不要保留两个版本，也不要写成"先后／曾经／包括两个…"这种并列。\n'
    '8. 如果新增轮次里没有值得保留的对象或叫法，就**原样返回已有摘要**（不要清空它）。'
)


def status():
    """/health 用。开关关掉时 available=False 且 reason 说明"未启用"（不算故障）。"""
    if not config.SESSION_SUMMARY:
        return {'available': False, 'reason': '未启用（SECRETARY_SESSION_SUMMARY=0）',
                'every': config.SUMMARY_EVERY}
    out = {'available': False, 'source': 'mysql:%s' % config.AGENT_DB,
           'every': config.SUMMARY_EVERY,
           'model': config.SUMMARY_MODEL or config.MODEL,
           'queue': _Q.qsize(),
           'worker_alive': bool(_WORKER['thread'] and _WORKER['thread'].is_alive())}
    try:
        c = qa_log._conn()
        try:
            cur = c.cursor()
            cur.execute('SELECT COUNT(*) AS n FROM t_chat_session '
                        'WHERE deleted_flag=0 AND summary IS NOT NULL')
            out['sessions_with_summary'] = int(cur.fetchone()['n'])
        finally:
            c.close()
    except Exception as exc:                          # noqa: BLE001
        out['reason'] = ('摘要列不可用（先跑 tools/apply_kb_file_schema.py）：%s: %s'
                         % (type(exc).__name__, str(exc)[:120]))
        return out
    out['available'] = True
    return out


def get(session_id):
    """取摘要（开关关掉时永远返回空，等于没有摘要）。"""
    if not config.SESSION_SUMMARY:
        return {'summary': '', 'upto_seq': 0, 'time': None}
    try:
        return qa_log.get_summary(session_id)
    except Exception as exc:                          # noqa: BLE001
        print('[session_summary] 读摘要失败（按无摘要继续）：%s: %s'
              % (type(exc).__name__, exc), flush=True)
        return {'summary': '', 'upto_seq': 0, 'time': None}


def maybe_enqueue(session_id):
    """问答落库后调用：未摘要轮数够阈值才入队。返回是否入队。"""
    if not (config.SESSION_SUMMARY and session_id):
        return False
    if config.SUMMARY_EVERY <= 0:
        return False
    try:
        n = qa_log.unsummarized_count(session_id)
    except Exception as exc:                          # noqa: BLE001
        print('[session_summary] 统计未摘要轮数失败：%s: %s' % (type(exc).__name__, exc), flush=True)
        return False
    if n < config.SUMMARY_EVERY:
        return False
    return enqueue(session_id)


def enqueue(session_id):
    with _LOCK:
        if session_id in _INFLIGHT:                   # 同一会话不重复排队
            return False
        _INFLIGHT.add(session_id)
        if _WORKER['thread'] is None or not _WORKER['thread'].is_alive():
            t = threading.Thread(target=_worker, name='session-summary', daemon=True)
            t.start()
            _WORKER['thread'] = t
    _Q.put(session_id)
    return True


def recover(limit=20):
    """启动时补跑：把「未摘要轮数已够阈值」的会话重新排一遍。

    和抽取不同，摘要**可以安全重跑**（幂等：读同一批轮次 + 乐观锁写回），
    所以中断后自动补上没问题。
    """
    if not config.SESSION_SUMMARY:
        return 0
    try:
        ids = qa_log.sessions_needing_summary(every=config.SUMMARY_EVERY, limit=limit)
    except Exception as exc:                          # noqa: BLE001
        print('[session_summary] 扫描待摘要会话失败：%s: %s' % (type(exc).__name__, exc), flush=True)
        return 0
    n = 0
    for sid in ids:
        n += 1 if enqueue(sid) else 0
    return n


def _chat(messages, model, max_tokens=700):
    body = {'model': model, 'messages': messages, 'enable_thinking': False,
            'temperature': 0, 'max_tokens': max_tokens}
    req = urllib.request.Request(
        config.LLM_BASE + '/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + config.LLM_KEY,
                 'Content-Type': 'application/json'})
    raw = urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT, context=_ctx) \
        .read().decode('utf-8', 'replace')
    return json.loads(raw)


def _summarize(session_id):
    old = qa_log.get_summary(session_id)
    turns = qa_log.turns_after(session_id, after_seq=old['upto_seq'], limit=40)
    if not turns:
        return
    # ★ 最近 SUMMARY_KEEP_RECENT 轮**始终会原样进【对话历史】**（见 agent.py 里的 `always`），
    #   所以不要再把它们写进摘要 —— 否则同一件事在提示里出现两遍：白烧 token，
    #   而且摘要里带的口径更容易和本轮的【统计范围】打架。
    keep = max(0, int(getattr(config, 'SUMMARY_KEEP_RECENT', 0) or 0))
    if keep:
        if len(turns) <= keep:
            return                      # 新增的全是"始终原样带"的轮次，还不值得摘要
        turns = turns[:-keep]
    upto = max(int(t.get('seq_no') or 0) for t in turns)
    lines = []
    for t in turns:
        q = (t.get('fixed_question') or t.get('raw_question') or '').strip()
        a = (t.get('answer') or '').strip().replace('\n', ' ')
        scope = ' / '.join(x for x in ((t.get('time_text') or ''), (t.get('place') or '')) if x)
        lines.append('- 用户问：%s%s\n  回答要点：%s' % (q, ('（口径：%s）' % scope) if scope else '', a))
    ask = '【已有摘要】\n' + (old['summary'] or '（暂无，这是第一次摘要）') + '\n\n'
    ask += ('【本场新增的 %d 轮问答】\n%s\n\n' % (len(turns), '\n'.join(lines)))
    ask += ('请输出更新后的上下文交接条（不超过 %d 字）。\n'
            '记住：只写**当前在关注的对象**和**固定叫法**；'
            '★ 不要写时间范围、不要写统计主体、不要写任何结论或状态（未查到／未生成／'
            '处于草稿／已改用累计…）、不要写数字。' % config.SUMMARY_MAX_CHARS)
    model = config.SUMMARY_MODEL or config.MODEL
    t0 = time.time()
    try:
        resp = _chat([{'role': 'system', 'content': SUMMARIZE_SYSTEM},
                      {'role': 'user', 'content': ask}], model)
        text = (resp['choices'][0]['message'].get('content') or '').strip()
    except Exception as exc:                          # noqa: BLE001
        print('[session_summary] 摘要生成失败（下次会重试）：%s: %s'
              % (type(exc).__name__, exc), flush=True)
        return
    if not text:
        return
    text = text[:config.SUMMARY_MAX_CHARS].strip()
    # ★ 乐观锁：只有 summary_upto_seq 还是我们读到的那个值才写回，
    #   否则说明有另一个任务刚写完，这次直接丢弃（下次会基于新摘要重算）。
    try:
        n = qa_log.set_summary(session_id, text, upto, expect_upto=old['upto_seq'])
    except Exception as exc:                          # noqa: BLE001
        print('[session_summary] 摘要写回失败：%s: %s' % (type(exc).__name__, exc), flush=True)
        return
    print('[session_summary] %s 摘要%s（覆盖到 seq=%s，%d 字，耗时 %.1fs）'
          % (session_id[:18], '已更新' if n else '被并发任务抢先，本次丢弃',
             upto, len(text), time.time() - t0), flush=True)


def _worker():
    while True:
        session_id = _Q.get()
        try:
            _summarize(session_id)
        except Exception as exc:                      # noqa: BLE001  兜底，别让线程死掉
            print('[session_summary] 任务异常：%s: %s' % (type(exc).__name__, exc), flush=True)
        finally:
            with _LOCK:
                _INFLIGHT.discard(session_id)
            _Q.task_done()


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print('状态：', status())
    sid = sys.argv[1] if len(sys.argv) > 1 else ''
    if sid:
        print('当前摘要：', get(sid))
        print('未摘要轮数：', qa_log.unsummarized_count(sid))
        print('立即生成一次…')
        _summarize(sid)
        print('生成后：', get(sid))
    else:
        print('用法： python -X utf8 session_summary.py <session_id>   # 立即给这场会话生成一次摘要')
