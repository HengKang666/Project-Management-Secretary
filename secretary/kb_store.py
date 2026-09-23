# -*- coding: utf-8 -*-
"""知识库文件台账 + 异步富文本抽取（local side）。

云端（百炼）只提供「文件元信息」，**没有原文、也不能按页取文本** —— 所以预览必须在本地留一份。

这里干三件事：

1. **原文件落盘** → `{KB_FILE_ROOT}/{index_id}/{md5}/{原文件名}`
   用 md5 当目录名：上传前就能算出、天然去重、失败时好清理，不依赖云端返回的 file_id。

2. **台账**（`t_kb_file`）—— 一行一个文件：file_id ↔ 磁盘路径 ↔ 抽取状态 ↔ 页数。

3. **异步抽富文本**（`t_kb_file_page`）—— daemon 线程 + 队列，把文件抽成**按页的 HTML**。
   为什么必须异步：一个 50MB 的 PDF 抽几十秒到几分钟，挂在 HTTP 请求里会让前端一直转圈。

与云端上传的配合（顺序很重要）：

    meta = kb_store.save_raw(index_id, name, content)     # ① 先落盘
    try:
        up = kb.upload_document(...)                       # ② 再传云端（最长 300s）
    except Exception:
        kb_store.discard(meta['path'])                     #    失败就清掉，别留孤儿文件
        raise
    kb_store.record(index_id, up['file_id'], name, meta, uploader=...)  # ③ 记台账 + 投递抽取

`t_kb_file` / `t_kb_file_page` 的建表见 `tools/kb_file_schema.sql`（幂等，可重复执行）。
"""
import hashlib
import os
import queue
import re
import threading
import time

import config

try:
    import pymysql
except ImportError:                                   # pragma: no cover
    pymysql = None

_LOCK = threading.Lock()
_Q = queue.Queue()
_WORKER = {'thread': None}
_SAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


# ---------------------------------------------------------------- 连接与状态

def _conn():
    if pymysql is None:
        raise RuntimeError('缺依赖：pymysql')
    return pymysql.connect(host=config.DB['host'], port=config.DB['port'],
                           user=config.DB['user'], password=config.DB['password'],
                           database=config.AGENT_DB, connect_timeout=15, charset='utf8mb4',
                           autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def status():
    """/health 用：台账这层能不能用。**不抽文件、不查云端**。"""
    out = {'available': False, 'source': 'mysql:%s' % config.AGENT_DB,
           'root': config.KB_FILE_ROOT}
    if pymysql is None:
        out['reason'] = '缺依赖：pymysql'
        return out
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute('SELECT COUNT(*) AS n FROM t_kb_file WHERE deleted_flag=0')
            out['files'] = int(cur.fetchone()['n'])
            cur.execute("SELECT COUNT(*) AS n FROM t_kb_file "
                        "WHERE deleted_flag=0 AND extract_status IN ('pending','extracting')")
            out['pending'] = int(cur.fetchone()['n'])
        finally:
            c.close()
    except Exception as exc:                          # noqa: BLE001
        out['reason'] = ('表不可用（先跑 tools/apply_kb_file_schema.py 建表）：%s: %s'
                         % (type(exc).__name__, str(exc)[:150]))
        return out
    out['available'] = True
    return out


# ---------------------------------------------------------------- 原文件落盘

def _safe_name(name):
    """去掉路径分隔符与控制字符，防止穿越目录。"""
    name = _SAFE.sub('_', os.path.basename(str(name or ''))).strip().strip('.')
    return name[:180] or 'unnamed'


def save_raw(index_id, file_name, content):
    """把上传的原始字节落到磁盘。返回 {'path','md5','size','file_name','existed'}。

    不做任何校验（大小/类型由 kb_api 那层管），只保证：目录建好、同内容不重复写。
    """
    md5 = hashlib.md5(content).hexdigest()
    name = _safe_name(file_name)
    folder = os.path.join(config.KB_FILE_ROOT, str(index_id or 'default'), md5)
    path = os.path.join(folder, name)
    existed = os.path.exists(path)
    if not existed:
        os.makedirs(folder, exist_ok=True)
        tmp = path + '.part'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, path)                          # 原子落盘，避免半截文件
    return {'path': path, 'md5': md5, 'size': len(content),
            'file_name': name, 'existed': existed}


def discard(path):
    """删除已落的原文件（云端上传失败时调用），并尽量清掉空的 md5 目录。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
        parent = os.path.dirname(path or '')
        if parent and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
    except OSError:
        pass


# ---------------------------------------------------------------- 台账

def record(index_id, file_id, file_name, meta, uploader=None, origin='api'):
    """登记台账 + 投递抽取任务。返回入库后的行。

    meta = save_raw() 的返回值；控制台直接传的文件没有 meta，用 origin='console' 且 storage_path=None。
    """
    ext = os.path.splitext(str(file_name or ''))[1].lower()
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute(
            'INSERT INTO t_kb_file (file_id, index_id, file_name, file_ext, size_bytes, md5, '
            '                       storage_path, origin, uploader, extract_status) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
            'ON DUPLICATE KEY UPDATE file_name=VALUES(file_name), size_bytes=VALUES(size_bytes), '
            '  md5=VALUES(md5), storage_path=VALUES(storage_path), origin=VALUES(origin), '
            '  uploader=VALUES(uploader), deleted_flag=0',
            (file_id, index_id, file_name, ext,
             (meta or {}).get('size'), (meta or {}).get('md5'),
             (meta or {}).get('path'), origin, uploader,
             'pending' if (meta or {}).get('path') else 'unsupported'))
        if not (meta or {}).get('path'):
            cur.execute("UPDATE t_kb_file SET extract_error=%s WHERE file_id=%s AND index_id=%s",
                        ('该文件是在百炼控制台直接上传的，本地没有原文，无法预览', file_id, index_id))
        cur.execute('SELECT * FROM t_kb_file WHERE index_id=%s AND file_id=%s', (index_id, file_id))
        row = cur.fetchone()
        if row and row.get('extract_status') == 'pending':
            enqueue(file_id)
        return row
    finally:
        c.close()


def get_file(file_id, include_deleted=False):
    """按 file_id 取台账行。

    ★ `include_deleted=True` 才能查到已软删的行 —— 删除类操作用的是它。
      踩过的坑：`hard_delete` / `soft_delete` 原先调的是默认（只查未删除）版本，
      对"已经软删过一次"的行就查不到，于是 `drop_raw=True` **静默失效**，
      磁盘原文留在那里没人清（实测漏了一个文件才发现）。
    """
    c = _conn()
    try:
        cur = c.cursor()
        if include_deleted:
            cur.execute('SELECT * FROM t_kb_file WHERE file_id=%s ORDER BY id DESC LIMIT 1',
                        (file_id,))
        else:
            cur.execute('SELECT * FROM t_kb_file WHERE file_id=%s AND deleted_flag=0 '
                        'ORDER BY id DESC LIMIT 1', (file_id,))
        return cur.fetchone()
    finally:
        c.close()


def map_by_ids(index_id, file_ids):
    """批量取台账行 → {file_id: row}（给文档列表合并本地字段用，避免 N 次查询）。"""
    ids = [i for i in (file_ids or []) if i]
    if not ids:
        return {}
    c = _conn()
    try:
        cur = c.cursor()
        marks = ','.join(['%s'] * len(ids))
        cur.execute('SELECT * FROM t_kb_file WHERE index_id=%s AND deleted_flag=0 '
                    'AND file_id IN (' + marks + ')', [index_id] + ids)
        return {r['file_id']: r for r in cur.fetchall()}
    finally:
        c.close()


def get_pages(file_id, page_no=None):
    """取某文件的按页富文本。page_no=None → 全部页（含元信息）。"""
    f = get_file(file_id)
    if not f:
        return None
    c = _conn()
    try:
        cur = c.cursor()
        if page_no:
            cur.execute('SELECT page_no, page_label, content, char_count FROM t_kb_file_page '
                        'WHERE file_id=%s AND page_no=%s', (file_id, int(page_no)))
            rows = cur.fetchall()
        else:
            cur.execute('SELECT page_no, page_label, content, char_count FROM t_kb_file_page '
                        'WHERE file_id=%s ORDER BY page_no', (file_id,))
            rows = cur.fetchall()
        return {'file': f, 'pages': rows}
    finally:
        c.close()


def soft_delete(file_id, drop_raw=False):
    """软删台账 + **把该文件的按页正文一并清掉**。

    为什么要连页一起删：`t_kb_file_page` 是按 file_id 挂的，台账软删后那些页就成了孤儿 ——
    不报错、但会一直占着 MEDIUMTEXT 空间，做数据核对时还会干扰统计。
    （2026-09-22 修：原先只软删台账，页留在库里。存量孤儿见 `purge_orphan_pages()`。）

    drop_raw=True 时连磁盘原文一起删。软删**可恢复**（deleted_flag=1），
    要彻底清掉用 `hard_delete()`。
    """
    f = get_file(file_id, include_deleted=True)       # 已软删过的也要能查到，否则 drop_raw 静默失效
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('DELETE FROM t_kb_file_page WHERE file_id=%s', (file_id,))
        cur.execute('UPDATE t_kb_file SET deleted_flag=1 WHERE file_id=%s', (file_id,))
        n = cur.rowcount
    finally:
        c.close()
    if f and drop_raw and f.get('storage_path'):
        discard(f['storage_path'])
    return n


def hard_delete(file_id, drop_raw=True):
    """物理删除一个文件（台账行 + 按页正文 + 可选磁盘原文）。维护/清测试数据用。"""
    f = get_file(file_id, include_deleted=True)
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('DELETE FROM t_kb_file_page WHERE file_id=%s', (file_id,))
        cur.execute('DELETE FROM t_kb_file WHERE file_id=%s', (file_id,))
        n = cur.rowcount
    finally:
        c.close()
    if f and drop_raw and f.get('storage_path'):
        discard(f['storage_path'])
    return n


# ---------------------------------------------------------------- 维护：清孤儿

def purge_orphan_pages():
    """删掉「没有台账（或台账已软删）却还留着正文」的孤儿页。返回删除行数。

    孤儿怎么来的：早期版本的 soft_delete 只软删台账、没删页；或者手工删过台账行。
    """
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('DELETE p FROM t_kb_file_page p '
                    'LEFT JOIN t_kb_file f ON f.file_id = p.file_id AND f.deleted_flag = 0 '
                    'WHERE f.id IS NULL')
        return cur.rowcount
    finally:
        c.close()


def orphan_raw_files():
    """列出磁盘上「台账里没有指向它」的原文件（**只报告，不删**）。"""
    root = config.KB_FILE_ROOT
    if not os.path.isdir(root):
        return []
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('SELECT storage_path FROM t_kb_file WHERE deleted_flag=0 '
                    'AND storage_path IS NOT NULL')
        known = {os.path.normcase(os.path.normpath(r['storage_path'])) for r in cur.fetchall()}
    finally:
        c.close()
    out = []
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            if f.endswith('.part'):
                continue
            p = os.path.normcase(os.path.normpath(os.path.join(dp, f)))
            if p not in known:
                out.append(os.path.join(dp, f))
    return out


def purge_orphan_raw(dry_run=True):
    """删掉磁盘上的孤儿原文件。**默认只报告不删**，要真删传 dry_run=False。"""
    orphans = orphan_raw_files()
    if dry_run:
        return {'dry_run': True, 'count': len(orphans), 'files': orphans[:50]}
    for p in orphans:
        discard(p)
    root = config.KB_FILE_ROOT                        # 顺手清空目录
    for dp, dn, _fn in os.walk(root, topdown=False):
        for d in dn:
            full = os.path.join(dp, d)
            try:
                if not os.listdir(full):
                    os.rmdir(full)
            except OSError:
                pass
    return {'dry_run': False, 'deleted': len(orphans)}


def purge_all(dry_run=False):
    """一次清完：孤儿页 + 孤儿原文件。"""
    pages = 0 if dry_run else purge_orphan_pages()
    return {'dry_run': dry_run, 'pages_deleted': pages, 'raw': purge_orphan_raw(dry_run)}


# ---------------------------------------------------------------- 异步抽取

def _update(file_id, **kw):
    if not kw:
        return
    cols = ', '.join('%s=%%s' % k for k in kw)
    c = _conn()
    try:
        cur = c.cursor()
        cur.execute('UPDATE t_kb_file SET ' + cols + ' WHERE file_id=%s', list(kw.values()) + [file_id])
    finally:
        c.close()


def _extract_one(file_id):
    """抽一个文件 → 写 t_kb_file_page → 更新状态。异常一律吞掉并记进 extract_error。"""
    row = get_file(file_id)
    if not row:
        return
    path = row.get('storage_path')
    if not path or not os.path.exists(path):
        _update(file_id, extract_status='unsupported',
                extract_error='原文件不在磁盘上（路径：%s），无法预览' % path)
        return
    _update(file_id, extract_status='extracting', extract_error=None)
    t0 = time.time()
    try:
        import text_extract
        pages = text_extract.extract(path, ext=row.get('file_ext'))
    except Exception as exc:                          # noqa: BLE001
        _update(file_id, extract_status='unsupported' if type(exc).__name__ == 'Unsupported'
                else 'failed',
                extract_error='%s: %s' % (type(exc).__name__, str(exc)[:300]),
                extract_ms=int((time.time() - t0) * 1000))
        return
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute('DELETE FROM t_kb_file_page WHERE file_id=%s', (file_id,))
            if pages:
                cur.executemany(
                    'INSERT INTO t_kb_file_page (file_id, page_no, page_label, content, char_count) '
                    'VALUES (%s,%s,%s,%s,%s)',
                    [(file_id, p['page_no'], p.get('page_label'), p.get('html'), p.get('chars', 0))
                     for p in pages])
        finally:
            c.close()
        _update(file_id, extract_status='done', page_count=len(pages),
                text_chars=sum(p.get('chars', 0) for p in pages),
                extract_error=None, extract_ms=int((time.time() - t0) * 1000))
    except Exception as exc:                          # noqa: BLE001
        _update(file_id, extract_status='failed',
                extract_error='写库失败：%s: %s' % (type(exc).__name__, str(exc)[:300]),
                extract_ms=int((time.time() - t0) * 1000))


def _worker():
    while True:
        file_id = _Q.get()
        try:
            _extract_one(file_id)
        except Exception as exc:                      # noqa: BLE001  兜底，绝不让线程死掉
            print('[kb_store] 抽取任务异常：%s: %s' % (type(exc).__name__, exc), flush=True)
        finally:
            _Q.task_done()


def enqueue(file_id):
    """投递抽取任务（进程内只启一个 daemon 线程）。

    队列**不设上限**（设了的话 put 会阻塞调用方的请求线程，得不偿失），
    但积压过多时打一行日志提醒 —— 否则只能靠内存涨上去才发现。
    """
    if not file_id:
        return
    with _LOCK:
        if _WORKER['thread'] is None or not _WORKER['thread'].is_alive():
            t = threading.Thread(target=_worker, name='kb-extract', daemon=True)
            t.start()
            _WORKER['thread'] = t
    if _Q.qsize() >= 500:
        print('[kb_store] 抽取队列积压 %d 个任务，处理速度跟不上投递速度'
              % _Q.qsize(), flush=True)
    _Q.put(file_id)


def recover(limit=200):
    """服务启动时把上次没抽完的（pending / extracting）重新排队 —— 进程重启用它接着干。"""
    try:
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute("SELECT file_id FROM t_kb_file WHERE deleted_flag=0 "
                        "AND extract_status IN ('pending','extracting') ORDER BY id LIMIT %s",
                        (int(limit),))
            ids = [r['file_id'] for r in cur.fetchall()]
        finally:
            c.close()
    except Exception as exc:                          # noqa: BLE001
        print('[kb_store] 恢复待抽取任务失败（不影响服务）：%s: %s'
              % (type(exc).__name__, exc), flush=True)
        return 0
    for i in ids:
        enqueue(i)
    return len(ids)


def queue_size():
    return _Q.qsize()


def stats():
    """台账概览（供运维看）。"""
    c = _conn()
    try:
        cur = c.cursor()
        out = {'queue': _Q.qsize(), 'worker_alive': bool(
            _WORKER['thread'] and _WORKER['thread'].is_alive())}
        cur.execute('SELECT extract_status, COUNT(*) AS n FROM t_kb_file '
                    'WHERE deleted_flag=0 GROUP BY extract_status')
        out['by_status'] = {r['extract_status']: int(r['n']) for r in cur.fetchall()}
        cur.execute('SELECT COUNT(*) AS n FROM t_kb_file_page')
        out['pages'] = int(cur.fetchone()['n'])
        out['root'] = config.KB_FILE_ROOT
        return out
    finally:
        c.close()


def purge_raw(file_id):
    """手动清一个文件的磁盘原文（调试/清理用）。"""
    f = get_file(file_id)
    if f and f.get('storage_path'):
        discard(f['storage_path'])
        return True
    return False


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    if args and args[0] == '--purge':
        dry = '--dry-run' in args
        print('状态：', status())
        print('清理孤儿（dry_run=%s）：%s' % (dry, purge_all(dry_run=dry)))
        print('清理后统计：', stats())
    elif args and args[0] == '-r':
        print('状态：', status())
        print('重新排队待抽取：', recover(), '个')
        while _Q.unfinished_tasks:
            time.sleep(0.5)
        print('完成：', stats())
    else:
        print('状态：', status())
        print('统计：', stats())
        o = orphan_raw_files()
        print('磁盘孤儿文件：%d 个%s' % (len(o), '（用 --purge 清理）' if o else ''))
        print('\n用法：')
        print('  python -X utf8 kb_store.py                # 看状态')
        print('  python -X utf8 kb_store.py --purge --dry-run   # 只看要清什么')
        print('  python -X utf8 kb_store.py --purge         # 真清（孤儿页 + 孤儿原文件）')
        print('  python -X utf8 kb_store.py -r              # 重新排队待抽取')
