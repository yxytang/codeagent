#!/usr/bin/env python3
"""一个简单的文字冒险游戏。

用法::

    python games/text_adventure.py

在游戏中可随时输入 ``help`` 查看指令、``quit`` 退出。

设计要点：
* 游戏状态集中在 :class:`GameState`，便于单独测试。
* 场景与事件数据集中在 :data:`SCENES`，与流程控制解耦。
* :func:`parse_command` 负责把原始输入解析成命令，非法输入不会崩溃。
* :func:`run_game` 是主循环，输入输出以参数注入，方便自动化测试。
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, TextIO

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

MAX_ENERGY = 10
START_SCENE = "village"

HELP_TEXT = """可用指令：
  look / l            查看当前场景描述
  go <出口>           前往某个出口，例如 go forest
  take <物品>         拾取当前场景中的物品
  inventory / i       查看背包与状态
  use <物品>          使用背包中的物品
  help / h            显示本帮助
  quit / q            退出游戏
"""


class GameError(Exception):
    """可预期的玩法错误（例如出口不存在），由主循环转为友好提示。"""


# --------------------------------------------------------------------------- #
# 游戏状态
# --------------------------------------------------------------------------- #


@dataclass
class GameState:
    """保存一局游戏的全部可变状态。"""

    scene: str = START_SCENE
    energy: int = MAX_ENERGY
    inventory: List[str] = field(default_factory=list)
    flags: Dict[str, bool] = field(default_factory=dict)
    turns: int = 0
    rng: random.Random = field(default_factory=random.Random)
    #: 结束时写入结果，None 表示游戏仍在进行。
    outcome: Optional[str] = None

    def has(self, item: str) -> bool:
        """背包中是否持有 ``item``。"""
        return item in self.inventory

    def add(self, item: str) -> None:
        """拾取物品，重复拾取同一物品是空操作。"""
        if item and item not in self.inventory:
            self.inventory.append(item)

    def spend_energy(self, amount: int) -> None:
        """消耗体力；体力归零时判定失败。"""
        self.energy = max(0, self.energy - amount)
        if self.energy == 0 and self.outcome is None:
            self.outcome = "lose"

    def wait(self, turns: int = 1) -> None:
        """推进回合数。"""
        self.turns += turns


# --------------------------------------------------------------------------- #
# 场景 / 事件数据
# --------------------------------------------------------------------------- #


@dataclass
class Scene:
    """一个场景的静态数据。"""

    key: str
    name: str
    description: str
    exits: Dict[str, str]
    items: Dict[str, str] = field(default_factory=dict)
    #: 进入场景时执行的钩子，可修改状态并返回额外文本。
    on_enter: Optional[Callable[[GameState], str]] = None


def _enter_forest(state: GameState) -> str:
    """森林入口：每次进入消耗 1 点体力。"""
    state.spend_energy(1)
    if state.outcome:
        return "你筋疲力尽，倒在了森林边缘……"
    return "潮湿的苔藓让你的脚步变得沉重。"


def _loot_sword(state: GameState) -> str:
    """溪流深处的结算：拿到光之剑但消耗体力，或者溺水结束。"""
    state.add("光之剑")
    state.flags["sword_found"] = True
    state.spend_energy(3)
    if state.outcome:
        return "你在水底耗尽了最后一丝力气，水面之上再也没有涟漪。"
    return "你浮出水面，手中握着一把散发着微光的长剑。"


def _enter_castle(state: GameState) -> str:
    """城堡大厅：结算最终胜负。"""
    if state.has("光之剑"):
        state.outcome = "win"
        return "你举起光之剑，黑暗四散退去，城堡重归光明！"
    state.outcome = "lose"
    return "守卫的阴影扑了上来——你手里没有能对抗黑暗的东西。"


SCENES: Dict[str, Scene] = {
    "village": Scene(
        key="village",
        name="灰石村",
        description=(
            "你站在灰石村的广场上，四周是低矮的石屋。\n"
            "北边的树林黑得异常，村口立着一块石碑，上面刻着一行字。"
        ),
        exits={"forest": "forest", "stone": "stone"},
        items={"石碑": "the_stone"},
    ),
    "stone": Scene(
        key="stone",
        name="村口石碑",
        description=(
            "石碑上写着：「欲过黑林者，需以火照路，以诚待影。」\n"
            "碑座下压着一支火把，似乎有人刻意留下的。"
        ),
        exits={"village": "village"},
        items={"火把": "torch"},
    ),
    "forest": Scene(
        key="forest",
        name="迷雾森林",
        description=(
            "迷雾缠绕着树干，一条小路分岔成两边。\n"
            "左边的路上有一双发亮的眼睛，右边则隐约传来流水声。"
        ),
        exits={"left": "clearing", "right": "stream", "village": "village"},
        on_enter=_enter_forest,
    ),
    "stream": Scene(
        key="stream",
        name="林间溪流",
        description=(
            "清澈的溪水边，一名受伤的旅人靠着石头，他对你伸出手。\n"
            "水里似乎有什么东西在闪光。"
        ),
        exits={"forest": "forest", "dive": "stream_dive"},
        items={"草药": "herb"},
    ),
    "stream_dive": Scene(
        key="stream_dive",
        name="溪流深处",
        description="你潜入水中，摸到了一件冰凉的金属器物，同时呛了好几口水。",
        exits={"stream": "stream"},
        on_enter=_loot_sword,
    ),
    "clearing": Scene(
        key="clearing",
        name="林间空地",
        description=(
            "空地上站着一只巨大的灰狼，它低吼着逼近。\n"
            "空地尽头是通往黑城堡的石门。"
        ),
        exits={"castle": "castle", "forest": "forest"},
    ),
    "castle": Scene(
        key="castle",
        name="黑城堡大厅",
        description="大厅中央悬浮着一团不断翻涌的黑暗，它正朝你扑来。",
        exits={},
        on_enter=_enter_castle,
    ),
}


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Command:
    """解析后的命令，``name`` 为动词，``argument`` 为可能为空的宾语。"""

    name: str
    argument: str = ""

    @property
    def raw(self) -> str:
        return f"{self.name} {self.argument}".strip()


#: 命令别名 -> 规范动词
ALIASES = {
    "l": "look",
    "look": "look",
    "查看": "look",
    "go": "go",
    "move": "go",
    "前往": "go",
    "take": "take",
    "get": "take",
    "拾取": "take",
    "i": "inventory",
    "inventory": "inventory",
    "背包": "inventory",
    "use": "use",
    "使用": "use",
    "h": "help",
    "help": "help",
    "帮助": "help",
    "q": "quit",
    "quit": "quit",
    "exit": "quit",
    "退出": "quit",
}


def parse_command(raw: str) -> Optional[Command]:
    """把玩家输入解析成命令。

    * 空输入（包括只输入空格）返回 ``None``。
    * 无法识别的动词返回 ``Command("unknown", ...)``，由调用方给出提示。
    """
    text = (raw or "").strip()
    if not text:
        return None
    parts = text.split()
    verb = ALIASES.get(parts[0].lower())
    if verb is None:
        return Command("unknown", text)
    argument = " ".join(parts[1:])
    return Command(verb, argument)


# --------------------------------------------------------------------------- #
# 命令处理
# --------------------------------------------------------------------------- #


def handle_command(state: GameState, command: Command) -> str:
    """执行一条命令并返回要显示给玩家的文本。

    真正的玩法失败通过 :class:`GameError` 抛出，由主循环捕获后转为提示，
    这样状态不会被半途修改成不一致的值。
    """
    if command.name == "look":
        return describe_scene(state)

    if command.name == "help":
        return HELP_TEXT.rstrip()

    if command.name == "inventory":
        items = "、".join(state.inventory) if state.inventory else "（空）"
        return f"体力：{state.energy}/{MAX_ENERGY}　回合：{state.turns}\n背包：{items}"

    if command.name == "go":
        return _handle_go(state, command.argument)

    if command.name == "take":
        return _handle_take(state, command.argument)

    if command.name == "use":
        return _handle_use(state, command.argument)

    return f"我不明白「{command.raw}」，输入 help 查看可用指令。"


def _handle_go(state: GameState, direction: str) -> str:
    scene = SCENES[state.scene]
    if not direction:
        available = "、".join(scene.exits) or "（无）"
        raise GameError(f"你想去哪里？可用的出口：{available}")
    key = scene.exits.get(direction.lower())
    if key is None:
        raise GameError(f"这里没有「{direction}」这个方向。")
    state.wait()
    state.scene = key
    return _enter_scene(state)


def _handle_take(state: GameState, item: str) -> str:
    scene = SCENES[state.scene]
    if not item:
        raise GameError("你要拾取什么？")
    if item not in scene.items:
        raise GameError(f"这里没有「{item}」。")
    state.add(item)
    state.flags[f"took_{item}"] = True
    state.wait()
    return f"你把「{item}」收进了背包。"


def _handle_use(state: GameState, item: str) -> str:
    if not item:
        raise GameError("你要使用什么？")
    if not state.has(item):
        raise GameError(f"背包里没有「{item}」。")

    if item == "火把":
        if state.scene != "forest":
            return "火把在这里派不上用场，你把它收了起来。"
        state.flags["torch_lit"] = True
        return "火把亮了起来，迷雾后面的小路清晰了：左边是空地，右边是溪流。"

    if item == "草药":
        if state.energy >= MAX_ENERGY:
            return "你的体力还很充足，暂时用不上草药。"
        healed = min(3, MAX_ENERGY - state.energy)
        state.energy += healed
        state.inventory.remove(item)
        state.wait()
        return f"你用草药处理了伤口，恢复了 {healed} 点体力。"

    if item == "光之剑":
        return "剑身在你手中嗡鸣，等待面对黑暗的一刻。"

    return f"你不知道该怎么使用「{item}」。"


def _enter_scene(state: GameState) -> str:
    """进入当前 ``state.scene`` 并返回包含描述的文本。"""
    scene = SCENES[state.scene]
    lines = [f"【{scene.name}】"]
    extra = scene.on_enter(state) if scene.on_enter else None
    lines.append(scene.description)
    if extra:
        lines.append(extra)
    if state.outcome is None:
        exits = "、".join(scene.exits) or "（无路可走）"
        lines.append(f"出口：{exits}")
    return "\n".join(lines)


def describe_scene(state: GameState) -> str:
    """仅描述当前场景，不触发进入钩子。"""
    scene = SCENES[state.scene]
    items = "、".join(scene.items) if scene.items else "（没有值得拿的东西）"
    exits = "、".join(scene.exits) or "（无路可走）"
    return (
        f"【{scene.name}】\n{scene.description}\n"
        f"可见物品：{items}\n出口：{exits}"
    )


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #


def run_game(
    inp: Optional[Iterable[str]] = None,
    out: TextIO = sys.stdout,
    seed: Optional[int] = None,
) -> GameState:
    """运行游戏主循环。

    :param inp: 输入行迭代器；为 ``None`` 时从标准输入读取。
        传入字符串序列即可做自动化测试。
    :param out: 输出流。
    :param seed: 随机种子，便于复现同一局。
    :return: 结束时（或被 ``quit``）的 :class:`GameState`。
    """
    state = GameState(rng=random.Random(seed))
    quit_requested = False

    def emit(text: str) -> None:
        print(text, file=out)

    emit("=" * 46)
    emit("灰石村的委托：带着光之剑击退黑城堡的黑暗。")
    emit("输入 help 查看指令。")
    emit("=" * 46)
    emit(_enter_scene(state))

    iterator = _line_source(inp)
    while state.outcome is None and not quit_requested:
        try:
            raw = next(iterator)
        except StopIteration:
            emit("（输入结束，游戏关闭。）")
            break

        emit(f"> {raw.strip()}")
        command = parse_command(raw)
        if command is None:
            emit("请先输入一个指令，例如 look。")
            continue
        if command.name == "quit":
            quit_requested = True
            emit("再见，旅人。")
            break

        try:
            result = handle_command(state, command)
        except GameError as exc:
            emit(f"× {exc}")
            continue
        emit(result)

        if state.outcome == "win":
            emit(f"\n★ 胜利！你在 {state.turns} 回合内驱散了黑暗。")
        elif state.outcome == "lose":
            emit("\n☠ 失败：你没能走出这片土地。")

    return state


def _line_source(lines: Optional[Iterable[str]]):
    """返回输入行迭代器：``lines`` 非空时按序消费，否则读取标准输入。"""
    if lines:
        return iter(lines)

    def _stdin_lines():
        for line in sys.stdin:
            yield line.rstrip("\n")

    return _stdin_lines()


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="灰石村的委托 —— 简单文字冒险游戏")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    parser.add_argument(
        "--script",
        default=None,
        help="用分号分隔的指令序列（非交互模式，便于验证）",
    )
    args = parser.parse_args(argv)

    if args.script is not None:
        commands = [c.strip() for c in args.script.split(";") if c.strip()]
        run_game(inp=commands, out=sys.stdout, seed=args.seed)
        return 0

    try:
        run_game(out=sys.stdout, seed=args.seed)
    except (KeyboardInterrupt, EOFError):
        print("\n游戏被中断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
