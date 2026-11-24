# -*- coding: utf-8 -*-
"""
audit_review.py — 事实一致性独立审计器（Skill 工作流第 4 步 / “双主体独立审计”的审计主体）

关键设计：审计阶段只接收【待审草稿 + 原始语料 + 程序化冲突清单】，
不接触生成主体的思路与模型先验，保证生成—审计信息隔离。

输入：draft.md（综述草稿）、corpus.json、conflicts.json
输出：audit.json（逐条主张审计明细 + 忠实度）、audit_report.md（人读报告）
检查项：
  SOURCE_OK / SOURCE_NOT_FOUND      来源材料是否存在（别名归一）
  LOCATOR_OK / LOCATOR_NOT_FOUND    具体位置是否可回查
  NUMBER_MATCH / NUMBER_MISMATCH    数值是否可在被引位置复现（容差 3%，支持派生值复算）
  MISATTRIBUTED                     数值在被引来源缺失、却出现在其他来源（张冠李戴）
  MISSING_CITATION                  事实性陈述无来源标注
  ALLEGATION                        传闻/指控类表述被写成确定事实
  CONFLICT_UNADDRESSED             引用了存在分歧的主题但未作分歧分析
忠实度 = 受支持事实性主张数 / 事实性主张总数
"""
import argparse
import json
import os
import re
import sys
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (dump_json, load_json, extract_numbers, split_sentences,
                    parse_citations, is_analysis_sentence, approx_equal, CITE_RE)

# 材料名别名（通用规则：去后缀 + 类目短名；命中多个时按最高相似度消歧）
ALIAS_RULES = [
    (r'白皮书', '白皮书'), (r'论文摘要|论文集|摘要集', '论文摘要'),
    (r'行业新闻|新闻集|新闻', '新闻'), (r'专家访谈|访谈记录|访谈', '访谈'),
    (r'行业评论|评论集|评论', '评论'), (r'技术评测|评测报告|评测', '评测'),
    (r'调研数据|数据表', '数据'),
]

def norm(s):
    return re.sub(r'[\s，。、“”"\-—·：:；;（）()\[\]【】]', '', str(s))


def build_doc_index(corpus):
    docs = {}
    for d in corpus['documents']:
        docs[d['name']] = d
    return docs


def resolve_doc(name, docs):
    """来源材料名归一，返回 (doc_or_None, method)。"""
    if name in docs:
        return docs[name], 'exact'
    n = norm(name)
    # 类目短名
    for pat, key in ALIAS_RULES:
        if re.search(pat, name):
            cands = [d for nm, d in docs.items() if key in norm(nm)]
            if len(cands) == 1:
                return cands[0], 'alias'
            if cands:
                best = max(cands, key=lambda d: SequenceMatcher(
                    None, n, norm(d['name'])).ratio())
                return best, 'alias_fuzzy'
    # 模糊包含
    cands = [d for nm, d in docs.items() if n and (n in norm(nm) or norm(nm) in n)]
    if len(cands) == 1:
        return cands[0], 'contains'
    if cands:
        best = max(cands, key=lambda d: SequenceMatcher(
            None, n, norm(d['name'])).ratio())
        return best, 'fuzzy'
    return None, 'not_found'


def doc_full_text(d):
    parts = [s['text'] for s in d['sections']]
    for t in d['tables']:
        for r in t.get('all_rows', []):
            parts.append(' '.join(map(str, r)))
        parts.append(t['locator'])
    return '\n'.join(parts)


CN_DIGIT = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
            '八': 8, '九': 9, '十': 10}
ITEM_HEAD_RE = re.compile(r'(论文\s*[A-Z]|新闻\s*[一二三四五六七八九十]+|'
                          r'评论\s*[一二三四五六七八九十]+|访谈\s*[一二三四五六七八九十]+|'
                          r'第[一二三四五六七八九十0-9]+章)')
GENERIC_HEADS = {'访谈内容', '正文', '内容', '讨论内容', '问答实录', '访谈实录', '续', '（续）'}


def _table_ids(text):
    ids = set()
    for m in re.finditer(r'表\s*([0-9]+|[一二三四五六七八九十]+)', str(text)):
        x = m.group(1)
        ids.add(int(x) if x.isdigit() else CN_DIGIT.get(x, -1))
    return ids


def resolve_locator(doc, loc):
    """在文档内回查定位（章节/整章范围/编号条目/表号/行号/汇总行），返回命中片段列表。"""
    if doc is None:
        return []
    nloc = norm(loc)
    secs = doc['sections']
    idxs = set()
    # 1) 前言/说明类定位 → 首个章节
    if re.search(r'(评测说明|前言|概述|简介|开篇|开头|总体说明)', loc):
        idxs.add(0)
    # 2) 第X章：命中章节并向后展开到下一章
    mch = re.search(r'第([一二三四五六七八九十0-9]+)章', loc)
    if mch:
        start = None
        for i, s in enumerate(secs):
            if re.sub(r'\s', '', s['heading']).startswith(mch.group(0)):
                start = i
                break
        if start is not None:
            idxs.add(start)
            for j in range(start + 1, len(secs)):
                if re.search(r'第[一二三四五六七八九十0-9]+章', secs[j]['heading']):
                    break
                # 章节正文可能被解析器切成多个续接段，整章范围全部纳入
                idxs.add(j)
    # 3) 编号条目（论文A/新闻三/评论一/访谈二）：条目段 + 紧随的通用正文段
    mitem = re.match(r'^(论文\s*[A-Z]|新闻\s*[一二三四五六七八九十]+|'
                     r'评论\s*[一二三四五六七八九十]+|访谈\s*[一二三四五六七八九十]+)',
                     loc.replace(' ', ''))
    if mitem:
        key = norm(mitem.group(1))
        for i, s in enumerate(secs):
            hn = norm(s['heading'])
            if key in hn or hn.startswith(key):
                idxs.add(i)
                for j in range(i + 1, len(secs)):
                    hj = secs[j]['heading'].strip()
                    if ITEM_HEAD_RE.search(hj):
                        break
                    if hj in GENERIC_HEADS or (not re.search(r'[0-9一二三四五六七八九十]+[、.．]', hj)
                                               and len(secs[j]['text']) > 0):
                        idxs.add(j)
                    else:
                        break
    # 4) 普通章节标题包含匹配（如 6.1 推理优化技术 / 1.2 评测结果）
    for i, s in enumerate(secs):
        hn = norm(s['heading'])
        if hn and len(hn) >= 3 and (hn in nloc or nloc in hn):
            idxs.add(i)
    hits = [secs[i]['text'] for i in sorted(idxs)]
    # 5) 表号定位（表1/表一，兼容“第N页·表1”等定位串）
    want_tids = _table_ids(loc)
    for t in doc['tables']:
        tids = _table_ids(t.get('locator', '')) | _table_ids(t.get('caption', ''))
        if want_tids & tids:
            for r in t.get('all_rows', []):
                hits.append(' '.join(map(str, r)))
        # 行号定位
        mrow = re.search(r'第?\s*(\d+)\s*行', loc)
        if mrow:
            want = int(mrow.group(1))
            for r in t.get('all_rows', []):
                if r and str(r[-1]) == f'第{want}行':
                    hits.append(' '.join(map(str, r)))
        # 汇总行定位
        if '汇总' in loc or '合计' in loc or '平均' in loc or '总额' in loc:
            for r in t.get('all_rows', []):
                if r and any(k in str(r[0]) for k in
                             ('汇总', '合计', '平均', '总额', '总企业', '总计')):
                    hits.append(' '.join(map(str, r)))
    return hits


def source_numbers(text):
    return {n['canonical']: n for n in extract_numbers(text)}


def number_matched(value, src_nums, claim_raw, rel=0.03):
    for sv in src_nums:
        if approx_equal(value, sv, rel):
            return True
        # 派生值：与来源中任意两数的比值/积近似（倍数、增速、份额）
    return False


def derived_matched(value, src_values):
    vals = [v for v in src_values if v > 0]
    for i, a in enumerate(vals):
        for b in vals[i + 1:]:
            lo, hi = sorted((a, b))
            for cand in (hi / lo, lo / hi, hi - lo, (hi - lo) / lo):
                if approx_equal(value, cand, 0.04) and cand >= 1.05:
                    return True
    return False


def is_factual(sentence, has_cite):
    if is_analysis_sentence(sentence) and not has_cite:
        return False
    nums = extract_numbers(sentence)
    latin = re.findall(r'[A-Za-z][A-Za-z0-9\.\-]{1,}', sentence)
    if has_cite:
        return True
    if nums and not is_analysis_sentence(sentence):
        return True
    if latin and re.search(r'(发布|成立|推出|提出|融资|上市|收购|认证|评审|收录|验证|实测)', sentence):
        return True
    return False


def strip_citations(text):
    return CITE_RE.sub('', text)


# 传闻线索词（体现“谁说的”而非“是不是真的”）
HEARSAY_CUES = ['据报道', '据悉', '据称', '据传', '有消息称', '消息人士', '知情人士',
                '内部人士', '据了解', '传闻', '或超', '有望成为', '被指', '据透露',
                '尚未证实', '未获证实', '传闻称', '坊间']
# 断言化动词：把传闻写成既成事实的标志
ASSERT_CUES = ['已确认', '确认', '已确定', '敲定', '证实', '已完成', '已达成',
               '已突破', '已经上市', '已上市', '已签订', '已签约', '实锤']
# 对冲/限定语：明确标注不确定性的表述，可豁免
HEDGE_CUES = ['尚未', '未经', '未获', '未证实', '有待', '尚待', '可能', '或将',
              '预计', '若', '如果', '存疑', '质疑', '争议', '不确定', '待验证',
              '需审慎', '需注意', '加限定', '属于传闻', '不属于事实', '仅作转述']

# 分歧主题“已讨论”线索词（列数据、点出口径不同）
CONFLICT_WORDS = ['分歧', '冲突', '口径', '不一致', '存在差异', '差异', '审慎', '争议',
                  '观点', '对比', '理想条件', '实测', '实际', '并存', '不同']
# 分歧主题“已判断”线索词（给出取舍或倾向）
JUDGE_WORDS = ['判断', '本报告认为', '我们认为', '本报告倾向', '据此', '建议', '宜',
               '应当', '应作为', '倾向', '结论是', '可见', '综合来看', '权衡', '取舍',
               '更稳妥', '不宜', '取用', '优先采用']


def is_allegation(sent):
    """传闻线索 + 断言化动词 + 无对冲语 → 传闻被写成定论。"""
    has_hearsay = any(c in sent for c in HEARSAY_CUES)
    has_assert = any(c in sent for c in ASSERT_CUES)
    hedged = any(c in sent for c in HEDGE_CUES)
    if has_hearsay and has_assert and not hedged:
        return True
    # 明确指向具体主体、以传闻线索开篇并直接断言“已/将”，同样按定论化处理
    if has_hearsay and re.match(r'^(据|有消息|据悉|据了解)', sent) and re.search(
            r'(已[^，。；]{0,6}(上市|确认|完成|达成|突破|签订|签约)|将[^，。；]{0,10}(上市|建成|落地))', sent):
        return True
    return False


def topic_addressed(topic, pats, scope, sec_texts, ded_sec_idx, judged=False):
    """判断分歧主题是否已在草稿中得到处理（judged=False 只看是否讨论，
    judged=True 还要求给出判断/取舍）。专设分歧小节按主题关键词匹配。"""
    cues = JUDGE_WORDS if judged else CONFLICT_WORDS
    if any(w in scope for w in cues):
        return True
    for i in ded_sec_idx:
        sec = sec_texts[i]
        if any(p in sec for p in pats) and any(w in sec for w in cues):
            return True
    return False


def audit_draft(draft_text, corpus, conflicts):
    docs = build_doc_index(corpus)
    # 预构建每文档数值库（规范值 + 原始尾数，用于张冠李戴检测与单位归一容错）
    doc_num_pool = {name: set() for name in docs}
    for name, d in docs.items():
        for n in extract_numbers(doc_full_text(d)):
            doc_num_pool[name].add(round(n['canonical'], 4))
            doc_num_pool[name].add(round(float(n['value']), 6))
    contested = build_contested(conflicts)

    claims, issues = [], []
    cid = 0
    chapter = '（前言）'
    lines = draft_text.split('\n')
    # 章节切分：分歧分析常另起段落甚至单独成节，判定范围按“所在章节”而非单行，
    # 否则按规范分三段书写的分歧分析会被误判为未分析。
    sec_ranges, cur = [], None
    for li_, raw_ in enumerate(lines):
        if re.match(r'^#{1,4}\s', raw_.strip()):
            if cur is not None:
                sec_ranges.append((cur, li_ - 1))
            cur = li_
    if cur is not None:
        sec_ranges.append((cur, len(lines) - 1))
    sec_texts = ['\n'.join(lines[s:e + 1]) for s, e in sec_ranges]
    sec_of = [0] * len(lines)
    for bi, (s, e) in enumerate(sec_ranges):
        for li_ in range(s, min(e + 1, len(lines))):
            sec_of[li_] = bi
    # 专设“分歧/争议/口径分析”小节时，该节覆盖的分歧主题视为已分析
    ded_sec_idx = [i for i, (s, _e) in enumerate(sec_ranges)
                   if re.search(r'分歧|差异|争议|冲突|口径', lines[s])]
    for li, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if re.match(r'^#{1,4}\s', line):
            chapter = re.sub(r'^#+\s*', '', line)
            continue
        if line.startswith('|') or line.startswith('>') or line.startswith('---'):
            continue
        # 管道表格的表题行（下一非空行为表格）跳过
        if re.match(r'^表\s*[A-Za-z]?\s*[-－—]?\s*\d', line):
            nxt = ''
            for k in range(li + 1, min(li + 4, len(lines))):
                if lines[k].strip():
                    nxt = lines[k].strip()
                    break
            if nxt.startswith('|'):
                continue
        plain = strip_citations(line)
        for sent in split_sentences(plain):
            if len(sent) < 8:
                continue
            # 句子级来源标注（标注可能在句末，整句共享）
            cits = parse_citations(line)
            factual = is_factual(sent, bool(cits))
            if not factual:
                continue
            cid += 1
            rec = {'id': cid, 'chapter': chapter, 'sentence': sent[:240],
                   'citations': [], 'status': 'UNVERIFIABLE', 'problems': [],
                   'numbers': [n['raw'] for n in extract_numbers(sent)]}
            if not cits:
                rec['problems'].append('MISSING_CITATION')
            # 解析全部来源，构建“被引位置并集”（一句多源时，数值在任一被引位置可复现即可）
            cited_num_pool = set()      # 被引位置中出现的数值
            cited_doc_pool = set()      # 被引文档全文数值（定位偏粗时兜底）
            cited_docs = set()
            for src_name, loc, rawcite in cits:
                doc, method = resolve_doc(src_name, docs)
                crec = {'source': src_name, 'locator': loc, 'resolved_doc': doc['name'] if doc else None,
                        'resolve_method': method, 'locator_hits': 0, 'number_checks': []}
                if doc is None:
                    rec['problems'].append('SOURCE_NOT_FOUND:' + src_name)
                    crec['status'] = 'SOURCE_NOT_FOUND'
                else:
                    cited_docs.add(doc['name'])
                    hits = resolve_locator(doc, loc)
                    crec['locator_hits'] = len(hits)
                    if not hits:
                        rec['problems'].append('LOCATOR_NOT_FOUND:%s/%s' % (doc['name'], loc))
                        crec['status'] = 'LOCATOR_NOT_FOUND'
                    for n in extract_numbers('\n'.join(hits)):
                        cited_num_pool.add(round(n['canonical'], 4))
                        cited_num_pool.add(round(float(n['value']), 6))
                    cited_doc_pool |= doc_num_pool.get(doc['name'], set())
                rec['citations'].append(crec)
            # 数值核验：被引位置并集 → 被引文档他处 → 派生复算 → 张冠李戴/失配
            for n in extract_numbers(sent):
                claim_vals = [round(n['canonical'], 4), round(float(n['value']), 6)]
                def _in(pool):
                    return any((abs(cv) < 1e-12 and abs(v) < 1e-12)
                               or (v > 0 and approx_equal(cv, v, 0.03))
                               for cv in claim_vals for v in pool)
                ok = _in(cited_num_pool)
                where = 'cited_locator'
                if not ok:
                    ok = any(derived_matched(cv, list(cited_num_pool)) for cv in claim_vals)
                    where = 'derived'
                if not ok and _in(cited_doc_pool):
                    ok, where = True, 'cited_doc_elsewhere'
                if not ok:
                    other = [nm for nm, pool in doc_num_pool.items()
                             if nm not in cited_docs and _in(pool)]
                    tag = 'MISATTRIBUTED' if other else 'NUMBER_MISMATCH'
                    rec['problems'].append('%s:%s' % (tag, n['raw']))
                    for crec in rec['citations']:
                        crec['number_checks'].append(
                            {'raw': n['raw'], 'ok': False, 'tag': tag, 'found_in': other[:3]})
                else:
                    for crec in rec['citations']:
                        crec['number_checks'].append({'raw': n['raw'], 'ok': True, 'where': where})
            for crec in rec['citations']:
                if crec.get('status') is None:
                    crec['status'] = 'OK' if crec['locator_hits'] and not any(
                        not x['ok'] for x in crec['number_checks']) else 'WEAK'
            # 传闻/指控写成事实：任一传闻线索词 + 任一断言化动词，且无对冲语 → 判为定论化
            if is_allegation(sent):
                rec['problems'].append('ALLEGATION')
            # 分歧主题未分析 / 只列数据不给判断（判定范围：所在章节 + 专设分歧小节）
            scope = sec_texts[sec_of[li]] if sec_ranges else line
            for topic, pats in contested.items():
                if not any(p in sent for p in pats):
                    continue
                discussed = topic_addressed(topic, pats, scope, sec_texts,
                                            ded_sec_idx, judged=False)
                judged = discussed and topic_addressed(topic, pats, scope,
                                                       sec_texts, ded_sec_idx,
                                                       judged=True)
                if not discussed:
                    rec['problems'].append('CONFLICT_UNADDRESSED:' + topic)
                elif not judged:
                    rec['problems'].append('CONFLICT_NO_JUDGEMENT:' + topic)
                else:
                    continue
                break
            statuses = set()
            for p in rec['problems']:
                statuses.add(p.split(':')[0])
            if not rec['problems']:
                rec['status'] = 'SUPPORTED'
            elif 'MISSING_CITATION' in statuses or 'SOURCE_NOT_FOUND' in statuses:
                rec['status'] = 'UNVERIFIABLE'
            elif 'MISATTRIBUTED' in statuses or 'NUMBER_MISMATCH' in statuses:
                rec['status'] = 'CONTRADICTED'
            elif 'LOCATOR_NOT_FOUND' in statuses:
                rec['status'] = 'WEAK_LOCATOR'
            else:
                rec['status'] = 'FLAGGED'
            claims.append(rec)
    return summarize(claims, conflicts)


GENERIC_SYNONYMS = [
    ('成本', ['成本', '投入', '费用', '花费', '开销']),
    ('加速', ['加速', '提速', '加快', '倍数', '效果']),
    ('规模', ['规模', '市场', '份额', '体量']),
    ('收入', ['收入', '营收', '营业额']),
    ('增速', ['增速', '增长', '增幅', '增长率']),
    ('融资', ['融资', '筹资', '募资']),
    ('估值', ['估值', '市值', '价值']),
    ('占比', ['占比', '比例', '份额', '比重']),
    ('准确率', ['准确率', '正确率', '精度']),
    ('延迟', ['延迟', '时延', '耗时', '响应时间']),
]


def build_contested(conflicts):
    """从程序化冲突清单构造需在综述中显式分析的主题关键词（通用同义扩展）。"""
    contested = {}
    for c in conflicts.get('numeric_conflicts', []):
        topic = c['topic']
        core = re.sub(r'（.*?）', '', topic)
        pats = [core]
        for key, syns in GENERIC_SYNONYMS:
            if key in core:
                for s in syns:
                    if s not in pats:
                        pats.append(s)
        contested[topic] = pats
    return contested


def summarize(claims, conflicts):
    total = len(claims)
    by_status = {}
    for c in claims:
        by_status[c['status']] = by_status.get(c['status'], 0) + 1
    supported = by_status.get('SUPPORTED', 0)
    fidelity = round(supported / total, 4) if total else 0.0
    chapters = {}
    for c in claims:
        ch = chapters.setdefault(c['chapter'], {'total': 0, 'supported': 0})
        ch['total'] += 1
        if c['status'] == 'SUPPORTED':
            ch['supported'] += 1
    problems = {}
    for c in claims:
        for p in c['problems']:
            k = p.split(':')[0]
            problems[k] = problems.get(k, 0) + 1
    return {
        'fidelity': fidelity,
        'total_factual_claims': total,
        'supported': supported,
        'by_status': by_status,
        'problem_counts': problems,
        'by_chapter': {k: {'total': v['total'], 'supported': v['supported'],
                           'fidelity': round(v['supported'] / v['total'], 4) if v['total'] else 0}
                       for k, v in chapters.items()},
        'claims': claims,
    }


def render_markdown(result, conflicts, out_path):
    L = ['# 事实一致性自审报告', '',
         '- 事实性主张总数：%d' % result['total_factual_claims'],
         '- 受支持（可溯源且数值一致）：%d' % result['supported'],
         '- 忠实度（支持数/主张总数）：**%.2f%%**' % (result['fidelity'] * 100),
         '- 状态分布：%s' % json.dumps(result['by_status'], ensure_ascii=False),
         '- 问题计数：%s' % json.dumps(result['problem_counts'], ensure_ascii=False),
         '', '## 分章忠实度', '',
         '| 章节 | 主张数 | 支持数 | 忠实度 |', '|---|---|---|---|']
    for ch, v in result['by_chapter'].items():
        L.append('| %s | %d | %d | %.1f%% |' % (ch, v['total'], v['supported'], v['fidelity'] * 100))
    L.append('')
    L.append('## 问题明细（按状态）')
    for status in ('CONTRADICTED', 'UNVERIFIABLE', 'WEAK_LOCATOR', 'FLAGGED'):
        items = [c for c in result['claims'] if c['status'] == status]
        if not items:
            continue
        L.append('\n### %s（%d 条）' % (status, len(items)))
        for c in items[:50]:
            L.append('- [%s] %s' % (c['chapter'][:20], c['sentence']))
            L.append('  问题：%s' % '；'.join(c['problems']))
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('draft')
    ap.add_argument('--corpus', default='corpus.json')
    ap.add_argument('--conflicts', default='conflicts.json')
    ap.add_argument('-o', '--out', default='audit.json')
    ap.add_argument('--report', default='audit_report.md')
    args = ap.parse_args()
    with open(args.draft, 'r', encoding='utf-8') as f:
        draft = f.read()
    corpus = load_json(args.corpus)
    conflicts = load_json(args.conflicts) if os.path.exists(args.conflicts) else {}
    result = audit_draft(draft, corpus, conflicts)
    dump_json(result, args.out)
    render_markdown(result, conflicts, args.report)
    print('[audit] 主张 %d，支持 %d，忠实度 %.2f%%，问题 %s'
          % (result['total_factual_claims'], result['supported'],
             result['fidelity'] * 100, json.dumps(result['problem_counts'], ensure_ascii=False)))


if __name__ == '__main__':
    main()
