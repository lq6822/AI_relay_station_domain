# AI 中转站域名情报原型

按 plan.md 的 Python 单机 CLI、人工公开取证、单 Agent 分析、程序校验、按需人工复核和 JSON 交付路线开发。
模型按用户最新指定固定为 `deepseek-v4-flash`，经现有中转的 Anthropic Messages 协议调用。
API 地址使用 `ANTHROPIC_BASE_URL`，密钥使用 `ANTHROPIC_API_KEY`；没有默认官方地址或备用模型。

本轮是研发。真实模型验收、50 个真实域名、实际种子扩展、六个真实案例和限页方案尚未完成。
默认测试完全离线，使用 unittest 和 AsyncMock；测试数据不能计为真实情报。

## 安装与测试

需要 Python 3.12 和 uv。源码工作目录即本项目根目录。

```powershell
uv sync --locked
uv run --no-sync python -m unittest discover -s tests
uv run --no-sync relay-intel --help
```

本开发环境也可使用 `.tools/Scripts/uv.exe`；`.tools` 只用于本地安装工具，不是运行依赖。
依赖只有一个锁文件 `uv.lock`。安装后域名公共后缀只使用 tldextract 随包快照，不运行时下载。

## 运行

仅人工采集公开资料，不自动抓取候选站点。输入保存为 UTF-8 JSONL，每行一个对象，放在 `data/inputs/`。
以下样例是合成数据，用于说明字段，不能作为真实取证。阶段文件不可手改。

`leads.jsonl`：

```json
{"lead_id":"lead-1","raw_value":"https://relay.example.com/","source_url":"https://directory.example.com/list","discovery_method":"manual_public_source","discovered_at":"2026-09-19T08:00:00+08:00"}
```

`materials.jsonl`：

```json
{"material_id":"m-1","domain":"relay.example.com","source_url":"https://relay.example.com/about","collected_at":"2026-09-19T09:00:00+08:00","access_status":"ok","evidence_state":"current","excerpt":"合成演示：我们是独立第三方，向用户提供模型 API，并将请求转发到外部模型提供方。","annotations":[{"fact":"third_party","value":"supported","quote":"独立第三方"},{"fact":"model_access","value":"supported","quote":"向用户提供模型 API"},{"fact":"upstream_proxy","value":"supported","quote":"将请求转发到外部模型提供方"}],"source_kind":"direct","subject_relation":"exact"}
```

失败材料使用 `access_status=failed/blocked` 与真实 `failure_reason`，省略 excerpt 和 annotations。
`published_at` 不知道时省略；所有时间必须有时区。`evidence_state` 为 current/historical/unknown。
source_kind=secondary 表示转述；subject_relation=uncertain 表示主体关联不明确，不能确认当前主机名。
跨站来源若声明 exact，片段中必须能定位当前完整主机名；仅兄弟子域或同注册域不算证据。
人工负责核验材料与主机名关系、时效及事实真实性，程序只验证引用及基本条件。

```powershell
# 在本机设置现有中转地址和密钥后运行；不要将密钥写入源码或输入材料。
relay-intel run --run-id demo --leads data/inputs/leads.jsonl --materials data/inputs/materials.jsonl --synthetic
relay-intel review --run-id demo --actions data/inputs/reviews.jsonl
relay-intel expand --run-id demo
relay-intel export --run-id demo
```

真实资料运行时省略 `--synthetic`；同一批次不能切换合成标记。保留域名样例自动视为合成。
`run` 接收新增材料时只重做受影响主机。没有材料或模型调用失败的对象保留为未完成，不伪造证据不足。
新发现域名先进入 candidates，人工补 leads/materials 后重新 run，独立分析，不继承种子结论。

## 判断与复核

确认需要第三方身份、用户模型访问、上游代理关系，且无影响判断的未解决反证。疑似需要具体中转线索。
排除需要明确非目标证据；证据不足必须有实际调查。兼容 API、模型名、模板本身不证明中转。
公开声明不能写成后台转发实测，服务标签不表示恶意或违法。

confidence 是所选标签的证据支持程度：未定=null（仅草稿）、暂定=0.55、有限支持=0.75、充分支持=0.90。
依据为 engineering_default，方法 label_evidence_grade_v1，未经统计校准，不等同于目标概率或正确率。
复核由影响判断的缺失、冲突、时效或业务不明确触发，不按标签或分值强制触发。

`reviews.jsonl` 样例（版本和时间必须对应当前待复核判断）：

```json
{"run_id":"demo","domain":"relay.example.com","assessment_version":1,"action":"accept","reviewer":"人工复核人","reviewed_at":"2026-09-20T10:00:00+08:00","reason":"核对引用后接受有依据的保守结论，未知事实继续保留。","citations":[{"material_id":"m-1","quote":"独立第三方"}]}
```

revise 还需 `new_label`，可用 `fact_revisions` 提交带 material_id/fact/value/quote 的人工解释，明确替换该事实的模型解释。
原调查、人工标注和复核记录全部保留；不允许用新解释静默覆盖原人工反证。程序重新计算标签和置信度。
改正原人工标注需建立新批次；新增真实观察用新 material_id。重复动作不重复增加版本。

## 接入约束

AsyncAnthropic 关闭 SDK 自动重试，每次域名分析最多 5 次请求、每次 60 秒、校验错误最多修正 1 次且计入总上限。
客户端发送两个只读工具，以及 `output_config.format` JSON Schema，再用 Pydantic 严格验证结果及引用。
现有中转须实际支持该字段；不能因 Messages 协议兼容就假定具备服务端结构化约束。
实际接入前用少量真实材料验证工具调用、结构化结果及用量，不用离线测试冒充真实模型验收。
官方接口参数参考：[Anthropic Messages 与结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)。

网页和材料中的指令只当数据，工具只读取本域允许的材料；没有 Shell、任意文件、网络取证或发布工具。
人工输入前须清除密钥和私人信息，仅提交必要公开材料。错误日志不回显原始 SDK HTTP 响应。

## 保存、交付与持续生产

`runs/<run_id>/` 保存 manifest、candidates、investigations、assessments、issues 和 exports。
同 ID 同内容重复导入不增加记录，同 ID 异内容拒绝；配置、模型、提示词改变须新批次。
源码变化使原复用失效。单域输入、配置、源码、提示词以及引用调查版本均一致才可复用。
模型失败也在 Investigation 中保存本次材料、调用摘要和失败原因，analysis 留空且不生成业务判断。
新调查使旧复核失效，导出只考虑当前版本，禁止回退发布旧判断。

写入采用单进程锁和临时文件原子替换。导出前标记未完成，两个导出文件均保存成功才标记完成；
这不是跨文件事务。读取交付结果须检查 manifest.export_completed 和 summary 的 intelligence 摘要。
保存失败时重新 export 整组文件，不把残留文件当成本次成功结果。

情报包含 domain/label/confidence/evidence/reason/discovery_source/last_verified 七字段。
last_verified 来源于实际材料采集时间，重复分析、复核或导出不刷新。
summary 明确列出数量、注册域覆盖、待复核、失败、扩展和交付缺项；不足 50 条或缺项时不宣布任务完成。
案例及不超过 5 页的 `docs/submission/方案.docx` 留待真实生产后制作，不创建空占位文件。

持续生产：人工补充公开线索及材料 → run → 按需 review → 一轮 expand → 独立补证 → export。
不引入数据库、Web 服务、定时调度或多 Agent；E1/E2/E3 可选增强未实现。

## 排错

命令返回 0 表示操作完成，1 表示关键配置/存储错误，2 表示参数错误或业务结果仍有未完成项。
先按 issues 的 domain/location/stage 定位，修改被拒绝的输入后重跑；已经导入的 ID 不可变更内容。
锁残留时核对 `.writer.lock` 的进程是否退出后再人工清理，不自动删除活动锁。
损坏文件、关键配置错误及保存失败停止相关操作，不继续覆盖有效文件。
开发错误先定位到具体文件/函数，最小修改，运行该模块及直接受影响测试，再继续；交付前全量回归及 wheel 冒烟。

AI 编程工具实际用于契约、代码、离线测试和故障定位。运行时 Agent 的真实验收状态仍待 API 地址、密钥和真实小样本具备后确认。

## 本轮研发验证记录（2026-09-20）

三个测试文件共 59 项离线用例。单元与流程回归覆盖四标签、必要复核、错误修正上限、工具越权、
49/50 条计数边界、输入复用失效、材料 ID 冲突、旧复核失效及导出保存失败。
独立虚拟环境按 uv.lock 的版本和哈希安装依赖，再安装 wheel，以隔离模式验证 run/review/expand/export：
返回码依次为 2/0/0/2，导出 2 条合成记录、真实计数 0，两个复核路径及输出摘要检查通过。
打包产物为 `dist/ai_relay_intel-0.1.0.tar.gz` 和 `dist/ai_relay_intel-0.1.0-py3-none-any.whl`。

已复现并最小修复的业务缺陷：

| 文件与函数 | 原因 | 修复及复测 |
| --- | --- | --- |
| delivery.py / eligible | 只核对调查和判断之间的摘要，遗漏当前候选来源变化 | 核对候选、材料及配置摘要；流程回归通过 |
| cli.py / analyze_domains | 模型失败只存 Issue，未保存已导入材料 | 保存失败 Investigation，保持材料 ID 不可变；重试与失败回归通过 |
| investigation.py / MaterialTools.__init__ | 跨站材料的 exact 主体关联未检查 | 要求原文中可定位目标完整主机名；判断及流程回归通过 |
| candidates.py / expand_seed | 扩展记录缺少模型调用和用量追溯 | 保存工具、次数、用量及执行类型；候选及流程回归通过 |

环境问题单独处理：Windows 系统临时目录权限导致 ensurepip 和部分测试无法执行，经授权后复测；
依赖下载受沙箱网络限制，经授权生成锁文件并安装依赖。没有为这些环境问题增加业务兼容分支。

尚未通过真实验收：S0 真实小样本与模型运行条件、S1 真实工具调用和结构化分析、S2 真实情报生产、S3 真实案例及限页方案。
`ANTHROPIC_BASE_URL` 仍由用户确认后提供。结构化输出是否被现有中转实际支持，须在真实 API 冒烟中核验。
