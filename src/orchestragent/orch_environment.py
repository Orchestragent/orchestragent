"""Explicit, content-pinned pytest and pure-Python dependency bundles; no installations."""

import json
import re
import sys
import sysconfig
from dataclasses import asdict, dataclass
from importlib import import_module
from importlib.metadata import Distribution, distributions
from pathlib import Path, PurePosixPath
from typing import cast

from orchestragent.envelope import digest
from orchestragent.serialization import dumps

ROOTS = ("pytest", "_pytest", "pluggy", "packaging", "iniconfig", "pygments")
DISTRIBUTIONS = ("pytest", "pluggy", "packaging", "iniconfig", "pygments")
MANIFEST = "manifest.json"


def _source_roots(value: object) -> tuple[str, ...]:
    if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 8 or value[0] != "."
            or any(not isinstance(p, str) or p != "." and (
                not re.fullmatch(r"[A-Za-z0-9_/-]+", p)
                or any(x in {".", ".."} for x in p.split("/"))
                or PurePosixPath(p).is_absolute() or str(PurePosixPath(p)) != p) for p in value)
            or len(set(value)) != len(value)):
        raise ValueError("Source roots must start with '.' and use distinct relative directories")
    return cast(tuple[str, ...], tuple(value))


@dataclass(frozen=True)
class _Manifest:
    schema_version: int
    python: tuple[int, int]
    versions: dict[str, str]
    files: dict[str, str]
    imports: tuple[str, ...] = ()
    source_roots: tuple[str, ...] = (".",)


def _packages(names: tuple[str, ...]) -> dict[str, Distribution]:
    """Resolve installed default dependencies without importing their code or using pip."""
    try:
        requirement = import_module("packaging.requirements").Requirement
    except ImportError:
        raise ValueError("Environment creation requires installed pytest/packaging; "
                         "no automatic installation") from None
    pending = [requirement(name) for name in (*DISTRIBUTIONS, *names)]
    packages: dict[str, Distribution] = {}
    while pending:
        req = pending.pop(0)
        if req.url or req.extras:
            raise ValueError("Environment packages require installed names/versions without extras")
        name = re.sub(r"[-_.]+", "_", req.name).lower()
        if name in packages:
            package = packages[name]
        else:
            found = list(distributions(name=name, path=[sysconfig.get_path("purelib")]))
            if len(found) != 1:
                raise ValueError(f"Expected one installed distribution for {name}; no auto-install")
            package = packages[name] = found[0]
            if len(packages) > 64:
                raise ValueError("Environment exceeds 64 installed distributions")
            for raw in package.requires or ():
                dependency = requirement(raw)
                if dependency.marker is None or dependency.marker.evaluate({"extra": ""}):
                    pending.append(dependency)
        if req.specifier and not req.specifier.contains(package.version, prereleases=True):
            raise ValueError(f"Installed {name}=={package.version} violates {req.specifier}")
    return packages


def _inventory(
    root: Path, versions: dict[str, str], extra: tuple[str, ...] = (),
) -> dict[str, str]:
    """Only pinned sources/data and minimal distribution metadata may load."""
    metadata = {f"{name}-{version}.dist-info/METADATA".casefold()
                for name, version in versions.items()}
    roots = {*ROOTS, *extra}
    files: dict[str, str] = {}
    directories: set[str] = set()
    size = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Environment symlinks are forbidden")
        relative = path.relative_to(root).as_posix()
        parts = PurePosixPath(relative).parts
        if path.is_dir():
            directories.add(relative)
            if (parts[0] not in roots
                    and not any(name.startswith(relative.casefold() + "/") for name in metadata)):
                raise ValueError("Unexpected environment directory")
            continue
        if not path.is_file() or path.stat().st_mode & 0o111:
            raise ValueError("Environment files must be non-executable regular files")
        if relative == MANIFEST:
            continue
        source = (parts[0] in ROOTS and (path.suffix == ".py" or path.name == "py.typed")
                  or parts[0] in extra
                  or len(parts) == 1 and path.suffix == ".py" and path.stem in extra)
        if (path.suffix.lower() in {".pyc", ".pth", ".so", ".pyd", ".dylib", ".dll"}
                or not source and relative != "py.py" and relative.casefold() not in metadata):
            raise ValueError("Unexpected environment file")
        raw = path.read_bytes()
        size += len(raw)
        if len(files) >= 8192 or size > 128 * 1024 * 1024:
            raise ValueError("Environment exceeds bundle bounds")
        files[relative] = digest(raw.decode("utf-8"))
    expected_directories = {str(parent) for name in files for parent in PurePosixPath(name).parents
                            if str(parent) != "."}
    if directories != expected_directories:
        raise ValueError("Unexpected empty environment directory")
    required = {f"{name}/__init__.py" for name in ROOTS} | {"py.py"}
    if not required <= files.keys() or not metadata <= {name.casefold() for name in files}:
        raise ValueError("Environment is missing required sources or metadata")
    if any(name + ".py" not in files and not any(
        path.startswith(name + "/") and path.endswith(".py") for path in files
    ) for name in extra):
        raise ValueError("Environment is missing an advertised import")
    return files


@dataclass(frozen=True)
class PytestEnvironment:
    root: Path
    fingerprint: str

    @property
    def modules(self) -> tuple[str, ...]:
        body = json.loads((self.root / MANIFEST).read_text())["manifest"]
        return tuple(sorted({*ROOTS, "py", *body.get("imports", ())}))

    @property
    def source_roots(self) -> tuple[str, ...]:
        body = json.loads((self.root / MANIFEST).read_text())["manifest"]
        return _source_roots(body.get("source_roots", ["."]))

    def prompt(self) -> str:
        body = json.loads((self.root / MANIFEST).read_text())["manifest"]
        return ("\n## PINNED EXECUTION ENVIRONMENT\n"
                "Use stdlib plus ONLY these installed imports. No installation, network or "
                "subprocesses. All contract and project tests use this same bundle.\n"
                + dumps({"fingerprint": self.fingerprint, "imports": self.modules,
                         "versions": body["versions"], "source_roots": self.source_roots}) + "\n")

    @classmethod
    def create(
        cls, root: Path, *, packages: tuple[str, ...] = (), source_roots: tuple[str, ...] = (".",),
    ) -> "PytestEnvironment":
        """Copy installed pure Python distributions and required dependencies; never install."""
        source_roots = _source_roots(source_roots)
        selected = _packages(packages)
        payload: dict[str, bytes] = {}
        imports: set[str] = set()
        for name, package in selected.items():
            location = Path(str(package.locate_file("")))
            if location.is_symlink() or not location.is_dir():
                raise ValueError("Installed distribution root must be a regular directory")
            entries = [p for p in package.files or () if p.parts and ".." not in p.parts
                       and not p.is_absolute() and not p.parts[0].endswith(".dist-info")]
            # Console scripts outside site-packages are not importable and are never copied.
            # Native wheels, startup hooks and editable installations are unsupported.
            if any(p.suffix.lower() in {".pth", ".so", ".pyd", ".dylib", ".dll", ".egg-link"}
                   for p in entries):
                raise ValueError(f"{name} is not a supported pure-Python distribution")
            roots = {p.parts[0] if len(p.parts) > 1 else p.stem
                     for p in entries if p.suffix == ".py"}
            if (not roots or any(not p.isascii() or not p.isidentifier()
                                 or p in sys.stdlib_module_names for p in roots)
                    or name not in DISTRIBUTIONS and roots & {*ROOTS, "py"}):
                raise ValueError(f"{name} has missing or conflicting Python import roots")
            imports.update(roots)
            expected = f"{name}-{package.version}.dist-info/METADATA"
            metadata = next((p for p in package.files or ()
                             if len(p.parts) == 2 and p.name == "METADATA"
                             and p.parts[0].endswith(".dist-info")), None)
            if metadata is None:
                raise ValueError(f"Installed distribution metadata is missing for {name}")
            candidates = [(p, p.as_posix()) for p in entries
                          if p.suffix != ".pyc" and (p.parts[0] in roots
                                                    or len(p.parts) == 1 and p.suffix == ".py")]
            candidates.append((metadata, expected))
            for record, relative in candidates:
                source = Path(str(package.locate_file(record)))
                if (source.is_symlink() or not source.is_file()
                        or not source.resolve().is_relative_to(location.resolve())
                        or any(parent.is_symlink() for parent in source.parents
                               if parent.is_relative_to(location))):
                    raise ValueError("Installed package files must not traverse symlinks")
                raw = source.read_bytes()
                try:
                    raw.decode("utf-8")
                except UnicodeError:
                    raise ValueError(f"{name} contains unsupported binary package data") from None
                if relative in payload and payload[relative] != raw:
                    raise ValueError(f"Dependency file collision: {relative}")
                payload[relative] = raw
        if len({p.casefold() for p in payload}) != len(payload):
            raise ValueError("Case-insensitive dependency file collision")
        root.mkdir(parents=True, exist_ok=False)
        root = root.resolve()
        for relative, raw in payload.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o600)
        versions = {name: package.version for name, package in selected.items()}
        extra = tuple(sorted(imports - {*ROOTS, "py"}))
        modern = bool(extra) or source_roots != (".",)
        manifest = _Manifest(2 if modern else 1, (sys.version_info.major, sys.version_info.minor),
                             versions, _inventory(root, versions, extra), extra, source_roots)
        body = asdict(manifest)
        if not modern:
            body.pop("imports")
        if source_roots == (".",):
            body.pop("source_roots")
        (root / MANIFEST).write_text(dumps({"manifest": body, "fingerprint": digest(dumps(body))}),
                                     encoding="utf-8")
        return cls.load(root)

    @classmethod
    def load(cls, root: Path) -> "PytestEnvironment":
        """Verify exact contents; the caller's fingerprint pins a previously loaded bundle."""
        if (root.is_symlink() or not root.is_dir() or (root / MANIFEST).is_symlink()
                or not (root / MANIFEST).is_file()):
            raise ValueError("Environment root/manifest must not be symlinks")
        root = root.resolve()
        document = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"manifest", "fingerprint"}:
            raise ValueError("Invalid environment manifest")
        body, fingerprint = document["manifest"], document["fingerprint"]
        extra = body.get("imports", []) if isinstance(body, dict) else []
        _source_roots(body.get("source_roots", ["."]) if isinstance(body, dict) else [])
        if (not isinstance(body, dict)
                or set(body) != {"schema_version", "python", "versions", "files"} | (
                    {"imports"} if body.get("schema_version") == 2 else set()) | (
                    {"source_roots"} if "source_roots" in body else set())
                or type(body["schema_version"]) is not int or body["schema_version"] not in {1, 2}
                or body["python"] != list(sys.version_info[:2])
                or not isinstance(body["versions"], dict)
                or not set(DISTRIBUTIONS) <= set(body["versions"]) or len(body["versions"]) > 64
                or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", name)
                       or not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9.!+_-]{1,80}", v)
                       for name, v in body["versions"].items())
                or not isinstance(extra, list)
                or any(not isinstance(p, str) or not p.isascii() or not p.isidentifier()
                       or p in {*ROOTS, "py", *sys.stdlib_module_names} for p in extra)
                or len(set(extra)) != len(extra)
                or not isinstance(body["files"], dict)
                or not isinstance(fingerprint, str) or digest(dumps(body)) != fingerprint):
            raise ValueError("Invalid or incompatible environment manifest fingerprint")
        if body["schema_version"] == 1 and (
            set(body["versions"]) != set(DISTRIBUTIONS) or "source_roots" in body
        ):
            raise ValueError("Legacy environment must contain only curated dependencies")
        versions = cast(dict[str, str], body["versions"])
        if _inventory(root, versions, tuple(extra)) != body["files"]:
            raise ValueError("Environment content changed")
        return cls(root, fingerprint)
