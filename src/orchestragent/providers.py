"""One dispatch seam; native subscriptions and explicitly authorized API stay distinct."""

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from orchestragent import byok, deepseek_api
from orchestragent.access import REJECTED_STATUSES, Access
from orchestragent.catalog import models as catalog_models
from orchestragent.cursor import CursorClient
from orchestragent.dsh import DshClient
from orchestragent.jev import JevClient
from orchestragent.native import NativeClients, NativeResult
from orchestragent.platform import dsh_binary

BACKGROUND_PROVIDERS = ("codex", "claude", "dsh", "cursor")

# Verified against this Mac: a catalog id can exist while the account or the CLI
# rejects it, so only ids proven to answer are offered.
def model_catalog(cursor: CursorClient) -> dict[str, list[dict[str, str]]]:
    catalog = {
        provider: catalog_models(provider) for provider in ("codex", "claude", "dsh", "kimi")
    }
    listed = cursor.list_models() or ["composer-2.5", "composer-2.5-fast"]
    catalog["cursor"] = [{"id": model, "label": model} for model in listed]
    return catalog

# Conservative CNY fen per million tokens: DeepSeek's published peak USD list price
# for V4.1-Flash-1017 (2026-10) at 7.5 CNY/USD, rounded up. input_tokens pay the
# cache-miss rate even if they include cache reads, which are charged again, and
# reasoning tokens are charged again as output, so the ledger can only over-count.
# Models without a row settle the full reservation.
DSH_PRICE_FEN_PER_MTOK: dict[str, dict[str, int]] = {
    "deepseek-flash": {
        "input_tokens": 250,
        "cache_creation_input_tokens": 250,
        "cache_read_input_tokens": 5,
        "output_tokens": 1000,
        "reasoning_tokens": 1000,
    }
}


def dsf_transport() -> str:
    """How DeepSeek is reached: "cli" (the official DSH CLI behind the request guard) when
    it is installed, else "api" (the same single request sent directly with the user's
    key). ORCHESTRA_DSF_TRANSPORT=cli|api chooses explicitly. Model, key and budget are
    the same either way; this is chosen once, never as a fallback after a failure."""
    chosen = os.environ.get("ORCHESTRA_DSF_TRANSPORT", "")
    if chosen in {"cli", "api"}:
        return chosen
    return "cli" if dsh_binary().is_file() else "api"


def settled_cents(model: str, usage: dict[str, object], reservation: int) -> int:
    """Charge reported tokens at the conservative rate; unknown usage keeps the reservation."""
    prices = DSH_PRICE_FEN_PER_MTOK.get(model)
    counts = {
        key: value for key, value in usage.items() if type(value) is int and value >= 0
    }
    if prices is None or "input_tokens" not in counts or "output_tokens" not in counts:
        return reservation
    micro = sum(counts.get(key, 0) * price for key, price in prices.items())
    return min(reservation, max(1, -(-micro // 1_000_000)))


DEFAULT_MODELS = {
    "codex": "gpt-6-astra",
    "claude": "sonnet",
    "dsh": "deepseek-flash",
    "cursor": "composer-2.5",
    "kimi": "",
}


class ProviderClients(NativeClients):
    # The bound is established at the model transport: dsh_guard forwards exactly one
    # POST and answers later calls with 402 before any upstream connection exists.
    # tests/test_dsh_guard.py proves the transport; tests/test_dsh.py proves routing.
    # No public/configurable override.
    dsh_request_bound_verified = True

    def __init__(self, root: Path):
        super().__init__(root / "native", float(os.environ.get("ORCHESTRA_NATIVE_TIMEOUT", "0")))
        self.access = Access(root)
        # A restart must not require re-entering an already granted API key.
        self.access.restore_keys()
        self.provider_root = root.resolve()
        self.dsh = DshClient(
            self, root / "dsh", account=os.environ.get("ORCHESTRA_DSH_ACCOUNT", "0") == "1"
        )
        self.cursor = CursorClient(self, root)
        # Decision model for orch triage; never part of the coding dispatch routes.
        self.jev = JevClient(self.access)

    def status(self) -> dict[str, object]:
        result = super().status()
        dsh = self.access.status("dsh")
        transport = "cli" if self.dsh.account else dsf_transport()
        authentication = self.access.authentication(
            "dsh", DEFAULT_MODELS["dsh"], "cli-account" if self.dsh.account else transport)
        installed = transport == "api" or dsh_binary().is_file()
        configured = bool(installed and dsh["credential_ready"] and dsh["authorized"]
                          and (self.dsh.account or authentication["generation"])
                          and self.dsh_request_bound_verified)
        verified = authentication["verified"] is True
        message = ("DSF authentication verified by actual inference" if verified
                   else "DSF authentication rejected; update the key locally"
                   if authentication["state"] == "auth_required"
                   else "DSF local setup configured; authentication unverified" if configured
                   else "DSF local setup incomplete; authentication unverified")
        result["dsh"] = {
            **dsh, "transport": transport, "model": DEFAULT_MODELS["dsh"],
            "configured": configured, "verified": verified, "ready": configured and verified,
            "authentication": authentication, "auth": "api_explicit_budget", "message": message,
        }
        result["cursor"] = self.cursor.status()
        result["jev"] = self.jev.status()
        result["kimi"] = {
            "ready": False,
            "auth": "interactive_only",
            "message": "Max 订阅禁止非交互自动化；仅官方人工交互",
        }
        return result

    def call(
        self,
        provider: str,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        *,
        effort: str = "",
    ) -> NativeResult:
        if provider in {"codex", "claude"}:
            return super().call(provider, model, prompt, schema, run_id, cancelled, effort=effort)
        if provider == "kimi":
            return NativeResult(
                "policy_blocked", {}, message="Kimi Max 订阅不允许后台非交互任务；未切换付费 API"
            )
        if provider == "cursor":
            return self.cursor.call(model, prompt, schema, run_id, cancelled)
        if provider != "dsh":
            entry = byok.registry(self.provider_root).get(provider)
            if entry is None:
                return NativeResult("failed", {}, message="Unknown provider")
            key = byok.api_key(entry, lambda: self.access.load_secret(provider))
            return byok.call(entry, key, model, str(prompt), schema, cancelled,
                             self.call_timeout(effort), effort)
        if model not in {"deepseek-flash", "deepseek-v4-pro"}:
            return NativeResult("failed", {}, message="Unsupported DSH model; no fallback")
        if not self.dsh.account and not self.access.key(provider):
            return NativeResult("auth_required", {}, message="请在 Mac 本机输入 DeepSeek API key")
        if self.access.status(provider)["authorized"] is not True:
            return NativeResult("waiting_approval", {}, message="DSH API 尚未授权消费预算")
        if cancelled():
            return NativeResult("cancelled", {}, usage={"forwarded": 0})
        if not self.dsh_request_bound_verified:
            return NativeResult(
                "policy_blocked", {}, message="DSH 执行循环请求上限尚未验证；未发起 API 调用"
            )
        generation = self.access.credential_generation(provider)
        api_key = self.access.key(provider)
        transport = "cli" if self.dsh.account else dsf_transport()
        if not self.dsh.account and (
            not generation or generation != self.access.credential_generation(provider)
        ):
            return NativeResult(
                "auth_required", {}, usage={"forwarded": 0},
                message="DSF credential changed before dispatch; retry with current key",
            )
        # deepseek-flash at DSH_PRICE_FEN_PER_MTOK: a full 1M-token input is about 250 fen and
        # output 1 fen per 1000 tokens, so 300 fen covers the 32768-token base output at any
        # input size; orch_run._widened allows the 131072-token tier only while request bytes
        # (as input tokens) plus that output still fit. No tools/retries within one call.
        # A reservation is conservative exposure, NOT a claimed provider invoice.
        reservation = 300 if model == "deepseek-flash" else 1000
        if not self.access.reserve(provider, run_id, reservation):
            return NativeResult("auth_required", {}, usage={"forwarded": 0},
                                message="DSH API key not configured (orch keys dsh); no call made")
        if not self.dsh.account and (
            not generation or generation != self.access.credential_generation(provider)
        ):
            self.access.release(run_id)
            return NativeResult(
                "auth_required", {}, usage={"forwarded": 0},
                message="DSF credential changed before dispatch; retry with current key",
            )
        result = self._call_dsh(
            model, prompt, schema, run_id, cancelled, reservation, effort,
            api_key=api_key, transport=transport,
        )
        forwarded = result.usage.get("forwarded")
        if not self.dsh.account and type(forwarded) is int and forwarded == 1:
            if result.outcome == "ok" and "rejected" not in result.usage:
                self.access.record_auth(provider, model, transport, generation, "ok")
            elif (result.outcome == "auth_required"
                  and result.usage.get("rejected") in {401, 403}):
                self.access.record_auth(provider, model, transport, generation, "auth_required")
        counted = "input_tokens" in result.usage and "output_tokens" in result.usage
        if result.outcome == "ok" or counted:
            # Reported tokens are what the provider bills, whether or not the answer was usable.
            settled = settled_cents(model, result.usage, reservation)
            self.access.settle(run_id, provider, settled)
            result = replace(result, usage={**result.usage, "settled_cents": settled})
        elif (
            result.outcome == "policy_blocked"
            or result.usage.get("forwarded") == 0
            or result.usage.get("rejected") in REJECTED_STATUSES
        ):
            # Nothing left the process, the guard forwarded nothing, or the API refused the
            # request before inference (4xx): nothing was spent.
            self.access.release(run_id)
        # Any other outcome may have reached the provider, so its exposure is kept.
        return result

    def _call_dsh(
        self,
        model: str,
        prompt: str,
        schema: dict[str, object],
        run_id: str,
        cancelled: Callable[[], bool],
        reservation: int,
        effort: str = "", *, api_key: str, transport: str,
    ) -> NativeResult:
        if transport == "api" and not self.dsh.account:
            result = deepseek_api.call(
                api_key, model, prompt, schema, cancelled,
                self.timeout, self.provider_root / "dsh" / "guard.log", effort,
            )
        else:
            result = self.dsh.call(
                model,
                prompt,
                schema,
                run_id,
                cancelled,
                api_key=api_key,
                effort=effort,
            )
        return replace(
            result,
            usage={
                **result.usage,
                "billing": "api",
                "currency": "CNY",
                "reserved_cents": reservation,
                "dsf_transport": ("cli-account" if self.dsh.account else
                                  "api" if transport == "api" else "cli"),
            },
        )
