# -*- coding: utf-8 -*-
"""办公文件工具：生成 / 编辑 docx、xlsx、pptx。

独立于主查询链路：不注册进 agent 的工具列表，按需单独调用（和 skills 一样是「额外的能力」）。

统一约束（安全边界，只此一份，不要在各调用方重复实现）：
  · 只写 <repo>/output/ 下（可含子目录），模型拿到的是文件名，不是路径；
  · 绝对路径、盘符、`..` 逃逸一律拒绝。

返回：dict；失败返回 {'error': '...'}，不向上层抛异常。
"""
import os
import re

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'output')

# Word 里 {{name}} 常被拆成多个 run，替换前必须先把整段 run 拼起来
_PLACEHOLDER = re.compile(r'\{\{\s*([^{}]+?)\s*\}\}')

# xlsx 里以这些字符开头的文本会被 Excel 当公式执行
_FORMULA_START = ('=', '+', '-', '@')


def output_dir():
    """文件落地根目录，供调用方展示路径用。"""
    return _ROOT


def _rel(p):
    return os.path.relpath(p, _ROOT).replace('\\', '/')


def _path(name, mkdir=False):
    """把调用方给的文件名解析到 output/ 下；越界一律拒绝。"""
    s = str(name or '').strip().replace('\\', '/')
    if not s:
        raise ValueError('文件名不能为空')
    if s.startswith('/') or (len(s) > 1 and s[1] == ':'):
        raise ValueError('只接受文件名或相对路径，不接受绝对路径：' + s)
    p = os.path.normpath(os.path.join(_ROOT, s))
    if p != _ROOT and not p.startswith(_ROOT + os.sep):
        raise ValueError('路径越出 output 目录：' + s)
    if os.path.isdir(p):
        raise ValueError('这是目录，不是文件：' + s)
    if mkdir:
        os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _missing(path, name):
    if not os.path.exists(path):
        return {'error': '文件不存在：' + str(name)}
    return None


def _cell(v):
    """空值统一成空串；字符串原样返回（公式注入在 _append 里挡）。"""
    return '' if v is None else v


def _append(ws, row):
    ws.append([_cell(v) for v in row])
    for c in ws[ws.max_row]:
        if isinstance(c.value, str) and c.value[:1] in _FORMULA_START:
            c.data_type = 's'   # 强制按文本存，别让 Excel 当公式跑


def _wlen(s):
    """显示宽度：中文按 2 个字符算，用于列宽自适应。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(s))


# ---------------------------------------------------------------- docx

def read_docx(path, max_paras=500):
    """读 .docx 的段落（含样式名）与表格，编辑前先看它有什么。"""
    from docx import Document
    try:
        p = _path(path)
    except ValueError as e:
        return {'error': str(e)}
    miss = _missing(p, path)
    if miss:
        return miss
    d = Document(p)
    paras = []
    for para in d.paragraphs:
        t = para.text.strip()
        if t:
            paras.append({'style': para.style.name if para.style else '', 'text': t})
        if len(paras) >= max_paras:
            break
    tables = [[[c.text.strip() for c in row.cells] for row in tb.rows] for tb in d.tables]
    return {'path': _rel(p), 'paragraphs': paras, 'tables': tables,
            'para_count': len(d.paragraphs), 'table_count': len(d.tables)}


def make_docx(path, blocks):
    """新建/覆盖 .docx。

    blocks 是列表，每项：
      {'type':'heading','text':..,'level':1}
      {'type':'para','text':..}
      {'type':'table','header':[..],'rows':[[..]]}
      {'type':'image','file':'图.png','width_in':5.5}
      {'type':'pagebreak'}
    """
    from docx import Document
    from docx.shared import Inches
    try:
        p = _path(path, mkdir=True)
    except ValueError as e:
        return {'error': str(e)}
    try:
        d = Document()
        n = 0
        for b in (blocks or []):
            kind = (b.get('type') or 'para').lower()
            if kind == 'heading':
                d.add_heading(str(b.get('text') or ''), level=int(b.get('level') or 1))
            elif kind == 'table':
                header = [_cell(v) for v in (b.get('header') or [])]
                rows = b.get('rows') or []
                cols = max([len(header)] + [len(r) for r in rows] + [1])
                tb = d.add_table(rows=0, cols=cols)
                tb.style = 'Table Grid'
                if header:
                    tb.add_row()
                    for i, v in enumerate(header):
                        tb.rows[-1].cells[i].text = str(v)
                for r in rows:
                    tb.add_row()
                    for i in range(cols):
                        tb.rows[-1].cells[i].text = str(r[i]) if i < len(r) else ''
            elif kind == 'image':
                ip = _path(b.get('file') or '')
                if not os.path.exists(ip):
                    return {'error': '图片不存在：' + str(b.get('file'))}
                d.add_picture(ip, width=Inches(float(b.get('width_in') or 5.5)))
            elif kind == 'pagebreak':
                d.add_page_break()
            else:
                d.add_paragraph(str(b.get('text') or ''))
            n += 1
        d.save(p)
    except ValueError as e:
        return {'error': str(e)}
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200]}
    return {'path': _rel(p), 'blocks': n}


def fill_docx(template, mapping, out=None):
    """就地替换模板里的 {{占位符}}，保留原有格式。

    段落正文、表格单元格、页眉页脚都会填。只动含 {{ 的段落，
    所以段落内的其他格式不受影响。
    """
    from docx import Document
    try:
        src = _path(template)
        dst = _path(out, mkdir=True) if out else src
    except ValueError as e:
        return {'error': str(e)}
    miss = _missing(src, template)
    if miss:
        return miss
    m = {str(k): ('' if v is None else str(v)) for k, v in (mapping or {}).items()}
    hits = [0]

    def fill_paras(paras):
        for para in paras:
            full = ''.join(r.text for r in para.runs)
            if '{{' not in full or not para.runs:
                continue
            seen = []

            def rep(mo):
                if mo.group(1) not in m:
                    return mo.group(0)      # 没给值的占位符原样留着，便于发现漏填
                seen.append(mo.group(1))
                return m[mo.group(1)]

            new = _PLACEHOLDER.sub(rep, full)
            if new == full:
                continue
            para.runs[0].text = new
            for r in para.runs[1:]:
                r.text = ''
            hits[0] += len(seen)

    try:
        d = Document(src)
        fill_paras(d.paragraphs)
        for tb in d.tables:
            for row in tb.rows:
                for c in row.cells:
                    fill_paras(c.paragraphs)
        for sec in d.sections:
            fill_paras(sec.header.paragraphs)
            fill_paras(sec.footer.paragraphs)
        d.save(dst)
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200]}
    return {'path': _rel(dst), 'replaced': hits[0], 'filled': len(m)}


# ---------------------------------------------------------------- xlsx

def read_xlsx(path, sheet=None, max_rows=300):
    """读 .xlsx 的单元格值（公式取结果值），默认第一张表。"""
    from openpyxl import load_workbook
    try:
        p = _path(path)
    except ValueError as e:
        return {'error': str(e)}
    miss = _missing(p, path)
    if miss:
        return miss
    try:
        wb = load_workbook(p, data_only=True)
        if sheet and sheet not in wb.sheetnames:
            return {'error': '没有这张表：' + str(sheet) + '；现有：' + '、'.join(wb.sheetnames)}
        ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= max_rows:
                break
            rows.append([_cell(v) for v in row])
        out = {'path': _rel(p), 'sheets': list(wb.sheetnames), 'sheet': ws.title,
               'rows': rows, 'row_count': len(rows)}
        wb.close()
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200]}
    return out


def write_xlsx(path, sheets):
    """新建/覆盖 .xlsx。

    sheets：[{'name':'预算','header':[..],'rows':[[..]]}, ...]
    表头加粗居中，列宽按内容自适应（中文按 2 宽）。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter
    try:
        p = _path(path, mkdir=True)
    except ValueError as e:
        return {'error': str(e)}
    try:
        wb = Workbook()
        wb.remove(wb.active)
        total = 0
        for s in (sheets or []):
            ws = wb.create_sheet(str(s.get('name') or 'Sheet')[:31])
            header = [_cell(v) for v in (s.get('header') or [])]
            if header:
                _append(ws, header)
                for c in ws[1]:
                    c.font = Font(bold=True)
                    c.alignment = Alignment(horizontal='center')
            for row in (s.get('rows') or []):
                _append(ws, row)
                total += 1
            widths = {}
            for row in ws.iter_rows(values_only=True):
                for i, v in enumerate(row):
                    w = _wlen(v)
                    if w > widths.get(i, 0):
                        widths[i] = w
            for i, w in widths.items():
                ws.column_dimensions[get_column_letter(i + 1)].width = min(w + 2, 60)
            ws.freeze_panes = 'A2' if header else None
        if not wb.sheetnames:
            wb.create_sheet('Sheet')
        wb.save(p)
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200]}
    return {'path': _rel(p), 'sheets': len(wb.sheetnames), 'rows': total}


def update_xlsx(path, sheet, cells):
    """就地改单元格，原有格式与其他内容都保留。

    cells: {'B2': 123, 'A3': '合计', ...}
    """
    from openpyxl import load_workbook
    try:
        p = _path(path)
    except ValueError as e:
        return {'error': str(e)}
    miss = _missing(p, path)
    if miss:
        return miss
    try:
        wb = load_workbook(p)
        if sheet and sheet not in wb.sheetnames:
            return {'error': '没有这张表：' + str(sheet) + '；现有：' + '、'.join(wb.sheetnames)}
        ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
        n = 0
        for ref, val in (cells or {}).items():
            ws[str(ref)] = _cell(val)
            c = ws[str(ref)]
            if isinstance(c.value, str) and c.value[:1] in _FORMULA_START:
                c.data_type = 's'
            n += 1
        wb.save(p)
        wb.close()
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200]}
    return {'path': _rel(p), 'sheet': ws.title, 'changed': n}


# ---------------------------------------------------------------- pptx

def make_pptx(path, slides, title=None, subtitle=None):
    """生成 16:9 .pptx。

    slides：[{'title':..,'bullets':['要点', {'text':'子项','level':1}],
              'table':{'header':[..],'rows':[[..]]},'notes':'备注'}]
    table 与 bullets 同时给时只出表格。
    """
    from pptx import Presentation
    from pptx.util import Inches
    try:
        p = _path(path, mkdir=True)
    except ValueError as e:
        return {'error': str(e)}
    try:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
        layouts = prs.slide_layouts
        n = 0
        if title:
            s = prs.slides.add_slide(layouts[0])
            if s.shapes.title is not None:
                s.shapes.title.text = str(title)
            if subtitle and len(s.placeholders) > 1:
                s.placeholders[1].text = str(subtitle)
            n += 1
        for item in (slides or []):
            head = str(item.get('title') or '')
            tb_data = item.get('table')
            if tb_data:
                s = prs.slides.add_slide(layouts[5] if len(layouts) > 5 else layouts[1])
                if s.shapes.title is not None:
                    s.shapes.title.text = head
                header = [str(_cell(v)) for v in (tb_data.get('header') or [])]
                rows = tb_data.get('rows') or []
                cols = max([len(header)] + [len(r) for r in rows] + [1])
                nrows = len(rows) + (1 if header else 0)
                shape = s.shapes.add_table(nrows, cols, Inches(0.6), Inches(1.7),
                                           Inches(12.1), Inches(0.4 * max(nrows, 1)))
                tb = shape.table
                r0 = 0
                if header:
                    for i in range(cols):
                        tb.cell(0, i).text = header[i] if i < len(header) else ''
                    r0 = 1
                for j, row in enumerate(rows):
                    for i in range(cols):
                        tb.cell(j + r0, i).text = str(_cell(row[i])) if i < len(row) else ''
            else:
                s = prs.slides.add_slide(layouts[1])
                if s.shapes.title is not None:
                    s.shapes.title.text = head
                body = s.placeholders[1].text_frame
                body.clear()
                first = True
                for b in (item.get('bullets') or []):
                    txt = b.get('text') if isinstance(b, dict) else b
                    lvl = int(b.get('level') or 0) if isinstance(b, dict) else 0
                    para = body.paragraphs[0] if first else body.add_paragraph()
                    para.text = str(_cell(txt))
                    para.level = min(max(lvl, 0), 4)
                    first = False
            if item.get('notes'):
                s.notes_slide.notes_text_frame.text = str(item['notes'])
            n += 1
        if n == 0:
            return {'error': '没有内容：slides 为空且未给 title'}
        prs.save(p)
    except Exception as e:
        return {'error': type(e).__name__ + ': ' + str(e)[:200]}
    return {'path': _rel(p), 'slides': n}
