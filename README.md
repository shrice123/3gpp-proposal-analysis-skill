# 轻量化 3GPP 提案分析 Skill

这是一个面向 Codex、Hermes 等 Agent 的轻量化 3GPP 会议提案分析 Skill。

用户只需要用自然语言提出问题，例如：

- “帮我分析 SA2#175-AH-e 会议 KI#18、Solution Variant#18.7 中不同公司的观点。”
- “分析这次会议中与 AI 相关的提案。”
- “梳理某个 Solution 从 baseline 到 approved 版本的变化。”
- “这个课题有哪些争议？哪些提案被合并或采纳了？”

### 为什么不直接让 Agent 分析？

当然可以直接提问，能力较强的 Agent 通常也能完成分析；这个 Skill 的价值不是替代
Agent 的推理，而是把 3GPP 提案分析中容易遗漏、容易误判且重复耗时的部分固化为一套
稳定方法：

- 先结合议程、正文、KI/Solution 标识和关系链确定范围，避免把关键词命中当成完整提案集合；
- 统一追踪 baseline、revision、merge、input 和 approval，并保留可回查的证据位置；
- 明确 `Not Handled`、`Merge into`、联合署名等容易被误读的语义边界；
- 面对模糊或超大范围问题时，先基于真实会议数据预览和分层，而不是盲目全量分析；
- 通过并行下载、缓存、断点续传、证据去重和版本差异减少等待及重复阅读；
- 输出范围、覆盖率、缺失项和证据包，使结论更稳定、更快，也更容易复核。

因此，对于偶尔查询一篇已知提案，直接提问可能已经足够；对于会议级、课题级、公司
观点对比或方案演进分析，这个 Skill 能显著减少 Agent 走弯路，并降低遗漏范围、误判
提案关系和给出无证据强结论的风险。

Agent 负责理解问题、必要时澄清范围、判断公司观点并组织结论；项目中的 Python
脚本负责可重复的会议检索、提案下载、Office 文档解析、关系候选提取、证据索引和
版本差异生成。

项目不依赖 MCP、数据库、常驻服务、专用 UI 或第三方 Python 包。

## 一、最快开始

### 方式一：直接让 Agent 安装

如果 Agent 支持从 GitHub 安装 Skill，可以直接对它说：

> 请从 https://github.com/shrice123/3gpp-proposal-analysis-skill 安装这个 Skill。

安装后重启或新建 Agent 会话。

### 方式二：手工安装到 Codex

仓库根目录本身就是完整的 Skill 目录。克隆时请将目录命名为
`analyze-3gpp-meeting-proposals`。

Windows PowerShell：

```powershell
git clone https://github.com/shrice123/3gpp-proposal-analysis-skill.git "$env:USERPROFILE\.codex\skills\analyze-3gpp-meeting-proposals"
```

macOS / Linux：

```bash
git clone https://github.com/shrice123/3gpp-proposal-analysis-skill.git ~/.codex/skills/analyze-3gpp-meeting-proposals
```

如果不使用 Git，也可以在 GitHub 页面选择 **Code → Download ZIP**，解压后将目录
重命名为 `analyze-3gpp-meeting-proposals`，再复制到 Codex 的 `skills` 目录。

安装后重启 Codex。可以显式调用：

```text
$analyze-3gpp-meeting-proposals
```

也可以直接提出 3GPP 会议、Agenda、KI、Solution、Solution Variant、TDoc、
公司观点、共识、争议、合并或采纳情况等问题，由 Codex 自动触发。

### 更新已有安装

如果是通过 Git 克隆安装的：

```powershell
git -C "$env:USERPROFILE\.codex\skills\analyze-3gpp-meeting-proposals" pull
```

更新后重新启动 Agent 或新建会话。

### Hermes 或其他 Agent

将整个仓库复制到对应 Agent 的 Skill 目录，并确保 `SKILL.md` 位于该 Skill
目录的根部。不同 Agent 和版本的 Skill 路径可能不同，应优先以宿主的 Skill
安装说明为准。Hermes 用户可在安装后运行：

```text
hermes skills list
```

确认 `analyze-3gpp-meeting-proposals` 已被发现并启用。

## 二、推荐使用方式

### 1. 直接让 Agent 分析

这是最推荐的方式。用户不需要自己运行脚本。

信息比较明确时，可以给出：

- 会议名称或会议 URL；
- KI、Solution 或 Solution Variant；
- 想比较的公司；
- 想分析的维度，例如观点差异、争议与共识、版本演进或采纳情况。

示例：

> 分析 SA2#175-AH-e 会议 KI#18、Solution Variant#18.7 中不同公司对 intent structure 的观点，并说明哪些内容进入了 approved 版本。

问题比较模糊时也可以直接提问：

> 分析这次会议的提案。

> 分析 AI 相关课题。

Skill 会指导 Agent 先获取真实会议范围预览，再根据实际 Agenda、KI、公司分布、
争议 Solution 或提案关系给出可选分析方向，而不是盲目下载整场会议。

### 2. 单独运行证据采集脚本

脚本适合以下情况：

- 希望先查看某个问题可能涉及哪些提案；
- 希望生成可复核的结构化证据包；
- 希望将证据交给其他 Agent 或分析流程；
- 需要调试提案范围、关系链、缓存或下载情况。

推荐 Python 3.10 或更高版本。脚本只使用 Python 标准库。

先预览范围：

```bash
python scripts/collect_3gpp_evidence.py preview --meeting "SA2#175-AH-e" --query "KI#18 Solution Variant#18.7 intent structure" --output output/preview
```

先采集会议结果、baseline 和 approved 等核心证据：

```bash
python scripts/collect_3gpp_evidence.py collect --meeting "SA2#175-AH-e" --query "KI#18 Solution Variant#18.7 intent structure" --stage core --output output/analysis
```

使用相同的会议、问题和输出目录，继续补齐完整有效范围：

```bash
python scripts/collect_3gpp_evidence.py collect --meeting "SA2#175-AH-e" --query "KI#18 Solution Variant#18.7 intent structure" --stage complete --output output/analysis
```

`core` 和 `complete` 只改变执行顺序，不改变 Agent 对完整分析范围的判断。

## 三、项目各部分是做什么的

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── analysis-patterns.md
│   ├── evidence-rules.md
│   └── performance-workflow.md
├── scripts/
│   ├── collect_3gpp_evidence.py
│   └── transfer_runtime.py
├── tests/
├── .github/workflows/
└── README.md
```

### `SKILL.md`

这是整个 Skill 的入口，也是 Agent 在执行提案分析时遵循的工作说明。它主要包含：

- 什么类型的用户问题应触发该 Skill；
- 如何理解清晰、模糊、超大范围和跨会议请求；
- 什么时候直接分析，什么时候先预览或询问一个关键澄清问题；
- 如何确定目标提案范围，而不是把关键词命中集合当成完整范围；
- 如何追踪 baseline、revision、merge、input、approval 等关系；
- 如何区分提案原文事实、关系候选和 Agent 推断；
- 如何判断不同公司的支持、反对、关切、中性澄清或无法判断；
- 来源缺失、文件损坏、旧 Office 格式、PDF、图片等异常情况的处理方式；
- 没有 Python 时的手工回退流程；
- 最终回答需要交代的检索范围、覆盖率、缺失项和证据依据。

如果只想把分析方法交给 Agent，而不运行任何自动化脚本，`SKILL.md` 仍然可以独立使用。

### `agents/openai.yaml`

保存 Skill 在支持该元数据格式的 Agent 中显示时所需的名称、简短说明和默认提示词。
它不包含分析逻辑。

### `scripts/collect_3gpp_evidence.py`

这是用户可以直接调用的命令行入口，负责协调完整的机械化证据采集流程，包括：

- 解析会议名称、会议 URL 或本地测试目录；
- 读取会议议程并生成真实范围预览；
- 根据自然语言课题、KI、Solution、TDoc 和公司过滤候选；
- 对提案任务进行优先级排序；
- 执行 `core` 或 `complete` 分阶段采集；
- 调用下载与缓存模块；
- 解析 DOCX、PPTX、XLSX 等 OOXML 文件；
- 提取段落、章节、标识符、关系表达及相邻上下文；
- 标记插入、删除和当前文本；
- 对不同 revision 的重复证据去重；
- 生成 baseline、revision、approved 之间的确定性段落差异；
- 增量写出 manifest、coverage、关系和证据文件；
- 在任务中断后根据现有输出和缓存恢复。

它只生成事实、证据和关系候选，**不会自行生成公司观点、共识或技术结论**。

### `scripts/transfer_runtime.py`

这是供采集脚本使用的底层传输和缓存模块，主要负责：

- 用户级公开文件缓存及缓存元数据；
- URL 归一化和同一资源去重；
- 多任务之间的缓存锁和失效锁处理；
- 有界并行下载和独立解析池；
- 429、403、超时、不完整响应等情况下的自适应降并发；
- 连续成功后的谨慎升并发；
- `Retry-After`、重试和随机抖动退避；
- ETag、Last-Modified 和 304 条件请求；
- `.part` 文件、Range 和 If-Range 断点续传；
- 分块流式写入和同步计算 SHA-256；
- ZIP 完整性与安全路径校验；
- 下载完成后的原子替换；
- 确定性解析结果缓存。

一般用户不需要直接运行这个文件。

### `references/analysis-patterns.md`

记录常见分析模式，例如：

- 公司观点对比；
- 争议与共识；
- baseline 到 approved 的方案演进；
- 合并、采纳和决策追踪；
- 跨会议变化；
- 风险、开放问题和证据不足。

Agent 会按用户问题选择相关模式，不需要每次加载全部内容。

### `references/evidence-rules.md`

记录提案分析中容易误判的证据规则和关系语义，例如：

- `Not Handled` 不等于被拒绝；
- `Merge into` 不等于所有内容都被采纳；
- 联合署名不代表同意所有后续修改；
- 无法直接证明的关系只能标记为候选；
- 强观点结论必须有直接证据；
- Agent 推断必须与原文事实分开。

### `references/performance-workflow.md`

记录性能和恢复相关说明，包括：

- 默认并发、解析线程、批次和重试参数；
- `core` 与 `complete` 的使用方式；
- 缓存位置、条件请求和解析缓存；
- 中断恢复和 `.part` 续传；
- 如何解读性能、缓存和覆盖率指标；
- 为什么不能仅凭运行速度判断分析是否完整。

### `tests/`

包含不访问真实 3GPP 公共服务器的本地自动化测试，覆盖：

- SA2#175-AH-e、KI#18、Solution Variant#18.7 黄金关系链；
- 错误关系归属隔离；
- 模糊范围预览；
- `core` 到 `complete` 恢复；
- 并行下载性能与并发上限；
- 200、206、304、403、416、429 等响应；
- 断点续传、缓存锁、不完整响应和损坏 ZIP；
- ZIP Slip 等安全问题。

### `.github/workflows/tests.yml`

GitHub Actions 配置。每次推送或 Pull Request 都会在多个 Python 版本上自动运行测试。

## 四、脚本输出说明

| 文件 | 用途 |
|---|---|
| `scope_preview.json` | 会议解析结果、候选范围、公司和状态分布、范围歧义 |
| `manifest.json` | 每篇文档的来源、优先级、缓存、哈希、下载、解析和恢复状态 |
| `relationships.json` | baseline、revision、merge、input、approval 等关系候选及证据定位 |
| `evidence.jsonl` | 首轮相关证据、段落哈希、变更状态和去重来源 |
| `coverage.json` | 已检查范围、失败项、覆盖状态、字节数、缓存命中、重试和耗时 |
| `document_index.jsonl` | 标题、章节、标识符和段落索引，供 Agent 按需回查 |
| `diffs.json` | baseline、revision、approved 等文档之间带定位的段落差异 |

这些文件共同构成可复核的证据包。不能只查看搜索命中或 `evidence.jsonl` 就声称范围完整，
还应同时检查 `scope_preview.json`、`manifest.json` 和 `coverage.json`。

## 五、常用参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--stage` | `core` 先取核心证据；`complete` 补齐有效范围 | `complete` |
| `--max-concurrency` | 最大下载并发，范围 1～8 | `4` |
| `--parse-workers` | 解析并发，范围 1～4 | `2` |
| `--batch-size` | 执行批次大小，不用于裁剪分析范围 | `8` |
| `--retries` | 下载失败重试次数 | `3` |
| `--cache-dir` | 覆盖默认缓存目录 | 未指定 |
| `--no-cache` | 完全关闭持久化缓存 | 关闭 |
| `--refresh` | 强制重新获取正文 | 关闭 |

查看缓存：

```bash
python scripts/collect_3gpp_evidence.py cache info
```

只有在明确需要时才清空缓存：

```bash
python scripts/collect_3gpp_evidence.py cache clear --yes
```

## 六、能力边界

- 脚本当前原生解析 DOCX、PPTX、XLSX 等 OOXML 格式。
- PDF、图片、旧 `.doc` 及布局敏感内容应交给宿主 Agent 的通用文件能力。
- 脚本不会代替 Agent 判断公司观点、共识、技术优劣或最终采纳含义。
- 来源不可访问、文件损坏或覆盖不完整时，会在 `coverage.json` 中明确记录。
- 公开 3GPP 服务器出现限流时，下载器会自动降低并发；不要同时启动多个采集器压测公共服务。

## 七、缓存与隐私

缓存只允许保存公开原始文件和确定性解析结果，不保存用户问题、Agent 推断或公司观点。

默认缓存目录：

- Windows：`%LOCALAPPDATA%\3GPP Proposal Cache`
- macOS：`~/Library/Caches/3gpp-proposal-analysis`
- Linux：`$XDG_CACHE_HOME/3gpp-proposal-analysis`，未设置时使用
  `~/.cache/3gpp-proposal-analysis`

不要将提案下载文件、缓存、证据输出或 `.part` 文件提交到 Git 仓库。

## 八、运行测试

```bash
python -m unittest discover -s tests -v
```

项目本身不需要安装第三方 Python 依赖。

## 九、许可证和声明

本项目采用 MIT License。

本项目为独立开源项目，与 3GPP 不存在隶属或官方认可关系。3GPP 及相关标识的权利
归其各自权利人所有。使用者应遵守相关来源的访问、使用和引用要求。
