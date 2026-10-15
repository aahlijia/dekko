"""Environment + install-state diagnostic: composition only, no new
extraction. Reuses ``mapfile``'s freshness signal, ``hooks``'s
``.claude/settings.json`` / ``.codex/hooks.json`` shape, and
``policy_md``'s marker string.

Three independent opt-in installers exist (``dekko install claude``,
``dekko hooks install``, ``dekko install claude-md``, plus a separate
``dekko install claude --mcp-only``) and nothing answers "what's
actually wired up in this session" — only ``dekko status`` answers
"is the map fresh." Meanwhile the single most-repeated friction point
is PATH shadowing: a stale
globally-installed ``dekko`` binary resolving ahead of the one a
project actually wants, silently producing wrong/empty answers with no
"command not found"-style error to flag it.

``dekko doctor`` (and the thin ``/dekko:doctor`` slash command that
relays it) is a single, real, standalone CLI subcommand that answers
both questions at once: is the running binary the one you think it is,
and which of dekko's opt-in layers are actually installed.

Every check below is independent and fails to a ``"unknown"`` finding
on its own error rather than aborting the rest — one broken check (a
missing ``claude``/``pgrep`` binary, a malformed settings file) must
never take down the whole report, matching the fail-silent ethos
``hooks.dispatch()`` already enforces elsewhere. ``doctor`` never
auto-fixes anything; it only reports and names the command that would.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dekko import selfcheck
from dekko.analysis import ambiguous
from dekko.integrations import policy_md as policy_md_mod
from dekko.integrations import hooks as hooks_mod
from dekko.render import mapfile

EXIT_OK = 0

# Best-effort subprocess timeouts: doctor must stay fast even when a
# shelled-out CLI hangs or a process listing is slow.
_SUBPROCESS_TIMEOUT = 10
_PGREP_TIMEOUT = 5


@dataclass
class Finding:
    """One diagnostic row.

    Attributes:
        name: Short machine-stable identifier for the check, e.g.
            ``"binary-resolution"`` or ``"hook:pre-bash"``.
        status: One of ``"ok"``, ``"missing"``, ``"stale"``,
            ``"unknown"``, or ``"advisory"``. ``"unknown"`` covers
            both "couldn't check" and "found but can't be verified
            further"; ``"advisory"`` is a check that found nothing
            broken but has information worth a glance (e.g. a high
            ambiguous-call rate).
        detail: One-line human-readable explanation.
        fix: The exact command to run to fix a ``"missing"``/``"stale"``/
            ``"advisory"`` finding, or ``None`` (always ``None`` on
            ``"ok"``).
    """

    name: str
    status: str
    detail: str
    fix: str | None = None


def _running_binary() -> Path:
    """Best-effort path to the currently running ``dekko`` entrypoint."""
    return Path(sys.argv[0]).resolve()


def _running_version() -> str:
    """The ``dekko`` version this process imported (``selfcheck``)."""
    return selfcheck.loaded_version()


def _which_version(which_path: str) -> str | None:
    """Best-effort ``<which_path> --version`` read, or ``None``."""
    try:
        result = subprocess.run(
            [which_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # `dekko --version` prints "dekko X.Y.Z" via argparse's built-in
    # version action.
    text = result.stdout.strip()
    return text.rsplit(" ", 1)[-1] if text else None


def _check_binary_resolution() -> Finding:
    """PATH-shadowing check: what `dekko` resolves to vs. what's running.

    Automates the manual "check `command -v dekko`, not just the
    version string" step.
    """
    running = _running_binary()
    running_version = _running_version()
    which_raw = shutil.which("dekko")
    if which_raw is None:
        return Finding(
            "binary-resolution",
            "missing",
            f"'dekko' not found on PATH; this process is running "
            f"{running} (dekko {running_version})",
            "add dekko's install directory to PATH, or continue "
            "invoking the absolute path shown above",
        )
    which_resolved = Path(which_raw).resolve()
    if which_resolved == running:
        return Finding(
            "binary-resolution",
            "ok",
            f"PATH resolves to the running binary: {which_resolved} "
            f"(dekko {running_version})",
            None,
        )
    which_version = _which_version(which_raw) or "unknown"
    return Finding(
        "binary-resolution",
        "stale",
        f"PATH resolves to {which_resolved} (dekko {which_version}), "
        f"but this process is running {running} (dekko "
        f"{running_version}) — a different install may be shadowing "
        "the one you intend",
        "reinstall the intended dekko so it resolves first on PATH "
        "(e.g. `uv tool install --reinstall dekko`), reorder PATH, or "
        "invoke the absolute path directly to bypass shadowing",
    )


def _check_map_freshness(root: Path) -> tuple[Finding, dict | None]:
    """Reuse ``status``'s exact freshness signal, folded into a Finding.

    Same two calls ``run_status`` (``cli.py``) already makes —
    ``load_provenance`` first (the cheap sidecar-only path), falling
    back to a full ``load_map`` for maps written before the sidecar
    existed. No new freshness logic.

    Returns:
        A ``(finding, prov)`` pair — ``prov`` is whichever provenance
        dict this check resolved (or ``None`` when no usable map/
        provenance exists at all), so ``collect()`` can thread it
        into ``_check_ambiguous_rate`` without a second load.
    """
    prov = mapfile.load_provenance(root)
    if prov is not None:
        fresh = mapfile.check_freshness_provenance(root, prov)
    else:
        index = mapfile.load_map(root)
        if index is None:
            return (
                Finding(
                    "map-freshness",
                    "missing",
                    f"no map.json under {root}",
                    "dekko map",
                ),
                None,
            )
        fresh = mapfile.check_freshness(root, index)
        prov = index.provenance

    if fresh.fresh:
        commit = ((prov or {}).get("git_commit") or "no git")[:12]
        n = len((prov or {}).get("files", {}))
        return (
            Finding(
                "map-freshness",
                "ok",
                f"map fresh ({n} files, commit {commit})",
                None,
            ),
            prov,
        )

    if fresh.reason == "missing":
        return (
            Finding(
                "map-freshness",
                "missing",
                f"no usable map provenance under {root}",
                "dekko map",
            ),
            prov,
        )
    if fresh.reason == "version":
        # Share the same signal-naming logic
        # status/map_status already use, so a spec_hash-only drift on
        # a long-lived process is named explicitly here too, instead
        # of collapsing to a self-contradictory "built by dekko X,
        # running X" tool_version-only message.
        detail = mapfile.describe_version_stale(fresh)
    else:
        n_changed = len(fresh.added) + len(fresh.removed) + len(fresh.changed)
        detail = f"map stale: {n_changed} file(s) added/changed/removed"
    return Finding("map-freshness", "stale", detail, "dekko map"), prov


def _check_ambiguous_rate(prov: dict | None) -> Finding:
    """Standing high-ambiguous-rate flag, read from the provenance stamp.

    Cheap by construction: ``prov`` is whichever provenance dict
    ``_check_map_freshness`` already resolved, and the rate itself is a
    plain ``dict.get()`` read of the value ``dekko map`` stamped in at
    write time (``mapfile.compute_provenance``) — no full ``map.json``
    parse, keeping ``doctor`` fast even on a tensorflow-scale repo.
    """
    if prov is None or "ambiguous_rate" not in prov:
        return Finding(
            "ambiguous-rate",
            "unknown",
            "no ambiguous-rate provenance (map predates this field, "
            "or no map at all)",
            "dekko map",
        )
    rate = prov["ambiguous_rate"]
    sites = prov.get("ambiguous_sites", 0)
    if rate < ambiguous.HIGH_AMBIGUOUS_RATE:
        return Finding(
            "ambiguous-rate",
            "ok",
            f"ambiguous rate {rate:.0%} ({sites:,} sites) — below the "
            f"{ambiguous.HIGH_AMBIGUOUS_RATE:.0%} standing-flag "
            "threshold",
            None,
        )
    return Finding(
        "ambiguous-rate",
        "advisory",
        f"this repo's call resolution is {rate:.0%} ambiguous "
        f"({sites:,} sites) — treat query callers/workset fan-in "
        "counts as a floor, not exact",
        "dekko ambiguous --by name",
    )


def _claude_exe() -> str | None:
    """Resolve the ``claude`` CLI to its full path, or ``None``.

    A private duplicate of ``cli._claude_exe()`` (never imported back
    from here — ``cli.py`` imports this module, so the reverse would be
    circular): silent on a miss, since a missing ``claude`` CLI is
    reported per-check as "can't check," not printed as a standalone
    warning the way ``cli.claude_install`` does.
    """
    return shutil.which("claude")


def _run_cli(exe: str, *args: str) -> subprocess.CompletedProcess | None:
    """Best-effort ``<exe> <args>`` run; ``None`` on any failure."""
    try:
        return subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _check_cli_registration(
    exe: str | None,
    cli_name: str,
    args: tuple[str, ...],
    name: str,
    judge: Callable[[str, str], Finding],
) -> Finding:
    """Shared body for every "is dekko registered with this client" check.

    Runs ``<cli_name> <args>`` and hands its stdout, with the command
    as typed, to ``judge``. A missing CLI or a failed run is
    ``"unknown"``, never a false ``"missing"``.

    Args:
        exe: The client CLI's resolved path, or ``None`` if not found.
        cli_name: The CLI's name as the user would type it.
        args: Arguments to run it with.
        name: Finding name for the unknown cases.
        judge: Reads ``(stdout, command)`` into the finding.

    Returns:
        The finding.
    """
    if exe is None:
        return Finding(
            name, "unknown", f"can't check ({cli_name} CLI not found)", None
        )
    cmd = " ".join([cli_name, *args])
    result = _run_cli(exe, *args)
    if result is None or result.returncode != 0:
        return Finding(name, "unknown", f"can't check ('{cmd}' failed)", None)

    return judge(result.stdout, cmd)


def _check_claude_registration(
    exe: str | None, args: tuple[str, ...], name: str, fix: str
) -> Finding:
    """Shared body for the ``claude mcp list``/``claude plugin list`` checks.

    Deliberately loose (a substring match on the raw output, not a
    parsed/versioned format): ``claude mcp list``/``claude plugin
    list``'s output shape is not a documented contract, so a strict
    parse would be fragile across Claude Code CLI versions. Fails to
    ``"unknown"`` rather than a false ``"missing"`` whenever the shape
    can't be read at all.
    """

    def judge(stdout: str, cmd: str) -> Finding:
        return _loose_match(stdout, cmd, name, fix)

    return _check_cli_registration(exe, "claude", args, name, judge)


def _loose_match(stdout: str, cmd: str, name: str, fix: str) -> Finding:
    """``ok`` when ``dekko`` appears anywhere in ``stdout``, else missing."""
    if "dekko" in stdout.lower():
        return Finding(name, "ok", f"found in '{cmd}'", None)

    return Finding(name, "missing", f"not found in '{cmd}'", fix)


def _check_mcp_registered(exe: str | None) -> Finding:
    """Whether ``dekko`` shows up in ``claude mcp list``."""
    return _check_claude_registration(
        exe,
        ("mcp", "list"),
        "mcp-registered",
        "dekko install claude --mcp-only (or dekko install claude, "
        "which also registers the plugin's bundled MCP server)",
    )


def _check_plugin_installed(exe: str | None) -> Finding:
    """Whether ``dekko`` shows up in ``claude plugin list``."""
    return _check_claude_registration(
        exe, ("plugin", "list"), "plugin-installed", "dekko install claude"
    )


def _check_mcp_server_running() -> Finding:
    """Best-effort ``pgrep`` for a live ``dekko serve --mcp`` process.

    Can only ever confirm a process exists, never its *loaded* code
    version (no IPC round trip through the MCP protocol itself — out
    of scope) — surfaces the
    existing documented caveat (``docs/claude-code.md``: a running
    server holds its code in memory for its whole lifetime) as an
    active finding instead of leaving it doc-only, but stays
    ``"unknown"`` rather than claiming "confirmed stale."
    """
    if sys.platform == "win32":
        return Finding(
            "mcp-server-running",
            "unknown",
            "can't check (pgrep unavailable on this platform)",
            None,
        )
    try:
        result = subprocess.run(
            ["pgrep", "-af", "dekko serve"],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Finding(
            "mcp-server-running",
            "unknown",
            "can't check (pgrep unavailable)",
            None,
        )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    if result.returncode != 0 or not lines:
        return Finding(
            "mcp-server-running",
            "ok",
            "no dekko serve --mcp process detected",
            None,
        )
    pids = [ln.split(None, 1)[0] for ln in lines]
    return _server_age_finding(pids)


def _pid_start_times(pids: list[str]) -> dict[str, float]:
    """Process start times (epoch seconds) via ``ps``, best-effort.

    Args:
        pids: Process ids to look up.

    Returns:
        Pid to start time, for every pid ``ps`` reported in a
        parseable form. Empty when ``ps`` is unavailable.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,lstart=", "-p", ",".join(pids)],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    starts: dict[str, float] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 6:
            continue
        try:
            started = datetime.strptime(
                " ".join(fields[1:]), "%a %b %d %H:%M:%S %Y"
            )
        except ValueError:
            continue
        starts[fields[0]] = started.timestamp()
    return starts


def _server_age_finding(pids: list[str]) -> Finding:
    """Say which running servers predate the installed dekko code.

    A server can't be asked for its loaded spec (no IPC round trip
    through MCP, out of scope), but it doesn't need to be: a process
    that started *before* the installed code last changed is running
    code that is no longer on disk. Three such servers once rewrote
    maps with an older extractor all day while this row said
    "unknown, can't tell".

    Compares against ``selfcheck.install_mtime`` of the dekko this
    ``doctor`` runs from. A server launched from a *different* install
    (another venv) is judged against the wrong clock; the wording says
    "predates" rather than claiming to have read the server's code.

    Args:
        pids: Pids of the running ``dekko serve`` processes.

    Returns:
        ``stale`` naming the outdated pids, ``ok`` when every server
        started after the install last changed, or the old
        ``unknown`` caveat when start times can't be read.
    """
    starts = _pid_start_times(pids)
    installed_at = selfcheck.install_mtime()
    if installed_at is None or any(pid not in starts for pid in pids):
        return Finding(
            "mcp-server-running",
            "unknown",
            f"dekko MCP server running (pid {', '.join(pids)}); it holds "
            "its code in memory for its whole lifetime — if you upgraded "
            "dekko since it started, this won't be reflected",
            "restart Claude Code",
        )
    outdated = [pid for pid in pids if starts[pid] < installed_at]
    if not outdated:
        return Finding(
            "mcp-server-running",
            "ok",
            f"dekko MCP server running (pid {', '.join(pids)}), started "
            "after the installed dekko code last changed",
            None,
        )

    when = datetime.fromtimestamp(installed_at).strftime("%Y-%m-%d %H:%M")
    return Finding(
        "mcp-server-running",
        "stale",
        f"dekko MCP server pid {', '.join(outdated)} predates the "
        f"installed dekko code (last changed {when}) and is running "
        "outdated code; servers from before 0.43.72 can rewrite "
        "map.json with their older extractor",
        "restart the Claude Code sessions that own those servers "
        "(or reconnect dekko via /mcp)",
    )


def _check_json_backend() -> Finding:
    """Whether ``map.json`` is parsed with ``orjson`` or stdlib ``json``.

    The extra named ``all`` once did not include orjson, so load
    timings were measured on stdlib json without anyone noticing -- the
    difference is only visible in a profile. Advisory: dekko is correct either
    way, just slower.
    """
    if mapfile.orjson is not None:
        return Finding("json-backend", "ok", "orjson", None)

    return Finding(
        "json-backend",
        "advisory",
        "stdlib json (map.json loads ~2x and writes ~6x slower than "
        "with orjson on large repos)",
        "pip install 'dekko[fastjson]' (or reinstall with [all])",
    )


def _check_hooks(root: Path) -> list[Finding]:
    """One row per hook event, reusing ``hooks``'s own install-state read."""
    settings = hooks_mod._load_settings(hooks_mod.settings_path(root))
    hooks_by_event = settings.get("hooks", {})
    if not isinstance(hooks_by_event, dict):
        hooks_by_event = {}
    findings = []
    for event, (claude_event, _matcher) in hooks_mod.EVENTS.items():
        bucket = hooks_by_event.get(claude_event, [])
        if not isinstance(bucket, list):
            bucket = []
        if hooks_mod._already_installed(bucket, event):
            findings.append(
                Finding(f"hook:{event}", "ok", f"{event} hook wired", None)
            )
        else:
            findings.append(
                Finding(
                    f"hook:{event}",
                    "missing",
                    f"{event} hook not installed",
                    f"dekko hooks install --enable {event}",
                )
            )
    return findings


def _check_claude_md(root: Path) -> Finding:
    """Whether the usage-policy marker block is present in CLAUDE.md."""
    path = policy_md_mod.policy_path(root, "claude")
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if policy_md_mod._find_block(text) is not None:
        return Finding(
            "claude-md-policy",
            "ok",
            f"usage-policy block present in {path}",
            None,
        )
    return Finding(
        "claude-md-policy",
        "missing",
        f"no dekko usage-policy block in {path}",
        "dekko install claude-md",
    )


# --- Codex -------------------------------------------------------------

_CODEX_PLUGIN_FIX = "dekko install codex"
_CODEX_MCP_FIX = "dekko install codex (or dekko install codex --mcp-only)"


def _codex_exe() -> str | None:
    """Resolve the ``codex`` CLI to its full path, or ``None``.

    Silent on a miss for the same reason as :func:`_claude_exe`.
    """
    return shutil.which("codex")


def _codex_home() -> Path:
    """Codex's state directory: ``$CODEX_HOME``, else ``~/.codex``."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env)

    return Path.home() / ".codex"


def _codex_present(exe: str | None) -> bool:
    """Whether Codex looks installed, so its rows are worth showing.

    A bare ``~/.codex/`` outlives an uninstalled Codex, so it isn't
    enough on its own: the binary has to resolve, or Codex's plugin
    cache has to exist.
    """
    if exe is not None:
        return True

    return (_codex_home() / "plugins" / "cache").is_dir()


def _named_dekko(items: list) -> dict | None:
    """The first entry in a Codex JSON listing named ``dekko``, if any."""
    for item in items:
        if isinstance(item, dict) and item.get("name") == "dekko":
            return item

    return None


def _judge_codex_plugin(stdout: str, cmd: str) -> Finding:
    """Read ``codex plugin list --json`` into the plugin finding.

    The JSON is ``{"installed": [{"name", "enabled", "version", ...}],
    ...}``. Output that doesn't parse to that shape falls back to the
    loose match the Claude checks use.
    """
    name = "codex-plugin-installed"
    try:
        installed = json.loads(stdout).get("installed")
    except (ValueError, AttributeError):
        installed = None
    if not isinstance(installed, list):
        return _loose_match(stdout, cmd, name, _CODEX_PLUGIN_FIX)
    entry = _named_dekko(installed)
    if entry is None:
        return Finding(
            name, "missing", f"not installed per '{cmd}'", _CODEX_PLUGIN_FIX
        )

    return _codex_plugin_state(entry, name)


def _codex_plugin_state(entry: dict, name: str) -> Finding:
    """Judge an installed dekko plugin entry: enabled, and current."""
    if entry.get("enabled") is False:
        return Finding(
            name,
            "advisory",
            "installed but disabled in Codex",
            "enable the dekko plugin in Codex's plugin settings",
        )
    cached = entry.get("version")
    running = _running_version()
    if isinstance(cached, str) and cached != running:
        # Codex copies the plugin into its cache and only refreshes the
        # copy when the bundled manifest's version changes.
        return Finding(
            name,
            "stale",
            f"Codex has dekko {cached} cached; this dekko is {running}",
            "dekko install codex, then restart Codex",
        )

    return Finding(name, "ok", f"installed ({cached or 'unknown version'})")


# How a server launching dekko names its executable, on any platform.
_DEKKO_EXE_NAMES = frozenset({"dekko", "dekko.exe", "dekko.cmd"})


def _is_dekko_server(transport: object) -> bool:
    """Whether an MCP transport launches ``dekko serve``."""
    if not isinstance(transport, dict):
        return False
    command = transport.get("command")
    args = transport.get("args")
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    exe = re.split(r"[\\/]", command)[-1].lower()

    return exe in _DEKKO_EXE_NAMES and "serve" in args


def _judge_codex_mcp(stdout: str, cmd: str) -> Finding:
    """Read ``codex mcp list --json`` into the MCP finding.

    The JSON is a list of ``{"name", "enabled", "transport": {...}}``,
    and it includes servers a plugin provides, so one check covers both
    install paths.
    """
    name = "codex-mcp-registered"
    try:
        servers = json.loads(stdout)
    except ValueError:
        servers = None
    if not isinstance(servers, list):
        return Finding(name, "unknown", f"can't read '{cmd}' output", None)
    entry = _named_dekko(servers)
    if entry is None:
        return Finding(
            name, "missing", f"not found in '{cmd}'", _CODEX_MCP_FIX
        )
    if entry.get("enabled") is False:
        return Finding(
            name,
            "advisory",
            "registered but disabled in Codex",
            "enable the dekko MCP server in Codex's config",
        )
    if not _is_dekko_server(entry.get("transport")):
        return Finding(
            name,
            "advisory",
            f"a server named 'dekko' in '{cmd}' doesn't run 'dekko serve'",
            _CODEX_MCP_FIX,
        )

    return Finding(name, "ok", f"found in '{cmd}'")


def _check_codex_plugin(exe: str | None) -> Finding:
    """Whether the dekko plugin is installed and current in Codex."""
    return _check_cli_registration(
        exe,
        "codex",
        ("plugin", "list", "--json"),
        "codex-plugin-installed",
        _judge_codex_plugin,
    )


def _check_codex_mcp(exe: str | None) -> Finding:
    """Whether Codex has a ``dekko`` MCP server that runs dekko."""
    return _check_cli_registration(
        exe,
        "codex",
        ("mcp", "list", "--json"),
        "codex-mcp-registered",
        _judge_codex_mcp,
    )


def _agents_md_has_block(root: Path) -> bool:
    """Whether the root ``AGENTS.md`` holds the usage-policy block."""
    path = policy_md_mod.policy_path(root, "agents")
    text = path.read_text(encoding="utf-8") if path.is_file() else ""

    return policy_md_mod._find_block(text) is not None


def _check_agents_md(root: Path) -> Finding:
    """Whether Codex will actually read the policy block in AGENTS.md."""
    name = "agents-md-policy"
    path = policy_md_mod.policy_path(root, "agents")
    if not _agents_md_has_block(root):
        return Finding(
            name,
            "missing",
            f"no dekko usage-policy block in {path}",
            "dekko install agents-md",
        )
    override = policy_md_mod.agents_md_override(root)
    if override is not None:
        return Finding(
            name,
            "advisory",
            f"block present, but Codex reads {override} instead of "
            f"{path} (shadowed)",
            f"move the dekko block into {override.name}, or remove it",
        )
    size = policy_md_mod.agents_md_overflow(root)
    if size is not None:
        return Finding(
            name,
            "advisory",
            f"{path} is {size} bytes, over Codex's "
            f"{policy_md_mod.AGENTS_MD_MAX_BYTES}-byte limit; Codex cuts "
            "the end of the file, where the dekko block is",
            f"trim {path.name} below "
            f"{policy_md_mod.AGENTS_MD_MAX_BYTES} bytes",
        )

    return Finding(name, "ok", f"usage-policy block present in {path}")


def _find_codex_handler(
    hooks_by_event: dict, codex_event: str, event: str
) -> tuple[int, int, dict, dict] | None:
    """Locate dekko's handler for ``event`` in a Codex hooks table.

    Returns:
        ``(group index, handler index, group, handler)``, or ``None``.
    """
    bucket = hooks_by_event.get(codex_event, [])
    if not isinstance(bucket, list):
        return None
    command = f"{hooks_mod._HOOK_COMMAND_PREFIX}{event}"
    for g, group in enumerate(bucket):
        handlers = group.get("hooks", []) if isinstance(group, dict) else []
        for h, handler in enumerate(handlers):
            cmd = handler.get("command") if isinstance(handler, dict) else None
            if isinstance(cmd, str) and (
                cmd == command or cmd.startswith(f"{command} ")
            ):
                return g, h, group, handler

    return None


def _codex_entry_matches(
    event: str, matcher: str | None, group: dict, handler: dict
) -> bool:
    """Whether an installed Codex entry is exactly what this dekko writes.

    Codex's trust hash covers the matcher and the whole handler, so any
    difference means Codex treats the hook as modified.
    """
    strict = "--strict" in handler.get("command", "").split()
    expected = hooks_mod._entry(event, matcher, strict=strict, agent="codex")

    return (
        group.get("matcher") == expected.get("matcher")
        and handler == expected["hooks"][0]
    )


def _codex_trust_keys(
    path: Path, codex_event: str, g: int, h: int
) -> set[str]:
    """The ``[hooks.state]`` keys Codex may file this handler under.

    Codex keys trust by ``<hooks.json path>:<event>:<group>:<handler>``
    with the event in snake case. The path is tried both as given and
    resolved, since a symlinked temp or home dir can make them differ.
    """
    label = re.sub(r"(?<!^)(?=[A-Z])", "_", codex_event).lower()

    return {f"{p}:{label}:{g}:{h}" for p in (path.absolute(), path.resolve())}


def _load_codex_hook_state() -> dict | None:
    """``[hooks.state]`` from Codex's user config, or ``None`` if unreadable.

    ``tomllib`` is stdlib only from Python 3.11, so on 3.10 the trust
    state can't be read at all. A missing config means nothing has been
    reviewed yet, which reads as an empty state.
    """
    if sys.version_info < (3, 11):
        return None
    import tomllib

    path = _codex_home() / "config.toml"
    if not path.is_file():
        return {}
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    state = config.get("hooks", {}).get("state", {})

    return state if isinstance(state, dict) else {}


def _codex_trust_finding(
    name: str, event: str, keys: set[str], state: dict | None
) -> Finding:
    """Report whether Codex has a trust record for a wired dekko hook.

    A record proves the user reviewed the hook at some point, not that
    its hash still matches, so the best this can say is "trusted (hash
    not verified)".
    """
    if state is None:
        return Finding(name, "ok", f"{event} hook wired (trust not checked)")
    record = next((state[k] for k in keys if k in state), None)
    if isinstance(record, dict) and record.get("trusted_hash"):
        return Finding(
            name, "ok", f"{event} hook wired, trusted (hash not verified)"
        )

    return Finding(
        name,
        "advisory",
        f"{event} hook wired but not yet reviewed in Codex, so it won't run",
        "start codex in this repo and approve it under 'Hooks need review'",
    )


def _check_codex_hooks(root: Path) -> list[Finding]:
    """One row per Codex hook event, read from ``.codex/hooks.json``."""
    path = hooks_mod.settings_path(root, "codex")
    hooks_by_event = hooks_mod._load_settings(path).get("hooks", {})
    if not isinstance(hooks_by_event, dict):
        hooks_by_event = {}
    state = _load_codex_hook_state()
    findings = []
    for event, (codex_event, matcher) in hooks_mod.events_for("codex").items():
        findings.append(
            _codex_hook_finding(
                path, hooks_by_event, event, codex_event, matcher, state
            )
        )

    return findings


def _codex_hook_finding(
    path: Path,
    hooks_by_event: dict,
    event: str,
    codex_event: str,
    matcher: str | None,
    state: dict | None,
) -> Finding:
    """The row for one Codex hook event."""
    name = f"codex-hook:{event}"
    found = _find_codex_handler(hooks_by_event, codex_event, event)
    if found is None:
        return Finding(
            name,
            "missing",
            f"{event} hook not installed for Codex",
            f"dekko hooks install --agent codex --enable {event}",
        )
    g, h, group, handler = found
    if not _codex_entry_matches(event, matcher, group, handler):
        return Finding(
            name,
            "stale",
            f"{event} entry in {path} differs from what this dekko writes; "
            "Codex treats a changed hook as modified and won't run it "
            "until it's reviewed again",
            "dekko hooks uninstall --agent codex, then dekko hooks install "
            f"--agent codex --enable {event}",
        )
    keys = _codex_trust_keys(path, codex_event, g, h)

    return _codex_trust_finding(name, event, keys, state)


def _collect_codex(root: Path) -> list[Finding]:
    """Codex rows, shown only when Codex looks installed.

    The ``AGENTS.md`` row also shows without Codex when the block is
    already there, since other agents read ``AGENTS.md`` too.
    """
    exe = _codex_exe()
    if not _codex_present(exe):
        if _agents_md_has_block(root):
            return _safe("agents-md-policy", lambda: _check_agents_md(root))
        return []
    findings: list[Finding] = []
    findings += _safe(
        "codex-plugin-installed", lambda: _check_codex_plugin(exe)
    )
    findings += _safe("codex-mcp-registered", lambda: _check_codex_mcp(exe))
    findings += _safe("agents-md-policy", lambda: _check_agents_md(root))
    findings += _safe("codex-hooks", lambda: _check_codex_hooks(root))

    return findings


def _safe(
    name: str, check: Callable[[], "Finding | list[Finding]"]
) -> list[Finding]:
    """Run one check, degrading a raised exception to an unknown Finding.

    One check's failure (a missing binary, a malformed settings file)
    must never abort the rest of the report — matches the fail-silent
    ethos ``hooks.dispatch()`` already enforces elsewhere.

    Args:
        name: Finding name to report if ``check`` raises.
        check: A zero-arg callable returning a ``Finding`` or
            ``list[Finding]``.

    Returns:
        Always a list, even for a single-Finding check.
    """
    try:
        result = check()
    except Exception as exc:
        return [Finding(name, "unknown", f"check failed: {exc}", None)]
    return result if isinstance(result, list) else [result]


def collect(root: Path) -> list[Finding]:
    """Run every check and return the full findings list, in report order.

    Args:
        root: Repository root to diagnose.

    Returns:
        One ``Finding`` per check (hooks contribute one per event).
    """
    exe = _claude_exe()
    findings: list[Finding] = []
    findings += _safe("binary-resolution", _check_binary_resolution)
    try:
        freshness_finding, prov = _check_map_freshness(root)
    except Exception as exc:
        freshness_finding = Finding(
            "map-freshness", "unknown", f"check failed: {exc}", None
        )
        prov = None
    findings.append(freshness_finding)
    findings += _safe("ambiguous-rate", lambda: _check_ambiguous_rate(prov))
    findings += _safe("mcp-registered", lambda: _check_mcp_registered(exe))
    findings += _safe("mcp-server-running", _check_mcp_server_running)
    findings += _safe("plugin-installed", lambda: _check_plugin_installed(exe))
    findings += _safe("hooks", lambda: _check_hooks(root))
    findings += _safe("claude-md-policy", lambda: _check_claude_md(root))
    findings += _safe("codex", lambda: _collect_codex(root))
    findings += _safe("json-backend", _check_json_backend)
    return findings


def _print_text(findings: list[Finding]) -> None:
    """Render findings as a compact table with fix-it lines."""
    print("dekko doctor")
    for f in findings:
        print(f"  [{f.status:7s}] {f.name}: {f.detail}")
        if f.fix:
            print(f"      fix: {f.fix}")


def run(root: Path, as_json: bool = False) -> int:
    """Diagnose environment/install state and print a report.

    Never regenerates the map (mirrors ``status``'s "never regenerates"
    contract) and never auto-fixes anything — reporting only. Always
    exits ``0``: every gap this reports is advisory (an opt-in layer
    simply not installed yet), not an error, matching ``dekko orient``'s
    contract. Callers that already validated ``root`` is a real
    directory (as ``run_doctor`` does before calling this) won't see a
    non-zero exit from here at all.

    Args:
        root: Repository root to diagnose.
        as_json: Emit a JSON array of findings instead of a text table.

    Returns:
        Always ``0``.
    """
    findings = collect(root)
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "name": f.name,
                        "status": f.status,
                        "detail": f.detail,
                        "fix": f.fix,
                    }
                    for f in findings
                ],
                indent=2,
            )
        )
    else:
        _print_text(findings)
    return EXIT_OK
