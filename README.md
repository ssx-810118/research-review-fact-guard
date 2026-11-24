# research-review-fact-guard

> 科研综述的防幻觉生成与事实一致性自审 Skill：把「写综述」拆成**生成**与**审计**两条隔离链路，让每条事实都能回查到原材料的某一页、某一行。

给定一批调研材料（论文摘要、白皮书、新闻集、访谈记录、评论集、评测报告、调研数据表）和一份结构要求，输出一篇**含来源标注**的 Word 科研报告，并附一份可复核的**事实一致性自审报告**。

## 它解决什么问题

| 问题 | 常见表现 | 本项目的做法 |
|---|---|---|
| 事实无法溯源 | 数字写得漂亮，但不知道来自哪份材料 | 事实句逐句绑定 `【材料名，位置】`，审计阶段逐条验证「来源存在 + 位置可回查 + 数值可在该位置复现」 |
| 多源数据打架 | 同一指标三家机构三个数，稿子里悄悄选一个 | 程序化聚类出分歧清单，要求写成「各方数据 → 口径差异 → 本报告判断」三段式，只列数据不给判断会被打回 |
| 传闻被写成定论 | 「据报道…已确认…」 | 传闻线索词 + 断言化动词 + 对冲语三要素联合判定，命中报 `ALLEGATION` |
| 理想值与实测混用 | 论文峰值 140 倍，生产环境只有 5~10 倍 | 证据强度 L1–L5 分层，访谈/评论作为负样本与一手实测对照 |

## 特性

- **双主体独立审计**：`generate_review.py`（只吃材料+大纲）与 `audit_review.py`（只看草稿+原始材料）互不通气，审计不用模型记忆裁决事实。
- **数值先过脚本**：中文数字/单位归一（万/亿/万亿/%/倍/token）、跨源聚类比对、表格复算（合计/均值/派生倍数/配比求和/主键去重）。
- **忠实度可量化**：`忠实度 = 受支持事实性主张数 / 事实性主张总数`，与赛题评分公式同构；实测终稿 187/187 = 100%。
- **硬门禁**：字数不足渲染阶段非零退出（`--min-chars`），管线未达标 `exit 3`；未通过审计的数字不得进入终稿。
- **跨领域可迁移**：指标词表分「领域专用 + 跨行业通用」两层，已在非 AI 领域（冷链物流）材料上实测跑通。
- **零密钥可跑**：离线模式产出证据包 + 骨架，由执行 Agent 依证据撰写；配 `OPENAI_API_KEY` 可全自动。

## 目录结构

```
SKILL.md                     技能说明书：描述/工作流/审计策略/输入输出/验收标准
references/
  citation-spec.md           引用与来源标注规范（格式、位置写法、数值—来源绑定）
  audit-rubric.md            证据强度 L1–L5、冲突裁决、问题码、忠实度口径
scripts/
  common.py                  公共库：数值归一、句子切分、标注解析、事实/分析句判定、文档读取
  parse_materials.py         ① 材料解析 → corpus.json（章节 + 表格 + locator）
  numeric_audit.py           ② 数值校验 → conflicts.json（六类异常）
  generate_review.py         ③ 综述生成 → evidence_pack.json / draft.md
  audit_review.py            ⑤ 事实一致性自审 → audit.json / audit_report.md
  build_report.py            ⑦ 渲染 Word（标注蓝标、表格、目录、字数门禁）
  run_pipeline.py            一键编排 ①→⑦，可自动迭代修正至忠实度 ≥98%
```

## 安装

需要一个支持 Skill 的 Agent 运行环境（如 WorkBuddy / Claude Skills 目录约定）。把本仓库放进 skills 目录即可，目录名与 `SKILL.md` 中的 `name` 保持一致：

```bash
# 以 WorkBuddy 用户级技能目录为例
git clone https://github.com/ssx-810118/research-review-fact-guard.git \
  ~/.workbuddy/skills/research-review-fact-guard
```

安装依赖（Python 3.9+）：

```bash
pip install python-docx openpyxl pdfplumber
```

## 快速开始

一键跑通（材料目录 + 结构要求）：

```bash
python scripts/run_pipeline.py \
  --materials ./materials \
  --outline ./报告主题与结构要求.docx \
  --outdir ./output \
  --min-words 10000 --min-spread 0.12
```

分步执行（推荐首次使用时这样跑，便于观察每步产物）：

```bash
python scripts/parse_materials.py materials -o output/corpus.json
python scripts/numeric_audit.py output/corpus.json -o output/conflicts.json --min-spread 0.12
python scripts/generate_review.py --corpus output/corpus.json \
    --outline 报告主题与结构要求.docx --out output/draft.md
# 撰写/修订正文 → draft.md，然后自审
python scripts/audit_review.py output/draft.md --corpus output/corpus.json \
    --conflicts output/conflicts.json -o output/audit.json --report output/audit_report.md
# 按审计报告修订 → final.md，复审通过后渲染
python scripts/build_report.py output/final.md -o output/综述终稿.docx --min-chars 10000
```

`--min-spread` 是跨源数值离散度阈值（默认 0.12）。换到口径差异更小的领域时建议降到 0.05。

## 审计会报什么

| 问题码 | 含义 |
|---|---|
| `SOURCE_NOT_FOUND` | 来源材料名不可解析（支持类别短名归一） |
| `LOCATOR_NOT_FOUND` | 位置不可回查（章号/条目号/表号/第 N 行） |
| `NUMBER_MISMATCH` | 数值无法在被引位置复现（3% 容差，支持比值/差值派生复算） |
| `MISATTRIBUTED` | 张冠李戴：数值出现在别的来源里 |
| `MISSING_CITATION` | 事实句缺来源标注 |
| `ALLEGATION` | 传闻/指控被写成确定事实 |
| `CONFLICT_UNADDRESSED` | 引用了分歧主题但未作分析 |
| `CONFLICT_NO_JUDGEMENT` | 并列了各方数据，但没给判断 |

主张状态：`SUPPORTED` / `WEAK_LOCATOR` / `FLAGGED` / `CONTRADICTED` / `UNVERIFIABLE`。

## 输出示例

在一次完整运行中（7 份材料、7 章结构要求）：

- 终稿 Word：正文 12041 字（不含标注）、151 处来源标注（蓝色加粗）、8 张表格、自动目录
- 自审报告：187 条事实性主张，**187 条受支持，忠实度 100%**
- 过程件：`corpus.json` / `conflicts.json` / `evidence_pack.json` / `audit.json` 全部留存，可逐步追溯

## 已知边界

- **无数字、无专名的定性类外部知识**无法自动识别，需靠提示词约束与人工抽检。
- 数值冲突依赖阈值，默认 12% 会漏掉 8% 级别的口径差，跨领域使用时请下调 `--min-spread`。
- 审计是**规则驱动**的：它保证「可核验的部分一定被核验」，但不能替代领域专家的实质判断。

## 许可

MIT，见 [LICENSE](LICENSE)。
