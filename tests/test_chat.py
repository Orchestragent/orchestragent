from dataclasses import dataclass, field
from typing import cast

import pytest

from orchestragent.chat import Chat, tick_chat
from orchestragent.controller import Controller
from orchestragent.native import NativeResult


@dataclass
class FakeLive:
    data: dict[str, object]
    prompts: list[str] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)

    def chat(
        self, provider: str, model: str, run_id: str, prompt: str, effort: str = ""
    ) -> NativeResult:
        self.prompts.append(prompt)
        self.efforts.append(effort)
        return NativeResult("ok", dict(self.data), usage={"effort": effort})


@pytest.fixture
def chat(controller: Controller) -> Chat:
    return Chat(controller.store)


def no_submit(request_id: str, prompt: str, conversation: str = "default") -> str:
    return "unused"


def policy_of(controller: Controller) -> dict[str, object]:
    return cast(dict[str, object], controller.settings.read()["policy"])


def test_saved_scheduler_effort_reaches_call_and_survives_reload(
    chat: Chat, controller: Controller,
) -> None:
    settings = controller.settings.read()
    policy = policy_of(controller)
    policy["chat_effort"] = "high"
    controller.settings.update(int(str(settings["revision"])), policy)
    live = FakeLive({"reply": "done", "action": "none"})
    chat.post("high-effort", "hello")
    tick_chat(chat, lambda: policy_of(controller), live, no_submit)
    assert live.efforts == ["high"]
    assert Chat(controller.store).messages()[-1]["usage"] == {"effort": "high"}


def test_legacy_messages_gain_empty_usage_without_losing_content(chat: Chat) -> None:
    chat.post("legacy", "keep this message")
    with chat.store.transaction() as db:
        db.execute("ALTER TABLE chat_messages DROP COLUMN usage")
    message = Chat(chat.store).messages()[0]
    assert (message["content"], message["usage"]) == ("keep this message", {})


def test_mock_turn_reports_live_requirement(chat: Chat, controller: Controller) -> None:
    chat.post("req-1", "帮我做个小工具")
    assert tick_chat(chat, lambda: policy_of(controller), None, no_submit) is True
    turns = chat.messages(0)
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["status"] == "failed"
    assert "Mock" in str(turns[1]["content"])
    assert tick_chat(chat, lambda: policy_of(controller), None, no_submit) is False


def test_post_is_idempotent_per_request(chat: Chat) -> None:
    chat.post("same", "第一句")
    chat.post("same", "重复提交")
    assert [t["content"] for t in chat.messages(0)] == ["第一句"]


def test_dispatch_creates_one_task_and_keeps_history(
    chat: Chat, controller: Controller
) -> None:
    live = FakeLive(
        {"reply": "好的，开始。", "action": "dispatch", "task_prompt": "写一个 slugify 项目"}
    )
    submitted: list[tuple[str, str, str]] = []

    def submit(request_id: str, prompt: str, conversation: str = "default") -> str:
        submitted.append((request_id, prompt, conversation))
        return "task-12345678"

    chat.post("req-2", "做个 slugify")
    seq = chat.messages(0)[0]["seq"]
    assert tick_chat(chat, lambda: policy_of(controller), live, submit) is True
    assert submitted == [(f"chat-{seq}", "写一个 slugify 项目", "default")]
    turns = chat.messages(0)
    assert turns[1]["content"] == "好的，开始。"
    assert turns[1]["task_id"] == "task-12345678"
    assert "用户最新消息：做个 slugify" in live.prompts[0]

    chat.post("req-3", "继续")
    tick_chat(
        chat,
        lambda: policy_of(controller),
        FakeLive({"reply": "好", "action": "none", "task_prompt": ""}),
        submit,
    )
    history = chat.history(chat.messages(0)[-1]["seq"])  # type: ignore[arg-type]
    assert [h["role"] for h in history].count("assistant") >= 1


def test_invalid_input_rejected(chat: Chat) -> None:
    for request_id, message in (("", "x"), ("a" * 129, "x"), ("ok", "   ")):
        with pytest.raises(ValueError):
            chat.post(request_id, message)


def test_failed_model_turn_is_reported(chat: Chat, controller: Controller) -> None:
    @dataclass
    class Broken:
        def chat(self, *_args: object) -> NativeResult:
            return NativeResult("rate_limited", {}, message="slow down")

    chat.post("req-4", "在吗")
    tick_chat(chat, lambda: policy_of(controller), Broken(), no_submit)
    turn = chat.messages(0)[1]
    assert turn["status"] == "failed"
    assert "限流" in str(turn["content"])


@pytest.mark.parametrize(
    ("message", "question"),
    [
        ("/kimi 帮我看看这段代码", "帮我看看这段代码"),
        ("用 Kimi：解释一下 envelope", "解释一下 envelope"),
        ("让kimi 复核", "复核"),
        ("你好", None),
    ],
)
def test_kimi_prefix_is_explicit(message: str, question: str | None) -> None:
    from orchestragent.chat import kimi_question

    assert kimi_question(message) == question


def test_kimi_turn_bypasses_the_scheduler(chat: Chat, controller: Controller) -> None:
    asked: list[str] = []

    def ask(question: str) -> dict[str, object]:
        asked.append(question)
        return {"ok": True, "text": "Kimi 的回答", "message": ""}

    Chat.post(chat, "kimi-1", "/kimi 用一段话解释 RAG")
    assert tick_chat(chat, lambda: policy_of(controller), None, no_submit, ask) is True
    assert asked == ["用一段话解释 RAG"]
    turns = chat.messages(0)
    assert turns[1]["content"] == "Kimi 的回答"
    assert turns[1]["status"] == "done"


def test_kimi_without_question_is_rejected(chat: Chat, controller: Controller) -> None:
    Chat.post(chat, "kimi-2", "/kimi")
    assert tick_chat(
        chat, lambda: policy_of(controller), None, no_submit,
        lambda _q: {"ok": True, "text": "unused", "message": ""},
    )
    assert "后面写你的问题" in str(chat.messages(0)[1]["content"])


@pytest.mark.parametrize(
    ("message", "expected"),
    [("同意", "accept"), ("接受交付", "accept"), ("打回，README 写错了", "reject"),
     ("拒绝", "reject"), ("帮我看看这个同意不同意", None), ("x" * 80 + "同意", None)],
)
def test_only_explicit_short_replies_count_as_decisions(
    message: str, expected: str | None
) -> None:
    from orchestragent.chat import decision

    assert decision(message) == expected


def test_chat_decision_is_handled_without_a_model_call(
    chat: Chat, controller: Controller
) -> None:
    seen: list[tuple[str, str]] = []

    def decide(action: str, message: str) -> str:
        seen.append((action, message))
        return "已接受：abc"

    Chat.post(chat, "decision-1", "同意")
    assert tick_chat(
        chat, lambda: policy_of(controller), None, no_submit, None, decide
    )
    assert seen == [("accept", "同意")]
    assert chat.messages(0)[1]["content"] == "已接受：abc"


def test_agent_can_forget_a_memory_by_keyword(chat: Chat, controller: Controller) -> None:
    from orchestragent.memory import MemoryStore, Note

    memory = MemoryStore(controller.store)
    memory.remember(Note(project="p1", kind="stage", title="review: x", body="旧结论"))
    memory.remember(Note(project="p1", kind="stage", title="plan: y", body="保留"))
    Chat.post(chat, "forget-1", "/忘掉 旧结论", conversation="p1")
    calls: list[tuple[str, str]] = []

    def forget(project: str, term: str) -> int:
        calls.append((project, term))
        return memory.forget(project, term)

    assert tick_chat(
        chat, lambda: policy_of(controller), None, no_submit, None, None, forget
    )
    assert calls == [("p1", "旧结论")]
    assert memory.stats("p1")["notes"] == 1
    assert "已忘记 1 条" in str(chat.messages(0, "p1")[-1]["content"])


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("把这个仓库连上 git@github.com:me/quant.git", "git@github.com:me/quant.git"),
        ("连接 https://github.com/me/quant.git 到当前项目", "https://github.com/me/quant.git"),
        ("帮我看看这个项目", None),
        ("git@github.com:me/quant.git", None),
    ],
)
def test_repo_binding_needs_intent_and_a_url(message: str, expected: str | None) -> None:
    from orchestragent.chat import repo_target

    assert repo_target(message) == expected


def test_chat_binds_a_repo_without_a_model_call(chat: Chat, controller: Controller) -> None:
    seen: list[tuple[str, str]] = []

    def bind(conversation: str, url: str) -> str:
        seen.append((conversation, url))
        return "已连接"

    Chat.post(chat, "bind-1", "连接 git@github.com:me/quant.git", conversation="p1")
    assert tick_chat(
        chat, lambda: policy_of(controller), None, no_submit, None, None, None, bind
    )
    assert seen == [("p1", "git@github.com:me/quant.git")]
    assert chat.messages(0, "p1")[-1]["content"] == "已连接"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/上下文 300k", (300000, [])),
        ("/上下文 120k engine/ contracts/", (120000, ["engine", "contracts"])),
        ("/上下文 2000", (0, [])),
        ("普通消息", None),
    ],
)
def test_context_command_parses_bytes_and_paths(
    message: str, expected: tuple[int, list[str]] | None
) -> None:
    from orchestragent.chat import context_request

    assert context_request(message) == expected


def test_context_command_is_applied_without_a_model_call(
    chat: Chat, controller: Controller
) -> None:
    seen: list[tuple[str, int, list[str]]] = []

    def set_ctx(conversation: str, budget: int, paths: list[str]) -> str:
        seen.append((conversation, budget, paths))
        return "已设置"

    Chat.post(chat, "ctx-1", "/上下文 300k engine/", conversation="p1")
    assert tick_chat(
        chat, lambda: policy_of(controller), None, no_submit, None, None, None, None, set_ctx
    )
    assert seen == [("p1", 300000, ["engine"])]


def test_deleting_a_conversation_removes_its_messages(chat: Chat) -> None:
    first = Chat.post(chat, "del-1", "第一个项目", conversation="a")
    assert first
    chat.create("第二个项目")
    chat.delete("a")
    assert all(thread["id"] != "a" for thread in chat.conversations())


def test_the_last_conversation_cannot_be_deleted(chat: Chat) -> None:
    with pytest.raises(ValueError):
        chat.delete("default")


def test_a_turn_interrupted_by_a_restart_is_retried(chat: Chat) -> None:
    Chat.post(chat, "recover-1", "重启前发出的消息")
    assert chat.pending() is not None  # moves it to running
    chat.recover()
    assert chat.pending() is not None  # available again
