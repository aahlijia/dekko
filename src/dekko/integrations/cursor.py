"""Cursor install: the local plugin copy and a project's ``mcp.json``.

Cursor has no command that installs one plugin from disk, so dekko
copies its bundle into ``~/.cursor/plugins/local/dekko/``, a folder
Cursor loads plugins from after a window reload. It's a real copy, not
a symlink: Cursor only loads a symlink whose target resolves inside
that folder. The copy goes stale when dekko is upgraded, which
``dekko doctor`` reports.

The copy's MCP entry gets the absolute path of the ``dekko``
executable, because an IDE launched from the Dock or Start menu may
not have the user's shell ``PATH``. The bundle the wheel ships keeps
the bare name.

Cursor also imports Claude Code's installed plugins, so a user with
both installs can get dekko twice; the installer says so.

``--mcp-only`` instead merges just the MCP server into a project's
``.cursor/mcp.json``, with the same idempotent read/merge/write as
:mod:`dekko.integrations.cline`: a malformed file is never reset
without ``force``, since it may hold the user's other servers.
"""

import json
import os
import shutil
import sys
from pathlib import Path

PLUGIN_NAME = "dekko"
# The MCP server's arguments. Cursor expands ``${workspaceFolder}`` in
# ``args``, and without ``--root`` the server would start wherever
# Cursor's own process runs, which in the IDE isn't the project.
MCP_ARGS: tuple[str, ...] = (
    "serve",
    "--mcp",
    "--root",
    "${workspaceFolder}",
)
_MANIFEST = Path(".cursor-plugin") / "plugin.json"
_MCP_FILE = "cursor.mcp.json"


def cursor_home() -> Path:
    """Cursor's per-user config directory, ``~/.cursor``."""
    return Path.home() / ".cursor"


def local_plugin_dir() -> Path:
    """Where ``dekko install cursor`` puts its plugin copy."""
    return cursor_home() / "plugins" / "local" / PLUGIN_NAME


def read_plugin_manifest(plugin_dir: Path) -> dict | None:
    """The parsed Cursor manifest of a plugin directory.

    Args:
        plugin_dir: A plugin's root directory.

    Returns:
        The manifest object, or ``None`` if it's missing, unreadable,
        or not a JSON object.
    """
    try:
        data = json.loads(
            (plugin_dir / _MANIFEST).read_text(encoding="utf-8"),
        )
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def claude_plugin_installed() -> bool:
    """Whether Claude Code lists a dekko plugin as installed.

    Cursor imports Claude Code's installed plugins, so with this and a
    Cursor plugin both present, dekko's skills and MCP server can load
    twice in Cursor.

    Returns:
        ``True`` when ``~/.claude/plugins/installed_plugins.json`` has a
        ``dekko@...`` plugin; ``False`` otherwise or on any read error.
    """
    path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return False

    return any(key.startswith(f"{PLUGIN_NAME}@") for key in plugins)


def _refusal(dest: Path) -> str | None:
    """Why ``dest`` can't be replaced, or ``None`` when it can."""
    if dest.is_symlink():
        return f"{dest} is a symlink"

    if dest.exists():
        manifest = read_plugin_manifest(dest)
        if manifest is None or manifest.get("name") != PLUGIN_NAME:
            return f"{dest} holds a plugin dekko didn't install"

    return None


def _pin_mcp_command(plugin_dir: Path) -> None:
    """Point the copy's MCP entry at the resolved ``dekko`` executable."""
    exe = shutil.which("dekko")
    if exe is None:
        print(
            "dekko: warning: 'dekko' not found on PATH; the plugin's MCP "
            "server will only start if Cursor can find it.",
            file=sys.stderr,
        )
        return

    path = plugin_dir / _MCP_FILE
    config = json.loads(path.read_text(encoding="utf-8"))
    config["mcpServers"][PLUGIN_NAME]["command"] = exe
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _swap_into_place(staged: Path, dest: Path) -> None:
    """Replace ``dest`` with ``staged``, keeping the old copy on failure.

    The old copy is moved aside first and only deleted once the new one
    is in place, so a failed rename never leaves Cursor without a
    working plugin.
    """
    backup = dest.with_name(f".dekko-old-{os.getpid()}")
    if dest.exists():
        dest.rename(backup)

    try:
        staged.rename(dest)
    except OSError:
        if backup.exists():
            backup.rename(dest)
        raise

    shutil.rmtree(backup, ignore_errors=True)


def _print_install_notes(dest: Path) -> None:
    """Tell the user how to load the plugin they just installed."""
    print(f"dekko: Cursor plugin installed in {dest}.")
    print(
        "  Reload the Cursor window (Developer: Reload Window) and "
        "restart any running `agent` session.\n"
        "  On a Teams or Enterprise plan, an admin must allow local "
        "plugin imports."
    )
    if claude_plugin_installed():
        print(
            "  Note: Cursor also imports the dekko Claude Code plugin, "
            "so dekko may load twice. Turn that off in Cursor under "
            "Settings > Agents > Third-Party Imports."
        )


def install_plugin(bundle: Path) -> int:
    """Copy the plugin bundle into Cursor's local plugin folder.

    Replaces dekko's own earlier copy, never anything else. The copy is
    staged in a sibling directory first, so an interrupted install
    leaves the previous copy in place.

    Args:
        bundle: The plugin bundle to copy (the wheel's ``_plugin``).

    Returns:
        Process exit code: ``0`` ok, ``1`` if ``bundle`` has no Cursor
        manifest, ``2`` if the destination isn't dekko's to replace.
    """
    if not (bundle / _MANIFEST).is_file():
        print(
            f"dekko: {bundle} has no {_MANIFEST.as_posix()}; this dekko "
            "build doesn't ship the Cursor plugin.",
            file=sys.stderr,
        )
        return 1

    dest = local_plugin_dir()
    refusal = _refusal(dest)
    if refusal is not None:
        print(
            f"dekko: {refusal}; refusing to replace it. Move it aside "
            "and rerun `dekko install cursor`.",
            file=sys.stderr,
        )
        return 2

    dest.parent.mkdir(parents=True, exist_ok=True)
    staged = dest.with_name(f".dekko-install-{os.getpid()}")
    try:
        shutil.copytree(bundle, staged, symlinks=False)
        _pin_mcp_command(staged)
        _swap_into_place(staged, dest)
    finally:
        shutil.rmtree(staged, ignore_errors=True)

    _print_install_notes(dest)
    return 0


def uninstall_plugin() -> int:
    """Remove dekko's copy from Cursor's local plugin folder.

    Returns:
        Process exit code: ``0`` ok (including nothing to remove), ``2``
        if the directory there isn't dekko's.
    """
    dest = local_plugin_dir()
    if not dest.exists() and not dest.is_symlink():
        print(f"dekko: no Cursor plugin at {dest}; nothing to remove.")
        return 0

    refusal = _refusal(dest)
    if refusal is not None:
        print(f"dekko: {refusal}; leaving it alone.", file=sys.stderr)
        return 2

    shutil.rmtree(dest)
    print(f"dekko: removed {dest}. Reload the Cursor window.")
    return 0


def mcp_config_path(root: Path) -> Path:
    """A project's Cursor MCP config file, ``<root>/.cursor/mcp.json``."""
    return root / ".cursor" / "mcp.json"


def _load_mcp_config(path: Path, *, force: bool) -> dict | None:
    """Best-effort read of a Cursor ``mcp.json``.

    Args:
        path: The config file to read.
        force: Treat malformed JSON as an empty object instead of
            refusing.

    Returns:
        ``{}`` for a missing file, the parsed object, or ``None`` when
        the file isn't a JSON object and ``force`` is ``False``.
    """
    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if force else None

    if not isinstance(data, dict):
        return {} if force else None

    return data


def _malformed_mcp_error(path: Path) -> None:
    """Print the "malformed JSON, use --force" refusal."""
    print(
        f"dekko: {path} is not a valid JSON object; refusing to "
        "overwrite it (it may hold other MCP servers). Fix it, or rerun "
        "with --force to replace it.",
        file=sys.stderr,
    )


def _write_mcp_config(path: Path, config: dict) -> None:
    """Write ``config`` with 2-space indent and a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def install_mcp(root: Path, *, force: bool) -> int:
    """Merge the dekko server into a project's ``.cursor/mcp.json``.

    Rerunning converges on the same entry, so it both installs and
    refreshes. Every other key and server in the file is kept.

    Args:
        root: The project root.
        force: Replace a malformed existing file instead of refusing.

    Returns:
        Process exit code: ``0`` ok, ``2`` on malformed JSON without
        ``force``.
    """
    path = mcp_config_path(root)
    config = _load_mcp_config(path, force=force)
    if config is None:
        _malformed_mcp_error(path)
        return 2

    exe = shutil.which("dekko")
    if exe is None:
        print(
            "dekko: warning: 'dekko' not found on PATH; writing the bare "
            "name, so Cursor must find it on its own PATH.",
            file=sys.stderr,
        )

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = config["mcpServers"] = {}

    servers[PLUGIN_NAME] = {
        "command": exe or "dekko",
        "args": list(MCP_ARGS),
    }
    _write_mcp_config(path, config)
    print(f"dekko: MCP server registered in {path}.")
    print(
        "  Cursor asks you to approve a project MCP server once; in the "
        "`agent` CLI, `agent mcp enable dekko` approves it."
    )
    return 0


def _remove_if_empty(directory: Path) -> None:
    """Delete ``directory`` if it exists and holds nothing."""
    try:
        directory.rmdir()
    except OSError:
        pass


def uninstall_mcp(root: Path, *, force: bool) -> int:
    """Remove the dekko server from a project's ``.cursor/mcp.json``.

    Drops only dekko's entry; an emptied ``mcpServers``, an emptied
    file and then an empty ``.cursor/`` go with it.

    Args:
        root: The project root.
        force: Treat a malformed file as empty instead of refusing.

    Returns:
        Process exit code: ``0`` ok (including nothing to remove),
        ``2`` on malformed JSON without ``force``.
    """
    path = mcp_config_path(root)
    config = _load_mcp_config(path, force=force)
    if config is None:
        _malformed_mcp_error(path)
        return 2

    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or PLUGIN_NAME not in servers:
        print(f"dekko: no dekko entry in {path}; nothing to remove.")
        return 0

    del servers[PLUGIN_NAME]
    if not servers:
        del config["mcpServers"]

    if config:
        _write_mcp_config(path, config)
    else:
        path.unlink()
        _remove_if_empty(path.parent)

    print(f"dekko: removed dekko from {path}.")
    return 0
