# -*- coding: utf-8 -*-
"""知识库 HTTP 接口层（阿里云百炼）。

对外暴露 6 个路由（**主服务 server.py 只负责把 /api/kb/** 转进来**）：

    GET    /api/kb/health                                    分层自检（凭证 / 空间权限）
    GET    /api/kb/indices                                   知识库列表
    GET    /api/kb/indices/{index_id}                        知识库详情
    GET    /api/kb/indices/{index_id}/documents              知识库里的文档
    POST   /api/kb/indices/{index_id}/documents              上传文档
    DELETE /api/kb/indices/{index_id}/documents/{file_id}    删除文档

四条设计底线（沿用参考实现 kb-lite）：
1. **本层不认识 SDK** —— 只调 `kb_bailian` 的封装方法；将来换云只动 `kb_bailian.py`。
2. **懒加载 + 不拖垮主服务** —— 知识库是附属能力：SDK 没装 / 凭据没配，
   只让 `/api/kb/**` 返 503，**主服务照常问答**。
3. **错误码表达真实语义** —— 权限 403 / 参数 400 / 未找到 404 / 限流 429 /
   云端故障 502 / 依赖缺失 503。一律返 500 会让排查方向直接错掉。
4. **不做自动重试** —— 提交类接口（ApplyFileUploadLease、SubmitIndexAddDocumentsJob）
   **不幂等**，重试会产生重复文档。
"""
import re
from urllib.parse import unquote

# ApplyFileUploadLease 规定单文件 1B ~ 100MB
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_PREFIX = '/api/kb/'

# 路由正则（index_id / file_id 用非贪婪的「非斜杠」匹配）
_RE_DETAIL = re.compile(r'^/api/kb/indices/([^/]+)$')
_RE_DOCS = re.compile(r'^/api/kb/indices/([^/]+)/documents$')
_RE_DOC = re.compile(r'^/api/kb/indices/([^/]+)/documents/([^/]+)$')


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


def _index(index_id):
    """路径里的 index_id；为空就退回 .env 的 DEFAULT_INDEX_ID。"""
    import kb_config
    target = (index_id or '').strip() or (kb_config.settings.default_index_id or '')
    if not target:
        raise ValueError('未指定知识库 ID，且 .env 里的 DEFAULT_INDEX_ID 为空。'
                         '请把 index_id 写在路径里，或在 .env 配置 DEFAULT_INDEX_ID。')
    return target


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


# ---------------------------------------------------------------- 业务

def _upload(index_id, query, headers, body):
    """上传：**multipart/form-data 与裸二进制都支持**。

    - multipart：Postman / curl -F / 表单 直接可用
    - 裸二进制：文件名放在 `X-Filename` 头，前端 `fetch(url,{body:file})` 最省事
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
        file_name = 'unnamed.txt'
    wait_parse = _bool(query.get('wait_parse'), True)
    return _kb().upload_document(
        _index(index_id),
        file_name=file_name,
        file_bytes=content,
        wait_parse=wait_parse,
        submit_index=_bool(query.get('submit_index'), True) and wait_parse,
        skip_if_exists=_bool(query.get('skip_if_exists'), True),
    )


def _route(method, path, query, headers, body):
    kb = _kb()

    if method == 'GET':
        if path == '/api/kb/health':
            return 200, kb.health_check()
        if path == '/api/kb/indices':
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
            return 200, kb.list_index_documents(
                _index(unquote(m.group(1))),
                status=(query.get('status') or '').strip() or None,
                name=(query.get('name') or '').strip() or None,
                name_like=_bool(query.get('name_like'), False),
                page_number=_int(query.get('page_number'), 1),
                page_size=_int(query.get('page_size'), 20))

    elif method == 'POST':
        m = _RE_DOCS.match(path)
        if m:
            return 200, _upload(unquote(m.group(1)), query, headers, body)

    elif method == 'DELETE':
        m = _RE_DOC.match(path)
        if m:
            index_id, file_id = unquote(m.group(1)), unquote(m.group(2))
            deleted = kb.delete_index_document(_index(index_id), file_id)
            return 200, {
                'deleted': deleted, 'requested': file_id,
                'note': '只删了知识库里的这份文档；「数据连接」里的源文件仍在，'
                        '要彻底清理请到百炼控制台处理。'}

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
