# -*- coding: utf-8 -*-
"""演示服务：GET / 页面、/kb 知识库页、/skills 技能触发台、/health、/api/sessions、/api/history；
POST /api/ask、/api/feedback、/api/skill/run、/api/skill/title、/api/kb/**；DELETE /api/kb/**。标准库实现，零额外依赖。

问答会自动落库到 agent_data（会话/消息/执行明细），历史接口从这里读。
落库由 qa_log.py 负责，**失败不影响问答**。

知识库（阿里云百炼）那组接口走 kb_api.py —— 那是**附属能力**，
SDK 没装 / 凭据没配都只影响它自己，不影响问答主链路。
"""
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import agent
import tools_asr

HERE = os.path.dirname(os.path.abspath(__file__))
ASR_DIR = os.path.join(os.path.dirname(HERE), 'output', 'asr')
PORT = int(os.environ.get('SECRETARY_PORT', '8200'))
# 默认只听本机；要局域网访问就设 SECRETARY_HOST=0.0.0.0
HOST = os.environ.get('SECRETARY_HOST', '127.0.0.1')


def _preheat():
    """后台预热纠错词典，并把结果打进启动日志。

    为什么需要：db 模式下词典要从数据库拉四万多行，冷启动约 7 秒
    （CSV 模式只要 0.3 秒）。放后台线程，不挡服务启动，
    等第一个请求进来时通常已经加载好了。

    顺便把「就绪 / 未就绪 + 原因」写进启动日志 —— 词典缺失时纠错是**静默失效**的，
    启动日志里有这么一行，比事后靠比对 name_fix_used 去排查省事得多。
    """
    def work():
        try:
            import name_fix
            t0 = time.time()
            ok = name_fix.available()
            st = name_fix.status()
            print('[启动] 纠错词典%s（数据源 %s，耗时 %.1fs）%s'
                  % ('就绪' if ok else '未就绪', st.get('source', '?'),
                     time.time() - t0, '' if ok else '：' + st.get('reason', '')),
                  flush=True)
        except Exception as e:                    # noqa: BLE001
            print('[启动] 纠错词典预热异常（不影响服务运行）：%s: %s'
                  % (type(e).__name__, e), flush=True)
    threading.Thread(target=work, daemon=True, name='namefix-preheat').start()

    def warm_semantic():
        """预热语义字典（表目录/字段/样例）与提示词缓存。

        为什么需要：`semantic._ensure()` 首建要跑一批字典查询 + 逐表取 3 行样例，
        实测约 4 秒。它虽然每进程只建一次，但**原先启动没预热它**，
        于是「第一个提问的人」要白等这 4 秒。放后台线程提前建好。
        """
        try:
            import semantic
            t0 = time.time()
            semantic.warm()
            print('[启动] 语义字典就绪（表目录/字段/提示词，耗时 %.1fs）'
                  % (time.time() - t0), flush=True)
        except Exception as e:                    # noqa: BLE001
            print('[启动] 语义字典预热异常（不影响服务运行，首个请求会补建）：%s: %s'
                  % (type(e).__name__, e), flush=True)
    threading.Thread(target=warm_semantic, daemon=True, name='semantic-preheat').start()

    def resume_kb_extract():
        """把上次没抽完的知识库文件重新排队。

        为什么需要：抽取是**异步**的（大 PDF 要几十秒到几分钟），
        进程重启会让队列里的任务丢掉。台账里 `pending / extracting` 的行就是"没抽完的"，
        启动时扫一遍重新投递 —— 否则那些文件会永远停在"抽取中"，前端一直转圈。
        """
        try:
            import kb_store
            n = kb_store.recover()
            st = kb_store.status()
            print('[启动] 文件台账%s（%s，已存 %s 个文件，本次恢复 %d 个待抽取）%s'
                  % ('就绪' if st.get('available') else '未就绪', st.get('source', '?'),
                     st.get('files', 0), n,
                     '' if st.get('available') else '：' + st.get('reason', '')),
                  flush=True)
        except Exception as e:                    # noqa: BLE001
            print('[启动] 文件台账不可用（不影响问答与上传，只是没有本地副本/预览）：%s: %s'
                  % (type(e).__name__, e), flush=True)
        # 异步上传任务：把上次中断的标成 failed（**不自动重试**：上传链路不幂等，重试会造重复文档）
        try:
            import kb_upload
            n = kb_upload.recover()
            st = kb_upload.status()
            print('[启动] 异步上传%s（%s，历史任务 %s，本次标记中断 %d 个）%s'
                  % ('就绪' if st.get('available') else '未就绪', st.get('source', '?'),
                     st.get('by_status', {}), n,
                     '' if st.get('available') else '：' + st.get('reason', '')),
                  flush=True)
        except Exception as e:                    # noqa: BLE001
            print('[启动] 异步上传不可用（同步上传不受影响）：%s: %s' % (type(e).__name__, e), flush=True)
        # L2 会话摘要：把「攒够轮数但还没摘要」的会话补跑一遍。
        # 摘要可以安全重跑（读同一批轮次 + 乐观锁写回），所以中断后自动补上没问题。
        try:
            import session_summary
            st = session_summary.status()
            n = session_summary.recover() if st.get('available') else 0
            print('[启动] 会话摘要%s（%s，每 %d 轮触发一次，本次补跑 %d 场）%s'
                  % ('就绪' if st.get('available') else '未启用', st.get('source', '?'),
                     st.get('every', 0), n,
                     '' if st.get('available') else '：' + st.get('reason', '')),
                  flush=True)
        except Exception as e:                    # noqa: BLE001
            print('[启动] 会话摘要不可用（不影响问答）：%s: %s' % (type(e).__name__, e), flush=True)
    threading.Thread(target=resume_kb_extract, daemon=True, name='kb-extract-resume').start()


def _lan_ips():
    """本机的局域网地址，打印出来直接发给同事。"""
    import socket
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))   # UDP 不发包，只为拿出口网卡的地址
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith('127.'))


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # 上一次 _read_json 的解析错误（None = 正常）。
    # 为什么要有它：_read_json 故意"宽容"（解析失败返 {}），这样调用方不用层层判空；
    # 但**业务接口需要知道"是没传 body 还是 body 是坏的"** —— 否则传了坏 JSON 会得到
    # 「问题为空」这种误导性的 200。需要严格判定的接口读它并返 400。
    _json_error = None

    def _send(self, code, body, ctype):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    @staticmethod
    def _health():
        """ok 只说明服务活着；下面这些字段才说明各个「附属能力」动没动。

        它们都可能静默失效，所以全部在这暴露，并带上各自的 _source：
        - name_fix ：词典默认从 agent_data 的 t_nc_* 表读（SECRETARY_LEXICON=db），
                     表没建 / 表是空的 / 连不上 → 错字纠正静默不生效
        - qa_log   ：记录库没建表 / 连不上 → 问答照答，但对话记录静默不落库
        - kb       ：知识库 SDK 没装 / 凭据没配 → /api/kb/** 全返 503（**不连云**，
                     只看依赖与配置；真正的连通性用 GET /api/kb/health 检查）
        - kb_files ：文件台账表没建 / 连不上 → 上传仍成功，但**没有本地副本、没有预览**
        - kb_upload：异步上传任务表没建 / 连不上 → `?async=1` 会失败（同步上传不受影响）
        - session_summary：L2 会话摘要。开关关掉（SECRETARY_SESSION_SUMMARY=0）时它算
                     **未启用**而不是故障 —— 所以这里只看 available，不看原因
        """
        out = {'ok': True, 'model': agent.config.MODEL}
        for mod, key in (('name_fix', 'name_fix'), ('qa_log', 'qa_log'),
                         ('kb_api', 'kb'), ('kb_store', 'kb_files'), ('kb_upload', 'kb_upload')):
            try:
                m = __import__(mod)
                st = m.status()
                out[key] = st['available']
                if st.get('source'):
                    out[key + '_source'] = st['source']
                if not st['available']:
                    out[key + '_reason'] = st['reason']
            except Exception as e:              # noqa: BLE001
                out[key] = False
                out[key + '_reason'] = '%s: %s' % (type(e).__name__, e)
        try:
            import session_summary
            st = session_summary.status()
            out['session_summary'] = bool(st.get('available'))
            if not st.get('available'):
                out['session_summary_reason'] = st.get('reason', '')
            elif st.get('source'):
                out['session_summary_source'] = st['source']
        except Exception as e:                  # noqa: BLE001
            out['session_summary'] = False
            out['session_summary_reason'] = '%s: %s' % (type(e).__name__, e)
        return out

    def _query(self):
        """解析 query string，取每个参数的第一个值（顺手把 limit 转成 int）。"""
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        for k in ('limit', 'offset'):
            if k in q:
                try:
                    q[k] = int(q[k])
                except (TypeError, ValueError):
                    q.pop(k)
        return q

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str), 'application/json')

    # ---------------------------------------------------------------- 兜底
    def do_GET(self):
        try:
            self._do_get()
        except Exception as e:                  # noqa: BLE001
            self._fail(e)

    def do_POST(self):
        try:
            self._do_post()
        except Exception as e:                  # noqa: BLE001
            self._fail(e)

    def _fail(self, e):
        """兜底：任何**没被各 handler 预料到**的异常，都回一个 500 JSON 并打日志。

        为什么需要：`BaseHTTPRequestHandler` 不会替我们兜异常 —— 一旦漏出来，
        客户端只会看到"连接被重置 / 空响应"，服务端日志里也就一行 traceback，
        排查时基本没有线索。宁可回一个明确的 500，也不要静默掐连接。
        """
        msg = '%s: %s' % (type(e).__name__, str(e)[:300])
        print('[server] 未预料的异常：' + msg, flush=True)
        try:
            self._send_json({'error': '服务内部错误：' + msg}, 500)
        except Exception:                       # noqa: BLE001
            pass                                # 响应头可能已经发出去，这时只能放弃

    def _send_static(self, relpath, ctype):
        """发一个**静态页面**（相对 HERE）；文件缺失返 404（而不是让 open() 抛出去变成 500）。

        ★ 名字不能叫 `_send_file` —— 那个已被"流式下发知识库原文件"占用
        （它带 Content-Disposition: attachment），重名会让页面变成"被下载"。
        """
        fp = os.path.join(HERE, relpath)
        try:
            with open(fp, encoding='utf-8') as f:
                self._send(200, f.read(), ctype)
        except FileNotFoundError:
            self._send(404, 'file not found: ' + relpath, 'text/plain')

    @staticmethod
    def _int_arg(query, key, default):
        """从 query string 里安全取一个整数参数（非法值退回默认，不抛异常）。

        直接用 `int(...)` 会让 `/api/gaps?limit=abc` 这种请求把服务打成 500 ——
        那是调用方传错，不该算服务端故障。复用已有的 parse_qs，不必再引 re。
        """
        vals = parse_qs(query or '').get(key) or []
        if not vals:
            return default
        try:
            return int(vals[0])
        except (TypeError, ValueError):
            return default

    def _do_get(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            self._send_static('static/index.html', 'text/html')
        elif path in ('/asr', '/asr.html'):
            self._send_static('static/asr.html', 'text/html')
        elif path == '/health':
            self._send(200, json.dumps(self._health()), 'application/json')
        elif path == '/api/asr/sample':
            sample = os.path.join(os.path.dirname(HERE), 'output', 'sample_asr.mp3')
            if os.path.exists(sample):
                with open(sample, 'rb') as f:
                    self._send(200, f.read(), 'audio/mpeg')
            else:
                self._send(404, 'sample not found', 'text/plain')
        elif path == '/api/gaps':
            import gaps as gapsmod
            rows = gapsmod.list_gaps(self._int_arg(self.path, 'limit', 200))
            self._send(200, json.dumps({'total': len(rows), 'rows': rows}, ensure_ascii=False), 'application/json')
        elif path in ('/skills', '/skills.html'):
            # 技能触发台：单独一页，按技能文档 + 上游数据触发
            with open(os.path.join(HERE, 'static', 'skills.html'), encoding='utf-8') as f:
                self._send(200, f.read(), 'text/html')
        elif path == '/api/statechange':
            import report as reportmod
            self._send(200, json.dumps(reportmod.state_change_sim(), ensure_ascii=False, default=str), 'application/json')
        elif path == '/api/triggers':
            import report as reportmod
            self._send(200, json.dumps(reportmod.list_triggers(), ensure_ascii=False), 'application/json')
        elif path == '/api/skills':
            import report as reportmod
            self._send(200, json.dumps({'skills': reportmod.list_skill_docs()}, ensure_ascii=False), 'application/json')
        elif path == '/api/report/meta':
            import report as reportmod
            y = reportmod.latest_year()
            self._send(200, json.dumps({'base_year': y, 'plan_year': str(int(y) + 1),
                                        'stations': reportmod.list_stations(),
                                        'title': '年度缺陷治理计划分析'}, ensure_ascii=False), 'application/json')
        elif path == '/api/models':
            self._send(200, json.dumps({'default': agent.config.MODEL,
                                        'models': agent.config.MODELS}, ensure_ascii=False), 'application/json')
        elif path == '/api/sessions':
            # 历史对话列表：一行一场会话，按最后活动时间倒序
            self._handle_sessions()
        elif path == '/api/history':
            # 某场会话的历史对话记录（消息 + 每条回答的口径/质量信息）
            self._handle_history()
        elif path == '/api/stats':
            # 记录库概览：问答量 / 缺口数 / 未核实数 / 纠错命中数
            self._handle_stats()
        elif path == '/api/page':
            # 取异步生成的页面：token 未就绪返 202，就绪返 HTML，失败/无效返 404
            tok = (self._query().get('token') or '').strip()
            got = agent.page_get(tok) if tok else None
            if not got:
                self._send(404, 'page token not found', 'text/plain')
            elif not got.get('done'):
                self._send(202, 'page generating', 'text/plain')
            elif got.get('html'):
                self._send(200, got['html'], 'text/html')
            else:
                self._send(500, 'page failed: %s' % got.get('error', ''), 'text/plain')
        elif path.startswith('/api/kb/'):
            # 知识库（阿里云百炼）：列表 / 详情 / 文档 / 上传 / 删除，实现见 kb_api.py
            self._handle_kb('GET')
        elif path in ('/kb', '/kb.html'):
            # 知识库控制台页面（上传/删除文档，给同事用）
            self._send_static('static/kb.html', 'text/html')
        else:
            self._send(404, 'not found', 'text/plain')

    def _do_post(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/kb/'):
            # 上传文档：multipart/form-data 与裸二进制都支持（见 kb_api._upload）
            self._handle_kb('POST')
            return
        if path == '/api/asr':
            self._handle_asr()
            return
        if path == '/api/skill/run':
            # 触发一个技能：技能文档 + 上游数据 + 身份范围 -> 一份合并回答
            data = self._read_json()
            try:
                import report as reportmod
                r = reportmod.run_skill((data.get('skill_id') or '').strip(),
                                        model=data.get('model') or None,
                                        work_name=data.get('work_name') or '',
                                        frm=data.get('from') or '', to=data.get('to') or '',
                                        questions=data.get('questions') or None,
                                        role=data.get('role') or '', scope=data.get('scope') or '',
                                        use_doc=bool(data.get('use_doc', True)),
                                        inputs=data.get('inputs') or None)
            except Exception as e:
                r = {'answer': '服务异常：' + type(e).__name__ + ' ' + str(e)[:200], 'trace': [], 'tool_calls': 0}
            self._send_json(r)
            return
        if path == '/api/skill/title':
            # 定时触发专用：只回一个标题。无异常也回 status=ok —— **绝不空返回**。
            data = self._read_json()
            try:
                import report as reportmod
                r = reportmod.run_skill_title((data.get('skill_id') or '').strip(),
                                              model=data.get('model') or None,
                                              account=data.get('account') or data.get('uid') or '',
                                              when=data.get('when') or data.get('triggered_at') or '',
                                              scope=data.get('scope') or '',
                                              inputs=data.get('inputs') or None,
                                              today=(data.get('today') or data.get('business_date') or '').strip() or None)
            except Exception as e:
                r = {'skill_id': data.get('skill_id'), 'status': 'unknown', 'count': None,
                     'title': '服务异常：' + type(e).__name__ + ' ' + str(e)[:200],
                     'checked_at': time.strftime('%Y-%m-%d %H:%M:%S')}
            self._send_json(r)
            return
        if path == '/api/report/notice':
            self._handle_notice()
            return
        if path == '/api/report':
            self._handle_report()
            return
        if path == '/api/feedback':
            self._handle_feedback()
            return
        if path != '/api/ask':
            self._send(404, 'not found', 'text/plain')
            return
        data = self._read_json()
        if self._json_error:
            self._send_json({'error': '请求体不是合法 JSON：' + self._json_error}, 400)
            return
        q = (data.get('question') or '').strip()
        model = data.get('model') or None
        if not q:
            self._send_json({'answer': '问题为空', 'trace': []})
            return
        max_steps = data.get('max_steps') or None
        profile = (data.get('profile') or '').strip() or None
        # 记录用参数（都可选，不传也能用）：
        #   session_id 不传 → 服务端生成一个并回显，前端存下来即可做多轮与历史
        #   uid / user_code → 业务用户ID（如 PROV-FIN-001），落库时换成内部主键
        session_id = (data.get('session_id') or '').strip() or ('s_' + uuid.uuid4().hex)
        user_code = (data.get('uid') or data.get('user_code') or '').strip() or None
        user_id = data.get('user_id') or None
        user_name = (data.get('user_name') or '').strip() or None
        channel = (data.get('channel') or '').strip() or None
        # 多轮上下文轮数：不传用配置默认（SECRETARY_HISTORY_TURNS，默认 5）；传 0 = 本次不用上下文
        history_turns = data.get('history_turns')
        # want_page：显式要分析页面。不传时只有「分析」类问题才生成（查数题不生成，保持快）。
        want_page = bool(data.get('want_page'))
        # page_mode: 'sync'（默认，页面随答案一起回）/ 'async'（先把答案回来，页面后台生成，用 /api/page 取）
        page_async = (data.get('page_mode') or '').strip().lower() == 'async'
        # scope：数据范围由上游传（强制按它取数）。
        scope = (data.get('scope') or data.get('business_scope') or '').strip() or None
        # skill_id：调用方知道这是哪个技能的追问 → 服务直接加载该技能自己的口径文档当标准，不靠检索。
        skill_id = (data.get('skill_id') or '').strip()
        standard = None
        if skill_id:
            try:
                import report as _rep
                for _s in (_rep.list_triggers().get('skills') or []):
                    if _s.get('id') == skill_id:
                        standard = _s.get('doc_text') or None
                        _tu = _s.get('trigger_user') or {}
                        scope = scope or _s.get('scope') or _tu.get('scope') or None
                        profile = profile or ('你的身份：%s（账号 %s，角色 %s）。数据范围：%s。'
                                              % (_tu.get('name') or _s.get('role') or '',
                                                 _tu.get('account') or '', _tu.get('role') or '',
                                                 _s.get('scope') or _tu.get('dept') or ''))
                        break
            except Exception:
                standard = None
        # today：业务上的「今天」，**由上游传**（定时任务/业务系统知道业务日期）。
        # 不传才退回服务器日期；停留天数、同比、"截至今天"全按它算。
        today = (data.get('today') or data.get('business_date') or '').strip() or None
        client_ip = self.client_address[0] if self.client_address else None
        try:
            r = agent.ask(q, model=model, max_steps=max_steps, profile=profile, want_page=want_page,
                          today=today, scope=scope, page_async=page_async, standard=standard,
                          session_id=session_id, user_id=user_id, user_code=user_code,
                          client_ip=client_ip, channel=channel, history_turns=history_turns)
        except Exception as e:
            r = {'input_question': q, 'question': q, 'session_id': session_id,
                 'answer': '服务异常：' + type(e).__name__ + ' ' + str(e), 'trace': [],
                 'no_db_query': True, 'db_query_count': 0, 'elapsed_ms': 0, 'steps': 0}
        if user_name:
            r['user_name'] = user_name
        self._send_json(r)

    # ------------------------------------------------ 历史会话相关

    def _handle_sessions(self):
        """历史对话列表。query: uid / limit / offset

        **limit 不传（或传 0）= 全部返回** —— 只受 config.API_MAX_ROWS 防呆上限保护，
        真被截断时响应里带 truncated=true。limit>0 才是分页取前 N 条。
        响应里 total 是符合条件的**总数**，returned 是本次实际返回条数。
        """
        q = self._query()
        try:
            import qa_log
            r = qa_log.list_sessions(user_code=q.get('uid') or q.get('user_code'),
                                     limit=q.get('limit'), offset=q.get('offset') or 0)
            self._send_json(r)
        except Exception as e:                  # noqa: BLE001
            self._send_json({'total': 0, 'returned': 0, 'limit': 0, 'offset': 0,
                             'truncated': False, 'sessions': [],
                             'error': '%s: %s' % (type(e).__name__, str(e)[:200])})

    def _handle_history(self):
        """某场会话的历史对话记录。query: session_id（必填）/ limit / with_steps=1

        limit 不传（或传 0）= 全部返回（同样受防呆上限保护）。
        """
        q = self._query()
        sid = (q.get('session_id') or q.get('session') or '').strip()
        if not sid:
            self._send_json({'error': '缺少 session_id'}, 400)
            return
        try:
            import qa_log
            r = qa_log.get_history(sid, limit=q.get('limit'),
                                   with_steps=str(q.get('with_steps') or '') in ('1', 'true', 'yes'))
            if r is None:
                self._send_json({'error': 'session 不存在', 'session_id': sid}, 404)
                return
            self._send_json(r)
        except Exception as e:                  # noqa: BLE001
            self._send_json({'error': '%s: %s' % (type(e).__name__, str(e)[:200]),
                             'session_id': sid}, 500)

    def _handle_stats(self):
        try:
            import qa_log
            self._send_json(qa_log.stats())
        except Exception as e:                  # noqa: BLE001
            self._send_json({'error': '%s: %s' % (type(e).__name__, str(e)[:200])}, 500)

    def _handle_feedback(self):
        """给某次回答打评价。body: {qa_id, feedback(1赞/0踩), note?}"""
        data = self._read_json()
        if self._json_error:
            self._send_json({'error': '请求体不是合法 JSON：' + self._json_error}, 400)
            return
        qa_id = data.get('qa_id')
        if qa_id in (None, ''):
            self._send_json({'error': '缺少 qa_id（取 /api/ask 返回里的 qa_id）'}, 400)
            return
        # ★ 必须在这里挡住非法值：qa_log.set_feedback 内部会 int(qa_id)，
        #   传 "nope" 这种会抛 ValueError，被下面的兜底 except 当成 500 ——
        #   那是**调用方传错**，不是服务端故障，应当 400（返 500 会把排查方向带错）。
        try:
            qa_id = int(qa_id)
        except (TypeError, ValueError):
            self._send_json({'error': 'qa_id 必须是整数（取 /api/ask 返回里的 qa_id）'}, 400)
            return
        try:
            fb = int(data.get('feedback'))
        except (TypeError, ValueError):
            self._send_json({'error': 'feedback 必须是 1（赞）或 0（踩）'}, 400)
            return
        try:
            import qa_log
            n = qa_log.set_feedback(qa_id, fb, data.get('note'))
            self._send_json({'ok': n > 0, 'updated': n, 'qa_id': qa_id, 'feedback': fb})
        except Exception as e:                  # noqa: BLE001
            self._send_json({'error': '%s: %s' % (type(e).__name__, str(e)[:200])}, 500)

    def _read_json(self):
        """解析 JSON 请求体。**解析失败返 {}**（宽容，调用方不必层层判空），
        同时把错误记到 `self._json_error`，需要严格判定的接口据此返 400。

        注意区分两种情况：
          - body 为空            → 返 {}，`_json_error` 仍是 None（很多接口允许空 body）
          - body 不是合法 JSON   → 返 {}，`_json_error` 非空
        """
        self._json_error = None
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception as e:                  # noqa: BLE001
            self._json_error = '%s: %s' % (type(e).__name__, str(e)[:120])
            return {}

    def _handle_notice(self):
        """按供电所生成一份模拟的预算分配通知：上期用库里真实值，本期是模拟值，页面上可改。"""
        data = self._read_json()
        station = (data.get('station') or '').strip()
        if not station:
            self._send(200, json.dumps({'error': '缺少 station'}, ensure_ascii=False), 'application/json')
            return
        try:
            import report as reportmod
            r = reportmod.build_notice(station, base_year=data.get('base_year'),
                                       plan_year=data.get('plan_year'))
        except Exception as e:
            r = {'error': type(e).__name__ + ' ' + str(e)[:200]}
        self._send(200, json.dumps(r, ensure_ascii=False, default=str), 'application/json')

    def _handle_report(self):
        """年度缺陷治理计划分析：把通知数据交给模型，它按技能文档自己查数、自己分析。"""
        data = self._read_json()
        try:
            import report as reportmod
            r = reportmod.run_report(notice=data.get('notice') or None,
                                     station=(data.get('station') or '环潭供电所'),
                                     model=data.get('model') or None)
        except Exception as e:
            r = {'report': '', 'trace': [], 'notice': {},
                 'warnings': ['服务异常：' + type(e).__name__ + ' ' + str(e)], 'elapsed_ms': 0}
        self._send(200, json.dumps(r, ensure_ascii=False, default=str), 'application/json')

    def _handle_asr(self):
        """接收浏览器录制的音频（原始二进制），走完 5 个阶段，把每阶段的原始请求/响应都返回。"""
        n = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(n) if n else b''
        fname = (self.headers.get('X-Filename') or 'rec.webm').replace('\\', '_').replace('/', '_')
        os.makedirs(ASR_DIR, exist_ok=True)
        import time as _t
        save_to = os.path.join(ASR_DIR, _t.strftime('%Y%m%d_%H%M%S_') + fname)
        with open(save_to, 'wb') as f:
            f.write(raw)
        try:
            r = tools_asr.transcribe(save_to)
        except Exception as e:
            r = {'text': '', 'error': type(e).__name__ + ' ' + str(e), 'stages': []}
        r['saved_to'] = save_to
        r['bytes'] = len(raw)
        self._send(200, json.dumps(r, ensure_ascii=False, default=str), 'application/json')

    # ------------------------------------------------ 知识库（阿里云百炼）

    def do_DELETE(self):
        try:
            self._do_delete()
        except Exception as e:                  # noqa: BLE001
            self._fail(e)

    def _do_delete(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/kb/'):
            self._handle_kb('DELETE')
            return
        self._send(404, 'not found', 'text/plain')

    def _handle_kb(self, method):
        """把所有 /api/kb/** 交给 kb_api 处理。

        知识库是**附属能力**：SDK 没装 / 凭据没配 / 连不上云端，
        都只影响这一组接口，**绝不拖垮问答主链路**（所以这里再包一层 try）。
        """
        try:
            import kb_api
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n) if n else b''
            headers = {k.lower(): v for k, v in self.headers.items()}
            r = kb_api.dispatch(method, unquote(self.path.split('?')[0]), q, headers, body)
        except Exception as e:                  # noqa: BLE001
            self._send_json({'error': '%s: %s' % (type(e).__name__, e)}, 503)
            return
        if r is None:
            self._send_json({'error': 'not found'}, 404)
            return
        status, payload = r
        # 下载原文件：payload 里带 __file__ 时走流式下发，不是 JSON
        if isinstance(payload, dict) and payload.get('__file__'):
            self._send_file(status, payload['__file__'], payload.get('name'), payload.get('ctype'))
            return
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str),
                   'application/json')

    def _send_file(self, code, path, name=None, ctype=None):
        """流式下发磁盘上的原文件（GET /api/kb/files/{file_id}/download）。

        分块读 + 预声明 Content-Length：大文件不占内存，前端也能显示下载进度。
        """
        try:
            size = os.path.getsize(path)
        except OSError as e:
            self._send_json({'error': '原文件读不到：%s' % e}, 404)
            return
        self.send_response(code)
        self.send_header('Content-Type', ctype or 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        # 中文文件名必须用 filename*（RFC 5987）编码，否则浏览器拿到的是乱码
        self.send_header('Content-Disposition',
                         "attachment; filename*=UTF-8''" + quote(name or os.path.basename(path)))
        self.end_headers()
        try:
            with open(path, 'rb') as f:
                while True:
                    buf = f.read(64 * 1024)
                    if not buf:
                        break
                    self.wfile.write(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass                                          # 客户端中途断开，不算错误

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print('model = %s' % agent.config.MODEL)
    print('本机访问：http://127.0.0.1:%d' % PORT)
    if HOST == '0.0.0.0':
        for ip in _lan_ips():
            print('同事访问：http://%s:%d' % (ip, PORT))
    else:
        print('（当前只监听 %s，同事访问不了；要开局域网：set SECRETARY_HOST=0.0.0.0）' % HOST)
    _preheat()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
