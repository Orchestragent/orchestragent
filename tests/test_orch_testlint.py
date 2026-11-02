import textwrap

import pytest

from orchestragent.orch_testlint import fixture_errors


@pytest.mark.parametrize("source,invalid", [
    ("class Case:\n def test_r1(self): pass\n def test_r1(self): pass", True),
    ("class Case:\n def test_r1(self): pass\n test_r1 = None", True),
    ("class Case:\n def test_r1(self): pass\n test_r1: object = None", True),
    ("class Case:\n def test_r1(self): pass\n del test_r1", True),
    ("class Case:\n def test_r1(self): pass\n test_r1: object", False),
    ("class Case:\n def test_r1(self): pass\nCase = None", True),
    ("class Case:\n def test_r1(self): pass\nfrom helper import Case", True),
    ("class Case:\n def test_r1(self): pass\nclass Case:\n def test_r2(self): pass", True),
    ("class Case:\n def setUp(self): pass\n def setUp(self): pass", True),
    ("class A:\n def test_r1(self): pass\nclass B:\n def test_r1(self): pass", False),
    ("class Case:\n value = 1\n value = 2\n def test_r1(self): pass", False),
    ("class Case:\n @property\n def value(self): pass\n @value.setter\n def value(self, v): pass",
     False),
])
def test_generated_test_namespace_does_not_silently_replace_checks(
    source: str, invalid: bool,
) -> None:
    errors = fixture_errors(source)
    assert bool(errors) is invalid
    assert all("test namespace" in error for error in errors)


@pytest.mark.parametrize("source,invalid", [
    ("callbacks = {k: lambda _: k for k in nodes}", True),
    ("callbacks = {k: lambda _, k=k: k for k in nodes}", False),
    ("callbacks = [lambda i: i for i in nodes]", False),
    ("callbacks = [lambda: i for i in nodes]", True),
    ("callbacks = {lambda: i for i in nodes}", True),
    ("callbacks = (lambda: i for i in nodes)", True),
    ("values = [(lambda: i)() for i in nodes]", False),
    ("callbacks = [(lambda: lambda: i)() for i in nodes]", True),
    ("callbacks = [lambda j=i: (lambda: j) for i in nodes]", False),
    ("callbacks = [lambda: 0 for i in nodes]", False),
    ("for i in nodes:\n    def worker():\n        return i", True),
    ("for i in nodes:\n    def worker(i=i):\n        return i", False),
    ("for i in nodes:\n    def worker():\n        i = 3\n        return i", False),
    ("for i in nodes:\n    def factory(i):\n        return lambda: i", False),
    ("def factory(k):\n    return lambda _: k\ncallbacks = [factory(k) for k in nodes]", False),
    ("def test():\n    callbacks = [lambda _: k for k in nodes]", True),
    ("def test():\n    for (a, b) in pairs:\n        callbacks.append(lambda: (a, b))", True),
    ("def test():\n    for i in nodes:\n        pass\n    else:\n        return lambda: i", False),
])
def test_generated_callback_scopes_require_explicit_loop_capture(
    source: str, invalid: bool,
) -> None:
    errors = fixture_errors(source)
    assert bool(errors) is invalid
    assert all("loop variables" in error for error in errors)


@pytest.mark.parametrize("assertion,invalid", [
    ("self.assertEqual(1, 1)", True),
    ("self.assertEqual(len(store_key(g) if False else []), 0)", True),
    ("self.assertTrue(True)", True),
    ("self.assertIsNone(None)", True),
    ("assert 1 == 1", True),
    ("assert False", True),
    ("self.assertEqual(len(result), 0)", False),
    ("self.assertEqual(result, [1, 2])", False),
    ("self.assertEqual(fn() if True else [], [])", False),
    ("self.assertRaises(ValueError)", False),
    ("self.fail('unexpected callback')", False),
])
def test_constant_assertions_cannot_pad_frozen_coverage(assertion: str, invalid: bool) -> None:
    source = f"class Case:\n    def test_r1_value(self):\n        {assertion}\n"
    assert bool(fixture_errors(source)) is invalid


@pytest.mark.parametrize("shadow", ["parameter", "global", "comprehension"])
def test_constant_check_respects_shadowed_builtin_names(shadow: str) -> None:
    prefix = "def len(value): return 4\n" if shadow == "global" else ""
    arguments = "self, len" if shadow == "parameter" else "self"
    body = ("[self.assertEqual(len([]), 4) for len in functions]"
            if shadow == "comprehension" else "self.assertEqual(len([]), 4)")
    source = prefix + textwrap.dedent(f"""\
        class Case:
            def test_r1_value({arguments}):
                {body}
    """)
    assert fixture_errors(source) == []
