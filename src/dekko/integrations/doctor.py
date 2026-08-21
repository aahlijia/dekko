"""Environment + install-state diagnostic: composition only, no new
extraction. Reuses ``mapfile``'s freshness signal, ``hooks``'s
``.claude/settings.json`` shape, and ``claude_md``'s marker string.

Three independent opt-in installers exist (``dekko --claude-install``,
``dekko hooks install``, ``dekko --claude-md-install``, plus a separate
``--mcp-install``) and nothing answers "what's actually wired up in
this session" — only ``dekko status`` answers "is the map fresh."
Meanwhile the single most-repeated friction point across eval rounds
(``test-repos/reports/``, rounds 08/17/18) is PATH shadowing: a stale
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
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from dekko.integrations import claude_md as claude_md_mod
from dekko.integrations import hooks as hooks_mod
from dekko.render import mapfile

EXIT_OK = 0

# Statuses a Finding can carry. "unknown" covers both "couldn't check"
# and "found but can't be verified further" (e.g. a live MCP server
# whose loaded version can't be introspected from outside).
_STATUSES = ("ok", "missing", "stale", "unknown")

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
        status: One of ``"ok"``, ``"missing"``, ``"stale"``, or
            ``"unknown"``.
        detail: One-line human-readable explanation.
        fix: The exact command to run to fix a ``"missing"``/``"stale"``
            finding, or ``None`` (always ``None`` on ``"ok"``).
    """

    name: str
    status: str
    detail: str
    fix: str | None = None


def _running_binary() -> Path:
    """Best-effort path to the currently running ``dekko`` entrypoint."""
    return Path(sys.argv[0]).resolve()


def _running_version() -> str:
    """This process's installed ``dekko`` version, or ``"unknown"``."""
    try:
        return _pkg_version("dekko")
    except PackageNotFoundError:
        return "unknown"


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

    The direct automation of ``TESTING-GUIDE.md`` §0's manual "check
    `command -v dekko`, not just the version string" step.
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


def _check_map_freshness(root: Path) -> Finding:
    """Reuse ``status``'s exact freshness signal, folded into a Finding.

    Same two calls ``run_status`` (``cli.py``) already makes —
    ``load_provenance`` first (the cheap sidecar-only path), falling
    back to a full ``load_map`` for maps written before the sidecar
    existed. No new freshness logic.
    """
    prov = mapfile.load_provenance(root)
    if prov is not None:
        fresh = mapfile.check_freshness_provenance(root, prov)
    else:
        index = mapfile.load_map(root)
        if index is None:
            return Finding(
                "map-freshness",
                "missing",
                f"no map.json under {root}",
                "dekko map",
            )
        fresh = mapfile.check_freshness(root, index)
        prov = index.provenance

    if fresh.fresh:
        commit = ((prov or {}).get("git_commit") or "no git")[:12]
        n = len((prov or {}).get("files", {}))
        return Finding(
            "map-freshness",
            "ok",
            f"map fresh ({n} files, commit {commit})",
            None,
        )

    if fresh.reason == "missing":
        return Finding(
            "map-freshness",
            "missing",
            f"no usable map provenance under {root}",
            "dekko map",
        )
    if fresh.reason == "version":
        built = fresh.built_version or "unknown"
        running = _running_version()
        detail = f"map built by dekko {built}, running {running}"
    else:
        n_changed = len(fresh.added) + len(fresh.removed) + len(fresh.changed)
        detail = f"map stale: {n_changed} file(s) added/changed/removed"
    return Finding("map-freshness", "stale", detail, "dekko map")


def _claude_exe() -> str | None:
    """Resolve the ``claude`` CLI to its full path, or ``None``.

    A private duplicate of ``cli._claude_exe()`` (never imported back
    from here — ``cli.py`` imports this module, so the reverse would be
    circular): silent on a miss, since a missing ``claude`` CLI is
    reported per-check as "can't check," not printed as a standalone
    warning the way ``cli.claude_install`` does.
    """
    return shutil.which("claude")


def _run_claude(exe: str, *args: str) -> subprocess.CompletedProcess | None:
    """Best-effort ``claude <args>`` run; ``None`` on any failure."""
    try:
        return subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


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
    if exe is None:
        return Finding(
            name, "unknown", "can't check (claude CLI not found)", None
        )
    result = _run_claude(exe, *args)
    if result is None or result.returncode != 0:
        cmd = " ".join(["claude", *args])
        return Finding(name, "unknown", f"can't check ('{cmd}' failed)", None)
    if "dekko" in result.stdout.lower():
        cmd = " ".join(["claude", *args])
        return Finding(name, "ok", f"found in '{cmd}'", None)
    cmd = " ".join(["claude", *args])
    return Finding(name, "missing", f"not found in '{cmd}'", fix)


def _check_mcp_registered(exe: str | None) -> Finding:
    """Whether ``dekko`` shows up in ``claude mcp list``."""
    return _check_claude_registration(
        exe,
        ("mcp", "list"),
        "mcp-registered",
        "dekko --mcp-install (or dekko --claude-install, which also "
        "registers the plugin's bundled MCP server)",
    )


def _check_plugin_installed(exe: str | None) -> Finding:
    """Whether ``dekko`` shows up in ``claude plugin list``."""
    return _check_claude_registration(
        exe, ("plugin", "list"), "plugin-installed", "dekko --claude-install"
    )


def _check_mcp_server_running() -> Finding:
    """Best-effort ``pgrep`` for a live ``dekko serve --mcp`` process.

    Can only ever confirm a process exists, never its *loaded* code
    version (no IPC round trip through the MCP protocol itself — out
    of scope, see the plan's open-questions section) — surfaces the
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
    return Finding(
        "mcp-server-running",
        "unknown",
        f"dekko MCP server running (pid {', '.join(pids)}); it holds "
        "its code in memory for its whole lifetime — if you upgraded "
        "dekko since it started, this won't be reflected",
        "restart Claude Code",
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
    path = claude_md_mod._claude_md_path(root)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if claude_md_mod._find_block(text) is not None:
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
        "dekko --claude-md-install",
    )


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
    findings += _safe("map-freshness", lambda: _check_map_freshness(root))
    findings += _safe("mcp-registered", lambda: _check_mcp_registered(exe))
    findings += _safe("mcp-server-running", _check_mcp_server_running)
    findings += _safe("plugin-installed", lambda: _check_plugin_installed(exe))
    findings += _safe("hooks", lambda: _check_hooks(root))
    findings += _safe("claude-md-policy", lambda: _check_claude_md(root))
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
