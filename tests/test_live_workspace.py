import pytest

from orchestragent.live_workspace import safe_relative


@pytest.mark.parametrize("path", [
    "../test_x.py", "test//x.py", "./test_x.py", "x/./test_x.py", "/abs/x.go", "a\\b.ts",
    ".hidden/x.rs", "", "x y.py", "a" * 201,
])
def test_path_aliases_and_traversal_rejected(path: str) -> None:
    assert not safe_relative(path)


@pytest.mark.parametrize("path", [
    "test_x.py", "web/src/cart.test.ts", "api/calc_test.go", "crates/core/src/lib.rs",
    "web/node_modules/@scope/pkg/index.d.ts", "tools/a+b.sh",
])
def test_paths_of_any_language_are_accepted(path: str) -> None:
    assert safe_relative(path)
