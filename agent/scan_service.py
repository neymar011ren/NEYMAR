"""本机 Agent 全量巡检：自动探测已知档案里哪些 Agent 已安装、哪些已经配了渠道，
并对已配置的渠道做真实探测（连通性 + function calling）+ 协议转换保真度检查，
最后汇总成一份可下载的报告。

这个模块只读、不写：不会创建/修改任何目标 Agent 的配置文件，只读取已有配置文件
去尝试提取"如果要拿这份配置去打一次真实请求，需要哪些参数"，用于探测。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_config_tools import probe_channel
from agent_install import check_agent_installed
from agent_profiles import AGENT_PROFILES, AgentProfile, profile_paths
from config_store import DATA_DIR, load_config, mask_secret
from protocol_adapter import check_protocol_fidelity

REPORTS_DIR = DATA_DIR / "reports"

# 自动巡检是"打开页面就跑"，不应该因为某个不可达的地址卡住整个页面很久，
# 所以这里用一个比用户全局 request_timeout_seconds 短得多、且固定的超时。
SCAN_TIMEOUT_SECONDS = 12


def _clean(value: str) -> str:
    return value.strip().strip("\"',")


def _extract_claude_code(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    env = data.get("env")
    if not isinstance(env, dict):
        return None
    base_url = env.get("ANTHROPIC_BASE_URL")
    api_key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
    model = env.get("ANTHROPIC_MODEL") or ""
    if not base_url or not api_key:
        return None
    return {"base_url": base_url, "model": model, "api_key": api_key}


def _extract_workbuddy(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or ""
        api_key = entry.get("apiKey")
        if not url or not api_key:
            continue
        base_url = url[: -len("/chat/completions")] if url.endswith("/chat/completions") else url
        model = entry.get("id") or entry.get("name") or ""
        return {"base_url": base_url, "model": model, "api_key": api_key}
    return None


def _extract_continue_yaml(text: str) -> dict[str, Any] | None:
    base = re.search(r"apiBase:\s*(\S+)", text)
    key = re.search(r"apiKey:\s*(\S+)", text)
    model = re.search(r"^\s*model:\s*(\S+)", text, re.MULTILINE)
    if not base or not key:
        return None
    return {
        "base_url": _clean(base.group(1)),
        "model": _clean(model.group(1)) if model else "",
        "api_key": _clean(key.group(1)),
    }


def _extract_aider(text: str) -> dict[str, Any] | None:
    base = re.search(r"^openai-api-base:\s*(\S+)", text, re.MULTILINE)
    key = re.search(r"^openai-api-key:\s*(\S+)", text, re.MULTILINE)
    model = re.search(r"^model:\s*(\S+)", text, re.MULTILINE)
    if not base or not key:
        return None
    return {
        "base_url": _clean(base.group(1)),
        "model": _clean(model.group(1)) if model else "",
        "api_key": _clean(key.group(1)),
    }


def _extract_codex_toml(text: str) -> dict[str, Any] | None:
    block_match = re.search(r"\[model_providers\.forge_custom\]([\s\S]*?)(?=\n\[|\Z)", text)
    if not block_match:
        return None
    block = block_match.group(1)
    base = re.search(r'base_url\s*=\s*"([^"]+)"', block)
    key = re.search(r'api_key\s*=\s*"([^"]+)"', block)
    if not base or not key:
        return None
    combo = re.search(r'model\s*=\s*"([^"]+)"\s*\n\s*model_provider\s*=\s*"forge_custom"', text)
    model = combo.group(1) if combo else ""
    return {"base_url": base.group(1), "model": model, "api_key": key.group(1)}


def _extract_opencode(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    provider = data.get("provider")
    if not isinstance(provider, dict):
        return None
    entry = provider.get("forge-custom")
    if not isinstance(entry, dict):
        return None
    options = entry.get("options") or {}
    base_url = options.get("baseURL")
    api_key = options.get("apiKey")
    if not base_url or not api_key:
        return None
    model = data.get("model") or ""
    if isinstance(model, str) and model.startswith("forge-custom/"):
        model = model[len("forge-custom/") :]
    return {"base_url": base_url, "model": model, "api_key": api_key}


def _extract_cline(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    base_url = data.get("openAiBaseUrl")
    api_key = data.get("openAiApiKey")
    if not base_url or not api_key:
        return None
    return {"base_url": base_url, "model": data.get("openAiModelId") or "", "api_key": api_key}


def _extract_kingswitch(_: Any = None) -> dict[str, Any] | None:
    cfg = load_config()
    if not cfg.api_key or not cfg.api_base_url:
        return None
    return {"base_url": cfg.api_base_url, "model": cfg.model, "api_key": cfg.api_key, "protocol": cfg.protocol}


# (读取方式, 提取函数)；"json" 会先 json.loads 再传给提取函数，"text"/"raw" 直接传原文。
_EXTRACTORS: dict[str, tuple[str, Callable[[Any], dict[str, Any] | None]]] = {
    "claude_code": ("json", _extract_claude_code),
    "workbuddy": ("json", _extract_workbuddy),
    "continue": ("text", _extract_continue_yaml),
    "aider": ("text", _extract_aider),
    "codex": ("text", _extract_codex_toml),
    "opencode": ("json", _extract_opencode),
    "cline": ("json", _extract_cline),
    "kingswitch": ("none", _extract_kingswitch),
}


def extract_channel_credentials(profile: AgentProfile) -> dict[str, Any] | None:
    """尽力从本地已有配置文件里提取出可用于真实探测的 base_url/model/api_key。

    只认识 KingSwitch 自己写过的那几种文件形状；如果用户的配置是自己手写的、
    形状不一样，提取会失败并返回 None（调用方应该把这种情况报告为
    "配置存在但无法识别凭证"，而不是当成"未配置"）。
    """
    spec = _EXTRACTORS.get(profile.id)
    if not spec:
        return None
    kind, fn = spec
    if kind == "none":
        result = fn(None)
        if result:
            result.setdefault("source_path", str(fn.__name__))
        return result
    for path in profile_paths(profile):
        if not path.exists() or not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        result = None
        if kind == "json":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            result = fn(data)
        else:
            result = fn(raw)
        if result:
            result["source_path"] = str(path)
            return result
    return None


def _scan_one_profile(profile: AgentProfile, include_tool_call: bool, include_fidelity: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "agent_id": profile.id,
        "name": profile.name,
        "native_protocol": profile.native_protocol,
    }
    install_status = check_agent_installed(profile.id)
    row["installed"] = bool(install_status.get("installed")) or profile.id == "kingswitch"

    creds = extract_channel_credentials(profile)
    row["config_found"] = creds is not None

    if creds:
        row["base_url"] = creds["base_url"]
        row["model"] = creds.get("model") or ""
        row["api_key_masked"] = mask_secret(creds["api_key"])
        row["source_path"] = creds.get("source_path", "")
        protocol = creds.get("protocol") or profile.native_protocol
        probe = probe_channel(
            profile.id,
            creds["base_url"],
            creds.get("model") or "probe-model",
            creds["api_key"],
            source_protocol=protocol,
            include_tool_call=include_tool_call,
            timeout_seconds=SCAN_TIMEOUT_SECONDS,
        )
        row["probe"] = probe
        row["status"] = "ok" if probe.get("ok") else "probe_failed"
    else:
        row["status"] = "not_installed" if not row["installed"] else "not_configured"

    if include_fidelity and profile.native_protocol != "chat_completions":
        row["protocol_fidelity"] = check_protocol_fidelity(profile.native_protocol)

    return row


def scan_all_agents(include_tool_call: bool = True, include_fidelity: bool = True) -> dict[str, Any]:
    """同步版本：依次巡检所有档案。给测试/脚本用；线上请求走 async 版本以并发跑。"""
    rows = [_scan_one_profile(p, include_tool_call, include_fidelity) for p in AGENT_PROFILES.values()]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "agents": rows}


async def scan_all_agents_async(include_tool_call: bool = True, include_fidelity: bool = True) -> dict[str, Any]:
    """并发跑各个 Agent 的巡检，避免"其中一个配置指向的地址连不上"拖慢总耗时——
    每个档案的探测都在各自的线程里跑，总耗时约等于最慢的那一个，而不是全部加总。"""
    import asyncio

    tasks = [
        asyncio.to_thread(_scan_one_profile, profile, include_tool_call, include_fidelity)
        for profile in AGENT_PROFILES.values()
    ]
    rows = await asyncio.gather(*tasks)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "agents": list(rows)}


def _status_mark(ok: bool | None) -> str:
    if ok is None:
        return "—"
    return "✅" if ok else "❌"


def render_report_markdown(scan: dict[str, Any]) -> str:
    lines = [
        "# KingSwitch 本机 Agent 巡检报告",
        "",
        f"生成时间（UTC）：{scan['generated_at']}",
        "",
        "| Agent | 已安装 | 已配置渠道 | 连通性 | 工具调用 | 协议保真 |",
        "|---|---|---|---|---|---|",
    ]
    for row in scan["agents"]:
        probe = row.get("probe") or {}
        ping = probe.get("ping")
        ping_mark = _status_mark(ping.get("ok")) if ping else "—"
        tool_probe = probe.get("tool_calling")
        tool_mark = _status_mark(tool_probe.get("ok")) if tool_probe else "—"
        fidelity = row.get("protocol_fidelity")
        fidelity_mark = "—"
        if fidelity and not fidelity.get("skipped"):
            fidelity_mark = _status_mark(fidelity.get("ok"))
        lines.append(
            f"| {row['name']} | {_status_mark(row['installed'])} | "
            f"{_status_mark(row['config_found']) if row['config_found'] else '—'} | "
            f"{ping_mark} | {tool_mark} | {fidelity_mark} |"
        )

    lines.append("")
    lines.append("## 详情")
    lines.append("")
    for row in scan["agents"]:
        lines.append(f"### {row['name']}（`{row['agent_id']}`）")
        lines.append(f"- 已安装：{'是' if row['installed'] else '否'}")
        lines.append(f"- 状态：{row['status']}")
        if row.get("source_path"):
            lines.append(f"- 配置文件：`{row['source_path']}`")
        if row.get("base_url"):
            lines.append(
                f"- Base URL：`{row['base_url']}`　模型：`{row.get('model') or '（未提取到）'}`　"
                f"Key：`{row.get('api_key_masked', '')}`"
            )
        probe = row.get("probe")
        if probe:
            ping = probe.get("ping") or {}
            lines.append(f"- 连通性测试：{'通过' if ping.get('ok') else '失败'}")
            tc = probe.get("tool_calling")
            if tc:
                lines.append(f"- 工具调用测试：{tc.get('note') or ('通过' if tc.get('ok') else '未通过')}")
            if not probe.get("ok") and probe.get("error"):
                lines.append(f"- 错误信息：{probe['error']}")
        fidelity = row.get("protocol_fidelity")
        if fidelity and not fidelity.get("skipped"):
            if fidelity.get("ok"):
                lines.append("- 协议保真检查：通过（复杂多轮 + 工具调用请求转换后未丢字段）")
            else:
                missing = fidelity.get("missing") or fidelity.get("error")
                lines.append(f"- 协议保真检查：未通过，缺失/异常：{missing}")
        lines.append("")
    return "\n".join(lines)


def save_report(markdown: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"scan-{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
