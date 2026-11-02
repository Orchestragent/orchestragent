"""Loopback-only development service with explicit native subscription execution."""

import argparse
import fcntl
import hmac
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from orchestragent import metrics as metrics_module
from orchestragent.chat import Chat, tick_chat
from orchestragent.codeindex import CodeIndex
from orchestragent.controller import Controller
from orchestragent.health import HealthMonitor
from orchestragent.kimi import KimiManual
from orchestragent.live import LiveWorkflow
from orchestragent.live_settings import LiveSettingsError, propose
from orchestragent.memory import MemoryStore
from orchestragent.models import Task
from orchestragent.project import ProjectRepo
from orchestragent.providers import model_catalog
from orchestragent.serialization import dumps
from orchestragent.store import Conflict, Store
from orchestragent.workspace import Workspace


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        controller: Controller,
        port: int = 8765,
        token: str | None = None,
        host: str = "127.0.0.1",
        chat: Chat | None = None,
    ) -> None:
        self.controller = controller
        self.mode = "live" if controller.live else "mock"
        self.token = token
        self.chat = chat
        super().__init__((host, port), Handler)

    def service_actions(self) -> None:
        self.controller.expire_leases()


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format: str, *args: object) -> None:
        # HTTP paths, bodies, and exception text are deliberately not logged.
        pass

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def reply(self, code: int, body: object) -> None:
        payload = dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def dispatch(self, method: str) -> None:
        if method in {"POST", "DELETE"} and urlsplit(self.path).path != "/maintenance/drain":
            with self.server.controller.store.activity() as admitted:
                if not admitted:
                    self.reply(503, {"error": "Restart in progress; retry shortly"})
                    return
                self._dispatch(method)
        else:
            self._dispatch(method)

    def _dispatch(self, method: str) -> None:
        started = time.monotonic()
        path_now = urlsplit(self.path).path
        if method == "POST" and path_now in {"/settings", "/settings/proposals"}:
            (Path(".data") / "logs").mkdir(parents=True, exist_ok=True)
            with (Path(".data") / "logs" / "settings.log").open("a", encoding="utf-8") as log:
                log.write(f"{time.time():.0f} POST {path_now} from {self.client_address[0]}\n")
        if self.headers.get("Origin") is not None:
            self.reply(403, {"error": "Only native loopback clients are allowed"})
            return
        token = self.server.token
        if token is not None:
            # The remote listener is proxied from loopback, so a bearer token is the
            # only device boundary; the Host header is owned by the proxy.
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {token}"):
                self.reply(401, {"error": "A device token is required"})
                return
        else:
            host = self.headers.get("Host", "")
            allowed_hosts = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if host not in allowed_hosts:
                self.reply(403, {"error": "Only native loopback clients are allowed"})
                return
        try:
            path = urlsplit(self.path)
            parts = path.path.strip("/").split("/")
            body: dict[str, object] = {}
            if method == "POST":
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Transfer-Encoding is not supported")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    raise ValueError("Request body must contain 1–16384 bytes")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected application/json")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                body = value
            controller = self.server.controller
            if method == "GET" and parts == ["health"]:
                self.reply(200, {"mode": self.server.mode, "version": 2})
            elif method == "POST" and parts == ["maintenance", "drain"]:
                if self.server.token is not None:
                    self.reply(403, {"error": "Restart gate is local-only"})
                    return
                self.keys(body, {"enabled"})
                enabled = body.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Expected boolean enabled")
                controller.store.drain(enabled)
                self.reply(200, {"draining": enabled})
            elif method == "GET" and parts == ["providers"]:
                self.reply(
                    200, controller.live.clients.status() if controller.live else {"mode": "mock"}
                )
            elif method == "POST" and parts in (
                ["providers", "authorization", "preview"],
                ["providers", "authorization", "apply"],
            ):
                if not controller.live:
                    raise ValueError("Live service required")
                self.keys(
                    body, {"provider", "cap_cents", "funding_kind", "operation_digest", "api_key"}
                )
                provider = self.string(body, "provider")
                cap = body.get("cap_cents")
                if type(cap) is not int:
                    raise ValueError("Expected integer cents")
                kind = self.string(body, "funding_kind")
                access = controller.live.clients.access
                operation = access.operation(provider, cap, kind)
                if parts[-1] == "apply":
                    access.authorize(
                        provider,
                        cap,
                        kind,
                        self.string(body, "operation_digest"),
                        self.string(body, "api_key"),
                    )
                    self.reply(200, access.status(provider))
                else:
                    self.reply(200, operation)
            elif parts == ["settings"]:
                if method == "GET":
                    self.reply(200, {**controller.settings.read(), "mode": self.server.mode})
                else:
                    self.keys(body, {"base_revision", "policy"})
                    revision = body.get("base_revision")
                    if type(revision) is not int:
                        raise ValueError("Expected revision integer")
                    self.reply(
                        200,
                        {
                            **controller.settings.update(revision, body.get("policy")),
                            "mode": self.server.mode,
                        },
                    )
            elif method == "POST" and parts == ["settings", "proposals"]:
                self.keys(body, {"instruction"})
                instruction = self.string(body, "instruction")
                proposal = (
                    propose(controller.settings, controller.live.clients, instruction)
                    if controller.live
                    else controller.settings.propose(instruction)
                )
                self.reply(200, proposal)
            elif (
                method == "POST"
                and len(parts) == 4
                and parts[:2] == ["settings", "proposals"]
                and parts[3] == "apply"
            ):
                self.keys(body, set())
                self.reply(200, {**controller.settings.apply(parts[2]), "mode": self.server.mode})
            elif method == "POST" and parts == ["chat", "reset"]:
                if controller.chat is None:
                    raise ValueError("Chat is unavailable")
                self.keys(body, set())
                controller.chat.clear()
                self.reply(200, {"messages": [], "cursor": 0})
            elif method == "GET" and parts == ["metrics"]:
                self.reply(
                    200,
                    {
                        **metrics_module.snapshot(
                            controller.store, parse_qs(path.query).get("project", [""])[0]
                        ),
                        "health": controller.health.latest() if controller.health else [],
                        "memory": (
                            controller.memory.stats(
                                parse_qs(path.query).get("project", [""])[0]
                            )
                            if controller.memory is not None
                            else {}
                        ),
                    },
                )
            elif method == "GET" and parts == ["kimi", "models"]:
                self.reply(
                    200,
                    {"models": controller.kimi.catalog() if controller.kimi else []},
                )
            elif method == "POST" and parts == ["kimi", "ask"]:
                if controller.kimi is None:
                    raise ValueError("Kimi is unavailable")
                self.keys(body, {"prompt", "auto", "model"})
                auto = body.get("auto", False)
                if not isinstance(auto, bool):
                    raise ValueError("Expected boolean auto")
                self.reply(
                    200,
                    controller.kimi.ask(
                        self.string(body, "prompt"),
                        auto=auto,
                        model=self.string(body, "model", ""),
                    ),
                )
            elif method == "GET" and parts == ["providers", "models"]:
                if not controller.live:
                    self.reply(200, {"models": {}})
                else:
                    self.reply(200, {"models": model_catalog(controller.live.clients.cursor)})
            elif method == "GET" and parts == ["memory"]:
                if controller.memory is None:
                    raise ValueError("Memory is unavailable")
                query = parse_qs(path.query)
                self.reply(
                    200,
                    {
                        "notes": controller.memory.recent(
                            query.get("project", [""])[0],
                            int(query.get("limit", ["20"])[0]),
                        )
                    },
                )
            elif method == "GET" and parts == ["providers", "health"]:
                self.reply(
                    200,
                    {"providers": controller.health.latest() if controller.health else []},
                )
            elif (
                method == "POST"
                and len(parts) == 4
                and parts[:2] == ["chat", "conversations"]
                and parts[3] == "rename"
            ):
                if controller.chat is None:
                    raise ValueError("Chat is unavailable")
                self.keys(body, {"title"})
                self.reply(
                    200, controller.chat.rename(parts[2], self.string(body, "title"))
                )
            elif (
                method == "POST"
                and len(parts) == 4
                and parts[:2] == ["chat", "conversations"]
                and parts[3] == "root"
            ):
                if controller.chat is None or controller.index is None:
                    raise ValueError("Project index is unavailable")
                self.keys(body, {"path"})
                root = Path(self.string(body, "path")).expanduser().resolve()
                stats = controller.index.build(parts[2], root)
                controller.chat.set_root(parts[2], str(root))
                self.reply(200, {"project": parts[2], "root": str(root), **stats})
            elif (
                method == "DELETE"
                and len(parts) == 3
                and parts[:2] == ["chat", "conversations"]
            ):
                if controller.chat is None:
                    raise ValueError("Project is unavailable")
                controller.chat.delete(parts[2])
                self.reply(200, {"deleted": parts[2]})
            elif (
                method == "POST"
                and len(parts) == 4
                and parts[:2] == ["chat", "conversations"]
                and parts[3] == "context"
            ):
                if controller.chat is None:
                    raise ValueError("Project is unavailable")
                self.keys(body, {"bytes", "paths"})
                raw_size = body.get("bytes")
                raw_paths = body.get("paths", [])
                if type(raw_size) is not int or not 8_000 <= raw_size <= 4_194_304:
                    raise ValueError("Context budget must be 8000–4194304 bytes")
                if (not isinstance(raw_paths, list) or len(raw_paths) > 8
                        or any(not isinstance(item, str) for item in raw_paths)):
                    raise ValueError("paths must contain at most 8 string prefixes")
                size = raw_size
                paths = [str(item).strip().strip("/") for item in raw_paths]
                controller.chat.set_context(parts[2], size, [p for p in paths if p])
                self.reply(200, {"id": parts[2], "bytes": size, "paths": paths})
            elif parts == ["chat", "conversations"]:
                if controller.chat is None:
                    raise ValueError("Chat is unavailable")
                if method == "GET":
                    self.reply(200, {"conversations": controller.chat.conversations()})
                else:
                    self.keys(body, {"title"})
                    self.reply(201, controller.chat.create(self.string(body, "title", "")))
            elif parts == ["chat"]:
                if controller.chat is None:
                    raise ValueError("Chat is unavailable")
                if method == "GET":
                    query = parse_qs(path.query)
                    after = int(query.get("after", ["0"])[0])
                    if after < 0:
                        raise ValueError("Invalid chat cursor")
                    messages = controller.chat.messages(
                        after, query.get("conversation", ["default"])[0]
                    )
                    self.reply(
                        200,
                        {
                            "messages": messages,
                            "cursor": messages[-1]["seq"] if messages else after,
                        },
                    )
                else:
                    self.keys(body, {"request_id", "message", "conversation"})
                    self.reply(
                        202,
                        {
                            "messages": controller.chat.post(
                                self.string(body, "request_id"),
                                self.string(body, "message"),
                                self.string(body, "conversation", "default"),
                            )
                        },
                    )
            elif parts == ["tasks"]:
                if method == "GET":
                    project = parse_qs(path.query).get("project", [""])[0]
                    tasks = [
                        task
                        for task in controller.store.list_tasks()
                        if not project or task.project == project
                    ]
                    self.reply(200, {"tasks": [asdict(t) for t in tasks]})
                else:
                    self.keys(body, {"request_id", "prompt", "scenario", "mode"})
                    task, created = controller.submit(
                        self.string(body, "request_id"),
                        self.string(body, "prompt"),
                        self.string(body, "scenario", "happy"),
                        self.string(body, "mode", self.server.mode),
                    )
                    self.reply(201 if created else 200, asdict(task))
            elif len(parts) >= 2 and parts[0] == "tasks":
                task_id = parts[1]
                if method == "GET" and len(parts) == 2:
                    self.reply(200, asdict(controller.store.read(task_id)))
                elif method == "GET" and len(parts) == 3 and parts[2] == "events":
                    after = int(parse_qs(path.query).get("after", ["0"])[0])
                    if after < 0:
                        raise ValueError("Invalid event cursor")
                    events = controller.store.events(task_id, after)
                    self.reply(
                        200,
                        {
                            "events": [asdict(e) for e in events],
                            "cursor": events[-1].seq if events else after,
                        },
                    )
                elif method == "POST" and len(parts) == 3 and parts[2] == "accept":
                    self.keys(body, {"approved", "note"})
                    approved = body.get("approved")
                    if type(approved) is not bool:
                        raise ValueError("Expected boolean approved")
                    task = controller.accept(
                        task_id, approved, self.string(body, "note", "")
                    )
                    self.reply(200, asdict(task))
                elif method == "POST" and len(parts) == 3 and parts[2] == "manual":
                    if controller.kimi is None:
                        raise ValueError("Kimi is unavailable")
                    self.keys(body, {"auto", "model"})
                    auto = body.get("auto", False)
                    if not isinstance(auto, bool):
                        raise ValueError("Expected boolean auto")
                    task = controller.store.read(task_id)
                    if task.status != "waiting_manual":
                        raise Conflict("This task is not waiting for a manual review")
                    prompt = (
                        "请以独立审查者身份复核这个 Python 标准库项目。只输出中文审查意见，"
                        "指出正确性、边界与测试缺口；不要改文件。\n\n需求："
                        + task.prompt
                        + "\n\n实现差异：\n"
                        + next(
                            (
                                a.content[:4000]
                                for a in task.artifacts
                                if a.name == "implementation.diff"
                            ),
                            "",
                        )
                        + "\n\n测试结果：\n"
                        + next(
                            (
                                a.content[:1500]
                                for a in task.artifacts
                                if a.name == "integration-tests.txt"
                            ),
                            "",
                        )
                    )
                    answer = controller.kimi.ask(
                        prompt, auto=auto, model=self.string(body, "model", "")
                    )
                    if answer.get("ok") is not True:
                        self.reply(409, {"error": str(answer.get("message", "Kimi 未完成"))})
                        return
                    updated = controller.manual_review(
                        task_id, "kimi", str(answer.get("text", ""))
                    )
                    self.reply(200, asdict(updated))
                elif method == "POST" and len(parts) == 3 and parts[2] == "actions":
                    self.keys(body, {"action"})
                    task = controller.action(task_id, self.string(body, "action"))
                    self.reply(200, asdict(task))
                elif method == "POST" and len(parts) == 4 and parts[2] == "approvals":
                    self.keys(body, {"decision", "operation_digest"})
                    task = controller.approve(
                        task_id,
                        parts[3],
                        self.string(body, "decision"),
                        self.string(body, "operation_digest"),
                    )
                    self.reply(200, asdict(task))
                else:
                    self.reply(404, {"error": "Endpoint not found"})
            else:
                self.reply(404, {"error": "Endpoint not found"})
            elapsed = time.monotonic() - started
            if elapsed > 2:
                (Path(".data") / "slow.log").open("a", encoding="utf-8").write(
                    f"{elapsed:.1f}s {method} {urlsplit(self.path).path}\n"
                )
        except LiveSettingsError as error:
            self.reply(409, {"error": str(error), "status": error.outcome})
        except Conflict as error:
            self.reply(409, {"error": str(error)})
        except KeyError:
            self.reply(404, {"error": "Task not found"})
        except (ValueError, UnicodeError):
            self.reply(400, {"error": "Invalid request; check the API contract"})
        except Exception:
            import traceback

            (Path(".data") / "logs").mkdir(parents=True, exist_ok=True)
            with (Path(".data") / "logs" / "server.err.log").open("a", encoding="utf-8") as log:
                log.write(f"{method} {self.path}\n{traceback.format_exc()}\n")
            self.reply(500, {"error": "Internal error; no billing fallback was attempted"})

    @staticmethod
    def string(body: dict[str, object], key: str, default: str | None = None) -> str:
        value = body.get(key, default)
        if not isinstance(value, str):
            raise ValueError("Expected a string")
        return value

    @staticmethod
    def keys(body: dict[str, object], allowed: set[str]) -> None:
        if body.keys() - allowed:
            raise ValueError("Unexpected fields")


def main() -> None:
    # The desktop account grant pins its destination to api.deepseek.com, so the
    # single-request transport guard cannot sit in front of it. API key stays the
    # default because it keeps the hard cost bound.
    os.environ.setdefault("ORCHESTRA_DSH_ACCOUNT", "0")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Enable native subscription providers")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument(
        "--remote-port",
        type=int,
        default=0,
        help="Also serve a bearer-token listener on this loopback port (0 disables it)",
    )
    parser.add_argument("--token-file", type=Path, default=None)
    parser.add_argument(
        "--remote-bind",
        default="127.0.0.1",
        help="Bind address for the token listener (127.0.0.1 or 0.0.0.0)",
    )
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with (args.data_dir / "controller.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another controller owns this data directory") from None
        controller = Controller(
            Store(args.data_dir / "tasks.sqlite"),
            Workspace(args.data_dir / "workspaces"),
            live=LiveWorkflow(args.data_dir) if args.live else None,
            # A max-effort planning turn can run many minutes; the lease must not
            # expire under a healthy call, or the watchdog cancels it as timed_out.
            lease_seconds=21600 if args.live else 30,
        )
        remote: Server | None = None
        token_file = args.token_file or args.data_dir / "remote-token"
        if args.remote_port:
            token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""
            if not token:
                token = secrets.token_urlsafe(32)
                token_file.write_text(token + "\n", encoding="utf-8")
                token_file.chmod(0o600)
            remote = Server(
                controller, args.remote_port, token, args.remote_bind, controller.chat
            )
        controller.chat = Chat(controller.store)
        controller.chat.recover()
        controller.memory = MemoryStore(controller.store)
        controller.index = CodeIndex.ensure(args.data_dir / "code-index.db")
        if controller.live is not None:
            index = controller.index
            chat_store = controller.chat

            def read_files(project: str, names: list[str]) -> str:
                """Use the shared indexed, hash-verified context reader."""
                root = chat_store.root_of(project) if chat_store is not None else ""
                if not root:
                    return ""
                return index.files(project, Path(root), names)

            def publish(task: Task) -> str:
                """Accepted work is committed on its own branch and pushed."""
                if controller.chat is None:
                    raise ValueError("No project is bound")
                root = controller.chat.root_of(task.project)
                if not root or not (Path(root) / ".git").exists():
                    raise ValueError("项目目录不是 git 仓库")
                repo = ProjectRepo(Path(root))
                branch = f"task/{task.id[:8]}"
                if controller.live is None:
                    raise ValueError("No live workflow")
                files = json.loads(
                    controller.live.workspace.bundle_at(task.id, task.revision)
                )
                repo.start_branch(branch, "HEAD")
                tree = Path(tempfile.mkdtemp(prefix="og-push-"))
                try:
                    worktree = repo.worktree(branch, tree / "wt")
                    for item in files:
                        target = worktree / item["path"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(item["content"], encoding="utf-8")
                    sha = repo.commit_worktree(
                        worktree, f"{task.prompt[:60]}\n\nOrchestragent task {task.id}",
                        "Orchestragent",
                    )
                finally:
                    repo.git("worktree", "remove", "--force", str(tree / "wt"))
                    shutil.rmtree(tree, ignore_errors=True)
                remote = repo.remote()
                if remote:
                    repo.push(branch)
                return f"branch={branch}\ncommit={sha}\nremote={remote or 'none'}\n"

            def code_context(project: str, query: str, budget: int | None = None) -> str:
                root = chat_store.root_of(project) if chat_store is not None else ""
                if not root or chat_store is None:
                    return ""
                override, paths = chat_store.context_of(project)
                return index.context(
                    project, Path(root), query, override or budget, paths
                )

            controller.live.code_context = code_context

            def ci_worker(
                task_id: str, revision: str, files: list[dict[str, str]]
            ) -> tuple[bool, str]:
                policy = cast(dict[str, object], controller.settings.read()["policy"])
                url = str(policy.get("ci_worker_url", "") or "")
                if not url:
                    raise ValueError("no CI worker configured")
                token_file = args.data_dir / "ci-token"
                token = (
                    token_file.read_text(encoding="utf-8").strip()
                    if token_file.exists()
                    else ""
                )
                if not token:
                    raise ValueError("missing .data/ci-token")
                request = urllib.request.Request(
                    url.rstrip("/") + "/run",
                    json.dumps(
                        {"task_id": task_id, "revision": revision, "files": files}
                    ).encode(),
                    {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    value = json.load(response)
                return bool(value.get("passed")), str(value.get("report", ""))

            def delegated_or_local(
                task_id: str, revision: str, files: list[dict[str, str]]
            ) -> tuple[bool, str]:
                policy = cast(dict[str, object], controller.settings.read()["policy"])
                if str(policy.get("ci_worker_url", "") or ""):
                    try:
                        return ci_worker(task_id, revision, files)
                    except Exception as error:
                        return False, f"远程 CI 未完成（{type(error).__name__}）；未回退到本地"
                assert controller.live is not None
                return controller.live.workspace.integrate_bundle(task_id, revision)

            controller.live.ci_worker = delegated_or_local
            controller.live.file_reader = read_files
            controller.publish = publish
            if not (args.data_dir / "ci-token").exists():
                (args.data_dir / "ci-token").write_text(secrets.token_urlsafe(32) + "\n")
                (args.data_dir / "ci-token").chmod(0o600)
        controller.memory.backfill()
        controller.health = HealthMonitor(controller.store)
        controller.kimi = (
            KimiManual(controller.live.clients, args.data_dir) if controller.live else None
        )
        if controller.live is not None:
            controller.live.memory = controller.memory
        # Bind before recovering: a second server must not invalidate active work.
        server = Server(controller, args.port, chat=controller.chat)
        controller.recover()
        stopped = threading.Event()

        def work() -> None:
            while not stopped.is_set():
                try:
                    controller.tick()
                except Exception:
                    # A single failed step must never kill the dispatch thread; the
                    # lease expiry turns an abandoned claim into a retryable state.
                    pass
                stopped.wait(0.35)

        workers = max(1, min(8, int(os.environ.get("ORCHESTRA_WORKERS", "3"))))
        pool = [
            threading.Thread(target=work, daemon=True, name=f"dispatch-worker-{index}")
            for index in range(workers)
        ]
        for thread in pool:
            thread.start()

        def converse() -> None:
            chat = controller.chat
            assert chat is not None

            def policy() -> dict[str, object]:
                return cast(dict[str, object], controller.settings.read()["policy"])

            def submit(request_id: str, prompt: str, conversation: str = "default") -> str:
                task, _ = controller.submit(request_id, prompt, "happy", "live")
                with controller.store.transaction() as db:
                    stored = controller.store.get(db, task.id)
                    stored.project = conversation
                    controller.store.save(db, stored)
                return task.id

            while not stopped.is_set():
                try:
                    def decide(action: str, message: str) -> str:
                        tasks = controller.store.list_tasks()
                        prefix = next(
                            (word for word in message.split() if len(word) >= 6), ""
                        )
                        pending = [
                            task
                            for task in tasks
                            if task.status in {"waiting_acceptance", "waiting_approval"}
                        ]
                        target = next(
                            (task for task in pending if prefix and task.id.startswith(prefix)),
                            pending[-1] if pending else None,
                        )
                        if target is None:
                            return "现在没有等待你决定的任务。"
                        if target.status == "waiting_acceptance":
                            controller.accept(
                                target.id, action == "accept", message[:200]
                            )
                            verb = "已接受" if action == "accept" else "已打回重做"
                            return f"{verb}：{target.id[:8]}"
                        approval = target.approval
                        if approval is None:
                            return "这个任务没有待审批项。"
                        controller.approve(
                            target.id,
                            approval.id,
                            "approve" if action == "accept" else "deny",
                            approval.operation_digest,
                        )
                        verb = "已批准" if action == "accept" else "已拒绝"
                        return f"{verb}：{target.id[:8]}"

                    store_memory = controller.memory
                    forget = (
                        (lambda project, term, m=store_memory: m.forget(project, term))
                        if store_memory is not None
                        else None
                    )

                    def set_context(conversation: str, budget: int, paths: list[str]) -> str:
                        if controller.chat is None:
                            raise ValueError("项目不可用")
                        controller.chat.set_context(conversation, budget, paths)
                        where = "、".join(paths) if paths else "整个仓库"
                        return f"已设置：上下文上限 {budget // 1000} KB，范围 {where}"

                    def bind_repo(conversation: str, url: str) -> str:
                        if controller.chat is None or controller.index is None:
                            raise ValueError("项目不可用")
                        dest = args.data_dir / "projects" / conversation
                        repo = ProjectRepo.clone(url, dest)
                        controller.chat.set_root(conversation, str(repo.root))
                        stats = controller.index.build(conversation, repo.root)
                        files = len(list(repo.root.rglob("*"))) if repo.root.exists() else 0
                        return (
                            f"已连接 {repo.remote()}\n目录：{repo.root}\n"
                            f"文件：{files}，索引：{stats['total']} 个"
                        )

                    client = controller.kimi
                    ask = (
                        (lambda q, c=client: c.ask(q)) if client is not None else None
                    )
                    busy = tick_chat(
                        chat,
                        policy,
                        controller.live,
                        submit,
                        ask,
                        decide,
                        forget,
                        bind_repo,
                        set_context,
                    )
                except Exception:
                    busy = False
                stopped.wait(0.2 if busy else 1.0)

        threading.Thread(target=converse, daemon=True, name="chat-worker").start()

        def heartbeat() -> None:
            health = controller.health
            assert health is not None
            while not stopped.is_set():
                try:
                    if controller.live is not None:
                        health.probe(controller.live.clients)
                except Exception:
                    pass
                stopped.wait(60)

        threading.Thread(target=heartbeat, daemon=True, name="health-worker").start()

        def warm_models() -> None:
            # The Cursor CLI takes seconds to list models; warm it before the app asks.
            try:
                if controller.live is not None:
                    controller.live.clients.cursor.list_models()
            except Exception:
                pass

        threading.Thread(target=warm_models, daemon=True, name="catalog-worker").start()
        print(f"{server.mode}: http://127.0.0.1:{server.server_port}", flush=True)
        if remote is not None:
            threading.Thread(
                target=remote.serve_forever,
                kwargs={"poll_interval": 0.25},
                daemon=True,
                name="remote-listener",
            ).start()
            print(
                f"remote: http://127.0.0.1:{remote.server_port} (bearer token: {token_file})",
                flush=True,
            )
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            stopped.set()
            if controller.live:
                controller.live.clients.close()
            for task in controller.store.list_tasks():
                if task.mode == "live" and task.status == "running":
                    controller.action(task.id, "pause")
            server.server_close()
            if remote is not None:
                remote.shutdown()
                remote.server_close()
            for thread in pool:
                thread.join(timeout=10)


if __name__ == "__main__":
    main()
