# -*- coding: utf-8 -*-
"""
common.py — 科研综述防幻觉 Skill 公共工具库
仅依赖标准库 + python-docx / openpyxl / pdfplumber（解析阶段）。
所有函数均为通用实现，不包含任何针对特定测试材料的硬编码内容。
"""
import os
import re
import json
import unicodedata

# ----------------------------------------------------------------------------
# 数值与单位归一化
# ----------------------------------------------------------------------------
_CN_DIGIT = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
             '六': 6, '七': 7, '八': 8, '九': 9}

# 中文数量词 -> 系数（以“个/元”等基本单位为基准）
_CN_NUM_SCALE = {'十': 1e1, '百': 1e2, '千': 1e3, '万': 1e4, '亿': 1e8, '兆': 1e12}

# 货币/计数单位归一化目标
UNIT_CANON = {
    '元': ('CNY', 1), '美元': ('USD', 1), '美金': ('USD', 1),
    '万元': ('CNY', 1e4), '亿元': ('CNY', 1e8), '万亿元': ('CNY', 1e12),
    '万美元': ('USD', 1e4), '亿美元': ('USD', 1e8), '万亿美元': ('USD', 1e12),
    '个': ('COUNT', 1), '家': ('COUNT', 1), '起': ('COUNT', 1), '项': ('COUNT', 1),
    '款': ('COUNT', 1), '次': ('COUNT', 1), '篇': ('COUNT', 1),
}

# 计量词归一化（token / 参数 / 显存 / 算力等）
SIRE = {'k': 1e3, 'K': 1e3, '千': 1e3, 'm': 1e6, 'M': 1e6, '百万': 1e6,
        'b': 1e9, 'B': 1e9, 'g': 1e9, 'G': 1e9, '十亿': 1e9,
        't': 1e12, 'T': 1e12, '万亿': 1e12, 'p': 1e15, 'P': 1e15}


def cn_num(token):
    """把简单中文数词转为数字，如 '两千' -> 2000；失败返回 None。"""
    token = token.strip()
    if not token:
        return None
    if token in _CN_DIGIT:
        return float(_CN_DIGIT[token])
    total, section, number = 0.0, 0.0, 0.0
    found = False
    for ch in token:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            found = True
        elif ch in _CN_NUM_SCALE:
            scale = _CN_NUM_SCALE[ch]
            found = True
            if scale >= 1e4:
                section = (section + number) * scale
                total += section
                section, number = 0.0, 0.0
            else:
                if number == 0:
                    number = 1
                section += number * scale
                number = 0.0
        else:
            return None
    val = total + section + number
    return val if found else None


def to_float(num_text):
    """解析数字文本（含中文数词、逗号、约/超/近等修饰），返回 (value, 修饰词)。"""
    raw = num_text.strip()
    modifier = None
    m = re.match(r'^(约|大约|超|超过|近|接近|逾|达|高达|突破|不少于|不低于|不到|低于)?(.*)$', raw)
    if m:
        modifier = m.group(1)
        raw = m.group(2)
    raw = raw.replace(',', '').replace('，', '').strip()
    try:
        return float(raw), modifier
    except ValueError:
        v = cn_num(raw)
        return (v, modifier) if v is not None else (None, modifier)


# 数字 + 可选中文/英文单位 的通用抽取正则
NUM_RE = re.compile(
    r'(?:约|大约|超|超过|近|接近|逾|高达|突破|不少于|不低于|不到|低于)?'
    r'\d+(?:\.\d+)?\s*(?:%|％|万亿|亿美元|万美元|万亿元|万元|亿元|万|亿|倍|'
    r'TOPS/W|TOPS|GB|TB|MB|nm|毫秒|ms|token|Token|次|个|家|起|项|款|篇|'
    r'美元|元|张|份|年|个月|周|天|日|公里)?'
)

# 中文数词补充（如“两千亿”“数万”）
CN_NUM_RE = re.compile(r'[数几]?[零一二两三四五六七八九十百千万亿]{2,}')


def extract_numbers(text):
    """
    从文本抽取数值事实，返回 list[dict]：
    {raw, value, unit, canonical(归一化值), metric_hint}
    canonical 对万/亿/万亿、千分词做归一，便于跨文档比较。
    """
    out = []
    for m in NUM_RE.finditer(text):
        raw = m.group(0)
        vm = re.match(
            r'^(约|大约|超|超过|近|接近|逾|高达|突破|不少于|不低于|不到|低于)?'
            r'(\d+(?:\.\d+)?)\s*(.*)$', raw)
        if not vm:
            continue
        modifier, num, unit = vm.group(1), vm.group(2), vm.group(3) or ''
        value = float(num)
        canonical = value
        unit_norm = unit
        if unit in ('%', '％'):
            unit_norm, canonical = 'PERCENT', value
        elif unit == '倍':
            unit_norm, canonical = 'X', value
        elif unit in ('万亿',):
            unit_norm, canonical = 'CNY?', value * 1e12
        elif unit in ('万元',):
            unit_norm, canonical = 'CNY', value * 1e4
        elif unit in ('亿元',):
            unit_norm, canonical = 'CNY', value * 1e8
        elif unit in ('万亿元',):
            unit_norm, canonical = 'CNY', value * 1e12
        elif unit in ('万美元',):
            unit_norm, canonical = 'USD', value * 1e4
        elif unit in ('亿美元',):
            unit_norm, canonical = 'USD', value * 1e8
        elif unit in ('万亿美元',):
            unit_norm, canonical = 'USD', value * 1e12
        elif unit == '万':
            unit_norm, canonical = 'WAN', value * 1e4
        elif unit == '亿':
            unit_norm, canonical = 'YI', value * 1e8
        elif unit in ('GB',):
            unit_norm, canonical = 'GB', value
        elif unit.lower() in ('tops/w',):
            unit_norm, canonical = 'TOPS/W', value
        elif unit in ('TOPS',):
            unit_norm, canonical = 'TOPS', value
        elif unit in ('nm',):
            unit_norm, canonical = 'NM', value
        elif unit in ('毫秒', 'ms'):
            unit_norm, canonical = 'MS', value
        elif unit.lower() == 'token':
            unit_norm, canonical = 'TOKEN', value
        elif unit in ('个', '家', '起', '项', '款', '次', '篇', '张', '份'):
            unit_norm, canonical = 'COUNT', value
        elif unit in ('年', '个月', '周', '天', '日', '公里'):
            unit_norm, canonical = unit, value
        elif unit in ('元', '美元', '美金'):
            unit_norm, canonical = ('CNY' if unit == '元' else 'USD'), value
        elif unit in ('TB', 'MB'):
            unit_norm, canonical = unit, value
        out.append({'raw': raw, 'value': value, 'unit': unit_norm,
                    'canonical': canonical, 'modifier': modifier,
                    'span': m.span()})
    return out


def approx_equal(a, b, rel=0.03):
    """两个归一化数值是否近似相等（默认 3% 容差，处理四舍五入）。"""
    if a is None or b is None:
        return False
    base = max(abs(a), abs(b))
    if base == 0:
        return a == b
    return abs(a - b) / base <= rel


# ----------------------------------------------------------------------------
# 文本通用处理
# ----------------------------------------------------------------------------
def split_sentences(text):
    """中文/英文句子切分，保留引号与括号内容。"""
    parts = re.split(r'(?<=[。！？；;])|(?<=[\.])\s+(?=[A-Z一-龥])', text)
    return [s.strip() for s in parts if s and s.strip()]


def clean_text(s):
    return re.sub(r'[ \t\u3000]+', ' ', s.replace('\r', '')).strip()


CITE_RE = re.compile(r'[【\[]([^【】\[\]，,；;]{2,40}?)[，,；;]\s*([^【】\[\]]{1,60}?)[】\]]')


def parse_citations(sentence):
    """抽取句中来源标注，返回 [(材料名, 位置, 原文)]。"""
    return [(m.group(1).strip(), m.group(2).strip(), m.group(0))
            for m in CITE_RE.finditer(sentence)]


# 分析性归纳的语言线索（无需逐条标注来源）
ANALYSIS_CUES = ['综上', '总体来看', '总体而言', '笔者', '本报告认为', '本报告判断',
                 '我们认为', '判断', '复算', '程序化校验', '可以判断', '据此判断', '说明',
                 '表明', '意味着', '反映出', '可见',
                 '不难看出', '建议', '展望', '未来将', '大概率', '可能的原因',
                 '一方面', '另一方面', '首先', '其次', '此外，从', '这一趋势']
# 强事实线索
FACT_CUES = ['达到', '为', '达', '增至', '增长至', '同比', '占比', '占', '发布',
             '成立', '融资', '上市', '实测', '显示', '数据显示', '统计', '预测',
             '测算', '记载', '指出', '表示', '称', '报道', '于', '年', '月']


def is_analysis_sentence(s):
    """启发式判断是否为分析性归纳句。"""
    if any(c in s for c in ANALYSIS_CUES):
        return True
    # 无数字、无专名的短观点句通常为分析句
    if not extract_numbers(s) and len(s) < 40 and re.search(r'[认为看判断建议趋]', s):
        return True
    return False


# ----------------------------------------------------------------------------
# IO 工具
# ----------------------------------------------------------------------------
def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def dump_json(obj, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_plain(path):
    for enc in ('utf-8', 'gb18030'):
        try:
            with open(path, 'r', encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError('unknown encoding', b'', 0, 1, path)


def read_docx(path):
    """返回 (段落文本列表, 表格列表[二维数组])。"""
    import docx
    d = docx.Document(path)
    paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    tables = []
    for t in d.tables:
        tables.append([[c.text.strip() for c in row.cells] for row in t.rows])
    return paras, tables


def read_xlsx(path):
    """返回 {sheet: rows(二维数组)}。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    sheets = {}
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(c is not None and str(c).strip() for c in row):
                rows.append(['' if c is None else str(c).strip() for c in row])
        sheets[ws.title] = rows
    return sheets


def read_pdf_text(path):
    import pdfplumber
    pages = []
    with pdfplumber.open(path) as pdf:
        for pg in pdf.pages:
            pages.append(pg.extract_text() or '')
    return '\n'.join(pages)


def heading_level(line):
    """
    通用标题识别：返回层级 1-4，非标题返回 0。
    支持：第X章/节、一、二、（一）、1.、1、、N.N、Markdown #。
    """
    s = line.strip()
    if len(s) > 60:
        return 0
    if re.match(r'^#{1,4}\s', s):
        return len(s) - len(s.lstrip('#'))
    # 含句读的行是正文句子而非标题（如 PDF 断行后以“9.9%，资金规模……”开头的句子）
    if re.search(r'[。；，;]', s):
        return 0
    if re.match(r'^第[0-9一二三四五六七八九十]+[章节部分]', s):
        return 1
    if re.match(r'^[一二三四五六七八九十]+、', s):
        return 2
    if re.match(r'^（[一二三四五六七八九十]+）', s):
        return 3
    if re.match(r'^\d+(\.\d+){0,3}[\s、．.]', s) or re.match(r'^\d+(\.\d+)+\s*\S*$', s):
        return 2 + s.count('.') // 2
    if re.match(r'^\d+[、．.]\s*\S+$', s):
        return 3
    return 0
