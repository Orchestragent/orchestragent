"""Local-only guided vendor setup. Run in a Mac terminal, never send secrets in chat."""

import argparse
import getpass
import json
import subprocess
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from orchestragent.access import Access
from orchestragent.native import NativeClients
from orchestragent.platform import (
    claude_binary,
    codex_binary,
    cursor_binary,
    default_data_dir,
    kimi_binary,
)
from orchestragent.serialization import dumps

ROOT = Path(__file__).resolve().parents[2]


def request(path: str, body: dict[str, object]) -> dict[str, object]:
    req = urllib.request.Request(
        "http://127.0.0.1:8765" + path,
        dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        value: dict[str, object] = json.load(response)
        return value


def dsh() -> None:
    print("DSH 后台执行尚未开放：执行循环的费用上限待验证。")
    print("以下仅预配置 API 授权；不会发起任务。密钥只保存在当前服务进程内存。")
    kind = input("提到的 500 元是余额还是预算？输入 balance 或 budget: ").strip()
    cap = Decimal(input("本项目累计授权上限（人民币元，默认 0 不启用）: ").strip() or "0")
    if not cap.is_finite() or cap <= 0 or cap > 500 or cap != cap.quantize(Decimal("0.01")):
        raise ValueError("Invalid cap")
    body: dict[str, object] = {"provider": "dsh", "cap_cents": int(cap * 100), "funding_kind": kind}
    operation = request("/providers/authorization/preview", body)
    print(f"预配置 DSH API 累计预算 ¥{cap}。Flash 每次保守预留 ¥3，Pro ¥10。")
    print("预留不是实际账单或已验证硬上限；执行保持关闭。失败不自动退回预留。")
    if input("确认这个具体授权？输入 YES: ").strip() != "YES":
        print("未授权。")
        return
    key = getpass.getpass("DeepSeek API key（不回显，不写入文件）: ")
    request(
        "/providers/authorization/apply",
        {**body, "api_key": key, "operation_digest": operation["operation_digest"]},
    )
    print("DSH 本地配置完成。服务重启后需要重新输入密钥。")


def jev() -> None:
    """Grant orch's decision model a CNY cap and store the key locally; no server needed."""
    print("Jev（TypeSafe 决策模型，经 Vercel AI Gateway）只给 orch 做失败分诊和预筛，")
    print("API 单独计费。")
    print("每次调用先预留 ¥0.02，按网关返回的实际费用结算；不充值、不换账号、不回退其他模型。")
    cap = Decimal(input("Jev 累计授权上限（人民币元，建议 10）: ").strip() or "0")
    if not cap.is_finite() or cap <= 0 or cap > 500 or cap != cap.quantize(Decimal("0.01")):
        raise ValueError("Invalid cap")
    cents = int(cap * 100)
    access = Access(default_data_dir())
    operation = access.operation("jev", cents, "budget")
    if input(f"授权 Jev API 累计预算 ¥{cap}？输入 YES: ").strip() != "YES":
        print("未授权。")
        return
    key = getpass.getpass("Vercel AI Gateway API key（不回显，只存本机钥匙串）: ")
    access.authorize("jev", cents, "budget", str(operation["operation_digest"]), key)
    print("Jev 已配置。运行 scripts/orch doctor 检查。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["codex", "claude", "cursor", "dsh", "jev", "kimi"])
    args = parser.parse_args()
    try:
        if args.provider == "dsh":
            dsh()
            return
        if args.provider == "jev":
            jev()
            return
        environment = NativeClients._environment()
        if args.provider == "codex":
            command = [str(codex_binary()), "login"]
        elif args.provider == "claude":
            command = [str(claude_binary()), "auth", "login", "--claudeai"]
        elif args.provider == "cursor":
            executable = cursor_binary()
            environment["CURSOR_CONFIG_DIR"] = str(default_data_dir() / "cursor")
            command = [str(executable), "login"]
            print("请使用已有 Ultra 账号。登录不会授权额外消费；后台模型需另行确认额度。")
        else:
            executable = kimi_binary()
            environment["KIMI_CODE_HOME"] = str(default_data_dir() / "kimi")
            environment["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
            command = [str(executable), "login"]
            print("Kimi Max 仅供官方人工交互；此登录不启用后台订阅调度。")
        Path(environment.get("CURSOR_CONFIG_DIR", str(default_data_dir()))).mkdir(
            parents=True, exist_ok=True
        )
        result = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        if result.returncode:
            print("官方登录尚未完成；请查看终端提示。")
    except (ValueError, InvalidOperation, OSError, urllib.error.URLError):
        print("配置未完成，请确认本地服务已运行并检查输入；没有回显密钥。")
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。")


if __name__ == "__main__":
    main()
