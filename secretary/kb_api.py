# -*- coding: utf-8 -*-
"""知识库 HTTP 接口层（阿里云百炼 + 本地文件台账）。

对外暴露的路由（**主服务 server.py 只负责把 /api/kb/** 转进来**）：

    GET    /api/kb/health                                    分层自检（凭证 / 空间权限）
    GET    /api/kb/indices                                   知识库列表
    GET    /api/kb/indices/{index_id}                        知识库详情
    GET    /api/kb/indices/{index_id}/documents              知识库里的文档（含本地抽取状态）
    POST   /api/kb/indices/{index_id}/documents              上传文档（同时落盘 + 记台账）
    DELETE /api/kb/indices/{index_id}/documents/{file_id}    删除文档
    GET    /api/kb/files/{file_id}                           本地文件详情（抽取状态 / 页数）
    GET    /api/kb/files/{file_id}/text?page=N               按页取 HTML 富文本（前端直接渲染）
    GET    /api/kb/files/{file_id}/download                  下载原文件

五条设计底线：
1. **本层不认识 SDK** —— 只调 `kb_bailian` 的封装方法；将来换云只动 `kb_bailian.py`。
2. **懒加载 + 不拖垮主服务** —— 知识库是附属能力：SDK 没装 / 凭据没配，
   只让 `/api/kb/**` 返 503，**主服务照常问答**。
   ★ 但 `/api/kb/files/**` 这组**只依赖本地台账**，所以刻意不先取云端客户端 —— 云端坏了预览照样能用。
3. **错误码表达真实语义** —— 权限 403 / 参数 400 / 未找到 404 / 限流 429 /
   云端故障 502 / 依赖缺失 503。一律返 500 会让排查方向直接错掉。
4. **不做自动重试** —— 提交类接口（ApplyFileUploadLease、SubmitIndexAddDocumentsJob）
   **不幂等**，重试会产生重复文档。
5. **原文件先落盘再传云端**，云端失败就清掉本地副本（见 `_upload` 里的顺序说明）。
6. **单库模式**（`config.KB_SINGLE_INDEX`，默认开）：服务端只暴露一个知识库 ——
   列表接口只返回它，路径里传别的 `index_id` 一律忽略。**隔离必须在服务端做**。
"""
import os
import re
from urllib.parse import unquote

import config                                     # 与主服务共用一份 .env 与运行开关

# ApplyFileUploadLease 规定单文件 1B ~ 100MB
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_PREFIX = '/api/kb/'

# 路由正则（index_id / file_id 用非贪婪的「非斜杠」匹配）
_RE_DETAIL = re.compile(r'^/api/kb/indices/([^/]+)$')
_RE_DOCS = re.compile(r'^/api/kb/indices/([^/]+)/documents$')
_RE_DOC = re.compile(r'^/api/kb/indices/([^/]+)/documents/([^/]+)$')
_RE_UPLOAD_TASK = re.compile(r'^/api/kb/uploads/([^/]+)$')
# 本地文件预览（只依赖台账 + 磁盘，不连云）
_RE_FILE = re.compile(r'^/api/kb/files/([^/]+)$')
_RE_FILE_TEXT = re.compile(r'^/api/kb/files/([^/]+)/text$')
_RE_FILE_DL = re.compile(r'^/api/kb/files/([^/]+)/download$')

# 预览接口的固定说明，省得前端猜
_PREVIEW_NOTE = ('HTML 已做白名单清洗，可直接 innerHTML 渲染；'
                 '页与页之间请按 page_no 顺序拼接展示。')


class KBUnavailable(RuntimeError):
    """知识库依赖不可用（SDK 未安装 / 配置缺失）。→ HTTP 503。"""


_KB = {'obj': None, 'err': None}


def _kb():
    """懒加载云端封装层（进程内只建一次）。

    配置缺项也在这里拦下，给一句能照做的提示，而不是让 SDK 抛一堆英文堆栈。
    """
    if _KB['obj'] is not None:
        return _KB['obj']
    try:
        import kb_config
        import kb_bailian
        need = kb_config.settings.missing()
        if need:
            raise KBUnavailable(
                '知识库未配置：缺少 ' + '、'.join(need)
                + '。请在项目根目录 .env 里补齐（键名见 .env.example），然后重启服务。')
        _KB['obj'] = kb_bailian.BailianKnowledgeBase(kb_config.settings)
    except KBUnavailable:
        raise
    except Exception as exc:                      # noqa: BLE001  SDK 没装等
        _KB['err'] = '%s: %s' % (type(exc).__name__, exc)
        raise KBUnavailable('知识库依赖不可用（' + _KB['err']
                            + '）。需要 pip install alibabacloud_bailian20231229 requests')
    return _KB['obj']


def _translate(exc):
    """异常 → (HTTP 状态码, 可读响应体)。"""
    if isinstance(exc, KBUnavailable):
        return 503, {'error': str(exc),
                     'note': '知识库是附属能力，智能问数主服务不受影响。'}
    try:
        from kb_bailian import BailianError
    except Exception:                             # noqa: BLE001
        BailianError = None
    if BailianError is not None and isinstance(exc, BailianError):
        low = ('%s %s' % (exc.code, exc.message)).lower()
        if any(k in low for k in ('not authorized', 'access denied', 'unauthorized',
                                  'forbidden', 'invalidaccesskey', 'invalidapi', 'signature')):
            return 403, exc.to_dict()
        if any(k in low for k in ('invalidparameter', 'invalid parameter', 'missing or invalid')):
            return 400, exc.to_dict()
        if 'notfound' in low or 'not found' in low:
            return 404, exc.to_dict()
        if 'throttl' in low:
            return 429, exc.to_dict()
        if 400 <= (getattr(exc, 'http_status', 0) or 0) < 500:
            return 400, exc.to_dict()
        return 502, exc.to_dict()
    if isinstance(exc, FileNotFoundError):
        return 404, {'error': str(exc)}
    if isinstance(exc, TimeoutError):
        return 504, {'error': str(exc)}
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return 400, {'error': '%s: %s' % (type(exc).__name__, exc)}
    return 500, {'error': '%s: %s' % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- 小工具

def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v, default):
    if v is None or v == '':
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'y', 'on')


_FORCED = set()


def _index(index_id):
    """确定这次要操作哪个知识库。

    ★ **单库模式**（`config.KB_SINGLE_INDEX`，默认开）：**忽略传进来的 index_id**，
      一律用 `DEFAULT_INDEX_ID`；传了别的库会打一行日志留痕（便于发现有人在试）。

    为什么在服务端忽略、而不是"前端不传"：
      前端不显示只是"看不见"，只要接口还接受 index_id，换个 ID 就能操作别的库 ——
      那不叫隔离，叫障眼法。
    """
    import kb_config
    default = (kb_config.settings.default_index_id or '').strip()
    asked = (index_id or '').strip()
    if not config.KB_SINGLE_INDEX:
        target = asked or default
        if not target:
            raise ValueError('未指定知识库 ID，且 .env 里的 DEFAULT_INDEX_ID 为空。'
                             '请把 index_id 写在路径里，或在 .env 配置 DEFAULT_INDEX_ID。')
        return target
    if not default:
        raise ValueError('服务处于「单库模式」但没配 DEFAULT_INDEX_ID。'
                         '请在项目根 .env 里配好它，或设 SECRETARY_KB_SINGLE=0 关闭单库模式。')
    if asked and asked != default:
        key = asked
        if key not in _FORCED:                        # 同一个库只提醒一次，别刷日志
            _FORCED.add(key)
            print('[kb_api] 单库模式：请求指定的 %s 已被忽略，改用 %s' % (asked, default),
                  flush=True)
    return default


def _single_index(kb):
    """单库模式下只下发这一个库（连"还有哪些库"都不告诉前端）。"""
    target = _index('')
    base = {'total_count': 0, 'page_number': 1, 'page_size': 1,
            'locked': True, 'index_id': target,
            'note': '服务处于单库模式（SECRETARY_KB_SINGLE=1），只暴露这一个知识库。'}
    try:
        detail = dict(kb.get_index_detail(target, with_monitor=False))
    except Exception as exc:                          # noqa: BLE001  云端不可用时也给出可读结论
        base['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:200])
        base['indices'] = []
        return base
    detail.pop('monitor', None)
    base.update({'total_count': 1, 'indices': [detail]})
    return base


def parse_multipart(body, content_type):
    """极简 multipart/form-data 解析：**只取第一个带 filename 的字段**。

    为什么不用 cgi / email 模块：`cgi` 在 Python 3.13 已被移除，
    而 multipart 只支持单文件上传，自己切边界反而更稳、更可控。
    """
    m = re.search(r'boundary=([^;]+)', content_type or '')
    if not m:
        return None, None
    boundary = m.group(1).strip().strip('"')
    if not boundary:
        return None, None
    delim = b'--' + boundary.encode('utf-8')
    for part in body.split(delim):
        if b'filename=' not in part:
            continue
        head, _, content = part.partition(b'\r\n\r\n')
        if not content:
            continue
        if content.endswith(b'\r\n'):
            content = content[:-2]
        fn = ''
        fm = re.search(rb'filename="([^"]*)"', head)
        if fm:
            fn = fm.group(1).decode('utf-8', 'replace')
        if not fn:                                # 兼容 filename*=UTF-8''xxx
            fm = re.search(rb"filename\*=[^']*''([^\r\n;]*)", head)
            if fm:
                fn = unquote(fm.group(1).decode('utf-8', 'replace'))
        return (fn or '').strip(), content
    return None, None


_STORE = {'obj': None, 'err': None}


def _store():
    """懒加载本地台账层（**不依赖云端 SDK / 凭据**）。

    单独一条懒加载链的原因：即使阿里云凭据没配、SDK 没装，`/api/kb/files/**`
    和本地已存的预览内容也应该照常可用 —— 预览是我们自己的数据。
    """
    if _STORE['obj'] is not None:
        return _STORE['obj']
    try:
        import kb_store
    except Exception as exc:                          # noqa: BLE001
        _STORE['err'] = '%s: %s' % (type(exc).__name__, exc)
        raise KBUnavailable('本地文件台账不可用（%s）。需要 pip install pymysql，'
                            '并先建表：python tools/apply_kb_file_schema.py' % _STORE['err'])
    _STORE['obj'] = kb_store
    return kb_store


# ---------------------------------------------------------------- 本地预览（只依赖台账）

def _file_public(f):
    """台账行 → 对外字段（ISO 时间、URL 顺手拼好）。"""
    fid = f.get('file_id')
    return {
        'file_id': fid,
        'index_id': f.get('index_id'),
        'file_name': f.get('file_name'),
        'file_ext': f.get('file_ext'),
        'size_bytes': f.get('size_bytes'),
        'md5': f.get('md5'),
        'origin': f.get('origin'),
        'page_count': f.get('page_count') or 0,
        'text_chars': f.get('text_chars') or 0,
        'text_format': f.get('text_format') or 'html',
        'extract_status': f.get('extract_status'),
        'extract_error': f.get('extract_error'),
        'extract_ms': f.get('extract_ms'),
        'has_text': bool(f.get('extract_status') == 'done' and (f.get('page_count') or 0) > 0),
        'has_raw': bool(f.get('storage_path') and os.path.exists(f['storage_path'])),
        'uploader': f.get('uploader'),
        'upload_time': str(f.get('upload_time') or '') or None,
        'text_url': '/api/kb/files/%s/text' % fid,
        'download_url': '/api/kb/files/%s/download' % fid,
        'note': _PREVIEW_NOTE,
    }


def _file_detail(file_id):
    f = _store().get_file(file_id)
    if not f:
        raise FileNotFoundError(
            '本地台账里没有 file_id=%s。可能它是在百炼控制台直接上传的 —— '
            '那种文件本服务没留副本，所以没有预览与下载。' % file_id)
    return _file_public(f)


def _file_text(file_id, query):
    """按页取 HTML 富文本。page 不传 / 传 0 / 传 all → 返回全部页。"""
    store = _store()
    raw_page = str(query.get('page') or '').strip()
    page = 0 if raw_page.lower() in ('', '0', 'all') else _int(raw_page, 0)
    d = store.get_pages(file_id, page_no=(page if page > 0 else None))
    if not d:
        raise FileNotFoundError(
            '本地台账里没有 file_id=%s。可能它是在百炼控制台直接上传的，本服务没留副本、无法预览。'
            % file_id)
    f = d['file']
    out = {'file_id': file_id, 'file_name': f.get('file_name'),
           'text_format': f.get('text_format') or 'html',
           'extract_status': f.get('extract_status'),
           'page_count': f.get('page_count') or 0,
           'note': _PREVIEW_NOTE}
    st = f.get('extract_status')
    if st in ('pending', 'extracting'):
        out.update({'ready': False, 'pages': [], 'returned': 0,
                    'message': '正在提取文件内容，请稍后重试（可轮询本接口或 /api/kb/files/%s）' % file_id})
        return 200, out
    if st != 'done':
        out.update({'ready': False, 'pages': [], 'returned': 0,
                    'message': '这个文件没法提取出文字，因此没有预览。请下载原文件查看。',
                    'error': f.get('extract_error')})
        return 200, out
    rows = d['pages']
    if page > 0 and not rows:
        raise FileNotFoundError('第 %d 页不存在（该文件共 %d 页）' % (page, out['page_count']))
    out['ready'] = True
    acc, nbytes = [], 0
    for r in rows:
        html = r.get('content') or ''
        # 「全部页」模式下的体积保护：超过 2MB 就截断，让前端改用逐页取
        if page <= 0 and nbytes + len(html) > 2_000_000 and acc:
            out['truncated'] = True
            break
        nbytes += len(html)
        acc.append({'page_no': r['page_no'], 'page_label': r['page_label'],
                    'html': html, 'char_count': r['char_count']})
    if page > 0:
        out['page'] = page
    out['pages'] = acc
    out['returned'] = len(acc)
    return 200, out


# 后缀 → 下载时的 Content-Type
_CTYPE = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xlsm': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.txt': 'text/plain; charset=utf-8',
    '.md': 'text/markdown; charset=utf-8',
    '.csv': 'text/csv; charset=utf-8',
}


def _file_download(file_id):
    """下载原文件。返回一个特殊 payload，由 server.py 流式发出去。"""
    f = _store().get_file(file_id)
    if not f:
        raise FileNotFoundError('本地台账里没有 file_id=%s。' % file_id)
    path = f.get('storage_path')
    if not path or not os.path.exists(path):
        raise FileNotFoundError(
            '该文件本地没有原文，无法下载（%s）。'
            % ('它是在百炼控制台直接上传的，本服务没留副本'
               if f.get('origin') == 'console' else '原文件在磁盘上已丢失：%s' % path))
    return 200, {'__file__': path, 'name': f.get('file_name') or os.path.basename(path),
                 'ctype': _CTYPE.get((f.get('file_ext') or '').lower(), 'application/octet-stream')}


def _documents(index_id, query):
    """云端文档列表 + **本地台账字段合并**。

    两份数据必须合起来看：
      - 云端有、本地没有 ⇒ 控制台直接传的文件（origin=console，无预览）
      - 本地有、云端没有 ⇒ 云端已删但本地还留着副本
    """
    res = _kb().list_index_documents(
        index_id,
        status=(query.get('status') or '').strip() or None,
        name=(query.get('name') or '').strip() or None,
        name_like=_bool(query.get('name_like'), False),
        page_number=_int(query.get('page_number'), 1),
        page_size=_int(query.get('page_size'), 20))
    docs = res.get('documents') or []
    ledger = {}
    try:
        ledger = _store().map_by_ids(index_id, [d.get('file_id') for d in docs])
    except Exception as exc:                          # noqa: BLE001  台账坏了不影响列表
        print('[kb_api] 合并本地台账失败（列表照常返回）：%s: %s'
              % (type(exc).__name__, exc), flush=True)
    for d in docs:
        row = ledger.get(d.get('file_id'))
        if row:
            d['origin'] = row.get('origin')
            d['extract_status'] = row.get('extract_status')
            d['page_count'] = row.get('page_count') or 0
            d['text_chars'] = row.get('text_chars') or 0
            d['has_text'] = bool(row.get('extract_status') == 'done' and (row.get('page_count') or 0) > 0)
            d['has_raw'] = bool(row.get('storage_path') and os.path.exists(row['storage_path']))
            d['local_id'] = row.get('id')
        else:
            d['origin'] = 'console'
            d['extract_status'] = 'none'
            d['page_count'] = 0
            d['text_chars'] = 0
            d['has_text'] = False
            d['has_raw'] = False
            d['local_id'] = None
        d['detail_url'] = '/api/kb/files/%s' % d.get('file_id')
        d['text_url'] = '/api/kb/files/%s/text' % d.get('file_id')
    res['index_id'] = index_id
    res['locked'] = bool(config.KB_SINGLE_INDEX)
    return res


# ---------------------------------------------------------------- 业务

def _upload(index_id, query, headers, body):
    """上传：**multipart/form-data 与裸二进制都支持**。

    - multipart：Postman / curl -F / 表单 直接可用
    - 裸二进制：文件名放在 `X-Filename` 头，前端 `fetch(url,{body:file})` 最省事

    **两种模式**（`?async=` 控制，默认看 `config.KB_UPLOAD_ASYNC_DEFAULT`）：

    | 模式 | 行为 | 什么时候用 |
    |---|---|---|
    | 同步（默认）| 请求一直挂着，等云端完成（最长 300 秒）才返回 | 兼容老前端；单文件小、能接受等待 |
    | **异步** `?async=1` | 落盘后**立刻**返回 `task_id`，后台慢慢传 | 大文件；不想让用户干等 |

    ★ 同步模式的顺序：**先落本地盘 → 再传云端 → 成功后记台账并投递抽取**。
      云端上传最长要等 300 秒，字节只在我们手里过一次；
      但也不能留孤儿 —— 云端失败就把刚落的文件删掉（`discard`）。
    """
    ctype = (headers.get('content-type') or '')
    if 'multipart/form-data' in ctype.lower():
        file_name, content = parse_multipart(body, ctype)
        if content is None:
            raise ValueError('multipart 里没找到文件字段（字段名随意，带 filename 即可）')
    else:
        # 裸二进制：文件名放 X-Filename。HTTP 头不能安全承载中文（Python 按 latin-1 解），
        # 所以**约定把文件名 URL-encode 一下再放进头**，这里 unquote 回来。
        file_name = unquote((headers.get('x-filename') or '').strip())
        content = body
    if not content:
        raise ValueError('上传内容为空')
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError('文件 %d 字节，超过 100MB 上限' % len(content))
    if not file_name:
        # 百炼要求文件名带后缀；没给就按类型兜一个
        file_name = 'unnamed'
    if not os.path.splitext(file_name)[1]:
        # ★ 没后缀时按**内容**把后缀补对。否则云端会按 .txt 解析，
        #   docx 就会被抽成 zip 二进制乱码（实测踩到过）。
        try:
            import text_extract
            file_name += (text_extract.sniff_ext_bytes(content) or '.txt')
        except Exception:                             # noqa: BLE001
            file_name += '.txt'

    index_id = _index(index_id)
    uid = (query.get('uid') or query.get('user_code') or '').strip() or None
    wait_parse = _bool(query.get('wait_parse'), True)
    submit_index = _bool(query.get('submit_index'), True) and wait_parse
    skip_if_exists = _bool(query.get('skip_if_exists'), True)

    # ---- 异步分支：落盘后立刻返回 task_id，后台再传 ----
    if _bool(query.get('async'), config.KB_UPLOAD_ASYNC_DEFAULT):
        import kb_upload
        return kb_upload.submit(index_id, file_name, content, uid=uid,
                                wait_parse=wait_parse, submit_index=submit_index,
                                skip_if_exists=skip_if_exists)

    # ---- 同步分支（默认，与 4.0 行为完全一致）----
    # ① 先落盘：台账层不可用时**不阻塞上传**，只是没有本地副本/预览
    store, meta = None, None
    try:
        store = _store()
        meta = store.save_raw(index_id, file_name, content)
    except Exception as exc:                          # noqa: BLE001
        print('[kb_api] 本地落盘失败（不影响云端上传与检索）：%s: %s'
              % (type(exc).__name__, exc), flush=True)

    # ② 传云端
    try:
        up = _kb().upload_document(
            index_id,
            file_name=file_name,
            file_bytes=content,
            wait_parse=wait_parse,
            submit_index=submit_index,
            skip_if_exists=skip_if_exists,
        )
    except Exception:
        if store and meta:
            store.discard(meta['path'])               # 不留孤儿文件
        raise

    # ③ 记台账 + 投递异步抽取
    if store and meta and up.get('file_id'):
        try:
            if up.get('skipped'):
                # 同名已存在、云端这次没落新文档。我们手里的字节与已有那份**不保证一致**，
                # 所以不覆盖已有台账（否则 file_id 会指向错的内容），本地副本也丢掉。
                store.discard(meta['path'])
                up['local_note'] = ('知识库已有同名文档，本次未上传；'
                                    '为避免张冠李戴，本地也没保留副本')
            else:
                row = store.record(index_id, up['file_id'], file_name, meta, uploader=uid)
                up['local'] = {'extract_status': row.get('extract_status'),
                               'extract_status_url': '/api/kb/files/%s' % up['file_id'],
                               'text_url': '/api/kb/files/%s/text' % up['file_id'],
                               'note': '已落本地副本，正在后台抽取正文（抽完 /text 就能取 HTML）'}
        except Exception as exc:                      # noqa: BLE001
            print('[kb_api] 台账登记失败（云端上传成功，只是本地没有副本）：%s: %s'
                  % (type(exc).__name__, exc), flush=True)
    up['index_id'] = index_id
    up['locked'] = bool(config.KB_SINGLE_INDEX)
    up['async'] = False
    return up


def _upload_task(task_id):
    """查异步上传任务的进度。"""
    import kb_upload
    t = kb_upload.get(task_id)
    if not t:
        raise FileNotFoundError(
            '没有这个上传任务：%s。任务号由上传接口返回；'
            '服务重启会让中断的任务标记为 failed（仍在表里，能查到）。' % task_id)
    return t


def _route(method, path, query, headers, body):
    if method == 'GET':
        # ↓ 本地预览这组放在最前面，且**不先取云端客户端** ——
        #   凭据没配 / SDK 没装 / 云端挂了，本地已存的预览都要能用。
        m = _RE_FILE_TEXT.match(path)
        if m:
            return _file_text(unquote(m.group(1)), query)
        m = _RE_FILE_DL.match(path)
        if m:
            return _file_download(unquote(m.group(1)))
        m = _RE_FILE.match(path)
        if m:
            return 200, _file_detail(unquote(m.group(1)))
        m = _RE_UPLOAD_TASK.match(path)
        if m:
            return 200, _upload_task(unquote(m.group(1)))

        kb = _kb()
        if path == '/api/kb/health':
            return 200, kb.health_check()
        if path == '/api/kb/indices':
            if config.KB_SINGLE_INDEX:
                return 200, _single_index(kb)
            return 200, kb.list_indices(
                name=(query.get('name') or '').strip() or None,
                page_number=_int(query.get('page_number'), 1),
                page_size=_int(query.get('page_size'), 20))
        m = _RE_DETAIL.match(path)
        if m:
            return 200, kb.get_index_detail(
                _index(unquote(m.group(1))),
                with_monitor=_bool(query.get('with_monitor'), True))
        m = _RE_DOCS.match(path)
        if m:
            return 200, _documents(_index(unquote(m.group(1))), query)

    elif method == 'POST':
        m = _RE_DOCS.match(path)
        if m:
            return 200, _upload(unquote(m.group(1)), query, headers, body)

    elif method == 'DELETE':
        m = _RE_DOC.match(path)
        if m:
            index_id, file_id = unquote(m.group(1)), unquote(m.group(2))
            deleted = _kb().delete_index_document(_index(index_id), file_id)
            # 本地台账同步软删。**默认保留磁盘原文**（删错了还有救，预览也还能用）；
            # 要连原文一起清掉就传 drop_raw=1。
            local, drop = None, _bool(query.get('drop_raw'), False)
            try:
                local = _store().soft_delete(file_id, drop_raw=drop)
            except Exception as exc:                  # noqa: BLE001
                print('[kb_api] 本地台账软删失败（云端已删）：%s: %s'
                      % (type(exc).__name__, exc), flush=True)
            return 200, {
                'deleted': deleted, 'requested': file_id, 'local_removed': local,
                'local_raw_kept': not drop,
                'note': '只删了知识库里的这份文档；「数据连接」里的源文件仍在，'
                        '要彻底清理请到百炼控制台处理。'
                        + ('本地台账已软删但**保留了磁盘原文**（传 drop_raw=1 可一并删除）。'
                           if not drop else '本地台账与磁盘原文都已清掉。')}

    else:
        return 405, {'error': '不支持的方法 ' + method}

    return 404, {'error': '未知的知识库接口：%s %s' % (method, path)}


def dispatch(method, path, query, headers, body):
    """处理 /api/kb/** 请求。

    返回 `(status, payload)`；**不是知识库路由时返回 None**（交回主服务）。
    """
    if not path.startswith(_PREFIX):
        return None
    try:
        return _route(str(method or '').upper(), path, query or {}, headers or {}, body or b'')
    except Exception as exc:                      # noqa: BLE001  统一翻成 HTTP 语义
        return _translate(exc)


def status():
    """/health 里用的状态：知识库到底能不能用。**不连云**，只看依赖与配置。"""
    import kb_config
    out = {'available': False, 'source': 'aliyun-bailian',
           'endpoint': kb_config.settings.endpoint}
    need = kb_config.settings.missing()
    if need:
        out['reason'] = '缺少配置：' + '、'.join(need) + '（填项目根 .env）'
        return out
    try:
        import kb_bailian                                 # noqa: F401
    except Exception as exc:                              # noqa: BLE001
        out['reason'] = '依赖未安装：%s（需要 alibabacloud_bailian20231229）' % exc
        return out
    out['available'] = True
    out['workspace_id'] = kb_config.settings.workspace_id
    out['default_index_id'] = kb_config.settings.default_index_id or None
    return out
