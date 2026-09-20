# AI 中转站域名情报：面试原型

这是围绕面试题实现的 Python 命令行原型：公开取证、一个材料分析 Agent、统一判定规则、必要复核和 JSON 导出。代码按顺序组织业务，不依赖数据库或后台服务。

当前交付已完成 **55 个域名、54 个注册域**的真实分析：确认 15、疑似 35、排除 4、证据不足 1。基于 13 个确认种子整理公开关联链接，发现 2 个新候选并分别完成取证与分析。56 项离线测试通过。40 条复核记录明确标注为 AI 复核，没有独立人工标注评估。

## 先看这几个文件

- [汇报文档](delivery/REPORT.md)：五章 Markdown，覆盖题目要求的六项内容。
- [域名情报](delivery/intelligence.json)：全部 55 条结果和可定位证据。
- [统计与批次对应](delivery/summary.json)：每个域名采用哪个实际批次、标签分布和调用统计。
- [关联扩展](delivery/expansions.json)：种子、原文链接关系及新增候选。
- [采集记录](delivery/collection.json)：实际访问时间、HTTP 状态、最终地址和正文摘要值。
- [复核记录](delivery/reviews.jsonl)：修改内容、实际复核者与理由。原始模型分析保存在 `delivery/batches/`。

七个必需字段为 `domain、label、confidence、evidence、reason、discovery_source、last_verified`。`evidence.facts` 是当前判定采用的事实，通过 `material_id` 和 `quote` 对应原文及来源；原始模型分析与后续复核分别保留。

## 1. 安装并填写 AI 配置

需要 Python 3.12 和 uv，在项目根目录执行：

```powershell
uv sync --locked
uv run --no-sync python -m unittest discover -s tests
```

AI 配置就是项目内的 **`config/ai.json`**。首次拉取代码没有这个文件时，复制模板：

```powershell
Copy-Item config/ai.example.json config/ai.json
```

已经配置过时直接编辑现有文件，不要重新复制覆盖。填写示例：

```json
{
  "base_url": "https://api.deepseek.com",
  "api_key": "填入自己的 DeepSeek API Key",
  "model": "deepseek-flash",
  "max_requests": 5,
  "timeout_seconds": 60,
  "max_tokens": 4096
}
```

替换 Key 只需修改 `api_key`，不需要环境变量。程序通过 OpenAI SDK 调用配置地址下的 `/chat/completions`；使用 DeepSeek 工具调用、JSON 输出及关闭思考模式的参数。实际验证使用的模型是 `deepseek-flash`，接口格式参考 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/)。

`ai.json` 正常保存在本项目中并在运行时读取，只是被 Git 忽略，Key 也不写入结果。所谓本地配置指的就是这个文件；调用 AI 时 Key 会发送给配置的 AI 接口做认证，候选网站不会收到该 Key。

## 2. 直接用交付输入重新运行

交付已提供整理后的真实输入，不需要先重新访问所有网站：

```powershell
uv run --no-sync relay-intel run --run-id verify-001 --leads delivery/inputs/leads.jsonl --materials delivery/inputs/materials.jsonl
```

`run_id` 必须未使用。每次新分析、补证或修改规则使用新批次；固定材料重跑不等于重新验证网站当前状态。`delivery/inputs/` 为独立重跑统一了记录编号，原编号仍保存在情报和原批次中。

结果保存在 `runs/verify-001/batch.json`。首次调用之前固定材料和模型配置，每处理完一个域名就保存。中断后执行：

```powershell
uv run --no-sync relay-intel resume --run-id verify-001
```

`resume` 只处理尚待分析的域名，已完成和已记录失败的域名不重复调用。它读取批次中的固定材料和配置，Key 则读取当前 `config/ai.json`，因此可以换 Key 后继续。地址、模型和调用限额发生变化时应新建批次。已记录的失败排除原因后，另建批次重新分析。

模型每域最多 5 次请求，单次最多 60 秒；SDK 自动重试关闭。错误引用或格式仅允许一次修正。调用失败、拒答或校验不通过记录为分析失败，不生成业务标签。

## 3. 检查疑点并按需复核

查看 `assessment.review_reasons` 和 `investigation.analysis.concerns`。模型提出疑点的结果会进入 `pending`，暂不进入正式情报；模型没有提出疑点的结果，也可以主动复核。复核同样必须提供逐字引用，不能直接指定一个标签绕过规则。

复核文件每行一条 JSON，实际示例可看 `delivery/reviews.jsonl`。自行复核时填写新的批次名、域名、时间与实际复核者，不能把旧批次意见原样提交给新批次。

- `accept`：接受有依据的原保守标签，保留未知和未解决限制。
- `revise`：修订事实或处理模型疑点，声明 `new_label`，由规则重新计算。
- `fact_revisions`：替换指定事实的模型解释；不能覆盖原始人工反证。
- `resolved_concerns`：已处理疑点的序号，从 1 开始，必须引用相关材料。
- `reviewer`：写明实际复核者；AI 复核必须标明 AI。

```powershell
uv run --no-sync relay-intel review --run-id verify-001 --actions data/inputs/reviews.jsonl
```

每个结果只保存一次复核；相同意见重复提交会跳过。还需补材料或继续修订时创建新批次。程序验证引用存在和规则条件，语义判断仍需要核对来源。

本次交付中，目录时效未知、前端模板、跳转、软件文档等问题通过实际复核处理；没有把所有疑似强行升级为确认。

## 4. 关联扩展与导出

```powershell
uv run --no-sync relay-intel expand --run-id verify-001
uv run --no-sync relay-intel export --run-id verify-001
```

`expand` 仅整理完成必要复核的确认种子中实际观察到的 API、聊天或迁移链接。每个种子只整理一次，不递归、不自动继承标签，也不自动访问新站。新增线索写入 `runs/verify-001/expanded_leads.jsonl`；补充材料后另建批次分析。

`export` 写入：

- `runs/verify-001/exports/intelligence.json`：通过检查的四类标签记录。
- `runs/verify-001/exports/summary.json`：数量、分布、扩展与未完成项。

单批导出允许部分结果，成功返回只代表文件写完，必须检查 `unfinished`。需要汇总初始、重跑和扩展批次时使用：

```powershell
uv run --no-sync python scripts/export_delivery.py real-53-20260920 real-retry-20260920 real-correction-20260920 real-expanded-20260920 --output delivery
```

该命令适用于本次工作目录中的实际 `runs/`。汇总按列出的先后顺序选择每个域名最后一次结果；后一次失败不能退回旧成功。发现未完成、待复核、演示数据或不足 50 条时拒绝汇总。随附的 `delivery/batches/` 是本次原批次快照，查看已有交付不需要重新运行汇总。

返回码：0 表示命令完成；1 表示整体配置、输入或文件错误；2 表示参数错误，或分析、复核、扩展仍有需要处理的事项。

## 5. 更新公开材料

采集和分类分成两步，避免网页抓取细节混入业务判定。

```powershell
uv run --no-sync python scripts/collect_sources.py data/manifests/initial.json --output data/research-next
uv run --no-sync python scripts/collect_sources.py data/manifests/collection.json --output data/research-next
```

采集脚本顺序 GET，每次间隔 2 秒，设置 20 秒请求超时与 2 MB 上限。只保留可见文本、元描述和实际链接，不运行脚本。已有来源编号会跳过，因此重新采集使用新目录；失败也如实落盘。

`selection.json` 指明目标、发现来源、所选原文和材料属性。旧摘录可能随网页或文本归一化方式变化失效，需要先核对新采集结果并更新选取内容，再执行：

```powershell
uv run --no-sync python scripts/build_inputs.py data/manifests/selection.json --research data/research-next --output data/inputs/next
```

`build_inputs.py` 不会悄悄用新内容替代旧引用，原文或链接找不到就报错。

- `leads.jsonl`：发现值、发现网址、发现时间和发现方式。
- `materials.jsonl`：材料编号、目标域名、来源网址、采集时间、原文或失败原因。
- `source_kind`：自身公开说明为 `direct`，目录或转述为 `secondary`。
- `evidence_state`：当前材料、历史材料或时效未知。刚刚抓到旧目录不代表目录内容已经验证为当前事实。
- `subject_relation`：`exact` 或 `uncertain`；跨站 exact 材料必须出现目标完整主机名。
- `annotations`：可选的人工预标注。本次实际输入为空，由 AI 提取并通过复核修订。

本次用到的初始、采集、摘录及补充来源清单保存在 `data/manifests/`。完整网页提取内容位于本机被忽略的 `data/research/`；交付保留短证据和采集元数据。

## 6. 从哪里读代码

先读 `cli.py`，再读 `pipeline.py`。主线只有：

```text
读取线索和材料 → 整理候选 → 调用 Agent → 校验引用 → 判定 → 保存
                                                  ↓
                                             按需复核 → 导出
```

| 文件 | 职责 |
| --- | --- |
| `src/relay_intel/cli.py` | 参数解析、调用业务函数、打印状态 |
| `src/relay_intel/pipeline.py` | 准备批次、顺序分析、逐域保存、复核和扩展 |
| `src/relay_intel/agent.py` | DeepSeek 请求和两个本地只读工具的调用循环 |
| `src/relay_intel/investigation.py` | 材料归属、引用、链接检查和事实合并 |
| `src/relay_intel/assessment.py` | 五项事实含义、四标签、分档和复核重判 |
| `src/relay_intel/candidates.py` | 域名规范化、去重与一轮关联候选整理 |
| `src/relay_intel/contracts.py` | 输入、分析、复核、情报的数据结构 |
| `src/relay_intel/workspace.py` | 配置读取、JSONL 输入和文件保存 |
| `src/relay_intel/delivery.py` | 单批导出检查、七字段情报和统计 |
| `scripts/` | 公开采集、摘录整理、交付批次汇总 |

证据链为：`merge_facts` 合并事实 → `assess` 生成标签并保存实际依据 → `to_intelligence` 导出同一份依据。复核仍调用 `assess`，原始模型分析保持不变。

确认需要当前主机的第三方身份、用户模型接入和上游关系都有证据；普通模型名、兼容协议、通用模板不能单独证明中转。明确的非目标证据可以排除。其余按具体线索判为疑似或证据不足。

置信度是标签的证据分档（0.55 / 0.75 / 0.90），不是目标概率。`last_verified` 取支撑当前标签的材料时间；无关的新访问失败、重分析和导出不会刷新旧确认时间。公开业务说明也不等于后台转发实测。

当前按单进程顺序执行；定期更新通过人工启动新批次完成。没有加入并发调度、数据库、缓存系统或多轮版本管理。
