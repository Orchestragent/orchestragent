"""Conversational scheduler: one dialogue where the scheduler model decides when to dispatch."""

import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Protocol

from orchestragent.native import NativeResult
from orchestragent.serialization import dumps
from orchestragent.store import Store


class ChatModel(Protocol):
    def chat(
        self, provider: str, model: str, run_id: str, prompt: str, effort: str = ""
    ) -> NativeResult: ...


REASONS = {
    "auth_required": "调度模型需要重新登录，请在 Mac 上完成官方登录",
    "rate_limited": "厂商限流，稍后会自动重试",
    "quota_exhausted": "额度或预算已用完，请提高上限",
    "policy_blocked": "该厂商当前被策略禁用",
    "timed_out": "本轮超时，可以重发一次",
    "cancelled": "本轮已取消",
}


def explain(outcome: str, message: str) -> str:
    reason = REASONS.get(outcome, "调度模型本轮没有完成")
    detail = message.strip()
    if "No JSON object" in detail:
        detail = "模型没有按结构返回"
    elif "Unsupported" in detail:
        detail = "模型名与该厂商不匹配"
    return f"{reason}。" + (f"（{detail}）" if detail else "")


SYSTEM = (
    "你是 Orchestragent 的总调度 agent，用中文和用户对话。"
    "你只负责理解需求、澄清关键信息、决定何时派单；真正的实现由后台多厂商流水线完成。"
    "需要派单时把 action 设为 dispatch，并在 task_prompt 里写出可直接执行的完整需求"
    "（Python 标准库项目、含验收标准）。闲聊或信息不足时 action 为 none，task_prompt 为空字符串。"
    "reply 保持简短，不超过 400 字。只输出 JSON。"
)


class Chat:
    """Durable multi-project conversations plus one in-flight turn per project."""

    history_limit = 20

    def __init__(self, store: Store) -> None:
        self.store = store
        with store.transaction() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS chat_messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                task_id TEXT,
                created_at REAL NOT NULL)"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS chat_request ON chat_messages(request_id)"
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(chat_messages)").fetchall()
            }
            if "conversation" not in columns:
                db.execute(
                    "ALTER TABLE chat_messages ADD COLUMN conversation TEXT NOT NULL"
                    " DEFAULT 'default'"
                )
            if "usage" not in columns:
                db.execute("ALTER TABLE chat_messages ADD COLUMN usage TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                "CREATE TABLE IF NOT EXISTS chat_threads ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL,"
                " root TEXT NOT NULL DEFAULT '')"
            )
            thread_columns = {
                row[1] for row in db.execute("PRAGMA table_info(chat_threads)").fetchall()
            }
            for column, ddl in (
                ("context_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("context_paths", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in thread_columns:
                    db.execute(f"ALTER TABLE chat_threads ADD COLUMN {column} {ddl}")
            if "root" not in thread_columns:
                db.execute(
                    "ALTER TABLE chat_threads ADD COLUMN root TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                "INSERT OR IGNORE INTO chat_threads(id, title, created_at, root)"
                " VALUES('default','默认对话',?,'')",
                (time.time(),),
            )

    def conversations(self) -> list[dict[str, object]]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT t.id, t.title, COUNT(m.seq),"
                " COALESCE(MAX(m.created_at), t.created_at) AS touched,"
                " t.context_bytes, t.context_paths"
                " FROM chat_threads t LEFT JOIN chat_messages m"
                " ON m.conversation = t.id AND m.role = 'user'"
                " GROUP BY t.id ORDER BY touched DESC"
            ).fetchall()
        return [
            {
                "id": row[0],
                "title": row[1],
                "count": row[2],
                "touched": row[3],
                "context_bytes": row[4],
                "context_paths": str(row[5] or ""),
            }
            for row in rows
        ]

    def context_of(self, conversation: str) -> tuple[int, list[str]]:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT context_bytes, context_paths FROM chat_threads WHERE id = ?",
                (conversation,),
            ).fetchone()
        if row is None:
            return 0, []
        paths = [part.strip() for part in str(row[1] or "").split(",") if part.strip()]
        return int(row[0] or 0), paths

    def set_context(self, conversation: str, budget: int, paths: list[str]) -> None:
        with self.store.transaction() as db:
            cursor = db.execute(
                "UPDATE chat_threads SET context_bytes = ?, context_paths = ? WHERE id = ?",
                (budget, ",".join(paths), conversation),
            )
            if cursor.rowcount == 0:
                raise KeyError(conversation)

    def delete(self, conversation: str) -> None:
        with self.store.transaction() as db:
            total = db.execute("SELECT COUNT(*) FROM chat_threads").fetchone()[0]
            if total <= 1:
                raise ValueError("至少保留一个对话")
            cursor = db.execute("DELETE FROM chat_threads WHERE id = ?", (conversation,))
            if cursor.rowcount == 0:
                raise KeyError(conversation)
            db.execute("DELETE FROM chat_messages WHERE conversation = ?", (conversation,))

    def root_of(self, conversation: str) -> str:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT root FROM chat_threads WHERE id = ?", (conversation,)
            ).fetchone()
        return str(row[0]) if row else ""

    def set_root(self, conversation: str, root: str) -> dict[str, object]:
        with self.store.transaction() as db:
            cursor = db.execute(
                "UPDATE chat_threads SET root = ? WHERE id = ?", (root, conversation)
            )
            if cursor.rowcount == 0:
                raise KeyError(conversation)
        return {"id": conversation, "root": root}

    def create(self, title: str) -> dict[str, object]:
        name = title.strip()[:60] or "新项目"
        thread_id = uuid.uuid4().hex[:12]
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO chat_threads(id, title, created_at, root) VALUES(?,?,?,'')",
                (thread_id, name, time.time()),
            )
        return {"id": thread_id, "title": name, "count": 0, "touched": time.time()}

    def rename(self, conversation: str, title: str) -> dict[str, object]:
        name = title.strip()[:60]
        if not name:
            raise ValueError("Title must not be empty")
        with self.store.transaction() as db:
            cursor = db.execute(
                "UPDATE chat_threads SET title = ? WHERE id = ?", (name, conversation)
            )
            if cursor.rowcount == 0:
                raise KeyError(conversation)
        return {"id": conversation, "title": name}

    def messages(self, after: int = 0, conversation: str = "default") -> list[dict[str, object]]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT seq, role, content, status, task_id, created_at, usage"
                " FROM chat_messages WHERE seq > ? AND conversation = ? ORDER BY seq",
                (after, conversation),
            ).fetchall()
        return [
            {
                "seq": row[0],
                "role": row[1],
                "content": row[2],
                "status": row[3],
                "task_id": row[4],
                "created_at": row[5],
                "usage": json.loads(row[6]),
            }
            for row in rows
        ]

    def post(
        self, request_id: str, message: str, conversation: str = "default"
    ) -> list[dict[str, object]]:
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must contain 1–128 characters")
        if not message.strip() or len(message) > 8000:
            raise ValueError("message must contain 1–8000 characters")
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT seq FROM chat_messages WHERE request_id = ? AND role = 'user'",
                (request_id,),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT OR IGNORE INTO chat_threads(id, title, created_at, root)"
                    " VALUES(?, '未命名项目', ?, '')",
                    (conversation, time.time()),
                )
                db.execute(
                    "INSERT INTO chat_messages(request_id, role, content, status,"
                    " conversation, created_at) VALUES(?, 'user', ?, 'pending', ?, ?)",
                    (request_id, message.strip(), conversation, time.time()),
                )
        return self.messages(0, conversation)

    def clear(self) -> None:
        with self.store.transaction() as db:
            db.execute("DELETE FROM chat_messages")

    def recover(self) -> int:
        """A turn interrupted by a restart goes back to pending instead of dying."""
        with self.store.transaction() as db:
            cursor = db.execute(
                "UPDATE chat_messages SET status = 'pending' WHERE status = 'running'"
            )
            return int(cursor.rowcount)

    def pending(self) -> tuple[int, str, str] | None:
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT seq, content, conversation FROM chat_messages"
                " WHERE status = 'pending' ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE chat_messages SET status = 'running' WHERE seq = ?", (row[0],))
        return int(row[0]), str(row[1]), str(row[2])

    def history(self, upto: int) -> list[dict[str, str]]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT role, content FROM chat_messages WHERE seq < ? AND status = 'done'"
                " ORDER BY seq DESC LIMIT ?",
                (upto, self.history_limit),
            ).fetchall()
        return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

    def finish(
        self, seq: int, content: str, status: str, task_id: str | None = None,
        usage: dict[str, object] | None = None,
    ) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE chat_messages SET status = 'done' WHERE seq = ?", (seq,)
            )
            # The reply belongs to the thread that asked, not to the default one.
            db.execute(
                "INSERT INTO chat_messages(request_id, role, content, status, task_id,"
                " conversation, created_at, usage) SELECT request_id, 'assistant', ?, ?, ?,"
                " conversation, ?, ? FROM chat_messages WHERE seq = ?",
                (content, status, task_id, time.time(), dumps(usage or {}), seq),
            )

    @staticmethod
    def prompt(history: list[dict[str, str]], message: str) -> str:
        lines = [SYSTEM, "", "对话历史："]
        for entry in history:
            lines.append(f"{entry['role']}: {entry['content']}")
        lines.extend(["", f"用户最新消息：{message}", "", "只输出符合 schema 的 JSON。"])
        return "\n".join(lines)


KIMI_PREFIX = ("/kimi", "／kimi", "用kimi", "用 kimi", "让kimi", "让 kimi")
FORGET_PREFIX = ("/忘掉", "/forget", "忘掉记忆")


REPO_INTENT = ("绑定", "连接", "链上", "接入", "clone", "克隆", "仓库", "repo")
REPO_PATTERN = re.compile(
    r"(git@[\w.-]+:[\w./-]+\.git|https?://[\w.-]+/[\w./-]+\.git|[\w.-]+@?[\w.-]+:[\w./-]+\.git)"
)


def repo_target(message: str) -> str | None:
    """A repo is bound only when the sentence asks for it and names one URL."""
    lowered = message.lower()
    if not any(word in lowered for word in REPO_INTENT):
        return None
    found = REPO_PATTERN.search(message)
    return found.group(1) if found else None


CONTEXT_PREFIX = ("/上下文", "/context")


def context_request(message: str) -> tuple[int, list[str]] | None:
    """`/上下文 300k engine/ contracts/` sets bytes and optional path filters."""
    stripped = message.strip()
    prefix = next((p for p in CONTEXT_PREFIX if stripped.lower().startswith(p)), None)
    if prefix is None:
        return None
    parts = stripped[len(prefix) :].replace("，", " ").split()
    if not parts:
        return 0, []
    token = parts[0].lower()
    multiplier = {"k": 1_000, "m": 1_000_000}.get(token[-1:], 1) if token[-1:].isalpha() else 1
    number = token[:-1] if multiplier > 1 else token
    try:
        budget = int(float(number) * multiplier)
    except ValueError:
        return None
    if not 8_000 <= budget <= 4_194_304:
        return 0, []
    return budget, [part.strip("/") for part in parts[1:]][:8]


def forget_term(message: str) -> str | None:
    stripped = message.strip()
    for prefix in FORGET_PREFIX:
        if stripped.lower().startswith(prefix):
            return stripped[len(prefix) :].lstrip("：: ，,").strip()
    return None


def kimi_question(message: str) -> str | None:
    """Return the question when the user explicitly asked Kimi, else None."""
    stripped = message.strip()
    lowered = stripped.lower()
    for prefix in KIMI_PREFIX:
        if lowered.startswith(prefix):
            rest = stripped[len(prefix) :]
            return rest.strip("：: ，,").strip()
    return None


DECISIONS = {
    "accept": ("同意", "接受", "通过", "批准", "approve", "accept"),
    "reject": ("打回", "拒绝", "不通过", "reject", "deny"),
}


def decision(message: str) -> str | None:
    """Only a short, explicit reply counts as a decision; prose is not a command."""
    text = message.strip().lower()
    if len(text) > 60:
        return None
    for action, words in DECISIONS.items():
        if any(text.startswith(word) or text == word for word in words):
            return action
    return None


def tick_chat(
    chat: Chat,
    policy: Callable[[], dict[str, object]],
    live: ChatModel | None,
    submit: Callable[[str, str, str], str],
    ask_kimi: Callable[[str], dict[str, object]] | None = None,
    decide: Callable[[str, str], str] | None = None,
    forget: Callable[[str, str], int] | None = None,
    bind_repo: Callable[[str, str], str] | None = None,
    set_context: Callable[[str, int, list[str]], str] | None = None,
) -> bool:
    with chat.store.activity() as admitted:
        if not admitted:
            return False
        return _tick_chat(
            chat, policy, live, submit, ask_kimi, decide, forget, bind_repo, set_context
        )


def _tick_chat(
    chat: Chat,
    policy: Callable[[], dict[str, object]],
    live: ChatModel | None,
    submit: Callable[[str, str, str], str],
    ask_kimi: Callable[[str], dict[str, object]] | None,
    decide: Callable[[str, str], str] | None,
    forget: Callable[[str, str], int] | None,
    bind_repo: Callable[[str, str], str] | None,
    set_context: Callable[[str, int, list[str]], str] | None,
) -> bool:
    """Advance one turn. Returns True when work was attempted."""
    item = chat.pending()
    if item is None:
        return False
    seq, message, conversation = item
    request = context_request(message)
    if request is not None and set_context is not None:
        budget, paths = request
        if budget == 0:
            chat.finish(seq, "上下文范围是 8 KB – 4 MB，例如 `/上下文 2m engine/`", "done")
        else:
            chat.finish(seq, set_context(conversation, budget, paths), "done")
        return True
    target = repo_target(message)
    if target is not None and bind_repo is not None:
        try:
            chat.finish(seq, bind_repo(conversation, target), "done")
        except Exception as error:
            chat.finish(seq, f"绑定仓库失败：{type(error).__name__}: {error}"[:300], "failed")
        return True
    term = forget_term(message)
    if term is not None and forget is not None:
        removed = forget(conversation, term)
        chat.finish(
            seq,
            f"已忘记 {removed} 条与“{term}”相关的记忆。" if removed
            else f"没有找到与“{term}”相关的记忆。",
            "done",
        )
        return True
    action = decision(message)
    if action is not None and decide is not None:
        chat.finish(seq, decide(action, message), "done")
        return True
    question = kimi_question(message)
    if question is not None:
        if ask_kimi is None:
            chat.finish(seq, "Kimi 当前未启用。", "failed")
            return True
        if not question:
            chat.finish(seq, "在 /kimi 后面写你的问题。", "failed")
            return True
        answer = ask_kimi(question)
        if answer.get("ok") is True:
            chat.finish(seq, str(answer.get("text", ""))[:8000], "done")
        else:
            chat.finish(seq, f"Kimi 未完成：{answer.get('message', '')}", "failed")
        return True
    history = chat.history(seq)
    if live is None:
        chat.finish(seq, f"（Mock）已收到：{message[:200]}。真实调度需要 --live 服务。", "failed")
        return True
    settings = policy()
    provider = str(settings.get("scheduler_provider", "codex"))
    effort = str(settings.get("chat_effort", "") or "")
    model = str(settings.get("scheduler_model", "")) or "gpt-6-astra"
    # chat_model is a latency knob for the Claude CLI only; other providers keep
    # their own model ids or the call would be rejected.
    if provider == "claude" and settings.get("chat_model"):
        model = str(settings["chat_model"])
    call = live.chat
    result = call(provider, model, f"chat-{seq}", Chat.prompt(history, message), effort)
    if result.outcome != "ok":
        chat.finish(seq, explain(result.outcome, result.message), "failed", usage=result.usage)
        return True
    reply = str(result.data.get("reply", "")).strip()[:4000]
    task_id: str | None = None
    if result.data.get("action") == "dispatch":
        task_prompt = str(result.data.get("task_prompt", "")).strip()
        if task_prompt:
            task_id = submit(f"chat-{seq}", task_prompt, conversation)
    chat.finish(seq, reply or "（空回复）", "done", task_id, result.usage)
    return True
