import json
from importlib.metadata import Distribution, PathDistribution, distributions
from pathlib import Path

import pytest

from orchestragent.orch_environment import MANIFEST, ROOTS, PytestEnvironment
from orchestragent.serialization import dumps


@pytest.fixture
def environment(tmp_path: Path) -> PytestEnvironment:
    return PytestEnvironment.create(tmp_path / "environment")


def test_curated_environment_is_exactly_reopenable(environment: PytestEnvironment) -> None:
    document = json.loads((environment.root / MANIFEST).read_text())
    files = document["manifest"]["files"]
    assert PytestEnvironment.load(environment.root) == environment
    assert document["fingerprint"] == environment.fingerprint
    assert set(document["manifest"]["versions"]) == set(ROOTS) - {"_pytest"}
    assert all(f"{name}/__init__.py" in files for name in ROOTS)
    assert (environment.root / "pytest" / "__init__.py").read_bytes() == Path(
        pytest.__file__).read_bytes()
    assert "py.py" in files and any(name.endswith(".dist-info/METADATA") for name in files)
    assert not any(name.endswith((".pyc", ".pth", ".so", "entry_points.txt")) for name in files)


def test_environment_creation_never_reuses_existing_directory(
    environment: PytestEnvironment,
) -> None:
    with pytest.raises(FileExistsError):
        PytestEnvironment.create(environment.root)
    assert PytestEnvironment.load(environment.root) == environment


def test_environment_creation_explains_missing_optional_runtime_without_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def absent(_: str) -> None:
        raise ModuleNotFoundError("packaging is not installed")

    monkeypatch.setattr("orchestragent.orch_environment.import_module", absent)
    target = tmp_path / "environment"
    with pytest.raises(ValueError, match="no automatic installation"):
        PytestEnvironment.create(target)
    assert not target.exists()


def test_installed_dependency_is_content_pinned_and_listed_to_models(
    dependency_environment: PytestEnvironment,
) -> None:
    environment = dependency_environment
    assert "typing_extensions" in environment.modules
    assert "typing_extensions" in environment.prompt()
    assert PytestEnvironment.load(environment.root) == environment
    target = environment.root / "typing_extensions.py"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    with pytest.raises(ValueError, match="content changed"):
        PytestEnvironment.load(environment.root)


@pytest.fixture
def installed_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "installed"
    for name, version, requires in [
        ("demo_parent", "1.0", "Requires-Dist: demo_child>=2\n"
         "Requires-Dist: absent_optional; python_version < '2'\n"),
        ("demo_child", "2.1", ""),
    ]:
        metadata = root / f"{name}-{version}.dist-info"
        metadata.mkdir(parents=True)
        (root / f"{name}.py").write_text("raise RuntimeError('controller must not import me')\n")
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{requires}")
        (metadata / "RECORD").write_text(f"{name}.py,,\n{metadata.name}/METADATA,,\n")

    def find(*, name: str, path: list[str]) -> list[Distribution]:
        if name.startswith("demo_"):
            return [PathDistribution(p) for p in root.glob(f"{name}-*.dist-info")]
        return list(distributions(name=name, path=path))

    monkeypatch.setattr("orchestragent.orch_environment.distributions", find)
    return root


@pytest.mark.parametrize("case", ["ok", "missing", "wrong-version", "native"])
def test_dependency_closure_checks_versions_markers_and_pure_python_content(
    installed_dependencies: Path, tmp_path: Path, case: str,
) -> None:
    child = installed_dependencies / "demo_child-2.1.dist-info"
    if case == "missing":
        child.rename(child.with_name("removed.dist-info"))
    elif case == "wrong-version":
        metadata = child / "METADATA"
        metadata.write_text(metadata.read_text().replace("Version: 2.1", "Version: 1.0"))
    elif case == "native":
        record = child / "RECORD"
        record.write_text(record.read_text() + "native.so,,\n")
        (installed_dependencies / "native.so").write_bytes(b"native fixture")
    target = tmp_path / "bundle"
    if case == "ok":
        env = PytestEnvironment.create(target, packages=("demo_parent",))
        assert {"demo_parent", "demo_child"} <= set(env.modules)
        assert PytestEnvironment.load(target) == env
    else:
        with pytest.raises(ValueError):
            PytestEnvironment.create(target, packages=("demo_parent",))
        assert not target.exists()


@pytest.mark.parametrize("roots", [(".", "../outside"), ("src",), (".", "."), (".", "/tmp")])
def test_environment_refuses_unsafe_source_roots_before_writing(
    tmp_path: Path, roots: tuple[str, ...],
) -> None:
    root = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Source roots"):
        PytestEnvironment.create(root, source_roots=roots)
    assert not root.exists()


@pytest.mark.parametrize(
    "mutation", ["edit", "remove", "extra", "native", "pth", "executable", "directory"],
)
def test_environment_rejects_mutation_and_extra_executable_content(
    environment: PytestEnvironment, mutation: str,
) -> None:
    target = environment.root / "pytest" / "__init__.py"
    if mutation == "edit":
        target.write_bytes(target.read_bytes() + b"\n# changed\n")
    elif mutation == "remove":
        target.unlink()
    elif mutation == "executable":
        target.chmod(0o700)
    elif mutation == "directory":
        (environment.root / "pytest" / "unexpected-empty").mkdir()
    else:
        suffix = {"extra": ".py", "native": ".so", "pth": ".pth"}[mutation]
        (environment.root / "pytest" / f"unexpected{suffix}").write_text("unsafe")
    with pytest.raises(ValueError):
        PytestEnvironment.load(environment.root)


@pytest.mark.parametrize("kind", ["file", "directory", "manifest", "root"])
def test_environment_rejects_symlinks(
    environment: PytestEnvironment, tmp_path: Path, kind: str,
) -> None:
    root = environment.root
    if kind == "root":
        link = tmp_path / "environment-link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        relative = {"file": "pytest/__init__.py", "directory": "pytest", "manifest": MANIFEST}[kind]
        source = root / relative
        moved = tmp_path / "moved"
        source.rename(moved)
        source.symlink_to(moved, target_is_directory=kind == "directory")
    with pytest.raises(ValueError):
        PytestEnvironment.load(root)


@pytest.mark.parametrize("part", ["fingerprint", "schema_version", "python", "extra"])
def test_environment_manifest_is_versioned_and_content_pinned(
    environment: PytestEnvironment, part: str,
) -> None:
    path = environment.root / MANIFEST
    document = json.loads(path.read_text())
    if part == "fingerprint":
        document[part] = "0" * 64
    elif part == "extra":
        document[part] = True
    else:
        document["manifest"][part] = 2 if part == "schema_version" else [0, 0]
    path.write_text(dumps(document))
    with pytest.raises(ValueError):
        PytestEnvironment.load(environment.root)
