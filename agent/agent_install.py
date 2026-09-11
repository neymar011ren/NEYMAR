"""目标 Agent 安装：按操作系统给出方案，并执行白名单安装命令。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_profiles import resolve_profile


@dataclass(frozen=True)
class InstallRecipe:
    agent_id: str
    auto_installable: bool
    package_name: str
    docs_url: str
    search_query: str
    # os -> list of shell commands (executed sequentially)
    commands: dict[str, tuple[str, ...]]
    # binaries that indicate install success
    check_bins: tuple[str, ...] = ()
    # paths that indicate presence
    check_paths: tuple[str, ...] = ()
    notes: str = ""
    download_url: str = ""


def detect_os() -> dict[str, str]:
    system = platform.system().lower()  # linux / darwin / windows
    if system == "darwin":
        os_key = "macos"
    elif system.startswith("win"):
        os_key = "windows"
    else:
        os_key = "linux"
    return {
        "os_key": os_key,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "platform": platform.platform(),
    }


INSTALL_RECIPES: dict[str, InstallRecipe] = {
    "claude_code": InstallRecipe(
        agent_id="claude_code",
        auto_installable=True,
        package_name="@anthropic-ai/claude-code",
        docs_url="https://docs.anthropic.com/en/docs/claude-code/overview",
        search_query="Claude Code CLI install npm @anthropic-ai/claude-code",
        commands={
            "linux": ("npm install -g @anthropic-ai/claude-code",),
            "macos": ("npm install -g @anthropic-ai/claude-code",),
            "windows": ("npm install -g @anthropic-ai/claude-code",),
        },
        check_bins=("claude",),
        check_paths=("~/.claude",),
        notes="需要本机已安装 Node.js / npm。",
    ),
    "codex": InstallRecipe(
        agent_id="codex",
        auto_installable=True,
        package_name="@openai/codex",
        docs_url="https://github.com/openai/codex",
        search_query="OpenAI Codex CLI install npm @openai/codex",
        commands={
            "linux": ("npm install -g @openai/codex",),
            "macos": ("npm install -g @openai/codex",),
            "windows": ("npm install -g @openai/codex",),
        },
        check_bins=("codex",),
        check_paths=("~/.codex",),
        notes="需要 Node.js / npm。",
    ),
    "aider": InstallRecipe(
        agent_id="aider",
        auto_installable=True,
        package_name="aider-chat",
        docs_url="https://aider.chat/docs/install.html",
        search_query="aider-chat pip install",
        commands={
            "linux": ("pip install --user aider-chat",),
            "macos": ("pip install --user aider-chat",),
            "windows": ("pip install --user aider-chat",),
        },
        check_bins=("aider",),
        check_paths=("~/.aider.conf.yml",),
        notes="使用 pip 安装到用户目录。",
    ),
    "opencode": InstallRecipe(
        agent_id="opencode",
        auto_installable=True,
        package_name="opencode",
        docs_url="https://opencode.ai/docs",
        search_query="opencode.ai install curl linux macos",
        commands={
            "linux": ("curl -fsSL https://opencode.ai/install | bash",),
            "macos": ("curl -fsSL https://opencode.ai/install | bash",),
            "windows": (),  # 无自动命令时走手动
        },
        check_bins=("opencode",),
        check_paths=("~/.config/opencode",),
        notes="官方一键脚本；Windows 请按文档手动安装。",
        download_url="https://opencode.ai",
    ),
    "continue": InstallRecipe(
        agent_id="continue",
        auto_installable=True,
        package_name="Continue (VS Code)",
        docs_url="https://docs.continue.dev/getting-started/install",
        search_query="Continue.dev VS Code extension install",
        commands={
            "linux": ("code --install-extension Continue.continue",),
            "macos": ("code --install-extension Continue.continue",),
            "windows": ("code --install-extension Continue.continue",),
        },
        check_bins=(),
        check_paths=("~/.continue",),
        notes="需要已安装 VS Code，且 `code` 命令可用。",
    ),
    "cline": InstallRecipe(
        agent_id="cline",
        auto_installable=True,
        package_name="Cline (VS Code)",
        docs_url="https://docs.cline.bot/getting-started/installing-cline",
        search_query="Cline VS Code extension saoudrizwan.claude-dev install",
        commands={
            "linux": ("code --install-extension saoudrizwan.claude-dev",),
            "macos": ("code --install-extension saoudrizwan.claude-dev",),
            "windows": ("code --install-extension saoudrizwan.claude-dev",),
        },
        check_paths=("~/.cline",),
        notes="需要已安装 VS Code，且 `code` 命令可用。",
    ),
    "workbuddy": InstallRecipe(
        agent_id="workbuddy",
        auto_installable=False,
        package_name="WorkBuddy Desktop",
        docs_url="https://workbuddy.tencent.com",
        search_query="WorkBuddy 腾讯 下载 安装 Windows macOS",
        commands={
            "linux": (),
            "macos": (),
            "windows": (),
        },
        check_paths=("~/.workbuddy",),
        notes="WorkBuddy 主要为桌面客户端；请从官网下载安装。安装后通常会出现 ~/.workbuddy。",
        download_url="https://workbuddy.tencent.com",
    ),
    "kingswitch": InstallRecipe(
        agent_id="kingswitch",
        auto_installable=False,
        package_name="KingSwitch",
        docs_url="local",
        search_query="",
        commands={"linux": (), "macos": (), "windows": ()},
        check_paths=(),
        notes="KingSwitch 已在运行，无需安装。",
    ),
}


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def check_agent_installed(agent_id: str) -> dict[str, Any]:
    profile = resolve_profile(agent_id)
    recipe = INSTALL_RECIPES.get((profile.id if profile else agent_id) or "")
    os_info = detect_os()
    bins_found: list[str] = []
    paths_found: list[str] = []
    if recipe:
        for b in recipe.check_bins:
            resolved = shutil.which(b)
            if resolved:
                bins_found.append(resolved)
        for p in recipe.check_paths:
            path = _expand(p)
            if path.exists():
                paths_found.append(str(path))
    # 也看配置文件档案
    config_exists = False
    config_paths: list[str] = []
    if profile:
        from agent_profiles import profile_paths

        for path in profile_paths(profile):
            config_paths.append(str(path))
            if path.exists():
                config_exists = True
                if str(path) not in paths_found:
                    paths_found.append(str(path))

    installed = bool(bins_found or paths_found or config_exists)
    return {
        "ok": True,
        "agent_id": profile.id if profile else agent_id,
        "name": profile.name if profile else agent_id,
        "installed": installed,
        "os": os_info,
        "bins_found": bins_found,
        "paths_found": paths_found,
        "config_paths": config_paths,
        "auto_installable": bool(recipe.auto_installable) if recipe else False,
        "package_name": recipe.package_name if recipe else "",
        "docs_url": recipe.docs_url if recipe else (profile.docs_hint if profile else ""),
        "search_query": recipe.search_query if recipe else f"{agent_id} install",
    }


def prepare_agent_install(agent_id: str, search_notes: str = "") -> dict[str, Any]:
    """生成安装方案，供前端弹出安装按钮。search_notes 可填入联网搜索摘要。"""
    profile = resolve_profile(agent_id)
    if not profile:
        return {"ok": False, "error": f"未知 agent_id: {agent_id}"}
    recipe = INSTALL_RECIPES.get(profile.id)
    os_info = detect_os()
    os_key = os_info["os_key"]
    status = check_agent_installed(profile.id)
    if status.get("installed"):
        return {
            "ok": True,
            "already_installed": True,
            "agent_id": profile.id,
            "name": profile.name,
            "message": f"{profile.name} 似乎已安装，可直接进行配置探测。",
            "status": status,
            "offer_install": False,
        }

    commands = list((recipe.commands.get(os_key) if recipe else ()) or ())
    auto = bool(recipe and recipe.auto_installable and commands)
    offer = {
        "ok": True,
        "already_installed": False,
        "offer_install": True,
        "agent_id": profile.id,
        "name": profile.name,
        "os": os_info,
        "package_name": recipe.package_name if recipe else profile.name,
        "auto_installable": auto,
        "commands": commands,
        "docs_url": (recipe.docs_url if recipe else profile.docs_hint) or "",
        "download_url": (recipe.download_url if recipe else "") or "",
        "search_query": (recipe.search_query if recipe else f"{profile.name} install {os_key}"),
        "search_notes": (search_notes or "").strip()[:2000],
        "notes": (recipe.notes if recipe else "请按官方文档手动安装。") or "",
        "button_label": f"安装 {profile.name}",
        "manual_hint": (
            None
            if auto
            else "当前系统不支持一键安装（或缺少自动脚本）。请打开下载/文档链接手动安装，完成后告诉我「已安装」。"
        ),
    }
    return offer


def _shell_for_os() -> str:
    if platform.system().lower().startswith("win"):
        return "cmd"
    return "bash"


def run_agent_install(agent_id: str) -> Iterator[dict[str, Any]]:
    """执行白名单安装命令，产出进度事件。"""
    profile = resolve_profile(agent_id)
    if not profile:
        yield {"type": "error", "message": f"未知 agent_id: {agent_id}"}
        return
    recipe = INSTALL_RECIPES.get(profile.id)
    if not recipe:
        yield {"type": "error", "message": f"{profile.name} 暂无安装配方"}
        return
    os_info = detect_os()
    os_key = os_info["os_key"]
    commands = list(recipe.commands.get(os_key) or ())
    if not recipe.auto_installable or not commands:
        yield {
            "type": "error",
            "message": (
                f"{profile.name} 在 {os_key} 上不支持一键安装。"
                f"请访问 {recipe.download_url or recipe.docs_url} 手动安装。"
            ),
            "docs_url": recipe.docs_url,
            "download_url": recipe.download_url,
        }
        return

    # 依赖预检查
    need_npm = any(cmd.strip().startswith("npm ") for cmd in commands)
    need_code = any("code --install-extension" in cmd for cmd in commands)
    need_pip = any(cmd.strip().startswith("pip ") for cmd in commands)
    need_curl = any(cmd.strip().startswith("curl ") for cmd in commands)
    if need_npm and not shutil.which("npm"):
        yield {"type": "error", "message": "未检测到 npm，请先安装 Node.js"}
        return
    if need_code and not shutil.which("code"):
        yield {"type": "error", "message": "未检测到 VS Code 的 code 命令，请先安装并启用 shell 命令"}
        return
    if need_pip and not (shutil.which("pip") or shutil.which("pip3")):
        yield {"type": "error", "message": "未检测到 pip/pip3"}
        return
    if need_curl and not shutil.which("curl"):
        yield {"type": "error", "message": "未检测到 curl"}
        return

    yield {
        "type": "status",
        "message": f"开始安装 {profile.name}（{os_key}）…",
        "commands": commands,
    }

    env = os.environ.copy()
    # npm 全局目录若无权限，尽量装到用户前缀
    if need_npm and not env.get("npm_config_prefix"):
        user_npm = Path.home() / ".npm-global"
        try:
            user_npm.mkdir(parents=True, exist_ok=True)
            env["npm_config_prefix"] = str(user_npm)
            env["PATH"] = f"{user_npm / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        except Exception:
            pass

    for idx, cmd in enumerate(commands, start=1):
        # 统一 pip -> pip3 回退
        run_cmd = cmd
        if cmd.startswith("pip ") and not shutil.which("pip") and shutil.which("pip3"):
            run_cmd = "pip3 " + cmd[4:]

        yield {"type": "command", "index": idx, "command": run_cmd}
        try:
            proc = subprocess.Popen(
                run_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=str(Path.home()),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                text = line.rstrip("\n")
                if text.strip():
                    yield {"type": "log", "line": text[:1000]}
            code = proc.wait(timeout=600)
            if code != 0:
                yield {"type": "error", "message": f"命令失败（exit={code}）：{run_cmd}"}
                return
            yield {"type": "command_done", "index": idx, "exit_code": code}
        except subprocess.TimeoutExpired:
            yield {"type": "error", "message": f"安装超时：{run_cmd}"}
            return
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"执行失败：{exc}"}
            return

    status = check_agent_installed(profile.id)
    yield {
        "type": "done",
        "ok": True,
        "agent_id": profile.id,
        "installed": status.get("installed"),
        "status": status,
        "message": (
            f"{profile.name} 安装流程已完成。"
            + (" 检测显示已就绪。" if status.get("installed") else " 若检测仍未就绪，请重开终端或按文档排查 PATH。")
        ),
    }


def tool_check_agent_installed(agent_id: str) -> str:
    return json.dumps(check_agent_installed(agent_id), ensure_ascii=False, indent=2)


def tool_prepare_agent_install(agent_id: str, search_notes: str = "") -> str:
    return json.dumps(prepare_agent_install(agent_id, search_notes=search_notes), ensure_ascii=False, indent=2)
