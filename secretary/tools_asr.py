# -*- coding: utf-8 -*-
"""阿里云百炼 语音识别（Fun-ASR 非实时）—— 分阶段调用，每一阶段的原始请求/响应都留档。

阶段：
  ① 取上传凭证   GET  /api/v1/uploads?action=getPolicy&model=fun-asr
  ② 上传到 OSS   POST {upload_host}   （multipart/form-data）
  ③ 提交转写任务 POST /api/v1/services/audio/asr/transcription
  ④ 轮询任务     GET  /api/v1/tasks/{task_id}
  ⑤ 取转写文本   GET  {transcription_url}
"""
import json
import mimetypes
import os
import ssl
import time
import urllib.error
import urllib.request
import uuid

import config

DASHSCOPE = 'https://dashscope.aliyuncs.com'
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _req(method, url, body=None, headers=None, timeout=90):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=_ctx)
        raw, code = r.read().decode('utf-8', 'replace'), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode('utf-8', 'replace'), e.code
    except Exception as e:
        raw, code = json.dumps({'error': type(e).__name__ + ': ' + str(e)[:200]}), 0
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw[:2000]
    return {'status': code, 'ms': int((time.time() - t0) * 1000), 'response': parsed}


def upload(path, model='fun-asr'):
    """阶段①②：把本地音频上传到 DashScope 临时存储（48 小时），返回 oss:// URL。"""
    stages = []
    h = {'Authorization': 'Bearer ' + config.LLM_KEY}
    st = _req('GET', DASHSCOPE + '/api/v1/uploads?action=getPolicy&model=' + model, headers=h)
    st.update({'name': '① 取上传凭证', 'method': 'GET',
               'url': DASHSCOPE + '/api/v1/uploads?action=getPolicy&model=' + model})
    stages.append(st)
    data = (st.get('response') or {}).get('data') or {}
    if not data.get('upload_host'):
        return None, stages

    filename = os.path.basename(path)
    key = data['upload_dir'].rstrip('/') + '/' + filename
    boundary = '----DSH' + uuid.uuid4().hex
    fields = [
        ('key', key), ('policy', data['policy']), ('OSSAccessKeyId', data['oss_access_key_id']),
        ('signature', data['signature']), ('success_action_status', '200'),
        ('x-oss-object-acl', data.get('x_oss_object_acl', 'default')),
        ('x-oss-forbid-overwrite', data.get('x_oss_forbid_overwrite', 'true')),
    ]
    body = b''
    for k, v in fields:
        body += ('--' + boundary + '\r\nContent-Disposition: form-data; name="' + k + '"\r\n\r\n' + str(v) + '\r\n').encode('utf-8')
    ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    body += ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + filename + '"\r\n'
             'Content-Type: ' + ctype + '\r\n\r\n').encode('utf-8')
    with open(path, 'rb') as f:
        body += f.read()
    body += ('\r\n--' + boundary + '--\r\n').encode('utf-8')

    st2 = _req('POST', data['upload_host'], body=body,
               headers={'Content-Type': 'multipart/form-data; boundary=' + boundary}, timeout=180)
    st2.update({'name': '② 上传到 OSS 临时存储', 'method': 'POST', 'url': data['upload_host'],
                'request': {'key': key, 'file': filename, 'bytes': len(body)}})
    stages.append(st2)
    if st2['status'] not in (200, 201, 204):
        return None, stages
    return 'oss://' + key, stages


def submit(file_url, model='fun-asr', language=None, diarization=False):
    """阶段③：提交非实时转写任务。"""
    payload = {'model': model, 'input': {'file_urls': [file_url]}, 'parameters': {}}
    if language:
        payload['parameters']['language_hints'] = [language]
    if diarization:
        payload['parameters']['diarization_enabled'] = True
    st = _req('POST', DASHSCOPE + '/api/v1/services/audio/asr/transcription',
              body=json.dumps(payload).encode('utf-8'),
              headers={'Authorization': 'Bearer ' + config.LLM_KEY,
                       'Content-Type': 'application/json',
                       'X-DashScope-OssResourceResolve': 'enable',
                       'X-DashScope-Async': 'enable'})
    st.update({'name': '③ 提交转写任务', 'method': 'POST',
               'url': DASHSCOPE + '/api/v1/services/audio/asr/transcription', 'request': payload})
    task_id = ((st.get('response') or {}).get('output') or {}).get('task_id')
    return task_id, st


def poll(task_id, interval=2, max_wait=180):
    """阶段④：轮询任务直到终态。"""
    stages = []
    waited = 0
    while waited <= max_wait:
        st = _req('GET', DASHSCOPE + '/api/v1/tasks/' + task_id,
                  headers={'Authorization': 'Bearer ' + config.LLM_KEY})
        out = (st.get('response') or {}).get('output') or {}
        status = out.get('task_status')
        st.update({'name': '④ 轮询任务状态（%s）' % (status or '?'), 'method': 'GET',
                   'url': DASHSCOPE + '/api/v1/tasks/' + task_id})
        stages.append(st)
        if status in ('SUCCEEDED', 'FAILED', 'CANCELED'):
            return out, stages
        time.sleep(interval)
        waited += interval
    return {'task_status': 'TIMEOUT'}, stages


def fetch_transcript(transcription_url):
    """阶段⑤：下载转写结果 JSON。"""
    st = _req('GET', transcription_url)
    st.update({'name': '⑤ 下载转写结果', 'method': 'GET', 'url': transcription_url})
    return st


def transcribe(path_or_url, model='fun-asr', language='zh', diarization=False):
    """完整链路：本地文件先上传，再提交、轮询、取文本。返回文本 + 全部阶段留档。"""
    stages = []
    url = path_or_url
    if not str(path_or_url).startswith(('http://', 'https://', 'oss://')):
        url, up_stages = upload(path_or_url, model=model)
        stages += up_stages
        if not url:
            return {'text': '', 'error': '上传失败', 'stages': stages}
    task_id, st = submit(url, model=model, language=language, diarization=diarization)
    stages.append(st)
    if not task_id:
        return {'text': '', 'error': '提交任务失败', 'stages': stages}
    out, poll_stages = poll(task_id)
    stages += poll_stages
    if out.get('task_status') != 'SUCCEEDED':
        return {'text': '', 'error': '任务未成功：' + str(out.get('task_status')), 'stages': stages,
                'task_id': task_id}

    text = ''
    for r in (out.get('results') or []):
        turl = r.get('transcription_url')
        st5 = fetch_transcript(turl)
        stages.append(st5)
        body = st5.get('response') or {}
        for tr in (body.get('transcripts') or []):
            text += (tr.get('text') or '')
    return {'text': text, 'task_id': task_id, 'stages': stages, 'url': url}
