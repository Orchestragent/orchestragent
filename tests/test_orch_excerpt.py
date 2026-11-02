"""Prompt excerpts preserve useful support while trimming shared test modules."""

import ast

import pytest

from orchestragent.orch_excerpt import excerpt


@pytest.fixture
def shared_source() -> str:
    return '''import unittest
from pytest import fixture as fx

VALUE = 2

def helper():
    return VALUE

def decorate(fn):
    return fn

@fx(autouse=True)
def environment():
    yield helper()

def setup_module():
    helper()

class Selected(unittest.TestCase):
    state = VALUE

    def setUp(self):
        self.value = helper()

    def check(self):
        return self.value

    @decorate
    def test_你好(self):
        # Keep Unicode and source formatting exactly.
        self.assertEqual(self.check(), 2)

    def test_other(self):
        self.fail("unrelated")

class Other(unittest.TestCase):
    def test_unrelated(self):
        self.fail("unrelated")

def unused():
    return "unrelated"
'''


@pytest.mark.parametrize("fragment", [
    "import unittest", "from pytest import fixture as fx", "VALUE = 2",
    "def helper():", "def decorate(fn):", "@fx(autouse=True)", "def environment():",
    "def setup_module():", "state = VALUE", "def setUp(self):", "def check(self):",
    "    @decorate\n    def test_你好(self):\n"
    "        # Keep Unicode and source formatting exactly.\n"
    "        self.assertEqual(self.check(), 2)\n",
])
def test_keeps_support(shared_source: str, fragment: str) -> None:
    assert fragment in excerpt("test_shared.py", shared_source, {"Selected.test_你好"})


@pytest.mark.parametrize("fragment", ["def test_other", "class Other", "def unused"])
def test_prunes_unrelated(shared_source: str, fragment: str) -> None:
    assert fragment not in excerpt("test_shared.py", shared_source, {"Selected.test_你好"})


@pytest.mark.parametrize("names", [set(), {"missing"}, {"Selected.missing"}])
def test_unknown_returns_original(shared_source: str, names: set[str]) -> None:
    assert excerpt("test_shared.py", shared_source, names) == shared_source


@pytest.mark.parametrize("hazard", [
    "globals()", "getattr(obj, name)", "request.getfixturevalue(name)",
    "import importlib as loader", "from builtins import getattr as lookup",
    "import importlib.util as loader", "def typed(value: list['Other']): pass",
    "from plugin import *", "if enabled:\n    generated = helper",
    "def __getattr__(name):\n    return helper", "def broken(:",
])
def test_conservative_hazards(shared_source: str, hazard: str) -> None:
    source = shared_source + "\n" + hazard + "\n"
    assert excerpt("test_shared.py", source, {"Selected.test_你好"}) == source


def test_deterministic_marked_context(shared_source: str) -> None:
    names = {"Selected.test_你好", "helper"}
    result = excerpt("test_shared.py", shared_source, names)
    assert result == excerpt("test_shared.py", shared_source, set(reversed(sorted(names))))
    assert result.startswith("# PARTIAL CONTEXT:")
    assert "not a replacement file" in result
    ast.parse(result)


def test_class_selection_retains_every_method(shared_source: str) -> None:
    assert "def test_other" in excerpt("test_shared.py", shared_source, {"Selected"})


def test_transitive_helpers_and_inheritance() -> None:
    source = ("def leaf(): return 3\n"
              "def helper(): return leaf()\n"
              "class Base:\n    def inherited(self): return helper()\n"
              "class Child(Base):\n    def test_one(self): return self.inherited()\n"
              "def unused(): pass\n")
    result = excerpt("test_example.py", source, {"Child.test_one"})
    assert "def leaf" in result
    assert "def helper" in result
    assert "class Base" in result
    assert "def unused" not in result


def test_duplicate_definition_is_ambiguous() -> None:
    source = "def selected(): pass\ndef selected(): return 2\ndef other(): pass\n"
    assert excerpt("example.py", source, {"selected"}) == source


def test_async_function_and_multiline_decorator() -> None:
    selected = "@decorate(\n    2,\n)\nasync def selected():\n    return helper()\n"
    source = ("def decorate(value): return lambda fn: fn\n"
              "def helper(): return 2\n" + selected + "def unused(): pass\n")
    result = excerpt("example.py", source, {"selected"})
    assert selected in result
    assert "def helper" in result
    assert "def unused" not in result


def test_explicit_test_method_dependency() -> None:
    source = ("class Cases:\n"
              "    def test_selected(self): return self.test_helper()\n"
              "    def test_helper(self): return 2\n"
              "    def test_unused(self): pass\n")
    result = excerpt("test_example.py", source, {"Cases.test_selected"})
    assert "def test_helper" in result
    assert "def test_unused" not in result
