# XCode 20 task Benchmark

这套小型评测用于验证 Agent 能否修改真实文件、利用公开测试反馈，并通过独立验收。它覆盖五类任务，每类四题，共 20 题，不是 SWE-bench 或生产级完成率基准。

已完成的真实 DeepSeek 四组对照、原始与复评分、测试反馈案例及结论边界见 [2026-10-21 评测记录](RESULTS_20261021.md)。

## 先验证评测器

从仓库根目录运行，无需模型凭据：

```powershell
.\scripts\benchmark.ps1 -Mode list
.\scripts\benchmark.ps1 -Mode selfcheck
```

对应 Python 入口：

```bash
python -m benchmarks --list
python -m benchmarks --self-check
```

自检分别运行每道题的参考实现与未修改初始版本：参考实现应通过，初始缺陷或空实现应被拒绝。参考实现还必须通过公开 unittest discovery，并实际发现至少一个测试，避免因题目缺少公开测试而系统性误判。测试编写题还要求生成的测试在正确实现上通过，并检出三个预置错误变体。自检证明验收器能区分这些正负样例，**不代表 Agent 的模型完成率**。

## 任务集

| 类别 | 题数 | 内容 |
| --- | ---: | --- |
| 代码理解 | 4 | FIFO 缓存、账单去重、依赖分类、retry 与 finally 的实际行为，输出 answers.json |
| Bug 修复 | 4 | 区间合并、Decimal 金额舍入、分页生成器、TTL 到期语义 |
| 功能实现 | 4 | Unicode slug、配置解析、重试函数、CSV 金额汇总 |
| 重构 | 4 | 抽取规范化函数、Repository、纯验证逻辑、可扩展 formatter registry |
| 测试编写 | 4 | 回文、运费、LRU、区间合并；用变异验收检查测试有效性 |

任务契约、初始文件、参考解和验收定义在 `task_suite.py`，每次生成全新字典。模型只收到任务目标和初始文件；参考解与私有验收源代码不复制进 Agent 工作目录。私有验收在模型运行结束后另行执行，结果不会反馈给模型继续调试本题。

这是评测输入隔离，不是 OS 安全沙箱。执行的 Python 代码仍可能访问当前用户资源；仅运行自己信任的模型输出，远端部署需要独立容器或系统隔离。

## 四组对照

| 组别 | 上下文压缩 | 执行进度提示 |
| --- | --- | --- |
| Baseline | 关闭 | 关闭 |
| Context Manager | 开启 | 关闭 |
| Prompt Guidance | 关闭 | 开启 |
| Full Harness | 开启 | 开启 |

这是 Context 与 Prompt 的 2×2 消融。Prompt Guidance 是现有 `remaining_rounds / completed_reads` 等固定执行进度提示，不是学习、搜索或训练得到的 Prompt Optimizer。Full 表示两个组件同时开启，不表示其他组没有 Agent Loop、规划或工具系统。

各组保持同一冻结的模型连接、任务、规划策略、工具定义、命令授权及预算。默认单执行线程、每次最多三子任务；MCP 关闭、技能目录为空，每个 task × variant × repeat 都创建独立工作区和 SQLite，避免前一题的历史记忆泄漏。各题轮换组别执行顺序，减少固定先后顺序的影响。

小任务可能根本不触发压缩。报告会记录实际触发量；未触发时不能用本次结果说明压缩有效，更不能预先照搬参考图片里的 90% 与 75%。

## 配置好凭据后运行真实模型

Key 只从当前进程的环境变量读取，不接受命令行 Key、不写入报告或公共配置。可在同一个 PowerShell 窗口中隐藏输入：

```powershell
$benchmarkCredential = Read-Host 'DeepSeek API Key' -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', $benchmarkCredential).Password
```

默认使用项目原有的 DeepSeek 兼容连接参数，也可在本机设置 `DEEPSEEK_BASE_URL` 与 `DEEPSEEK_MODEL`。首次先跑一个任务两组冒烟，再跑全量：

```powershell
.\scripts\benchmark.ps1 -Mode live -Tasks bugfix-01 -Variants baseline,full -MaxBatchTokens 100000
.\scripts\benchmark.ps1 -Mode live -MaxBatchTokens 3000000 -Output '.xcode\benchmarks\live-first'
```

Python 等价命令及详细参数：

```bash
python -m benchmarks --live --tasks bugfix-01 --variants baseline full --max-batch-tokens 100000
python -m benchmarks --live --variants baseline context prompt full --max-batch-tokens 3000000 --output .xcode/benchmarks/live-first
python -m benchmarks --help
```

20 题 × 四组 × 一轮共计划 80 次 Agent 运行；重复三轮是同样 20 题各测三次，不会变成 60 个独立题目。真实模式显式要求 `--live`，会消耗模型额度。默认每次最多 18 个逻辑模型调用、48 个工具调用、32,000 已报告 Token、180 秒任务时限，整批为 500,000 已报告 Token；各组限制相同。

上面的全量示例将整批预算显式设为 3,000,000；默认 500,000 不保证完成 80 次。两组冒烟示例也预留了在途响应越过单题阈值后的批次余量。实际预算与已执行数量以每份报告为准，预留额度不等于一定消耗。

批次剩余额度不足以容纳完整的单题预算时，剩余题目不再启动，不会降低后续组的预算。Token 达到已报告阈值后才禁止下一次请求，最后一个在途响应仍可能越界。失败请求或取消响应可能没有 usage，此时标记不完整并停止后续题目，因此这些上限和统计不等于精确账单。HTTP 调用与子进程的取消也不是硬实时抢占；若取消后仍有在途工作，整批停止，避免与下一题交叠。独立验收超时会清理它启动的进程树，无法确认清理时也停止批次。

## 代码测试反馈与成功判定

Agent 可以运行授权的公开 `unittest` 命令。退出码和合并的标准输出、错误输出回填工具消息，使它能在预算内依据失败信息继续修改并重跑。指标记录每次命令结果、失败次数和失败后再次通过的观察序列；后者不能单独证明是哪次代码改动造成的。

最后还会对汇总副本运行公开验证。**公开验证通过或状态 ready 仍不等于成功**：评测器随后复制候选文件到单独目录，用评测方维护的验收验证功能、边界或变异检出能力；只有运行状态为 ready、未超时且独立验收通过，任务才记成功。修改或清空公开测试无法替代私有验收。

## 指标与报告

| 指标 | 口径 |
| --- | --- |
| 成功率 | 每组通过独立验收的任务数 / 已计划任务数；整组未完成时主成功率留空，另列已执行分母 |
| Model Rounds | 包括规划的逻辑模型调用；Provider 内部 HTTP 重试不另算，不能当 HTTP 请求数 |
| Tool Calls 与 Errors | 实际工具调用尝试；异常、无效参数、非零命令退出和超时分别可追踪 |
| Token | Provider 已返回的 prompt / completion / total usage；缺失标记不完整，不补成零消耗 |
| Latency | 从 Engine 创建任务到结束的墙钟耗时；独立验收耗时另列；平均值包含失败尝试 |

另外记录上下文压缩触发量和公开测试结果。固定任务定义、模型标识、预算、Python 版本、任务集指纹、逐题结果和失败原因均落盘，便于重现。采样参数沿用 Provider 默认值，四组相同；未声称模型输出完全确定。

默认保存到 `.xcode/benchmarks/<UTC时间>/`：

- `report.json`：原始逐题记录、配置和汇总；每完成一项即更新。
- `report.md`：四组与各类别的结果表。
- `results.csv`：完整计划，包括未执行项，便于筛选和比较。
- `resume_excerpt.md`：根据实际状态生成的简历参考文字；离线自检不会生成模型成功率。
- `cells/`：每题的初始文件、隔离 Runtime、候选代码、命令日志与独立验收。

输出目录必须新建或为空，防止覆盖既有数据。遇到批次预算耗尽或运行器异常会保留已完成结果，未运行项单列，不能把部分任务的结果当完整 20 题成绩。暂不自动恢复或拼接旧批次。
