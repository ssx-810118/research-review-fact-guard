# -*- coding: utf-8 -*-
"""
generate_review.py — 综述生成主体（Skill 工作流第 3 步）

双主体信息隔离中的“生成主体”：只接触【材料语料 + 章节大纲】，不接触审计规则细节。
- LLM 模式（设置环境变量 OPENAI_API_KEY，可选 OPENAI_BASE_URL / LLM_MODEL）：
  逐章调用 OpenAI 兼容接口，系统提示强制“只用材料、事实句必须带来源标注、
  分歧需显式分析、禁止外部知识”。
- 离线模式（默认）：生成结构化草稿骨架与逐章证据包（evidence_pack.json），
  供执行 Agent 据其完成万字正文，保证无密钥也能跑通全链路。
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.request


def _make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.verify_flags = ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


_SSL_CTX = _make_ssl_context()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import dump_json, load_json

GEN_SYS_PROMPT = """你是科研综述“生成主体”。严格遵守：
1. 只能使用用户提供的调研材料，禁止引入任何外部知识、参数名与常识性数字。
2. 每条事实性陈述（数据、事件、方法、结论、谁在何时说了什么）句末必须标注来源，
   格式为【来源材料名称，具体位置】，例如【行业白皮书节选，第一章】、【论文摘要集，论文A】。
3. 分析性归纳（总结、评价、趋势判断、原因推断）不要求逐条标注，但不得引入材料外事实。
4. 材料间存在数据或观点分歧时，必须并列各方数据、分析口径或条件差异，并给出有依据的判断，
   不得悄悄选边；一手来源（同行评审论文、官方统计、统一条件实测）优先于转述与个人观点。
5. 传闻、未证实指控、厂商口号必须使用“据报道/受访人认为/该观点未获证实”等限定语，
   不得写成确定事实。
6. 表格中的企业自报参数若与独立实测冲突，以实测为准并指出差异。
7. 语言为中文学术书面语，区分“事实”与“本报告判断”。
8. 每章正文必须按主题分节，使用“X.Y 节标题”编号（X为章序号，Y从1递增），
   用Markdown二级标题（## X.Y 节标题）标记，每节300-600字；
   章末以“## X.N 本章小结”收束全章要点。"""


_CN_NUM = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}

def chapter_num(title):
    """Extract numeric chapter index from title like '第二章 ...' -> 2."""
    m = re.search(r'第([一二三四五六七八九十\d]+)章', title)
    if not m:
        return 0
    s = m.group(1)
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s, 0)


def chapter_keywords(outline_text):
    """从大纲中解析章节标题与关键词（# 标题 / 第X章 标题）。"""
    chapters = []
    for line in outline_text.split('\n'):
        m = re.match(r'^#{0,4}\s*(第[一二三四五六七八九十\d]+章[^\n#（(]*)', line.strip())
        if m:
            title = m.group(1).strip()
            kws = re.findall(r'[一-龥A-Za-z][一-龥A-Za-z0-9]{1,5}', line)
            chapters.append({'title': title, 'must_cover': line.strip(), 'keywords': kws})
    return chapters


def build_evidence_pack(corpus, chapters):
    """按章节关键词对章节/表格做相关性打分，形成证据包。"""
    packs = []
    sections = []
    for d in corpus['documents']:
        for s in d['sections']:
            sections.append({'doc': d['name'], 'locator': s['locator'], 'text': s['text']})
        for t in d['tables']:
            txt = '\n'.join(' | '.join(r) for r in t.get('all_rows', [])[:60])
            sections.append({'doc': d['name'], 'locator': t['locator'], 'text': txt,
                             'is_table': True})
    for ch in chapters:
        kws = [k for k in ch['keywords'] if len(k) >= 2]
        scored = []
        for sec in sections:
            hay = sec['locator'] + sec['text']
            score = sum(hay.count(k) for k in kws)
            if score:
                scored.append((score, sec))
        scored.sort(key=lambda x: -x[0])
        evidence = []
        budget = 0
        for score, sec in scored:
            excerpt = sec['text'][:1200]
            budget += len(excerpt)
            evidence.append({'doc': sec['doc'], 'locator': sec['locator'],
                             'score': score, 'excerpt': excerpt,
                             'is_table': sec.get('is_table', False)})
            if budget > 9000:
                break
        packs.append({'chapter': ch['title'], 'must_cover': ch['must_cover'],
                      'evidence': evidence})
    return packs


def llm_chat(messages, model, base_url, api_key, temperature=0.3, max_retries=3):
    url = base_url.rstrip('/') + '/chat/completions'
    body = json.dumps({'model': model, 'messages': messages,
                       'temperature': temperature, 'stream': False}).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=600, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return data['choices'][0]['message']['content']
        except (TimeoutError, OSError) as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 10
                print('[llm] 第%d次请求失败(%s)，%d秒后重试...' % (attempt+1, e, wait))
                time.sleep(wait)
            else:
                raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', default='corpus.json')
    ap.add_argument('--outline', required=True, help='调研主题与结构要求（.md/.txt/.docx）')
    ap.add_argument('--out', default='draft.md')
    ap.add_argument('--evidence-out', default='evidence_pack.json')
    ap.add_argument('--min-words', type=int, default=10000)
    args = ap.parse_args()

    corpus = load_json(args.corpus)
    if args.outline.endswith('.docx'):
        from common import read_docx
        paras, _ = read_docx(args.outline)
        outline_text = '\n'.join(paras)
    else:
        with open(args.outline, 'r', encoding='utf-8') as f:
            outline_text = f.read()
    chapters = chapter_keywords(outline_text)
    packs = build_evidence_pack(corpus, chapters)
    dump_json(packs, args.evidence_out)
    print('[gen] 证据包：%d 章 -> %s' % (len(packs), args.evidence_out))

    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        skeleton = ['# ' + re.search(r'综述主题\s*\n?\s*(.+)', outline_text)
                    .group(1).strip() if re.search(r'综述主题\s*\n?\s*(.+)', outline_text)
                    else '调研综述（离线骨架）', '',
                    '> 离线模式：未设置 OPENAI_API_KEY，已生成逐章证据包 evidence_pack.json，',
                    '> 由执行 Agent 依据证据包与 SKILL.md 工作流完成正文（事实句逐条标注来源）。', '']
        for pk in packs:
            skeleton.append('## ' + pk['chapter'])
            skeleton.append('（本章须覆盖：%s）' % pk['must_cover'][:120])
            skeleton.append('可用证据：')
            for e in pk['evidence']:
                skeleton.append('- 【%s，%s】%s' % (e['doc'], e['locator'],
                                                    e['excerpt'][:120].replace('\n', ' ')))
            skeleton.append('')
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(skeleton))
        print('[gen] 离线骨架 -> %s（设置 OPENAI_API_KEY 后可自动生成正文）' % args.out)
        return

    base_url = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    model = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
    parts = []
    for pk in packs:
        evidence = '\n\n'.join('材料《%s》位置[%s]：\n%s' % (e['doc'], e['locator'], e['excerpt'])
                               for e in pk['evidence'])
        ch_num = chapter_num(pk['chapter'])
        user = ('章节：%s（第%d章）\n要求：%s\n本章可用材料（仅限以下材料，引用时使用材料原名与具体位置）：\n%s\n'
                '请撰写本章正文，不少于 %d 字，事实句末尾以【材料名，位置】标注来源。\n'
                '结构要求：正文必须分为3-5个主题小节，每节用 ## %d.Y 小节标题 编号（Y从1递增），'
                '每节300-600字；章末用 ## %d.N 本章小结 收束。\n'
                '重要：直接从小节标题开始写，不要输出章级标题（如"## 第X章"），不要添加任何开头说明文字。'
                % (pk['chapter'], ch_num, pk['must_cover'], evidence,
                   max(1200, args.min_words // max(1, len(packs))), ch_num, ch_num))
        content = llm_chat([{'role': 'system', 'content': GEN_SYS_PROMPT},
                            {'role': 'user', 'content': user}], model, base_url, api_key)
        parts.append('## ' + pk['chapter'] + '\n\n' + content)
        print('[gen] 已生成章节：%s（%d 字）' % (pk['chapter'], len(content)))
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(parts))
    print('[gen] 草稿 -> %s' % args.out)


if __name__ == '__main__':
    main()
