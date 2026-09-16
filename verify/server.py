# -*- coding: utf-8 -*-
"""本地最小前端 + 接口。只用标准库，不装额外依赖。"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8200


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'static', 'index.html'), encoding='utf-8') as f:
                self._send(200, f.read(), 'text/html')
        else:
            self._send(404, 'not found', 'text/plain')

    def do_POST(self):
        if self.path != '/api/ask':
            self._send(404, 'not found', 'text/plain')
            return
        n = int(self.headers.get('Content-Length') or 0)
        data = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        q = (data.get('question') or '').strip()
        try:
            r = agent.ask(q)
        except Exception as e:
            r = {'answer': '服务异常：' + type(e).__name__ + ' ' + str(e), 'trace': [], 'steps': 0}
        self._send(200, json.dumps(r, ensure_ascii=False, default=str), 'application/json')

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    print('open http://127.0.0.1:' + str(PORT))
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
