"""Source-level checks for binary history wiring without HA imports."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


BRIDGE_SOURCE = (
  Path(__file__).resolve().parents[1]
  / "custom_components"
  / "tab5_lvgl"
  / "__init__.py"
)


def _bridge_source() -> str:
  return BRIDGE_SOURCE.read_text(encoding="utf-8")


def _bridge_tree() -> ast.Module:
  return ast.parse(_bridge_source())


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
  for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
      return node
  raise AssertionError(f"Bridge function {name} was not found")


def _called_names(function: ast.AST) -> set[str]:
  names: set[str] = set()
  for node in ast.walk(function):
    if not isinstance(node, ast.Call):
      continue
    if isinstance(node.func, ast.Name):
      names.add(node.func.id)
    elif isinstance(node.func, ast.Attribute):
      names.add(node.func.attr)
  return names


class BinaryHistoryWiringTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls) -> None:
    cls.source = _bridge_source()
    cls.tree = _bridge_tree()

  def test_generic_handler_dispatches_binary_without_changing_numeric_contract(self) -> None:
    handler = _find_function(self.tree, "_async_handle_history_request")
    calls = _called_names(handler)
    source = ast.get_source_segment(self.source, handler)

    self.assertIn("_async_handle_binary_history_request", calls)
    self.assertIsNotNone(source)
    self.assertIn('"period_minutes": period_minutes', source)
    self.assertIn('"stat": stat', source)
    self.assertIn('"values": values', source)
    self.assertLess(
      source.index("BINARY_HISTORY_KIND"),
      source.index('parsed.get("period_minutes")'),
    )

  def test_binary_handler_uses_validated_bounded_helper_and_recorder(self) -> None:
    handler = _find_function(self.tree, "_async_handle_binary_history_request")
    calls = _called_names(handler)
    source = ast.get_source_segment(self.source, handler)

    self.assertIsNotNone(source)
    self.assertIn("parse_binary_history_request", calls)
    self.assertIn("build_binary_history_response", calls)
    self.assertIn("build_binary_history_error", calls)
    self.assertIn("get_last_state_changes", calls)
    self.assertIn("state_changes_during_period", calls)
    self.assertNotIn("get_significant_states", calls)
    self.assertIn("async_add_executor_job", calls)
    self.assertIn("entity_id not in self.binary_sensors", source)
    self.assertIn('"entity_not_configured"', source)
    self.assertIn("history_available=history_available", source)
    self.assertIn("recent_limit = request.max_transitions + 1", source)
    self.assertIn("if len(recent_states) >= recent_limit", source)

    period_calls = [
      node
      for node in ast.walk(handler)
      if isinstance(node, ast.Call)
      and isinstance(node.func, ast.Name)
      and node.func.id == "state_changes_during_period"
    ]
    self.assertTrue(period_calls)
    for call in period_calls:
      with self.subTest(call=ast.get_source_segment(self.source, call)):
        self.assertIn("limit", {keyword.arg for keyword in call.keywords})
        self.assertEqual(ast.unparse(call.args[1]), "start")
        self.assertEqual(ast.unparse(call.args[2]), "start")
        limit_keyword = next(
          keyword for keyword in call.keywords if keyword.arg == "limit"
        )
        self.assertEqual(ast.unparse(limit_keyword.value), "1")

    last_calls = [
      node
      for node in ast.walk(handler)
      if isinstance(node, ast.Call)
      and isinstance(node.func, ast.Name)
      and node.func.id == "get_last_state_changes"
    ]
    self.assertEqual(len(last_calls), 1)
    self.assertEqual(ast.unparse(last_calls[0].args[1]), "recent_limit")


if __name__ == "__main__":
  unittest.main()
