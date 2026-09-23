# -*- coding: utf-8 -*-
"""异步上传：把「落盘 → 传云端 → 记台账 → 投递抽取」整条链路搬到后台线程。

**为什么需要**：同步上传时 HTTP 请求要一直挂着等云端完成（等解析最长 300 秒），
而这期间前端连 `file_id` 都拿不到，等于**什么都做不了** —— 传大文件时用户只能干等。

改成异步后：

    POST /api/kb/indices/{index_id}/documents?async=1
      → 立刻返回 {"task_id": "up_xxx", "status": "pending", "status_url": "..."}
    GET  /api/kb/uploads/{task_id}
      → 轮询；done 时带 file_id，前端拿它去 /api/kb/files/** 看预览

★ **任务为什么要落表**（而不是放内存字典）：进程一重启内存任务就没了，
  前端会永远轮询一个不存在的任务。落表 + 启动时把中断的标成 `failed`，
  至少能让前端明确知道"这次失败了，请重传"。

★ **重启后为什么不自动重试**：上传链路里的 `ApplyFileUploadLease` 与
  `SubmitIndexAddDocumentsJob` **都不幂等**，重试会产生重复文档；
  而且我们无法判断上次究竟传到哪一步了。宁可让人重传一次。
"""
import os
import queue
import threading
import time
import uuid

import config
import kb_store

_LOCK = threading.Lock()
_Q = queue.Queue()
_WORKER = {'thread': None}

# 任务状态
PENDING, UPLOADING, DONE, FAILED = 'pending', 'uploading', 'done', 'failed'


def _update(task_id, **kw):
    if not kw:
        return
    cols = ', '.join('%s=%%s' % k for k in kw)
    c = kb_store._conn()
    try:
        cur = c.cursor()
        cur.execute('UPDATE t_kb_upload_task SET ' + cols + ' WHERE task_id=%s',
                    list(kw.values()) + [task_id])
    finally:
        c.close()


def _public(row):
    if not row:
        return None
    fid = row.get('file_id')
    out = {
        'task_id': row.get('task_id'),
        'index_id': row.get('index_id'),
        'file_name': row.get('file_name'),
        'size_bytes': row.get('size_bytes'),
        'md5': row.get('md5'),
        'uploader': row.get('uploader'),
        'status': row.get('status'),
        'file_id': fid,
        'error': row.get('error'),
        'elapsed_ms': row.get('elapsed_ms'),
        'create_time': str(row.get('create_time') or '') or None,
        'update_time': str(row.get('update_time') or '') or None,
    }
    if fid:
        out['text_url'] = '/api/kb/files/%s/text' % fid
        out['detail_url'] = '/api/kb/files/%s' % fid
    out['status_url'] = '/api/kb/uploads/%s' % row.get('task_id')
    return out


def get(task_id):
    c = kb_store._conn()
    try:
        cur = c.cursor()
        cur.execute('SELECT * FROM t_kb_upload_task WHERE task_id=%s', (task_id,))
        return _public(cur.fetchone())
    finally:
        c.close()


def enqueue(task_id):
    with _LOCK:
        if _WORKER['thread'] is None or not _WORKER['thread'].is_alive():
            t = threading.Thread(target=_worker, name='kb-upload', daemon=True)
            t.start()
            _WORKER['thread'] = t
    if _Q.qsize() >= 200:
        print('[kb_upload] 上传队列积压 %d 个任务' % _Q.qsize(), flush=True)
    _Q.put(task_id)


def submit(index_id, file_name, content, uid=None,
           wait_parse=True, submit_index=True, skip_if_exists=True):
    """落盘 → 建任务 → 入队。**不做云端调用**，所以毫秒级返回。"""
    meta = kb_store.save_raw(index_id, file_name, content)      # 先落盘：字节只在我们手里过一次
    task_id = 'up_' + uuid.uuid4().hex
    c = kb_store._conn()
    try:
        cur = c.cursor()
        cur.execute(
            'INSERT INTO t_kb_upload_task (task_id, index_id, file_name, size_bytes, md5, '
            '                              storage_path, uploader, status, '
            '                              wait_parse, submit_index, skip_if_exists) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (task_id, index_id, meta['file_name'], meta['size'], meta['md5'], meta['path'],
             uid, PENDING, 1 if wait_parse else 0, 1 if submit_index else 0,
             1 if skip_if_exists else 0))
    finally:
        c.close()
    enqueue(task_id)
    return {'task_id': task_id, 'status': PENDING, 'file_name': meta['file_name'],
            'size_bytes': meta['size'], 'md5': meta['md5'], 'index_id': index_id,
            'status_url': '/api/kb/uploads/%s' % task_id,
            'note': '已收到文件并落入本地，正在后台上传；请轮询 status_url。'
                    'done 后用它返回的 file_id 调 /api/kb/files/{file_id}/text 看预览。'}


def _client():
    """懒加载云端客户端（与 kb_api 同一套配置）。"""
    import kb_config
    import kb_bailian
    need = kb_config.settings.missing()
    if need:
        raise RuntimeError('知识库未配置：缺少 ' + '、'.join(need))
    return kb_bailian.BailianKnowledgeBase(kb_config.settings)


def _run(task_id):
    """真正干活：传云端 → 记台账 → 投递抽取。所有异常都落进 task.error。"""
    c = kb_store._conn()
    try:
        cur = c.cursor()
        cur.execute('SELECT * FROM t_kb_upload_task WHERE task_id=%s', (task_id,))
        row = cur.fetchone()
    finally:
        c.close()
    if not row or row.get('status') in (DONE, FAILED):
        return
    path = row.get('storage_path')
    if not path or not os.path.exists(path):
        _update(task_id, status=FAILED, error='本地原文不见了（%s），无法上传' % path)
        return
    _update(task_id, status=UPLOADING, error=None)
    t0 = time.time()
    try:
        with open(path, 'rb') as f:
            content = f.read()
        up = _client().upload_document(
            row['index_id'], file_name=row['file_name'], file_bytes=content,
            wait_parse=bool(row.get('wait_parse')),
            submit_index=bool(row.get('submit_index')) and bool(row.get('wait_parse')),
            skip_if_exists=bool(row.get('skip_if_exists')))
    except Exception as exc:                          # noqa: BLE001
        kb_store.discard(path)                        # 上传失败就清掉本地副本，别留孤儿
        _update(task_id, status=FAILED,
                error='%s: %s' % (type(exc).__name__, str(exc)[:300]),
                elapsed_ms=int((time.time() - t0) * 1000))
        return

    file_id = up.get('file_id')
    if not file_id:
        _update(task_id, status=FAILED, error='云端没有返回 file_id，上传结果不确定',
                elapsed_ms=int((time.time() - t0) * 1000))
        return

    note = None
    try:
        if up.get('skipped'):
            # 同名已存在、云端本次没落新文档。我们手里的字节与已有那份**不保证一致**，
            # 所以既不覆盖台账，也不留副本。
            kb_store.discard(path)
            note = '知识库已有同名文档，本次未上传；为避免张冠李戴，本地也没保留副本'
        else:
            kb_store.record(row['index_id'], file_id, row['file_name'],
                            {'path': path, 'md5': row.get('md5'), 'size': row.get('size_bytes')},
                            uploader=row.get('uploader'))
    except Exception as exc:                          # noqa: BLE001
        note = '云端上传成功，但本地台账登记失败（不影响知识库检索）：%s' % str(exc)[:200]
        print('[kb_upload] %s' % note, flush=True)
    _update(task_id, status=DONE, file_id=file_id, error=note,
            elapsed_ms=int((time.time() - t0) * 1000))


def _worker():
    while True:
        task_id = _Q.get()
        try:
            _run(task_id)
        except Exception as exc:                      # noqa: BLE001  兜底，别让线程死掉
            print('[kb_upload] 任务异常：%s: %s' % (type(exc).__name__, exc), flush=True)
            try:
                _update(task_id, status=FAILED, error='%s: %s' % (type(exc).__name__, str(exc)[:300]))
            except Exception:                          # noqa: BLE001
                pass
        finally:
            _Q.task_done()


def recover():
    """启动时把上次中断的任务标成 failed（**不自动重试**，原因见模块注释）。"""
    try:
        c = kb_store._conn()
        try:
            cur = c.cursor()
            cur.execute("UPDATE t_kb_upload_task SET status=%s, error=%s "
                        "WHERE status IN (%s,%s)",
                        (FAILED, '服务重启导致这次上传中断（结果不确定），请重新上传',
                         PENDING, UPLOADING))
            return cur.rowcount
        finally:
            c.close()
    except Exception as exc:                          # noqa: BLE001
        print('[kb_upload] 标记中断任务失败（不影响服务）：%s: %s'
              % (type(exc).__name__, exc), flush=True)
        return 0


def status():
    """/health 用。"""
    out = {'available': False, 'source': 'mysql:%s' % config.AGENT_DB,
           'queue': _Q.qsize(),
           'worker_alive': bool(_WORKER['thread'] and _WORKER['thread'].is_alive())}
    try:
        c = kb_store._conn()
        try:
            cur = c.cursor()
            cur.execute('SELECT status, COUNT(*) AS n FROM t_kb_upload_task GROUP BY status')
            out['by_status'] = {r['status']: int(r['n']) for r in cur.fetchall()}
        finally:
            c.close()
    except Exception as exc:                          # noqa: BLE001
        out['reason'] = ('上传任务表不可用（先跑 tools/apply_kb_file_schema.py 建表）：%s: %s'
                         % (type(exc).__name__, str(exc)[:120]))
        return out
    out['available'] = True
    return out


def recent(limit=20):
    """最近的任务（排障用）。"""
    c = kb_store._conn()
    try:
        cur = c.cursor()
        cur.execute('SELECT * FROM t_kb_upload_task ORDER BY id DESC LIMIT %s', (int(limit),))
        return [_public(r) for r in cur.fetchall()]
    finally:
        c.close()


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print('状态：', status())
    print('最近 10 个任务：')
    for t in recent(10):
        print('  %s %-9s %-28s %s' % (t['task_id'], t['status'], t['file_name'],
                                     t.get('error') or t.get('file_id') or ''))
