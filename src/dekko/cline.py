"""Cline MCP registration: install/uninstall the ``dekko`` server.

Cline (the VS Code extension, and a hypothetical standalone CLI) has no
plugin system and does not read Claude Code's ``.mcp.json`` — it keeps
its own ``cline_mcp_settings.json``, and there is no ``cline mcp
add``-equivalent CLI to shell out to (unlike ``claude mcp add`` in
``cli.py``). This module edits that JSON file directly, following the
same idempotent read/merge/write shape as :mod:`dekko.hooks` uses for
``.claude/settings.json``.

The dekko MCP server itself (``dekko serve --mcp``) needs no changes to
support Cline — it is a plain stdio JSON-RPC 2.0 process with no
client-specific assumptions. Only the installer differs.

Unlike ``hooks.py``, a malformed existing config file is **not**
silently reset to ``{}``: ``cline_mcp_settings.json`` may hold other,
unrelated MCP server entries a user would be upset to lose, so a parse
failure aborts with an error unless ``force=True`` is passed.
"""

import json
import os
import platform
import shutil
import sys
from pathlib import Path

_CLINE_EXTENSION_ID = "saoudrizwan.claude-dev"
_SETTINGS_NAME = "cline_mcp_settings.json"
SCOPES = ("vscode", "global")


def _vscode_global_storage() -> Path:
    """The VS Code ``globalStorage`` root directory for this OS.

    Covers the common Code/Code-Insiders-style layout; VSCodium/Cursor
    or a nonstandard ``--user-data-dir`` are not auto-detected — use
    ``--config`` to point at those directly.
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = (
            Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        )
    else:
        base = Path.home() / ".config"
    return base / "Code" / "User" / "globalStorage"


def default_config_path(scope: str = "vscode") -> Path:
    """Best-guess Cline MCP settings path for this OS and scope.

    Args:
        scope: ``"vscode"`` for the VS Code extension's globalStorage
            path (the common case), or ``"global"`` for a hypothetical
            standalone ``cline`` CLI's ``~/.cline/settings/`` path.

    Returns:
        The guessed path. Not guaranteed correct for every install
        (VSCodium/Cursor, a custom ``--user-data-dir``, or an
        unreleased CLI layout) — pass ``--config`` to override.

    Raises:
        ValueError: If ``scope`` is not one of :data:`SCOPES`.
    """
    if scope == "global":
        return Path.home() / ".cline" / "settings" / _SETTINGS_NAME
    if scope == "vscode":
        return (
            _vscode_global_storage()
            / _CLINE_EXTENSION_ID
            / "settings"
            / _SETTINGS_NAME
        )
    raise ValueError(f"dekko: unknown Cline scope: {scope!r}")


def _scope_root(scope: str) -> Path:
    """The directory whose existence stands in for "Cline is installed".

    ``"global"`` checks ``~/.cline``; ``"vscode"`` checks VS Code's
    ``User/globalStorage`` directory (a level above the per-extension
    subfolder, since a fresh Cline install may not have created its
    own subfolder yet even though VS Code itself is present).
    """
    if scope == "global":
        return Path.home() / ".cline"
    return _vscode_global_storage()


def _scope_plausibly_installed(scope: str) -> bool:
    """Soft check for "does Cline/VS Code seem to exist at all".

    There is no ``shutil.which()``-style check for a VS Code extension,
    so this only rules out the extreme case (no VS Code / no ``.cline``
    directory whatsoever). A ``False`` result is a warning, never a
    hard gate (false negatives — Cline installed somewhere nonstandard
    — are likely).
    """
    return _scope_root(scope).is_dir()


def _load_config(path: Path, *, force: bool) -> dict | None:
    """Best-effort read of the Cline settings file.

    Args:
        path: The config file to read.
        force: If ``True``, treat malformed JSON as an empty object
            instead of aborting.

    Returns:
        ``{}`` if the file is missing, the parsed object if it's valid
        JSON, or ``None`` if it exists but isn't a valid JSON object
        and ``force`` is ``False`` (the caller must abort rather than
        silently clobber unrelated settings).
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {} if force else None
    if not isinstance(data, dict):
        return {} if force else None
    return data


def _dekko_entry(dekko_exe: str) -> dict:
    """The stdio MCP server block Cline expects for ``dekko``."""
    return {
        "type": "stdio",
        "command": dekko_exe,
        "args": ["serve", "--mcp"],
        "cwd": "${workspaceFolder}",
    }


def _resolve_path(config_path: Path | None, scope: str) -> Path:
    """The effective config path: an explicit override or the guess."""
    if config_path is not None:
        return config_path
    return default_config_path(scope)


def _malformed_error(path: Path) -> None:
    """Print the shared "malformed JSON, use --force" error."""
    print(
        f"dekko: {path} exists but is not valid JSON — refusing to "
        "overwrite it (it may hold other MCP servers). Re-run with "
        "--force to reset it, or pass --config to point at a "
        "different file.",
        file=sys.stderr,
    )


def install(
    config_path: Path | None = None,
    scope: str = "vscode",
    *,
    force: bool = False,
) -> int:
    """Merge the dekko entry into Cline's ``mcpServers`` (idempotent).

    Re-running always converges to the same ``mcpServers.dekko`` entry
    (recomputed from the currently resolved ``dekko`` executable), so
    it both installs fresh and refreshes a stale entry. Every other key
    in the file — other MCP servers, other Cline settings — is left
    untouched.

    Args:
        config_path: Explicit path to ``cline_mcp_settings.json``,
            overriding auto-detection.
        scope: ``"vscode"`` or ``"global"``; see
            :func:`default_config_path`. Ignored if ``config_path`` is
            given.
        force: Reset a malformed existing config to ``{}`` instead of
            aborting.

    Returns:
        Process exit code (``0`` ok, ``1`` if ``dekko`` isn't on PATH,
        ``2`` on malformed JSON without ``force``).
    """
    dekko_exe = shutil.which("dekko")
    if dekko_exe is None:
        print(
            "dekko: 'dekko' not found on PATH — install it first so "
            "Cline can launch it (e.g. `uv tool install dekko`).",
            file=sys.stderr,
        )
        return 1

    path = _resolve_path(config_path, scope)
    if config_path is None and not _scope_plausibly_installed(scope):
        print(
            "dekko: warning — Cline/VS Code doesn't appear to be "
            f"installed (no {_scope_root(scope)} found). Proceeding "
            "anyway; pass --config if this path is wrong.",
            file=sys.stderr,
        )

    config = _load_config(path, force=force)
    if config is None:
        _malformed_error(path)
        return 2

    servers = config.setdefault("mcpServers", {})
    servers["dekko"] = _dekko_entry(dekko_exe)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"dekko: MCP server registered in {path}. Restart Cline.")
    return 0


def uninstall(
    config_path: Path | None = None,
    scope: str = "vscode",
    *,
    force: bool = False,
) -> int:
    """Remove only the dekko entry from Cline's ``mcpServers``.

    Tolerates a missing file or a missing ``dekko`` key as success
    (nothing to remove), matching the ``hooks.uninstall`` precedent.

    Args:
        config_path: Explicit path to ``cline_mcp_settings.json``,
            overriding auto-detection.
        scope: ``"vscode"`` or ``"global"``; see
            :func:`default_config_path`. Ignored if ``config_path`` is
            given.
        force: Reset a malformed existing config to ``{}`` instead of
            aborting (there will then be nothing to remove).

    Returns:
        Process exit code (``0`` ok, ``2`` on malformed JSON without
        ``force``).
    """
    path = _resolve_path(config_path, scope)
    config = _load_config(path, force=force)
    if config is None:
        _malformed_error(path)
        return 2

    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or "dekko" not in servers:
        print(f"dekko: no dekko entry in {path}; nothing to remove.")
        return 0

    del servers["dekko"]
    if not servers:
        config.pop("mcpServers", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"dekko: removed dekko from {path}. Restart Cline.")
    return 0
