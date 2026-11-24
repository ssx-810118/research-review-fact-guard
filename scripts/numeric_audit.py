# -*- coding: utf-8 -*-
"""
numeric_audit.py — 数值程序化校验与跨源冲突检测（Skill 工作流第 2 步 / 自审策略组件）

输入：corpus.json（parse_materials.py 产物）
输出：conflicts.json，发现类别：
  NUMERIC_INCONSISTENCY  跨文档同指标数值不一致（归一化 + 上下文重合配对）
  QUALITATIVE_CONFLICT   同主题结论微冲突（对立线索词通用检测）
  TABLE_AGGREGATE_ERROR  汇总行/均值行复算不符（含“标签+数值”两列布局）
  RATIO_SUM_ERROR        配比/占比求和异常（列级与单元格内嵌两类）
  DERIVED_MISMATCH       派生倍数无法由表内原始列复现
  DUPLICATE_ENTITY       数据表主键重名但属性冲突
全部规则均为通用规则，不依赖具体材料内容。
"""
import argparse
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import dump_json, load_json, extract_numbers, split_sentences, approx_equal

METRIC_PATTERNS_BASE = [
    ('产业规模', r'(核心)?产业规模|相关产业规模'),
    ('市场规模', r'全球.{0,4}市场规模|市场规模|市场总量'),
    ('细分市场规模', r'细分.{0,4}市场|细分规模'),
    ('平台市场', r'平台(市场|支出)'),
    ('融资额', r'融资(金额|总额|事件)?|融资金额'),
    ('投资额', r'投资(总额|金额)?|私募投资|私募总量'),
    ('估值', r'估值|市值'),
    ('收入', r'年收入|营收|营业收入'),
    ('成本', r'成本|投入|费用|花费|开销'),
    ('调用量', r'调用量|日均调用量'),
    ('加速倍数', r'加速(比|倍数|效果)?|提速|速度(提升|加速)'),
    ('吞吐量', r'吞吐量'),
    ('利用率', r'利用率'),
    ('接受率', r'接受率'),
    ('成功率', r'成功率|完成率'),
    ('准确率', r'准确率|正确率'),
    ('困惑度', r'困惑度'),
    ('压缩比', r'压缩比'),
    ('精度保持', r'精度保持|精度损失'),
    ('能效比', r'能效比'),
    ('制程', r'制程'),
    ('延迟', r'延迟|时延|耗时'),
    ('占比', r'占比|份额|比重'),
    ('增速', r'同比(增速|增长)?|增速|增长率|增幅'),
    ('企业数量', r'企业(数量|总数)|服务商.{0,4}(数|突破)'),
    ('研发投入', r'研发投入'),
    ('市场预期', r'市场预期'),
    ('下载量', r'下载量'),
    ('专利数量', r'专利(申请)?量'),
    ('模型数量', r'模型.{0,4}数量|共发布'),
    # ——— 跨行业通用指标（放在具体指标之后，同位置命中时优先取上面的专用指标，
    #     以保证换用非 AI 领域材料时仍能归组比对，而不是一律退化为“数值:单位”）———
    ('规模', r'(?<!市场)(?<!产业)(?<!参数)(?<!模型)(?<!大)(?<!小)(?<!特定)规模(?!化)'),
    ('容量', r'容量|装机量|产能|产量|出货量|库存量'),
    ('数量', r'(?<![企事业])数量|总数|个数|家数|门店数|用户数|人数|台数'),
    ('单价', r'单价|均价|价格|报价|售价|票价'),
    ('渗透率', r'渗透率|覆盖率|普及率|国产化率|市占率'),
    ('损耗率', r'损耗率|良率|合格率|退货率|故障率|空置率|投诉率'),
    ('转化率', r'转化率|复购率|留存率|续费率|履约率'),
    ('面积', r'面积|里程|航程|运距'),
    ('能耗', r'能耗|碳排放|排放量|耗电量'),
    ('时长', r'时长|周期|工期|账期|周转天数'),
    ('满意度', r'满意度|净推荐值|好评率'),
]

METRIC_PATTERNS = METRIC_PATTERNS_BASE[:]


def build_dynamic_metrics(corpus):
    """从语料动态提取领域指标短语，补充 METRIC_PATTERNS_BASE。"""
    dynamic = []
    seen = {canon for canon, _ in METRIC_PATTERNS}
    for d in corpus['documents']:
        for t in d.get('tables', []):
            headers = t.get('headers', [])
            body = t.get('body_rows') or t.get('rows', [])
            for ci, h in enumerate(headers):
                h = str(h).strip()
                if not h or len(h) > 20 or h in seen:
                    continue
                nums = 0
                for r in body:
                    if ci < len(r):
                        try:
                            float(str(r[ci]).replace(',', '').replace('%', '').strip())
                            nums += 1
                        except (ValueError, TypeError):
                            pass
                if nums >= 2:
                    seen.add(h)
                    dynamic.append((h, re.escape(h)))
        for s in d['sections']:
            for sent in split_sentences(s['text']):
                for m in re.finditer(
                    r'([一-龥][一-龥A-Za-z0-9]{1,12})\s*(为|达|约|达到|增至|增长至|超过|突破|接近|高达)\s*\d',
                    sent
                ):
                    phrase = m.group(1)
                    if phrase not in seen and 2 <= len(phrase) <= 12:
                        seen.add(phrase)
                        dynamic.append((phrase, re.escape(phrase)))
    return dynamic

OPPOSE_CUES = ['并非', '不是', '误读', '被夸大', '夸大', '偏高', '偏低', '注水',
               '水分', '存疑', '争议', '远低于', '远高于', '打折扣', '泡沫',
               '误导', '理想条件', '特定条件', '需要审慎', '理性看待', '不是万能',
               '不可逆', '缓解', '实际加速', '实际吞吐', '实际生产', '实际规模',
               '现实', '达不到', '严重低估']

TIME_RE = re.compile(r'(20\d{2})\s*年')
STOP_TOKENS = set('的 了 在 是 为 与 和 及 对 将 已 也 都 就 中 上 下 可 该 这 那 '
                  '一个 一种 一个 进行 通过 根据 关于 以及 其中 数据 显示 表示 称 '
                  '实际 条件 公司 企业 模型 智能体 论文 技术 产品 行业 市场 规模 '
                  '中国 全球 预计 年 月 达到 约 达 超 近 突破 增长 同比 报告'.split())


def metric_of(text):
    for canon, pat in METRIC_PATTERNS:
        m = re.search(pat, text)
        if m:
            return canon, m.span()
    return None, None


def context_tokens(sent):
    raw = re.findall(r'[A-Za-z][A-Za-z0-9\-\.]{1,30}|[一-龥]{2,}', sent)
    toks = {t for t in raw if t not in STOP_TOKENS and len(t) >= 2}
    # 追加中文显著二元词，提升不同措辞下同主体的召回
    for run in re.findall(r'[一-龥]{3,}', sent):
        for k in range(len(run) - 1):
            bg = run[k:k + 2]
            if bg not in STOP_BIGRAM:
                toks.add(bg)
    return toks


STOP_BIGRAM = set('''市场 规模 数据 显示 表示 预计 预测 测算 中国 全球 全球 智能 能体 模型
技术 应用 行业 企业 公司 报告 论文 新闻 访谈 评论 实际 条件 理想 生产 环境 同比
增长 增长 达到 突破 超过 接近 约为 亿元 万亿 美元 人民币 相关 核心 产业 平台
系统 产品 方案 能力 场景 阶段 时间 水平 效果 性能 指标 平均 合计 统计 调研'''.split())


def subject_of(text, metric_span, num_span):
    anchor = re.findall(r'[A-Za-z][A-Za-z0-9\-\.]{1,30}', text)
    window = text[max(0, (metric_span[0] if metric_span else num_span[0]) - 20):
                  metric_span[0] if metric_span else num_span[0]]
    tokens = [t for t in re.split(r'[\s，。；、（）()的中将与和及对在为是达约]', window) if len(t) >= 2]
    subj = ''.join(tokens[-2:]) if tokens else ''
    latin = [a for a in anchor if a.upper() not in ('AI', 'LLM', 'GPU', 'API', 'CPU')]
    if latin:
        subj = (latin[-1] + subj)[:32]
    return subj or '未命名主体'


# 指标 -> 允许的归一化单位集合（None 表示不限制）
# 通用分类：金额类 / 百分比类 / 倍数类 / 计数类 / 调用量类
METRIC_UNITS = {
    '产业规模': {'CNY', 'WAN', 'YI'}, '市场规模': {'USD', 'CNY', 'WAN', 'YI'},
    '细分市场规模': {'CNY', 'WAN', 'YI'}, '平台市场': {'USD', 'CNY', 'WAN', 'YI'},
    '融资额': {'CNY', 'WAN', 'YI'}, '投资额': {'CNY', 'WAN', 'YI'},
    '估值': {'USD', 'CNY', 'WAN', 'YI'}, '收入': {'USD', 'CNY', 'WAN', 'YI'},
    '成本': {'USD', 'CNY', 'WAN', 'YI'}, '研发投入': {'CNY', 'WAN', 'YI'},
    '市场预期': {'CNY', 'WAN', 'YI'},
    '加速倍数': {'X'},
    '占比': {'PERCENT'}, '增速': {'PERCENT'}, '接受率': {'PERCENT'},
    '成功率': {'PERCENT'}, '准确率': {'PERCENT'}, '压缩比': {'PERCENT'},
    '精度保持': {'PERCENT'}, '利用率': {'PERCENT'},
    '调用量': {'TOKEN', 'WAN', 'YI', 'COUNT'},
    '下载量': {'COUNT', 'WAN', 'YI'},
    '企业数量': {'COUNT', 'WAN', 'YI'}, '模型数量': {'COUNT', 'WAN', 'YI'},
    '专利数量': {'COUNT', 'WAN', 'YI'},
    # 通用指标的单位相容性：单位不符则放弃该指标归属，避免“占比被绑到数量”
    '规模': {'CNY', 'USD', 'WAN', 'YI'},
    '容量': {'COUNT', 'WAN', 'YI'},
    '数量': {'COUNT', 'WAN', 'YI'},
    '单价': {'CNY', 'USD', 'WAN', 'YI'},
    '渗透率': {'PERCENT'}, '损耗率': {'PERCENT'}, '转化率': {'PERCENT'},
    '满意度': {'PERCENT', 'COUNT'},
    '面积': {'COUNT', 'WAN', 'YI'},
    '能耗': {'COUNT', 'WAN', 'YI', 'TON'},
}


def nearest_metric(sent, pos, lo=None, hi=None, unit=None):
    """返回支配该数值的指标词。

    中文数据叙述的指标通常前置（“市场规模达到 1200 亿元”），句尾另起的
    “同比增长 18.5%”属于下一个数值的指标。因此按三级优先取词：
      1) 数值左侧、且落在 (lo, hi) 数值间隙内的最近指标；
      2) 数值左侧、句子范围内的最近指标（跨逗号继承，如“市场规模增长 18%，
         达到 1200 亿元”的 1200 亿仍属市场规模）；
      3) 数值右侧、间隙内的最近指标（数字前无指标时的回退，如“共计 5 家企业”）。
    lo/hi 为候选指标的字符区间约束，用于避免把相邻数值的指标错绑过来；
    unit 给出数值单位，同层候选中优先选单位口径相容的指标（如百分比优先归“增速”）。
    """
    def in_gap(m):
        return (lo is None or m.start() >= lo) and (hi is None or m.end() <= hi)

    def unit_ok(canon):
        allowed = METRIC_UNITS.get(canon)
        return not allowed or unit is None or unit in allowed

    left_gap, left_all, right_gap = [], [], []
    for canon, pat in METRIC_PATTERNS:
        for m in re.finditer(pat, sent):
            if m.end() <= pos:
                d = pos - m.end()
                left_all.append((d, m.start(), canon))
                if in_gap(m):
                    left_gap.append((d, m.start(), canon))
            elif m.start() >= pos and in_gap(m):
                right_gap.append((m.start() - pos, m.start(), canon))
    for pool in (left_gap, left_all, right_gap):
        if not pool:
            continue
        compatible = [c for c in pool if unit_ok(c[2])]
        return min(compatible or pool, key=lambda x: (x[0], -x[1]))[2]
    return None


def nearest_year(sent, pos):
    """年份归属规则：优先取数字之前最近的年份（中文数据叙述的支配年份通常在前），
    无前导年份时才取之后最近的。"""
    before = [(pos - m.end(), m.group(1)) for m in TIME_RE.finditer(sent)
              if m.end() <= pos]
    after = [(m.start() - pos, m.group(1)) for m in TIME_RE.finditer(sent)
             if m.start() >= pos]
    if before:
        return min(before)[1]
    if after:
        return min(after)[1]
    return None


RANGE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:到|~|至|–|—|-)\s*(\d+(?:\.\d+)?)\s*'
                      r'(万亿|亿美元|万美元|万亿元|万元|亿元|万|亿|倍|%|％|GB|个|家)?')


def range_metric_map(sent):
    """返回 [(右端点起始, 右端点结束, 指标)]，区间右端点继承左端点的指标。"""
    out = []
    for m in RANGE_RE.finditer(sent):
        metric = nearest_metric(sent, m.start(1) + 0.5)
        out.append((m.start(2), m.end(2), metric))
    return out


def collect_facts(corpus):
    """仅从章节叙述文本抽取数值事实；表格数值交由 audit_tables 专规校验。
    指标取距数字最近的指标词，年份取最近年份；主体/上下文取局部窗口。"""
    facts = []
    for d in corpus['documents']:
        for s in d['sections']:
            locator = s['locator']
            for sent in split_sentences(s['text']):
                nums = extract_numbers(sent)
                if not nums:
                    continue
                rmap = range_metric_map(sent)
                ordered = sorted(nums, key=lambda x: x['span'][0])
                for idx, n in enumerate(ordered):
                    a, b = n['span']
                    mid = (a + b) / 2
                    start = max(0, a - 24)
                    window = sent[start:min(len(sent), b + 12)]
                    lo = ordered[idx - 1]['span'][1] if idx else 0
                    hi = (ordered[idx + 1]['span'][0]
                          if idx + 1 < len(ordered) else len(sent))
                    inherited = [mt for (rs, re_, mt) in rmap if rs == a]
                    metric = inherited[0] if inherited else nearest_metric(
                        sent, mid, lo=lo, hi=hi, unit=n['unit'])
                    # 区间继承得到空指标时，回退到间隙/句内最近指标，避免整条被丢弃
                    if metric is None and inherited:
                        metric = nearest_metric(sent, mid, lo=lo, hi=hi,
                                                unit=n['unit'])
                    allowed = METRIC_UNITS.get(metric)
                    if allowed and n['unit'] not in allowed:
                        metric = None
                    mspan = None
                    mm = metric and re.search(dict(METRIC_PATTERNS).get(metric, '$^'), window)
                    if mm:
                        mspan = mm.span()
                    facts.append({
                        'doc': d['name'], 'doc_type': d['type'],
                        'locator': locator, 'sentence': sent,
                        'metric': metric or ('数值:' + n['unit']),
                        'subject': subject_of(window, mspan, (a - start, b - start))
                                   if metric else '',
                        'tokens': context_tokens(window),
                        'value': n['canonical'], 'raw': n['raw'],
                        'unit': n['unit'], 'modifier': n['modifier'],
                        'year': nearest_year(sent, mid),
                    })
    return facts


# 天然随个体变化、不做跨源一致性比较的指标
PER_OBJECT_METRICS = {'占比', '增速', '准确率', '成功率', '压缩比', '精度保持',
                      '能效比', '制程', '利用率', '延迟', '困惑度',
                      '接受率', '研发投入', '市场预期', '吞吐量',
                      '下载量', '专利数量', '企业数量', '模型数量'}


def _anchors(sent):
    return {x.lower() for x in re.findall(r'[A-Za-z][A-Za-z0-9\-\.]{2,}', sent)}


def detect_numeric_conflicts(facts, min_spread=0.12):
    """同指标/单位/年份分组内，并查集聚类同主体事实；跨文档且离散度超阈值即冲突。
    min_spread 为离散度阈值（默认 12%），不同领域可下调以捕捉更小的口径差。"""
    groups = defaultdict(list)
    for f in facts:
        if f['metric'].startswith('数值:') or f['metric'] in PER_OBJECT_METRICS:
            continue
        groups[(f['metric'], f['unit'], f['year'] or '')].append(f)

    conflicts = []
    for (metric, unit, year), items in groups.items():
        n = len(items)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        for i in range(n):
            for j in range(i + 1, n):
                a, b = items[i], items[j]
                anchor_hit = bool(_anchors(a['sentence']) & _anchors(b['sentence']))
                shared = a['tokens'] & b['tokens']
                # 同文档：锚点一致或局部上下文高度重合才合并；
                # 跨文档：锚点一致（型号/机构名）或 >=3 个显著词重合。
                need = 1 if anchor_hit else (3 if a['doc'] == b['doc'] else 3)
                if anchor_hit or len(shared) >= need:
                    union(i, j)

        comps = defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)
        for _, idxs in comps.items():
            pts = [items[i] for i in idxs]
            docs = {p['doc'] for p in pts}
            vals = [p['value'] for p in pts if p['value'] and p['value'] > 0]
            if len(docs) < 2 or len(vals) < 2:
                continue
            spread = (max(vals) - min(vals)) / max(vals)
            if spread <= min_spread:
                continue
            subjects = {p['subject'] for p in pts if p['subject']}
            conflicts.append({
                'type': 'NUMERIC_INCONSISTENCY',
                'topic': metric + (f'（{year}年）' if year else ''),
                'subject': ' / '.join(sorted(subjects))[:40],
                'severity': '高' if spread > 0.5 else ('中' if spread > 0.25 else '低'),
                'spread': round(spread, 3),
                'threshold': min_spread,
                'values': [{'doc': p['doc'], 'locator': p['locator'],
                            'raw': p['raw'], 'value': p['value'],
                            'sentence': p['sentence'][:200]} for p in pts],
            })
    conflicts.sort(key=lambda c: {'高': 0, '中': 1, '低': 2}[c['severity']])

    # 同主题多值面板（同一 metric+year 下全部来源，供综述“分歧分析”引用）
    panels = []
    merged = defaultdict(list)
    for f in facts:
        if f['metric'].startswith('数值:') or f['metric'] in PER_OBJECT_METRICS:
            continue
        merged[(f['metric'], f['unit'], f['year'] or '')].append(f)
    for (metric, unit, year), fs in merged.items():
        docs = {x['doc'] for x in fs}
        if len(docs) < 2:
            continue
        vals = [x['value'] for x in fs if x['value']]
        if vals and max(vals) > 0 and (max(vals) - min(vals)) / max(vals) > min_spread:
            panels.append({'metric': metric, 'unit': unit, 'year': year,
                           'points': [{'doc': x['doc'], 'locator': x['locator'],
                                       'raw': x['raw']} for x in fs]})
    return conflicts, panels


def detect_qualitative_conflicts(corpus):
    sents = []
    for d in corpus['documents']:
        for s in d['sections']:
            for sent in split_sentences(s['text']):
                if len(sent) < 12:
                    continue
                metric, _ = metric_of(sent)
                sents.append({'doc': d['name'], 'locator': s['locator'],
                              'sentence': sent, 'metric': metric,
                              'tokens': context_tokens(sent),
                              'opposed': any(c in sent for c in OPPOSE_CUES)})
    out, used = [], set()
    for a in sents:
        if not a['opposed'] or not a['metric']:
            continue
        for b in sents:
            if b['doc'] == a['doc'] or not b['metric']:
                continue
            if a['metric'] != b['metric'] and a['metric'] not in ('加速倍数', '利用率'):
                continue
            overlap = (a['tokens'] & b['tokens'])
            anchor_a = set(re.findall(r'[A-Za-z][A-Za-z0-9\-\.]{2,}', a['sentence']))
            anchor_b = set(re.findall(r'[A-Za-z][A-Za-z0-9\-\.]{2,}', b['sentence']))
            if len(overlap) < 2 and not (anchor_a & anchor_b):
                continue
            sig = tuple(sorted([a['doc'] + a['locator'][:8], b['doc'] + b['locator'][:8]])) + (a['metric'],)
            if sig in used:
                continue
            used.add(sig)
            out.append({
                'type': 'QUALITATIVE_CONFLICT',
                'topic': a['metric'],
                'severity': '中',
                'positions': [
                    {'doc': b['doc'], 'locator': b['locator'], 'sentence': b['sentence'][:220]},
                    {'doc': a['doc'], 'locator': a['locator'], 'sentence': a['sentence'][:220]},
                ],
            })
            break
    return out


# 汇总行标签（与 parse_materials 的拆分规则一致，用于旧语料兜底）
try:
    from parse_materials import _is_summary_label
except Exception:  # 独立运行时的兜底
    _SUMMARY_LABEL = re.compile(
        r'^(合计|总计|小计|汇总|总额|总值|总数|平均值|平均|均值|累计|全部|总体)$')

    def _is_summary_label(cell, headers=()):
        return bool(_SUMMARY_LABEL.match(
            re.sub(r'[（(][^）)]*[）)]', '', str(cell)).strip()))


def to_number(s):
    s = str(s).replace(',', '').replace('%', '').strip()
    s = re.sub(r'(x|X|倍|ms|GB|TOPS/W|TOPS|nm)$', '', s).strip()
    return float(s) if re.match(r'^-?\d+(?:\.\d+)?$', s) else None


def audit_tables(corpus):
    findings, passed = [], []
    for d in corpus['documents']:
        for t in d['tables']:
            headers = t['headers']
            body = t.get('body_rows') or t['rows']
            summary = t.get('summary_rows', [])
            # 兼容旧版 corpus.json：未做明细/汇总拆分时按首列标签就地拆分，
            # 否则 Word/PDF/Markdown 表格的汇总复算会被整体跳过。
            if not t.get('body_rows') and not summary and t.get('rows'):
                summary = [r for r in t['rows']
                           if r and _is_summary_label(r[0], headers)]
                if summary:
                    body = [r for r in t['rows'] if r not in summary]
            if not body:
                continue
            ncol = len(headers)
            numeric_cols = []
            for ci in range(ncol):
                vals = [to_number(r[ci]) for r in body if ci < len(r)]
                vals = [v for v in vals if v is not None]
                if vals and len(vals) >= max(3, len(body) * 0.6):
                    numeric_cols.append((ci, vals))

            # 1) 常规汇总行（汇总值与明细列对齐）
            for sr in summary:
                label = ''.join(str(x) for x in sr[:2])
                for ci, vals in numeric_cols:
                    if ci >= len(sr):
                        continue
                    claimed = to_number(sr[ci])
                    if claimed is None:
                        continue
                    total, avg = sum(vals), sum(vals) / len(vals)
                    ok, expect = None, None
                    if re.search(r'(总|合计|总计|总额)', label):
                        ok, expect = approx_equal(claimed, total, 0.01), total
                    elif re.search(r'(平均|均值)', label):
                        ok, expect = approx_equal(claimed, avg, 0.01), avg
                    if ok is False:
                        findings.append({'type': 'TABLE_AGGREGATE_ERROR', 'doc': d['name'],
                                         'locator': t['locator'], 'column': headers[ci],
                                         'claimed': claimed, 'recomputed': round(expect, 2),
                                         'detail': f'汇总行“{label}”的 {headers[ci]} 声称 {claimed}，按 {len(vals)} 条明细复算为 {round(expect, 2)}'})

            # 1b) “标签在首列、数值在次列”的汇总行（常见调研表布局）
            def _core(s):
                return re.sub(r'(平均|均值|总额|总和|合计|总计|总数|总|年|万元|亿元|%|'
                              r'[\(\)（）\s])', '', str(s))
            for sr in summary:
                label = str(sr[0])
                claimed = to_number(sr[1]) if len(sr) > 1 else None
                if claimed is None:
                    continue
                expect, kind = None, None
                if re.search(r'总(企业数|数|数量|计数|记录)', label):
                    expect, kind = float(len(body)), '明细记录数'
                else:
                    is_avg = bool(re.search(r'平均|均值', label))
                    is_sum = bool(re.search(r'总额|总和|合计|总计', label))
                    lcore = _core(label)
                    for ci, h in enumerate(headers):
                        hcore = _core(h)
                        if len(hcore) < 2 or hcore not in lcore:
                            continue
                        vals = [to_number(r[ci]) for r in body if ci < len(r)]
                        vals = [v for v in vals if v is not None]
                        if not vals:
                            continue
                        if is_avg:
                            expect, kind = sum(vals) / len(vals), str(h) + '均值'
                            break
                        if is_sum:
                            expect, kind = sum(vals), str(h) + '合计'
                            break
                if expect is not None and not approx_equal(claimed, expect, 0.01):
                    findings.append({'type': 'TABLE_AGGREGATE_ERROR', 'doc': d['name'],
                                     'locator': t['locator'], 'column': kind,
                                     'claimed': claimed, 'recomputed': round(expect, 2),
                                     'detail': f'汇总项“{label}”声称 {claimed}，复算{kind}为 {round(expect, 2)}'})
                elif expect is not None:
                    passed.append({'type': 'TABLE_AGGREGATE_OK', 'doc': d['name'],
                                   'locator': t['locator'], 'column': kind,
                                   'value': claimed,
                                   'detail': f'汇总项“{label}”={claimed} 与复算{kind} {round(expect, 2)} 一致'})

            # 2a) 列级占比/配比求和
            pct_cols = [ci for ci, h in enumerate(headers) if re.search(r'(占比|配比|比例|%|％)', str(h))]
            if 2 <= len(pct_cols) <= 4:
                for r in body:
                    vals = [to_number(r[ci]) for ci in pct_cols]
                    if all(v is not None for v in vals) and len(vals) >= 3:
                        s = sum(vals)
                        if abs(s - 100) > 1.5:
                            findings.append({'type': 'RATIO_SUM_ERROR', 'doc': d['name'],
                                             'locator': f'{t["locator"]}，{r[-1]}',
                                             'values': dict(zip([str(headers[ci]) for ci in pct_cols], vals)),
                                             'sum': s,
                                             'detail': f'{r[1] if len(r) > 1 else ""} 的 {len(vals)} 项配比求和为 {s}%，不等于 100%'})

            # 2b) 单元格内嵌百分比三元组（如“语言70%, 视觉理解26%, 生成6%”）
            for r in body:
                for ci, cell in enumerate(r[:-1]):
                    pms = re.findall(r'([一-龥A-Za-z]{2,8}?)\s*(\d+(?:\.\d+)?)\s*[%％]', str(cell))
                    if len(pms) >= 3:
                        labels = [p[0] for p in pms]
                        vals = [float(p[1]) for p in pms]
                        context_ok = (re.search(r'(配比|占比|比例)', str(headers[ci]) + str(cell))
                                      or (len(set(labels)) == len(labels)))
                        if context_ok and abs(sum(vals) - 100) > 1.5:
                            findings.append({'type': 'RATIO_SUM_ERROR', 'doc': d['name'],
                                             'locator': f'{t["locator"]}，{r[-1]}',
                                             'entity': r[1] if len(r) > 1 else '',
                                             'values': dict(zip(labels, vals)),
                                             'sum': sum(vals),
                                             'detail': f'{r[1] if len(r) > 1 else ""} 的内嵌配比 {cell} 求和为 {sum(vals)}%，不等于 100%'})

            # 3) 派生倍数列复算
            ratio_col = next((ci for ci, h in enumerate(headers)
                              if re.search(r'(加速比|提升倍数|倍数)', str(h))), None)
            if ratio_col is not None:
                for r in body:
                    rv = to_number(r[ratio_col])
                    cands = [(ci, to_number(r[ci])) for ci in range(ratio_col)
                             if ci < len(r) and to_number(r[ci]) is not None]
                    if rv is None or len(cands) < 2:
                        continue
                    found = False
                    found_pair = None
                    for i in range(len(cands)):
                        for j in range(i + 1, len(cands)):
                            a, b = cands[i][1], cands[j][1]
                            if a and b and approx_equal(max(a, b) / min(a, b), rv, 0.02):
                                found = True
                                found_pair = (headers[cands[i][0]], headers[cands[j][0]],
                                              round(max(a, b) / min(a, b), 3))
                    ent = r[1] if len(r) > 1 else ''
                    loc = '%s，%s' % (t['locator'], r[-1])
                    if found:
                        passed.append({'type': 'DERIVED_OK', 'doc': d['name'],
                                       'locator': loc,
                                       'detail': '%s 派生倍数 %s 可由 %s/%s 复现（%s）'
                                                 % (ent, rv, found_pair[0], found_pair[1],
                                                    found_pair[2])})
                    if not found:
                        findings.append({'type': 'DERIVED_MISMATCH', 'doc': d['name'],
                                         'locator': loc,
                                         'claimed_ratio': rv,
                                         'detail': '%s 表内派生倍数 %s 无法由同行原始列复现'
                                                   % (ent, rv)})

            # 4) 主键重名但属性冲突
            if body:
                key_col = 1 if ncol > 1 else 0
                groups = defaultdict(list)
                for r in body:
                    if len(r) > key_col:
                        groups[r[key_col]].append(r)
                for name, rs in groups.items():
                    if len(rs) > 1:
                        diff_cols = [headers[ci] for ci in range(min(ncol, len(rs[0])))
                                     if len({str(r[ci]) for r in rs if ci < len(r)}) > 1]
                        if diff_cols:
                            findings.append({'type': 'DUPLICATE_ENTITY', 'doc': d['name'],
                                             'locator': t['locator'], 'entity': name,
                                             'rows': [r[-1] for r in rs],
                                             'conflict_fields': [str(x) for x in diff_cols],
                                             'detail': f'主键“{name}”出现 {len(rs)} 次（{"、".join(r[-1] for r in rs)}），字段冲突：{"、".join(map(str, diff_cols))}'})
    return findings, passed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('corpus', help='corpus.json')
    ap.add_argument('-o', '--out', default='conflicts.json')
    ap.add_argument('--min-spread', type=float, default=0.12,
                    help='跨源数值冲突的离散度阈值，默认 0.12')
    args = ap.parse_args()
    corpus = load_json(args.corpus)

    dynamic = build_dynamic_metrics(corpus)
    METRIC_PATTERNS.extend(dynamic)

    facts = collect_facts(corpus)
    numeric, panels = detect_numeric_conflicts(facts, args.min_spread)
    qualitative = detect_qualitative_conflicts(corpus)
    table_f, table_ok = audit_tables(corpus)
    result = {
        'fact_count': len(facts),
        'numeric_conflicts': numeric,
        'numeric_panels': panels,
        'qualitative_conflicts': qualitative,
        'table_findings': table_f,
        'table_checks_passed': table_ok,
        'stats': {
            'NUMERIC_INCONSISTENCY': len(numeric),
            'QUALITATIVE_CONFLICT': len(qualitative),
            'table_findings': len(table_f),
            'table_checks_passed': len(table_ok),
            'by_table_type': {k: sum(1 for x in table_f if x['type'] == k)
                              for k in ('TABLE_AGGREGATE_ERROR', 'RATIO_SUM_ERROR',
                                        'DERIVED_MISMATCH', 'DUPLICATE_ENTITY')},
        },
    }
    dump_json(result, args.out)
    print(f"[done] 阈值 {args.min_spread:.2f}；数值事实 {len(facts)}；数值冲突 {len(numeric)}；多值面板 {len(panels)}；"
          f"结论冲突 {len(qualitative)}；表格异常 {len(table_f)}；表格校验通过 {len(table_ok)} -> {args.out}")


if __name__ == '__main__':
    main()
