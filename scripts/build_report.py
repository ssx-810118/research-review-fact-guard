# -*- coding: utf-8 -*-
"""
build_report.py — 将终稿 Markdown 渲染为带来源标注样式的 Word 报告
（Skill 工作流第 6 步）。

特性：
- 中文公文字体（标题黑体，正文宋体），1.5 倍行距，首行缩进；
- 来源标注【来源，位置】自动渲染为蓝色加粗，便于溯源检查；
- Markdown 管道表格 -> Word 三线表样式；
- 自动统计全文字数（不含标点/空白的汉字与字母数）并写入构建日志。
"""
import argparse
import os
import re
import sys

from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

class WordCountError(RuntimeError):
    """正文计字未达到结构要求时抛出，供 CLI 与 run_pipeline 判定失败。"""

    def __init__(self, chars, need):
        super().__init__('正文计字 %d 少于结构要求 %d' % (chars, need))
        self.chars, self.need = chars, need


CITE_PAT = re.compile(r'(【[^】]*】|\[[^\[\]]{2,40}[，,；;][^\[\]]{1,60}?\])')
BOLD_PAT = re.compile(r'\*\*(.+?)\*\*')
CITE_COLOR = RGBColor(0x1F, 0x4E, 0x79)


def set_run_font(run, name='宋体', size=12, bold=False, color=None):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn('w:rFonts'))
    if rfonts is None:
        rfonts = OxmlElement('w:rFonts')
        rpr.append(rfonts)
    rfonts.set(qn('w:eastAsia'), name)
    rfonts.set(qn('w:ascii'), name)
    rfonts.set(qn('w:hAnsi'), name)


def add_runs_with_citations(p, text, base_font='宋体', size=12, base_bold=False):
    """按来源标注切分文本，标注片段蓝色加粗；同时处理 **加粗**。"""
    # 先按 **bold** 切，再按引用切
    segments = []
    for i, part in enumerate(BOLD_PAT.split(text)):
        if part == '':
            continue
        segments.append((part, base_bold or (i % 2 == 1)))
    for plain, bold in segments:
        pieces = CITE_PAT.split(plain)
        for k, piece in enumerate(pieces):
            if piece == '':
                continue
            is_cite = bool(CITE_PAT.fullmatch(piece))
            r = p.add_run(piece)
            set_run_font(r, base_font, size,
                         bold=bold or is_cite,
                         color=CITE_COLOR if is_cite else None)


def set_cell_text(cell, text, bold=False, size=10.5):
    cell.text = ''
    p = cell.paragraphs[0]
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    add_runs_with_citations(p, text, '宋体', size, bold)


def add_table(doc, rows):
    if len(rows) < 1:
        return
    ncol = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncol)
    table.style = 'Table Grid'
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        for j in range(ncol):
            txt = row[j] if j < len(row) else ''
            set_cell_text(table.rows[i].cells[j], txt, bold=(i == 0),
                          size=10.5)
    # 表头底纹
    for cell in table.rows[0].cells:
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:fill'), 'D9E2F3')
        tcPr.append(shd)
    doc.add_paragraph()


def add_heading(doc, text, level):
    """标题使用 Word 内置 Heading 样式，使导航窗格与自动目录（TOC 域）可用；
    字号/字体在 run 级别覆写，保证与原有版式一致。"""
    style = 'Heading %d' % min(max(level, 1), 4)
    p = doc.add_paragraph(style=style)
    p.paragraph_format.space_before = Pt(10 if level <= 2 else 6)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.first_line_indent = Pt(0)
    size = {1: 18, 2: 15, 3: 13, 4: 12}.get(level, 12)
    add_runs_with_citations(p, text, '黑体', size, base_bold=True)
    for r in p.runs:                      # 覆写 Heading 样式默认的蓝色标题色
        r.font.color.rgb = RGBColor(0, 0, 0)
    return p


def add_toc(doc, levels='1-2'):
    """插入 TOC 域：打开文档时由 Word 提示更新域即可生成带页码的目录。"""
    p = doc.add_paragraph('目　录')
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(12)
    r = p.add_run()
    set_run_font(r, '黑体', 16, bold=True)
    r.font.color.rgb = RGBColor(0, 0, 0)

    p = doc.add_paragraph()
    run = p.add_run()
    fld_begin = OxmlElement('w:fldChar')
    fld_begin.set(qn('w:fldCharType'), 'begin')
    instr = OxmlElement('w:instrText')
    instr.set(qn('xml:space'), 'preserve')
    instr.text = 'TOC \\o "%s" \\h \\z \\u' % levels
    fld_sep = OxmlElement('w:fldChar')
    fld_sep.set(qn('w:fldCharType'), 'separate')
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_sep)

    tip = p.add_run('【请在此处右键选择“更新域”生成目录】')
    set_run_font(tip, '宋体', 10.5)
    tip.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    run_end = p.add_run()._r
    fld_end = OxmlElement('w:fldChar')
    fld_end.set(qn('w:fldCharType'), 'end')
    run_end.append(fld_end)

    # 打开文档时自动更新域，免去手动 F9
    settings = doc.settings.element
    if settings.find(qn('w:updateFields')) is None:
        uf = OxmlElement('w:updateFields')
        uf.set(qn('w:val'), 'true')
        settings.append(uf)
    doc.add_page_break()


def count_chars(text):
    return len(re.findall(r'[一-龥A-Za-z0-9]', re.sub(r'【[^】]*】', '', text)))


def build(md_path, out_path, meta=None, min_chars=0):
    meta = meta or {}
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.read().split('\n')
    doc = Document()
    normal = doc.styles['Normal']
    normal.font.name = '宋体'
    normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')

    # 封面
    for _ in range(4):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(meta.get('title', '调研综述'))
    set_run_font(r, '黑体', 26, bold=True)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(meta.get('subtitle', '——含来源标注与事实一致性自审'))
    set_run_font(r, '楷体', 15)
    for _ in range(10):
        doc.add_paragraph()
    for info in meta.get('info_lines', []):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(info)
        set_run_font(r, '宋体', 12)
    doc.add_page_break()

    title_text = (meta.get('title') or '').strip()
    title_consumed = False
    if meta.get('toc', True):
        add_toc(doc, meta.get('toc_levels', '1-2'))

    body_chars = 0
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        m = re.match(r'^(#{1,4})\s*(.*)$', line)
        if m:
            md_level, htext = len(m.group(1)), m.group(2).strip()
            # 首个一级标题若与封面标题相同，视为文档标题，不重复排版也不进目录
            if md_level == 1 and not title_consumed:
                title_consumed = True
                if not title_text or htext == title_text or htext in title_text:
                    i += 1
                    continue
            # 封面已占一级标题，正文层级整体上提一级（## → Heading 1）
            level = md_level - 1 if (title_consumed and md_level > 1) else md_level
            add_heading(doc, htext, level)
            body_chars += len(htext)
            i += 1
            continue
        if line.lstrip().startswith('|'):
            tbl_rows = []
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-{2,}:?', c.replace(' ', '')) for c in cells):
                    tbl_rows.append(cells)
                i += 1
            add_table(doc, tbl_rows)
            body_chars += sum(len(''.join(r)) for r in tbl_rows)
            continue
        if re.match(r'^\s*[-*]\s+', line):
            text = re.sub(r'^\s*[-*]\s+', '', line)
            p = doc.add_paragraph(style='List Bullet')
            p.paragraph_format.line_spacing = 1.5
            add_runs_with_citations(p, text)
            body_chars += count_chars(text)
            i += 1
            continue
        if re.match(r'^\s*\d+[.、]\s*', line):
            text = re.sub(r'^\s*\d+[.、]\s*', '', line)
            p = doc.add_paragraph(style='List Number')
            p.paragraph_format.line_spacing = 1.5
            add_runs_with_citations(p, text)
            body_chars += count_chars(text)
            i += 1
            continue
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.line_spacing = 1.5
        pf.first_line_indent = Pt(24)
        add_runs_with_citations(p, line)
        body_chars += count_chars(line)
        i += 1

    doc.save(out_path)
    print('[docx] %s 正文计字(汉字/字母/数字，不含来源标注) %d -> %s'
          % (os.path.basename(out_path), body_chars, out_path))
    if min_chars and body_chars < min_chars:
        # 先落盘再抛错：便于查看已生成内容，同时让调用方明确知道未达标
        raise WordCountError(body_chars, min_chars)
    return body_chars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('markdown')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--title', default=None)
    ap.add_argument('--subtitle', default='——基于多源调研材料的综述、来源标注与事实一致性自审')
    ap.add_argument('--no-toc', action='store_true', help='不生成目录页')
    ap.add_argument('--min-chars', type=int, default=0,
                    help='正文计字下限（汉字/字母/数字，不含来源标注）；不足则非零退出')
    args = ap.parse_args()
    title = args.title
    if title is None:
        with open(args.markdown, 'r', encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^#\s+(.+)', line.strip())
                if m:
                    title = m.group(1).strip()
                    break
        if title is None:
            title = '调研综述报告'
    try:
        build(args.markdown, args.out,
              {'title': title, 'subtitle': args.subtitle,
               'toc': not args.no_toc,
               'info_lines': ['（依据调研材料包生成，事实性陈述均标注来源）']},
              min_chars=args.min_chars)
    except WordCountError as e:
        print('[FAIL] %s（要求 %d）；终稿未达篇幅要求，不得作为交付件'
              % (e, e.need), file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
