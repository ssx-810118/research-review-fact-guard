# -*- coding: utf-8 -*-
"""
run_pipeline.py — 端到端编排：材料解析 → 程序化数值校验 → 综述生成 →
独立自审 → （LLM 模式）迭代修正 → Word 终稿

用法：
  python run_pipeline.py --materials <材料目录> --outline <结构要求.docx|.md> \
      --outdir <输出目录> [--llm] [--max-iter 3] [--min-words 10000]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import dump_json, load_json
import parse_materials
import numeric_audit
import audit_review
import generate_review
import build_report


def step(title):
    print('\n' + '=' * 20 + ' ' + title + ' ' + '=' * 20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--materials', required=True)
    ap.add_argument('--outline', required=True)
    ap.add_argument('--outdir', default='output')
    ap.add_argument('--llm', action='store_true')
    ap.add_argument('--max-iter', type=int, default=3)
    ap.add_argument('--min-words', type=int, default=10000,
                    help='正文计字下限；渲染阶段不足即失败退出')
    ap.add_argument('--min-spread', type=float, default=0.12,
                    help='跨源数值冲突离散度阈值，默认 0.12')
    ap.add_argument('--title', default='大模型技术发展及智能体应用研究报告')
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    P = lambda *n: os.path.join(args.outdir, *n)

    step('1/5 材料解析')
    corpus_path = P('corpus.json')
    # 复用脚本主流程
    sys.argv = ['parse_materials.py', args.materials, '-o', corpus_path]
    parse_materials.main()
    corpus = load_json(corpus_path)

    step('2/5 程序化数值校验')
    facts = numeric_audit.collect_facts(corpus)
    nc, panels = numeric_audit.detect_numeric_conflicts(facts, args.min_spread)
    qc = numeric_audit.detect_qualitative_conflicts(corpus)
    tf, tok = numeric_audit.audit_tables(corpus)
    conflicts = {
        'fact_count': len(facts), 'numeric_conflicts': nc, 'numeric_panels': panels,
        'qualitative_conflicts': qc, 'table_findings': tf, 'table_checks_passed': tok,
        'stats': {'NUMERIC_INCONSISTENCY': len(nc), 'QUALITATIVE_CONFLICT': len(qc),
                  'table_findings': len(tf), 'table_checks_passed': len(tok)}}
    dump_json(conflicts, P('conflicts.json'))
    print('数值冲突 %d，结论冲突 %d，表格异常 %d，校验通过 %d'
          % (len(nc), len(qc), len(tf), len(tok)))

    step('3/5 综述生成')
    draft_path = P('draft.md')
    gen_argv = ['generate_review.py', '--corpus', corpus_path,
                '--outline', args.outline, '--out', draft_path,
                '--evidence-out', P('evidence_pack.json'),
                '--min-words', str(args.min_words)]
    if args.llm:
        # 生成主体是否走 LLM 由 OPENAI_API_KEY 环境变量决定（见 generate_review.py）
        pass
    sys.argv = gen_argv
    generate_review.main()

    step('4/5 独立自审 + 迭代修正')
    final_path = P('final.md')
    if args.llm and os.environ.get('OPENAI_API_KEY'):
        revise_with_llm(draft_path, final_path, corpus, conflicts, args)
    else:
        # 离线：执行 Agent 据审计意见人工/Agent 修订；这里先产出首轮审计
        with open(draft_path, 'r', encoding='utf-8') as f:
            draft = f.read()
        result = audit_review.audit_draft(draft, corpus, conflicts)
        dump_json(result, P('audit_round1.json'))
        audit_review.render_markdown(result, conflicts, P('audit_report_round1.md'))
        print('首轮忠实度 %.2f%%；离线模式请按 %s 逐条修订草稿，' 
              '另存为 %s 后执行以下两步复核并渲染：'
              % (result['fidelity'] * 100, P('audit_report_round1.md'), final_path))
        print('  python scripts/audit_review.py %s --corpus %s --conflicts %s '
              '-o %s --report %s'
              % (final_path, corpus_path, P('conflicts.json'),
                 P('audit_final.json'), P('audit_report_final.md')))
        print('  python scripts/build_report.py %s -o %s'
              % (final_path, P('综述终稿.docx')))
        final_path = draft_path if not os.path.exists(final_path) else final_path

    if os.path.exists(final_path):
        result = audit_review.audit_draft(open(final_path, encoding='utf-8').read(),
                                         corpus, conflicts)
        dump_json(result, P('audit_final.json'))
        audit_review.render_markdown(result, conflicts, P('audit_report_final.md'))
        print('终稿忠实度 %.2f%%（支持 %d / 主张 %d）'
              % (result['fidelity'] * 100, result['supported'],
                 result['total_factual_claims']))

        step('5/5 渲染 Word 终稿')
        out_docx = P('综述终稿.docx')
        try:
            build_report.build(final_path, out_docx,
                               {'title': args.title,
                                'subtitle': '——含来源标注与事实一致性自审',
                                'info_lines': ['（依据调研材料包生成，事实性陈述均标注来源）']},
                               min_chars=args.min_words)
        except build_report.WordCountError as e:
            print('[FAIL] %s（要求 %d 字）；终稿未达篇幅要求，请扩写后重跑'
                  % (e, e.need))
            sys.exit(3)
    print('\n[完成] 产物目录：%s' % args.outdir)


def revise_with_llm(draft_path, final_path, corpus, conflicts, args):
    """LLM 模式的生成—审计—修正闭环。"""
    import urllib.request
    base_url = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    model = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
    api_key = os.environ['OPENAI_API_KEY']
    draft = open(draft_path, encoding='utf-8').read()
    current = draft
    for rnd in range(1, args.max_iter + 1):
        result = audit_review.audit_draft(current, corpus, conflicts)
        dump_json(result, os.path.join(args.outdir, 'audit_round%d.json' % rnd))
        print('第 %d 轮自审忠实度 %.2f%%' % (rnd, result['fidelity'] * 100))
        if result['fidelity'] >= 0.98:
            break
        problems = [c for c in result['claims'] if c['status'] != 'SUPPORTED']
        notes = '\n'.join('- [%s] %s || 问题：%s' % (c['chapter'][:16], c['sentence'],
                                                    '；'.join(c['problems']))
                          for c in problems[:80])
        rev_prompt = (
            '你是综述修正主体。以下草稿经独立审计发现事实一致性问题，请逐条修正：\n'
            '原则：无来源的事实句补来源（只能用给定材料名与位置）；数值与来源不符的，'
            '改为来源原文数值或删除；张冠李戴的改注正确来源；传闻加限定语；'
            '分歧主题补“各方口径+判断”。不得引入材料外信息。\n问题清单：\n' + notes +
            '\n\n输出修正后的完整草稿（Markdown）：\n\n' + current)
        messages = [{'role': 'system', 'content': generate_review.GEN_SYS_PROMPT},
                    {'role': 'user', 'content': rev_prompt}]
        current = generate_review.llm_chat(messages, model, base_url, api_key,
                                           temperature=0.2)
    open(final_path, 'w', encoding='utf-8').write(current)


if __name__ == '__main__':
    main()
