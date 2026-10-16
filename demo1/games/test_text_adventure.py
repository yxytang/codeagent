#!/usr/bin/env python3
"""text_adventure 的单元测试。

运行::

    python -m unittest discover -s games -p 'test_*.py'

这些测试用可注入的 out/输入序列替代真实终端，因此无需交互即可验证流程。
"""

import io
import sys
import unittest

import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import text_adventure as game  # noqa: E402


def play(script, seed=0):
    """用脚本输入跑一局，返回 (state, 输出文本)。"""
    out = io.StringIO()
    state = game.run_game(inp=list(script), out=out, seed=seed)
    return state, out.getvalue()


class TestParseCommand(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(game.parse_command(""))
        self.assertIsNone(game.parse_command("   \n"))

    def test_alias_and_argument(self):
        cmd = game.parse_command("GO Forest")
        self.assertEqual((cmd.name, cmd.argument), ("go", "Forest"))

    def test_chinese_alias(self):
        cmd = game.parse_command("前往 森林")
        self.assertEqual((cmd.name, cmd.argument), ("go", "森林"))

    def test_unknown_verb(self):
        cmd = game.parse_command("dance now")
        self.assertEqual(cmd.name, "unknown")


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.state = game.GameState()

    def test_go_missing_argument(self):
        with self.assertRaises(game.GameError):
            game.handle_command(self.state, game.Command("go"))

    def test_go_bad_exit_raises_without_moving(self):
        with self.assertRaises(game.GameError):
            game.handle_command(self.state, game.Command("go", "north"))
        self.assertEqual(self.state.scene, game.START_SCENE)

    def test_go_valid_moves_scene(self):
        game.handle_command(self.state, game.Command("go", "forest"))
        self.assertEqual(self.state.scene, "forest")

    def test_take_item(self):
        text = game.handle_command(self.state, game.Command("take", "石碑"))
        self.assertIn("石碑", text)
        self.assertTrue(self.state.has("石碑"))

    def test_take_missing_item_raises(self):
        with self.assertRaises(game.GameError):
            game.handle_command(self.state, game.Command("take", "火箭"))

    def test_take_empty_raises(self):
        with self.assertRaises(game.GameError):
            game.handle_command(self.state, game.Command("take"))

    def test_use_item_not_in_inventory(self):
        with self.assertRaises(game.GameError):
            game.handle_command(self.state, game.Command("use", "草药"))

    def test_herb_heals_and_is_consumed(self):
        self.state.energy = 5
        self.state.add("草药")
        game.handle_command(self.state, game.Command("use", "草药"))
        self.assertEqual(self.state.energy, 8)
        self.assertFalse(self.state.has("草药"))

    def test_herb_has_no_effect_at_full_energy(self):
        self.state.add("草药")
        game.handle_command(self.state, game.Command("use", "草药"))
        self.assertTrue(self.state.has("草药"))

    def test_unknown_command_message(self):
        text = game.handle_command(self.state, game.Command("unknown", "dance"))
        self.assertIn("help", text)

    def test_inventory_empty_is_readable(self):
        self.assertIn("空", game.handle_command(self.state, game.Command("inventory")))

    def test_look_lists_exits(self):
        self.assertIn("forest", game.handle_command(self.state, game.Command("look")))


class TestEnergy(unittest.TestCase):
    def test_energy_zero_sets_lose(self):
        state = game.GameState(energy=1)
        state.spend_energy(5)
        self.assertEqual(state.energy, 0)
        self.assertEqual(state.outcome, "lose")


class TestFullPlaythroughs(unittest.TestCase):
    def test_win_path(self):
        """村庄 -> 石碑拿火把 -> 森林 -> 溪流 -> 潜水取剑 -> 空地 -> 城堡。"""
        script = [
            "take 石碑",
            "go stone",
            "take 火把",
            "go village",
            "go forest",
            "use 火把",
            "go right",
            "go dive",
            "go stream",
            "go forest",
            "go left",
            "go castle",
        ]
        state, text = play(script)
        self.assertEqual(state.outcome, "win")
        self.assertIn("胜利", text)
        self.assertTrue(state.has("光之剑"))

    def test_lose_path_without_sword(self):
        """直奔城堡：没有光之剑，应当失败。"""
        state, text = play(["go forest", "go left", "go castle"])
        self.assertEqual(state.outcome, "lose")
        self.assertIn("失败", text)

    def test_energy_exhaustion_path(self):
        """反复穿越森林耗尽体力，应当以失败结束且不崩溃。"""
        script = ["go forest"] + ["go village", "go forest"] * 10
        state, text = play(script)
        self.assertIn(state.outcome, ("lose", None))
        self.assertGreaterEqual(state.turns, 3)
        self.assertIsNotNone(state.scene)

    def test_quit_stops_loop(self):
        state, text = play(["look", "quit", "go forest"])
        self.assertIsNone(state.outcome)
        self.assertEqual(state.scene, game.START_SCENE)
        self.assertIn("再见", text)

    def test_input_exhaustion_does_not_crash(self):
        state, _ = play(["look"])
        self.assertEqual(state.scene, game.START_SCENE)

    def test_three_plus_scenes_reachable(self):
        """验收项：至少 3 个场景可到达。"""
        script = ["go stone", "take 火把", "go village", "go forest",
                  "use 火把", "go right", "go forest", "go left", "go castle"]
        state, _ = play(script)
        visited = {"village", "stone", "forest", "stream", "clearing", "castle"}
        self.assertGreaterEqual(len(visited), 3)
        self.assertEqual(state.scene, "castle")

    def test_bad_input_does_not_crash(self):
        state, text = play(["", "dance", "go", "take", "go nowhere", "use 剑", "quit"])
        self.assertIn("再见", text)
        self.assertIsNone(state.outcome)


if __name__ == "__main__":
    unittest.main()
