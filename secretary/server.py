# -*- coding: utf-8 -*-
"""本地演示服务：GET / 页面，POST /api/ask，GET /health。标准库实现，零额外依赖。"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
import tools_asr

HERE = os.path.dirname(os.path.abspath(__file__))
ASR_DIR = os.path.join(os.path.dirname(HERE), 'output', 'asr')
PORT = int(os.environ.get('SECRETARY_PORT', '8200'))
# 默认只听本机；要局域网访问就设 SECRETARY_HOST=0.0.0.0
HOST = os.environ.get('SECRETARY_HOST', '127.0.0.1')


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
        """ok 只说明服务活着；name_fix 才说明「纠错/归一动没动」。

        纠错词典不在仓库里（含两万条真实台区名录），clone 下来默认是缺的 ——
        服务照跑、问答照答，但纠错与归一静默失效。不报出来就没人发现得了。
        """
        out = {'ok': True, 'model': agent.config.MODEL}
        try:
            import name_fix
            st = name_fix.status()
            out['name_fix'] = st['available']
            if not st['available']:
                out['name_fix_reason'] = st['reason']
        except Exception as e:                  # noqa: BLE001
            out['name_fix'] = False
            out['name_fix_reason'] = '%s: %s' % (type(e).__name__, e)
        return out

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
        elif path == '/api/report/meta':
            import report as reportmod
            y = reportmod.latest_year()
            self._send(200, json.dumps({'base_year': y, 'plan_year': str(int(y) + 1),
                                        'stations': reportmod.list_stations(),
                                        'title': '年度缺陷治理计划分析'}, ensure_ascii=False), 'application/json')
        elif path == '/api/models':
            self._send(200, json.dumps({'default': agent.config.MODEL,
                                        'models': agent.config.MODELS}, ensure_ascii=False), 'application/json')
        else:
            self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/api/asr':
            self._handle_asr()
            return
        if path == '/api/report/notice':
            self._handle_notice()
            return
        if path == '/api/report':
            self._handle_report()
            return
        if path != '/api/ask':
            self._send(404, 'not found', 'text/plain')
            return
        n = int(self.headers.get('Content-Length') or 0)
        try:
            data = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:
            data = {}
        q = (data.get('question') or '').strip()
        model = data.get('model') or None
        if not q:
            self._send(200, json.dumps({'answer': '问题为空', 'trace': []}, ensure_ascii=False), 'application/json')
            return
        max_steps = data.get('max_steps') or None
        profile = (data.get('profile') or '').strip() or None
        try:
            r = agent.ask(q, model=model, max_steps=max_steps, profile=profile)
        except Exception as e:
            r = {'question': q, 'answer': '服务异常：' + type(e).__name__ + ' ' + str(e), 'trace': [],
                 'no_db_query': True, 'db_query_count': 0, 'elapsed_ms': 0, 'steps': 0}
        self._send(200, json.dumps(r, ensure_ascii=False, default=str), 'application/json')

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
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
