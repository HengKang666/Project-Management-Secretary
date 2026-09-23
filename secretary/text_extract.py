# -*- coding: utf-8 -*-
"""文件 → 按页 HTML 富文本（给前端预览用）。

四条设计约束：

1. **零 DB、零网络** —— 纯函数：给一个文件路径，还一组页。好测、好替换实现。
2. **依赖全懒加载** —— 某个格式的库没装，只让**那一种格式**不可用，
   不影响服务启动、也不影响其他格式。缺库时抛 `Unsupported`，由上层落成 `unsupported` 状态。
3. **统一产出 HTML** —— 前端同事拿到就能渲染（不再需要 PDF.js / docx-preview）。
   ★ 所有 HTML **必须过白名单清洗**（防 XSS）：文件内容可能带 `<script>` / `onerror=`。
4. **天然分页** —— PDF 按页、xlsx 按 sheet、pptx 按 slide；
   docx / txt 这类格式**本身没有页**，按字数（默认 2000 字）切块，保证前端能一页页翻。

对外只有一个函数：

    pages = extract('/path/to/a.pdf')
    # → [{'page_no': 1, 'page_label': '第 1 页', 'html': '<p>…</p>', 'chars': 512}, ...]
"""
import csv
import io
import os
import re

# 没有天然分页的格式，按这个字数切块
PAGE_TARGET_CHARS = 2000

# 单页 HTML 的硬上限。xlsx / txt 这类可能一次抽出几十万字，
# 超过就算截断（MEDIUMTEXT 上限 16MB，但不该把一页塞那么满）。
MAX_PAGE_CHARS = 200_000

# 电子表格最多渲染多少行（防止一个 csv 抽出一整张表把页面撑爆）
MAX_SHEET_ROWS = 3000

# ---------------- 允许保留的 HTML（其余一律剥掉）----------------
ALLOWED_TAGS = {
    'p', 'br', 'hr', 'div', 'span',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'strong', 'b', 'em', 'i', 'u', 's', 'del', 'sub', 'sup', 'mark', 'small',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
    'blockquote', 'pre', 'code', 'figure', 'figcaption',
    'a', 'img',
}
ALLOWED_ATTRS = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title', 'width', 'height'},
    'td': {'colspan', 'rowspan'},
    'th': {'colspan', 'rowspan'},
    'col': {'span'},
    'colgroup': {'span'},
    '*': {'class'},
}
ALLOWED_PROTOCOLS = {'http', 'https', 'mailto', 'data'}   # data: 是给 docx 里内嵌图片用的

# 切块边界：只在「整个块元素结束」之后切，避免把 ul / table 割成不闭合的半截。
# 用 finditer 定位而不是 split 的 lookbehind —— 后者要求定长，`</(?:p|h[1-6]|…)>` 会直接报错。
_BLOCK_END = re.compile(r'(?is)</(?:p|h[1-6]|table|ul|ol|blockquote|pre|dl|figure)\s*>')
_TAG = re.compile(r'<[^>]+>')
_SCRIPTY = re.compile(r'(?is)<(script|style|iframe|object|embed|form|meta|link)\b.*?</\1>|<(script|style|iframe|object|embed|form|meta|link)\b[^>]*/?>')


class Unsupported(RuntimeError):
    """这种格式不支持 / 缺依赖。上层会落成 extract_status='unsupported'。"""


# ---------------------------------------------------------------- 清洗与工具

_sanitizer = {'mod': None, 'tried': False}


def _get_sanitizer():
    """返回一个 `fn(html) -> html`。优先 nh3（快、在维护），退回 bleach（纯 Python）。"""
    if _sanitizer['tried']:
        return _sanitizer['mod']
    _sanitizer['tried'] = True
    try:
        import nh3

        def _nh3(h):
            return nh3.clean(h, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                             url_schemes=ALLOWED_PROTOCOLS, strip_comments=True)
        _sanitizer['mod'] = _nh3
        return _nh3
    except Exception:                                     # noqa: BLE001
        pass
    try:
        import bleach

        def _bleach(h):
            return bleach.clean(h, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                                protocols=ALLOWED_PROTOCOLS, strip=True)
        _sanitizer['mod'] = _bleach
        return _bleach
    except Exception:                                     # noqa: BLE001
        return None


def sanitize(html):
    """白名单清洗。**没有清洗库就直接报错**，绝不把未清洗的 HTML 入库。"""
    if not html:
        return ''
    # 先把 script/style/iframe 整段切掉（regime 之外的双保险：
    # 白名单本来就会剥掉它们，但先切掉可以让清洗库少干活、也避免属性里的漏网）
    html = _SCRIPTY.sub('', html)
    fn = _get_sanitizer()
    if fn is None:
        raise Unsupported('缺少 HTML 清洗库，未清洗的 HTML 不能入库。'
                          '请安装：pip install nh3   或   pip install bleach')
    return fn(html)


def plain_len(html):
    """按纯文本估算字符数（用于统计与切块阈值）。"""
    return len(_TAG.sub('', html or '').replace('&nbsp;', ' ').strip())


def _clip(html):
    """单页内容上限保护。返回 (html, chars)。"""
    if len(html) > MAX_PAGE_CHARS:
        html = html[:MAX_PAGE_CHARS] + ('<p><em>（本页内容过长，已截断显示；'
                                       '完整内容请下载原文件）</em></p>')
    return html, plain_len(html)


def _page(page_no, label, html):
    html = sanitize(html)
    html, chars = _clip(html)
    return {'page_no': page_no, 'page_label': label, 'html': html, 'chars': chars}


def _read_text(path):
    """读文本文件，兼容 UTF-8 / UTF-8-BOM / GBK / GB18030。"""
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'gbk'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def _iter_blocks(html):
    """把 HTML 切成「块」序列：每个块以某个块级标签的结束标记收尾。"""
    out, pos = [], 0
    for m in _BLOCK_END.finditer(html or ''):
        out.append(html[pos:m.end()])
        pos = m.end()
    if pos < len(html or ''):
        out.append(html[pos:])
    return [x for x in out if x and x.strip()]


def _split_blocks(html, target_chars, label_fmt='第 %d 节'):
    """把一段长 HTML 按块边界切成多页（用于 docx / txt 这种没有天然分页的）。"""
    parts = _iter_blocks(html)
    if not parts:
        return [_page(1, label_fmt % 1, html or '<p><em>（没抽到内容）</em></p>')]
    out, buf, n = [], '', 0
    for p in parts:
        buf += p
        if plain_len(buf) >= target_chars:
            n += 1
            out.append((n, buf))
            buf = ''
    if buf.strip():
        n += 1
        out.append((n, buf))
    return [_page(i, label_fmt % i, h) for i, h in out]


def _md_to_html(md):
    import markdown
    return markdown.markdown(md or '', extensions=['tables', 'sane_lists', 'fenced_code'])


def _table_html(rows, head=True):
    """list[list[str]] → HTML 表格（已转义）。"""
    def esc(v):
        return (str(v) if v is not None else '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    if not rows:
        return '<p><em>（空表）</em></p>'
    out = ['<table>']
    body = rows
    if head:
        out.append('<thead><tr>' + ''.join('<th>%s</th>' % esc(c) for c in rows[0]) + '</tr></thead>')
        body = rows[1:]
    out.append('<tbody>')
    for r in body:
        out.append('<tr>' + ''.join('<td>%s</td>' % esc(c) for c in r) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


# ---------------------------------------------------------------- 各格式实现

def _pdf(path, target_chars):
    """PDF：pymupdf4llm 按页给 Markdown，再转 HTML。**天然分页**。

    ⚠️ 已知局限：跨页的表格会被切断（表头在上一页时，下一页只剩数据行）。
       前端按页顺序连续阅读不受影响；如果很在意，可以后续按页拼接时补表头。
    """
    try:
        import pymupdf4llm
        import markdown                                    # noqa: F401
    except ImportError:
        # pymupdf4llm 会拉进 onnxruntime 等一堆依赖（体积不小）。
        # 服务器上装不动时退回「纯 PyMuPDF 抽文本」：拿不到表格结构，但正文照样能看。
        return _pdf_plain(path)
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True)
    pages = []
    for i, c in enumerate(chunks or [], 1):
        md = (c.get('text') or '').strip()
        pages.append(_page(i, '第 %d 页' % i, _md_to_html(md) if md else '<p><em>（本页无文字层）</em></p>'))
    if not pages:
        pages = [_page(1, '第 1 页', '<p><em>（没抽到内容）</em></p>')]
    return pages


def _pdf_plain(path):
    """退路：只用 PyMuPDF 逐页抽文本（按段落切 <p>，标题靠字号粗判）。

    比 pymupdf4llm 差在**没有表格结构**，好处是依赖极小（只需 pymupdf）。
    """
    try:
        import pymupdf
    except ImportError as e:
        raise Unsupported('缺依赖：pymupdf4llm（pip install pymupdf4llm）') from e
    doc = pymupdf.open(str(path))
    pages = []
    for i, pg in enumerate(doc, 1):
        blocks = [b for b in pg.get_text('blocks') if (b[4] or '').strip()]
        sizes = sorted({round(b[3], 1) for b in blocks}, reverse=True) if blocks else []
        big = sizes[0] if sizes else 0
        buf = []
        for b in blocks:
            txt = (b[4] or '').strip().replace('&', '&amp;').replace('<', '&lt;')
            if not txt:
                continue
            tag = 'h2' if (big and round(b[3], 1) >= big - 0.5 and len(txt) < 60) else 'p'
            buf.append('<%s>%s</%s>' % (tag, txt.replace('\n', '<br>'), tag))
        pages.append(_page(i, '第 %d 页' % i, ''.join(buf) or '<p><em>（本页无文字层）</em></p>'))
    doc.close()
    return pages or [_page(1, '第 1 页', '<p><em>（没抽到内容）</em></p>')]


def _docx(path, target_chars):
    """DOCX：mammoth 转 HTML（**它的强项，表格会输出真正的 <table>**），再按块切页。"""
    try:
        import mammoth
    except ImportError as e:
        raise Unsupported('缺依赖：mammoth（pip install mammoth）') from e
    with open(path, 'rb') as f:
        r = mammoth.convert_to_html(f)
    return _split_blocks(r.value or '', target_chars)


def _xlsx(path, target_chars):
    """XLSX：**每个 sheet 一页**。"""
    try:
        import openpyxl
    except ImportError as e:
        raise Unsupported('缺依赖：openpyxl（pip install openpyxl）') from e
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    pages = []
    for i, ws in enumerate(wb.worksheets, 1):
        rows, cut = [], False
        for j, row in enumerate(ws.iter_rows(values_only=True)):
            if j >= MAX_SHEET_ROWS:
                cut = True
                break
            rows.append(['' if v is None else v for v in row])
        html = _table_html(rows)
        if cut:
            html += '<p><em>（超过 %d 行，只显示前 %d 行）</em></p>' % (MAX_SHEET_ROWS, MAX_SHEET_ROWS)
        pages.append(_page(i, 'Sheet: %s' % (ws.title or ('#%d' % i)), html))
    wb.close()
    return pages or [_page(1, 'Sheet1', '<p><em>（空工作簿）</em></p>')]


def _pptx(path, target_chars):
    """PPTX：**每张 slide 一页**（标题 + 正文 + 表格）。"""
    try:
        from pptx import Presentation
    except ImportError as e:
        raise Unsupported('缺依赖：python-pptx（pip install python-pptx）') from e
    prs = Presentation(path)
    pages = []
    for i, slide in enumerate(prs.slides, 1):
        buf = []
        for shp in slide.shapes:
            if getattr(shp, 'has_table', False) and shp.has_table:
                rows = [[c.text for c in r.cells] for r in shp.table.rows]
                buf.append(_table_html(rows))
                continue
            txt = (getattr(shp, 'text', '') or '').strip()
            if not txt:
                continue
            lines = [x.strip() for x in txt.splitlines() if x.strip()]
            if len(lines) == 1:
                buf.append('<h3>%s</h3>' % lines[0].replace('<', '&lt;'))
            else:
                buf.append('<h3>%s</h3><ul>%s</ul>' % (
                    lines[0].replace('<', '&lt;'),
                    ''.join('<li>%s</li>' % x.replace('<', '&lt;') for x in lines[1:])))
        pages.append(_page(i, 'Slide %d' % i, ''.join(buf) or '<p><em>（本页无文字）</em></p>'))
    return pages or [_page(1, 'Slide 1', '<p><em>（空演示文稿）</em></p>')]


def _md(path, target_chars):
    return _split_blocks(_md_to_html(_read_text(path)), target_chars, '第 %d 段')


def _csv(path, target_chars):
    text = _read_text(path)
    rows = list(csv.reader(io.StringIO(text)))
    return [_page(1, '表格', _table_html(rows))]


def _txt(path, target_chars):
    """TXT：没有结构，按行切块后用 <pre> 保住空白。"""
    text = _read_text(path)
    per = max(50, target_chars // 40)              # 约 40 字一行 ⇒ 每块目标行数
    lines = text.splitlines()
    pages = []
    for i in range(0, max(len(lines), 1), per):
        chunk = '\n'.join(lines[i:i + per])
        pages.append(_page(len(pages) + 1, '第 %d 段' % (len(pages) + 1),
                           '<pre>%s</pre>' % chunk.replace('&', '&amp;').replace('<', '&lt;')))
    return pages or [_page(1, '第 1 段', '<p><em>（空文件）</em></p>')]


# 后缀 → 处理函数
_EXTRACTORS = {
    '.pdf': _pdf, '.docx': _docx, '.xlsx': _xlsx, '.xlsm': _xlsx,
    '.pptx': _pptx, '.md': _md, '.markdown': _md, '.csv': _csv,
    '.txt': _txt, '.log': _txt, '.json': _txt, '.sql': _txt, '.py': _txt,
    '.html': _txt, '.htm': _txt, '.xml': _txt, '.yaml': _txt, '.yml': _txt,
}

# 明确知道不支持、且给出原因的（比"未知格式"更友好）
_KNOWN_BAD = {
    '.doc': '老版 .doc 二进制格式，需先用 LibreOffice 转成 .docx',
    '.xls': '老版 .xls 二进制格式，需先转成 .xlsx',
    '.ppt': '老版 .ppt 二进制格式，需先转成 .pptx',
    '.wps': 'WPS 格式，需先转成 .docx / .pdf',
    '.zip': '压缩包，请解压后逐个上传',
    '.rar': '压缩包，请解压后逐个上传',
    '.7z': '压缩包，请解压后逐个上传',
    '.png': '图片，暂不支持文字抽取（如需请上 OCR）',
    '.jpg': '图片，暂不支持文字抽取（如需请上 OCR）',
    '.jpeg': '图片，暂不支持文字抽取（如需请上 OCR）',
    '.gif': '图片，暂不支持文字抽取（如需请上 OCR）',
    '.bmp': '图片，暂不支持文字抽取（如需请上 OCR）',
    '.tif': '图片，暂不支持文字抽取（如需请上 OCR）',
    '.tiff': '图片，暂不支持文字抽取（如需请上 OCR）',
}


def supported_exts():
    """能处理的扩展名（供接口/文档展示）。"""
    return tuple(sorted(_EXTRACTORS))


def ext_of(name):
    return os.path.splitext(str(name or ''))[1].lower()


# 纯文本家族 / 二进制文档家族 —— 两者冲突时以**内容**为准
_TEXT_EXTS = {'.txt', '.md', '.markdown', '.csv', '.log', '.json', '.sql',
              '.py', '.html', '.htm', '.xml', '.yaml', '.yml'}
_BINARY_EXTS = {'.pdf', '.docx', '.xlsx', '.xlsm', '.pptx'}
_ZIP_MAGIC = (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')


def sniff_ext_bytes(data, max_head=4096):
    """按**内容**猜类型，返回扩展名；看不出返回 None。

    为什么需要：上传时的文件名不可信 —— 前端 `fetch(url, {body:file})` 要自己塞 `X-Filename`，
    漏了就是 `unnamed.txt`；用户也可能把 docx 改名成 txt。一旦类型判错，
    docx 会被当纯文本抽，把 zip 二进制 dump 成几页乱码存进库（实测踩到）。
    """
    head = bytes(data[:max_head]) if data else b''
    if not head:
        return None
    if head.startswith(b'%PDF'):
        return '.pdf'
    if head[:4] in _ZIP_MAGIC:
        # OOXML（docx/xlsx/pptx）本质都是 zip，看里面的目录名区分
        try:
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(bytes(data))) as z:
                names = z.namelist()[:300]
            if any(n.startswith('word/') for n in names):
                return '.docx'
            if any(n.startswith('xl/') for n in names):
                return '.xlsx'
            if any(n.startswith('ppt/') for n in names):
                return '.pptx'
        except Exception:                             # noqa: BLE001
            pass
        return '.zip'
    if head[:4] == b'\xd0\xcf\x11\xe0':
        return '.doc'                                 # 老式 OLE 复合文档（.doc/.xls/.ppt 通吃）
    if b'\x00' in head:
        return None                                   # 其它二进制（图片等）
    return '.txt'


def sniff_ext(path):
    """按内容猜类型（读文件头）。文件读不到就返回 None。"""
    try:
        with open(path, 'rb') as f:
            data = f.read(65536)
    except OSError:
        return None
    return sniff_ext_bytes(data)


def _looks_binary(path):
    try:
        with open(path, 'rb') as f:
            return b'\x00' in f.read(4096)
    except OSError:
        return False


def extract(path, ext=None, target_chars=PAGE_TARGET_CHARS):
    """主入口：文件 → 按页 HTML。

    返回 [{'page_no', 'page_label', 'html', 'chars'}]。
    不支持 / 缺依赖 → 抛 `Unsupported`（带一句能照做的中文说明）。

    ★ **内容优先于文件名**：后缀缺失、或后缀与内容矛盾时，以 `sniff_ext` 的判断为准。
    """
    path = str(path)
    if not os.path.exists(path):
        raise Unsupported('文件不存在：%s' % path)
    ext = (ext or ext_of(path)).lower()
    sniffed = sniff_ext(path)
    if sniffed and sniffed != ext:
        # 只在「原后缀不可用」或「两边家族冲突」时才改判，避免把 .md 硬掰成 .txt 这种降级
        if ext not in _EXTRACTORS and ext not in _KNOWN_BAD:
            ext = sniffed
        elif ext in _TEXT_EXTS and sniffed in _BINARY_EXTS:
            ext = sniffed
        elif ext in _BINARY_EXTS and sniffed in _TEXT_EXTS:
            ext = sniffed
    if ext in _TEXT_EXTS and sniffed is None:
        raise Unsupported('这个文件里是二进制内容，不是文本（后缀是 %s，与实际内容不符）。'
                          '把它按真实格式重新上传就能预览。' % ext)
    if ext in _KNOWN_BAD:
        raise Unsupported('不支持的文件类型 %s：%s' % (ext, _KNOWN_BAD[ext]))
    fn = _EXTRACTORS.get(ext)
    if fn is None:
        raise Unsupported('暂不支持的文件类型 %s（支持：%s）'
                          % (ext or '(无后缀)', '、'.join(supported_exts())))
    if os.path.getsize(path) == 0:
        raise Unsupported('文件是空的（0 字节）')
    pages = fn(path, target_chars)
    # 统一保证页码连续、page_label 不为空
    for i, p in enumerate(pages, 1):
        p['page_no'] = i
        p['page_label'] = p.get('page_label') or ('第 %d 页' % i)
    return pages


def status():
    """/health / 后台任务用的能力自检：哪些格式现在真的能用（不试跑，只看依赖）。"""
    mods = {'docx': 'mammoth', 'xlsx': 'openpyxl', 'pptx': 'pptx',
            'md': 'markdown', 'txt': None}
    ready, missing = [], []
    # PDF 有两条路：pymupdf4llm（带表格结构）优先，退化到 pymupdf（纯文本）
    try:
        import pymupdf4llm                                   # noqa: F401
        ready.append('pdf')
    except Exception:                                        # noqa: BLE001
        try:
            import pymupdf                                   # noqa: F401
            ready.append('pdf(纯文本模式，无表格结构)')
        except Exception:                                    # noqa: BLE001
            missing.append('pdf(缺 pymupdf4llm/pymupdf)')
    for fmt, mod in mods.items():
        if mod is None:
            ready.append(fmt)
            continue
        try:
            __import__(mod)
            ready.append(fmt)
        except Exception:                                    # noqa: BLE001
            missing.append('%s(缺 %s)' % (fmt, mod))
    if _get_sanitizer() is None:
        missing.append('全部(缺 HTML 清洗库 nh3/bleach)')
    return {'ready': ready, 'missing': missing,
            'available': not [m for m in missing if m.startswith('全部')]}
