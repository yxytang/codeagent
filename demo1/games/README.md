# 灰石村的委托 —— 简单文字冒险游戏

一个只用 Python 标准库实现的命令行文字冒险游戏：在 7 个场景之间探索，
带着「光之剑」闯进黑城堡，驱散那里的黑暗。

## 环境要求

- Python 3.7 及以上（脚本使用 `from __future__ import annotations`，无需第三方依赖）。

检查版本：

```bash
python --version
```

如果你的环境里 `python` 不可用，把下面命令中的 `python` 换成 `python3`。

## 如何运行

在**项目根目录**（即包含 `games/` 目录的那一层）执行：

```bash
python games/text_adventure.py
```

游戏启动后会打印开场白与第一个场景，随后等待你输入指令。按 `Ctrl+C` 可随时中断。

也可以用非交互模式验证一局固定流程（指令之间用 `;` 分隔）：

```bash
python games/text_adventure.py --script "go stone;take 火把;go village;go forest;use 火把;go right;go dive;go stream;go forest;go left;go castle"
```

这条脚本会走完整条通关路线，最后输出 `★ 胜利！`。

可选参数：

| 参数 | 说明 |
| --- | --- |
| `--script "指令;指令"` | 非交互模式，按顺序执行指令序列 |
| `--seed N` | 指定随机种子，便于复现同一局 |
| `-h` / `--help` | 查看命令行帮助 |

## 操作方式

在提示符下输入指令并回车。动词不区分大小写，部分指令有中文别名：

| 指令 | 中文别名 | 作用 |
| --- | --- | --- |
| `look` / `l` | `查看` | 查看当前场景描述、可见物品与出口 |
| `go <出口>` | `前往` | 前往某个出口，例如 `go forest`、`前往 stone` |
| `take <物品>` | `拾取` | 拾取当前场景中的物品 |
| `use <物品>` | `使用` | 使用背包里的物品 |
| `inventory` / `i` | `背包` | 查看体力、回合数与背包内容 |
| `help` / `h` | `帮助` | 显示指令帮助 |
| `quit` / `q` | `退出` | 退出游戏 |

说明：

- 出口与物品名称请照抄场景描述里出现的词（例如 `go right`、`take 火把`）。
- 输入空行或无法识别的指令不会导致游戏崩溃，只会给出提示。

## 游戏目标

穿过迷雾森林、取得沉在溪流深处的**光之剑**，然后进入黑城堡大厅击退黑暗。

- **胜利**：带着光之剑抵达黑城堡大厅。
- **失败**：没能拿到光之剑就闯进城堡；或体力（初始 10 点）降到 0。
- 也可随时用 `quit` 主动退出，此时不会判定胜负。

几个关键机制：

- **体力**：每次进入迷雾森林消耗 1 点，潜水取剑消耗 3 点；体力归零即失败。
- **火把**：在村口石碑处取得，在森林里 `use 火把` 可以点亮道路、看清两个方向。
- **草药**：在林间溪流拾取，体力不满时 `use 草药` 可回复最多 3 点体力（每次消耗一份）。
- **回合数**：每次移动或拾取都会推进一回合，胜利时会显示所用回合数。

## 推荐通关路线

1. `go stone` → `take 火把` → `go village`
2. `go forest` → `use 火把` → `go right`
3. 在溪流 `take 草药`（可选），再 `go dive` 取剑，然后 `go stream`
4. `go forest` → `go left` → `go castle`

## 运行测试

项目自带标准库 `unittest` 测试，无需安装任何依赖：

```bash
python -m unittest discover -s games -p 'test_*.py'
```

测试覆盖指令解析、各指令的正常与异常分支、体力耗尽、完整胜利/失败路径等。
也可以用详细模式查看每个用例名：

```bash
python -m unittest discover -s games -p 'test_*.py' -v
```

## 目录结构

```
games/
├── text_adventure.py       # 游戏主程序（场景数据、指令解析、主循环）
├── test_text_adventure.py  # 单元测试
└── README.md               # 本文件
```
