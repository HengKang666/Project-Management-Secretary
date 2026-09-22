# -*- coding: utf-8 -*-
"""演示服务：GET / 页面、/kb 知识库页、/skills 技能触发台、/health、/api/sessions、/api/history；
POST /api/ask、/api/feedback、/api/skill/run、/api/kb/**；DELETE /api/kb/**。标准库实现，零额外依赖。

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
from urllib.parse import parse_qs, unquote, urlparse

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

    def _send(self, code, body, ctype):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    @staticmethod
    def _health():
        """ok 只说明服务活着；name_fix / qa_log / kb 才说明三个「附属能力」动没动。

        这几个都可能静默失效：
        - name_fix：词典默认从 agent_data 的 t_nc_* 表读（SECRETARY_LEXICON=db），
                    表没建 / 表是空的 / 连不上 → 错字纠正静默不生效；
                    改成 file 模式时，则是 libs 下的 data/ 缺失
        - qa_log  ：记录库没建表 / 连不上 → 问答照答，但对话记录静默不落库
        - kb      ：知识库的 SDK 没装 / 凭据没配 → /api/kb/** 全返 503（**不连云**，
                    只看依赖与配置；真正的连通性用 GET /api/kb/health 检查）
        不报出来就没人发现得了，所以都在这里暴露，并带上各自的 _source。
        """
        out = {'ok': True, 'model': agent.config.MODEL}
        for mod, key in (('name_fix', 'name_fix'), ('qa_log', 'qa_log'), ('kb_api', 'kb')):
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

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'static', 'index.html'), encoding='utf-8') as f:
                self._send(200, f.read(), 'text/html')
        elif path in ('/asr', '/asr.html'):
            with open(os.path.join(HERE, 'static', 'asr.html'), encoding='utf-8') as f:
                self._send(200, f.read(), 'text/html')
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
            rows = gapsmod.list_gaps(int((self.path.split('limit=') + ['200'])[1]) if 'limit=' in self.path else 200)
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
        elif path.startswith('/api/kb/'):
            # 知识库（阿里云百炼）：列表 / 详情 / 文档 / 上传 / 删除，实现见 kb_api.py
            self._handle_kb('GET')
        elif path in ('/kb', '/kb.html'):
            # 知识库控制台页面（上传/删除文档，给同事用）
            with open(os.path.join(HERE, 'static', 'kb.html'), encoding='utf-8') as f:
                self._send(200, f.read(), 'text/html')
        else:
            self._send(404, 'not found', 'text/plain')

    def do_POST(self):
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
        client_ip = self.client_address[0] if self.client_address else None
        try:
            r = agent.ask(q, model=model, max_steps=max_steps, profile=profile,
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
        """历史对话列表。query: uid / limit / offset"""
        q = self._query()
        try:
            import qa_log
            rows = qa_log.list_sessions(user_code=q.get('uid') or q.get('user_code'),
                                        limit=q.get('limit') or 20, offset=q.get('offset') or 0)
            self._send_json({'total': len(rows), 'sessions': rows})
        except Exception as e:                  # noqa: BLE001
            self._send_json({'total': 0, 'sessions': [], 'error': '%s: %s' % (type(e).__name__, str(e)[:200])})

    def _handle_history(self):
        """某场会话的历史对话记录。query: session_id（必填）/ limit / with_steps=1"""
        q = self._query()
        sid = (q.get('session_id') or q.get('session') or '').strip()
        if not sid:
            self._send_json({'error': '缺少 session_id'}, 400)
            return
        try:
            import qa_log
            r = qa_log.get_history(sid, limit=q.get('limit') or 200,
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
        qa_id = data.get('qa_id')
        if qa_id in (None, ''):
            self._send_json({'error': '缺少 qa_id（取 /api/ask 返回里的 qa_id）'}, 400)
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
        n = int(self.headers.get('Content-Length') or 0)
        try:
            return json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:
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
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str),
                   'application/json')

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
