# -*- coding: utf-8 -*-
"""
parse_materials.py — 多源调研材料解析器（Skill 工作流第 1 步）

输入：材料目录（支持 .docx/.pdf/.xlsx/.txt/.md，可混合多源）
输出：corpus.json，结构：
{
  "documents": [
    {"doc_id","name","type","path",
     "sections":[{"sec_id","heading","level","locator","text","char_count"}],
     "tables":[{"table_id","caption","locator","headers","rows","all_rows",
                "body_rows","summary_rows"}]}
  ]
}
定位规则：标题层级通用识别（第X章/一、/1./N.N/#）；表格按出现顺序编号并向上回溯表题；
Excel 每行附带数据行号，汇总/合计行单独标记。
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (dump_json, heading_level, read_xlsx,
                    read_pdf_text, read_plain, clean_text)

SECTION_LIKE = re.compile(r'^(论文|新闻|访谈|评论|案例|条目|附录|附件)\s*[A-Z一二三四五六七八九十\d]+')


def new_section(doc_id, idx, heading, level, text_parts):
    return {
        'sec_id': f'{doc_id}-s{idx:02d}',
        'heading': heading,
        'level': level,
        'locator': heading,
        'text': '\n'.join(text_parts).strip(),
        'char_count': sum(len(t) for t in text_parts),
    }


def flush(sections, doc_id, idx_holder, heading, level, buf):
    text_parts = [t for t in buf if t.strip()]
    if not heading and not text_parts:
        return
    idx_holder['i'] += 1
    sections.append(new_section(doc_id, idx_holder['i'], heading or '（开头）',
                                level or 0, text_parts))
    buf.clear()


def parse_linear_lines(doc_id, lines):
    """按标题层级把线性文本行切成 sections。"""
    sections, buf = [], []
    idx = {'i': 0}
    cur_heading, cur_level = None, 0
    for raw in lines:
        line = clean_text(raw)
        if not line:
            continue
        lvl = heading_level(line)
        if lvl or SECTION_LIKE.match(line):
            flush(sections, doc_id, idx, cur_heading, cur_level, buf)
            cur_heading, cur_level = re.sub(r'^#+\s*', '', line), (lvl or 2)
        else:
            buf.append(line)
    flush(sections, doc_id, idx, cur_heading, cur_level, buf)
    return sections


def _split_pipe_row(line):
    cells = [c.strip() for c in line.strip().strip('|').split('|')]
    return cells


def _is_separator_row(cells):
    return all(re.fullmatch(r':?-{2,}:?', c.replace(' ', '')) for c in cells) and len(cells) > 0


# 汇总行标签：纯汇总词直接命中；带修饰的（平均研发投入、市场预期总额）
# 需其剩余部分与表头核心词对得上，避免把“平均单价”这类明细字段误判为汇总行。
PURE_AGG = {'合计', '总计', '小计', '汇总', '总额', '总值', '总数', '平均值',
            '平均', '均值', '累计', '全部', '总体', '总值', '总合', '共计'}
AGG_PREFIX = re.compile(r'^(总|合计|总计|小计|汇总|总额|平均|均值|平均值|累计|全部|总体)')
AGG_SUFFIX = re.compile(r'(总额|总值|合计|总计|总数|均值|平均值|总和)$')
AGG_REST = {'企业数', '数量', '条数', '记录数', '家数', '金额', '规模', '数',
            '值', '合计', '总额', '共计', '量'}


def _compact(s):
    return re.sub(r'[（(][^）)]*[）)]', '', str(s)).strip()


def _core(s):
    return re.sub(r'[^一-龥A-Za-z0-9]', '', _compact(s))


def _is_summary_label(cell, headers=()):
    s = re.sub(r'[\s:：]', '', _compact(cell))
    if not s:
        return False
    if s in PURE_AGG:
        return True

    def _header_hit(rest):
        for h in headers:
            hc = _core(h)
            if hc and len(hc) >= 2 and (hc in rest or rest in hc):
                return True
        return False

    m = AGG_PREFIX.match(s)
    if m:
        rest = s[m.end():]
        if not rest or rest in AGG_REST:
            return True
        if _header_hit(rest):
            return True
    ms = AGG_SUFFIX.search(s)
    if ms:
        rest = s[:ms.start()]
        if rest and _header_hit(rest):
            return True
    return False


def _row_split(rows, headers=()):
    """把表格明细行与汇总行分开：Word/PDF/Markdown 表格无显式标记，
    按首列标签识别，供 numeric_audit 的汇总复算使用。"""
    body, summary = [], []
    for r in rows:
        if not r or all(str(c).strip() in ('', 'None') for c in r):
            continue
        (summary if _is_summary_label(r[0], headers) else body).append(r)
    return {'body_rows': body, 'summary_rows': summary}


def extract_pipe_tables(doc_id, sections):
    """从章节文本中识别 Markdown 风格管道表格（含“伪表格”段落），
    物化为 table 对象并从正文中移除；表题取表格上方最近的“表N”行。"""
    tables = []
    tcount = 0
    cap_re = re.compile(r'^(?:表|Table)\s*\d+')
    for sec in sections:
        lines = sec['text'].split('\n')
        out, i = [], 0
        pending_caption = None
        while i < len(lines):
            line = lines[i]
            # 题注行须短且不含句读，否则容易把“表3 …+正文”整段误当题注吞掉
            if (cap_re.match(line) and len(line) <= 40
                    and not re.search(r'[。；]', line)):
                pending_caption = line
                i += 1
                continue
            if line.lstrip().startswith('|') and line.count('|') >= 2:
                j = i
                while j < len(lines) and lines[j].lstrip().startswith('|'):
                    j += 1
                rows = []
                for ln in lines[i:j]:
                    cells = _split_pipe_row(ln)
                    if cells and not _is_separator_row(cells):
                        rows.append(cells)
                if len(rows) >= 2:
                    tcount += 1
                    cap = pending_caption or f'{sec["heading"]}·表{tcount}'
                    tables.append({'table_id': f'{doc_id}-pt{tcount:02d}',
                                   'caption': cap, 'locator': cap,
                                   'headers': rows[0], 'rows': rows[1:],
                                   **_row_split(rows[1:], rows[0]),
                                   'all_rows': rows})
                    out.append(f'［表格位置：{cap}］')
                else:
                    if pending_caption:
                        out.append(pending_caption)
                    out.extend(lines[i:j])
                pending_caption = None
                i = j
                continue
            pending_caption = None
            out.append(line)
            i += 1
        if pending_caption:
            out.append(pending_caption)
        sec['text'] = '\n'.join(out)
    return tables


def parse_docx(doc_id, path):
    """按 body XML 顺序遍历段落(w:p)与表格(w:tbl)。"""
    import docx
    from docx.oxml.ns import qn
    d = docx.Document(path)
    p_iter = iter(d.paragraphs)
    t_iter = iter(d.tables)
    sections, buf = [], []
    idx = {'i': 0}
    cur_heading, cur_level = None, 0
    table_objs = []
    last_caption = None
    tcount = 0
    for child in d.element.body.iterchildren():
        if child.tag == qn('w:p'):
            try:
                obj = next(p_iter)
            except StopIteration:
                continue
            line = clean_text(obj.text)
            if not line:
                continue
            style = (obj.style.name or '') if obj.style else ''
            lvl = 0
            if style.startswith('Heading') or style.startswith('标题'):
                mm = re.search(r'(\d+)', style)
                lvl = int(mm.group(1)) if mm else 1
            if not lvl:
                lvl = heading_level(line)
            if not lvl and SECTION_LIKE.match(line):
                lvl = 2
            if lvl:
                flush(sections, doc_id, idx, cur_heading, cur_level, buf)
                cur_heading, cur_level = re.sub(r'^#+\s*', '', line), lvl
            else:
                if re.match(r'^表\s*\d+', line) or re.match(r'^Table\s*\d+', line, re.I):
                    last_caption = line
                buf.append(line)
        elif child.tag == qn('w:tbl'):
            try:
                obj = next(t_iter)
            except StopIteration:
                continue
            tcount += 1
            rows = [[c.text.strip() for c in r.cells] for r in obj.rows]
            caption = last_caption or f'表{tcount}'
            last_caption = None
            table_objs.append({'table_id': f'{doc_id}-t{tcount:02d}',
                               'caption': caption, 'locator': caption,
                               'headers': rows[0] if rows else [],
                               'rows': rows[1:] if len(rows) > 1 else rows,
                               **_row_split(rows[1:] if len(rows) > 1 else [],
                                          rows[0] if rows else []),
                               'all_rows': rows})
            buf.append(f'［表格位置：{caption}］')
    flush(sections, doc_id, idx, cur_heading, cur_level, buf)
    pipe_tables = extract_pipe_tables(doc_id, sections)
    table_objs.extend(pipe_tables)
    return sections, table_objs


def parse_pdf_doc(doc_id, path):
    import pdfplumber
    line_bag, table_objs = [], []
    tcount = 0
    with pdfplumber.open(path) as pdf:
        for pno, pg in enumerate(pdf.pages, start=1):
            # 先取表格，再过滤正文行：PDF 常把表格行重复导入正文，
            # 这些行会被误判成标题（如“2025 212 企业级AI智能体市场”）。
            page_tables = []
            row_sigs = set()
            for tbl in (pg.extract_tables() or []):
                rows = [[(c or '').replace('\n', ' ').strip() for c in r] for r in tbl]
                rows = [r for r in rows if any(r)]
                if len(rows) >= 2:
                    page_tables.append(rows)
                for r in rows:
                    row_sigs.add(re.sub(r'\s', '', ''.join(str(c) for c in r)))
            txt = pg.extract_text() or ''
            for ln in txt.split('\n'):
                if not clean_text(ln):
                    continue
                if re.sub(r'\s', '', ln) in row_sigs:
                    continue
                line_bag.append(ln)
            for rows in page_tables:
                tcount += 1
                caption = f'第{pno}页·表{tcount}'
                table_objs.append({'table_id': f'{doc_id}-t{tcount:02d}',
                                   'caption': caption,
                                   'locator': caption,
                                   'headers': rows[0], 'rows': rows[1:],
                                   **_row_split(rows[1:], rows[0]),
                                   'all_rows': rows})
                line_bag.append(f'［表格位置：{caption}］')
    sections = parse_linear_lines(doc_id, _pdf_paragraphs(line_bag))
    pipe_tables = extract_pipe_tables(doc_id, sections)
    table_objs.extend(pipe_tables)
    return sections, table_objs


def _pdf_paragraphs(lines):
    """PDF 换行修复：把被行宽硬断开的句子接回去，避免“同比增12 / 9.9%”式
    断数字，以及“多 模态”式中文字符间多余空格；标题与表格标记单独成段。"""
    paras, buf = [], ''
    for ln in lines:
        raw = clean_text(ln).strip()
        if not raw or raw.startswith('AIGC:'):
            continue
        # 标题先按原样判定，避免“第一章 行业概述”中的空格被清理逻辑吃掉
        if raw.startswith('［表格位置') or heading_level(raw) or SECTION_LIKE.match(raw):
            if buf:
                paras.append(buf)
                buf = ''
            paras.append(raw)
            continue
        s = re.sub(r'(?<=[\u4e00-\u9fa5，。；：、（）“”])\s+'
                   r'(?=[\u4e00-\u9fa5，。；：、（）“”])', '', raw)
        s = re.sub(r'(?<=\d)\s+(?=\d)', '', s)
        s = re.sub(r'\s+(?=[%％])', '', s)
        s = re.sub(r'(?<![A-Za-z])([A-Z])\s+([A-Z]{2,})', r'\1\2', s)  # “A GI”→“AGI”式断字
        if buf:
            prev, nxt = buf[-1:], s[:1]
            # 换行处西文断词：仅当上一行以单个字母结尾（如“A”）才补空格，
            # 其余alpha-alpha直接接合，避免把“Sci/ence”拼成“Sci ence”
            tail = re.search(r'[A-Za-z]+$', buf)
            if (prev.isascii() and prev.isalpha() and nxt.isascii() and nxt.isalpha()
                    and tail and len(tail.group(0)) == 1):
                buf += ' '
        buf += s
        if s.endswith(('。', '！', '？')):
            paras.append(buf)
            buf = ''
    if buf:
        paras.append(buf)
    return paras


def parse_xlsx_doc(doc_id, path):
    sheets = read_xlsx(path)
    sections, tables = [], []
    tcount = 0
    for name, rows in sheets.items():
        tcount += 1
        headers = rows[0]
        body_rows, summary_rows, indexed = [], [], []
        for ri, r in enumerate(rows[1:], start=2):
            rr = r + [f'第{ri}行']
            indexed.append(rr)
            if not r or all(str(c).strip() in ('', 'None') for c in r):
                continue
            is_summary = bool(r) and _is_summary_label(r[0], headers)
            (summary_rows if is_summary else body_rows).append(rr)
        caption = f'工作表“{name}”'
        tables.append({'table_id': f'{doc_id}-t{tcount:02d}',
                       'caption': name, 'locator': caption,
                       'headers': headers + ['数据行号'],
                       'rows': indexed, 'body_rows': body_rows,
                       'summary_rows': summary_rows, 'all_rows': indexed})
        sections.append({'sec_id': f'{doc_id}-s{tcount:02d}',
                         'heading': f'数据表：{name}', 'level': 1,
                         'locator': caption,
                         'text': f'调研数据表，工作表“{name}”，共 {len(body_rows)} 条数据记录、'
                                 f'{len(summary_rows)} 条汇总行；字段：' + '、'.join(map(str, headers)),
                         'char_count': 0})
    return sections, tables


def parse_text_doc(doc_id, path):
    text = read_plain(path)
    lines = text.split('\n')
    sections = parse_linear_lines(doc_id, lines)
    pipe_tables = extract_pipe_tables(doc_id, sections)
    return sections, pipe_tables


PARSERS = {'.docx': parse_docx, '.pdf': parse_pdf_doc, '.xlsx': parse_xlsx_doc,
           '.txt': parse_text_doc, '.md': parse_text_doc}


def main():
    ap = argparse.ArgumentParser(description='多源调研材料解析器')
    ap.add_argument('materials_dir', help='材料目录')
    ap.add_argument('-o', '--out', default='corpus.json', help='输出语料 JSON')
    args = ap.parse_args()

    documents = []
    for fn in sorted(os.listdir(args.materials_dir)):
        path = os.path.join(args.materials_dir, fn)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext not in PARSERS:
            continue
        name = os.path.splitext(fn)[0]
        doc_id = 'd%02d' % (len(documents) + 1)
        print(f'[parse] {fn} -> {doc_id}')
        sections, tables = PARSERS[ext](doc_id, path)
        documents.append({'doc_id': doc_id, 'name': name,
                          'file_name': fn, 'type': ext.lstrip('.'),
                          'sections': sections, 'tables': tables})
    corpus = {'materials_dir': os.path.abspath(args.materials_dir),
              'document_count': len(documents), 'documents': documents}
    dump_json(corpus, args.out)
    total_sec = sum(len(d['sections']) for d in documents)
    total_tbl = sum(len(d['tables']) for d in documents)
    print(f'[done] {len(documents)} 个文档, {total_sec} 个章节, {total_tbl} 个表格 -> {args.out}')


if __name__ == '__main__':
    main()
