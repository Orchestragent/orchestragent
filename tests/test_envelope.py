from dataclasses import replace

from orchestragent.envelope import build, digest, verify
from orchestragent.models import Artifact, Task


def task() -> Task:
    return Task(
        id="t1",
        request_id="r1",
        prompt="写一个加函数",
        scenario="happy",
        created_at=0.0,
        updated_at=0.0,
        revision="rev1",
        project="demo",
        artifacts=[Artifact("plan.json", '{"implementation": "i", "acceptance": "a"}')],
    )


def test_envelope_records_hashes_and_spec() -> None:
    files = [{"path": "a.py", "content": "x = 1\n"}]
    envelope = build(task(), stage="review", provider="claude", model="sonnet", files=files)
    assert envelope["version"] == 1
    spec = envelope["spec"]
    assert isinstance(spec, dict)
    assert spec["goal"] == "写一个加函数"
    artifacts = envelope["artifacts"]
    assert isinstance(artifacts, list)
    first = artifacts[0]
    assert isinstance(first, dict)
    assert first["sha256"] == digest("x = 1\n")
    assert first["bytes"] == 6


def test_verify_rejects_tampered_content() -> None:
    files = [{"path": "a.py", "content": "x = 1\n"}]
    envelope = build(task(), stage="review", provider="dsh", model="deepseek-flash", files=files)
    assert verify(envelope, files)
    assert not verify(envelope, [{"path": "a.py", "content": "x = 2\n"}])
    assert not verify(envelope, [{"path": "b.py", "content": "x = 1\n"}])


def test_envelope_carries_findings_for_the_next_vendor() -> None:
    base = task()
    base = replace(
        base,
        artifacts=[
            *base.artifacts,
            Artifact("review-findings.json", '{"findings": ["边界测试缺失"]}'),
        ],
    )
    envelope = build(base, stage="implement", provider="dsh", model="deepseek-flash")
    assert envelope["findings"] == ["边界测试缺失"]
