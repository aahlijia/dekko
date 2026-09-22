"""Process identity: which dekko code is loaded vs. installed on disk.

A long-lived process (``dekko serve --mcp``, the daemon) keeps whatever
code it imported at startup for its whole lifetime. When dekko is
upgraded underneath it, that process and a freshly started CLI disagree
about what a correct map looks like, and before this module existed
each treated the other's ``map.json`` as stale and rewrote it, forever
(round 33 Track 1: ``.features/fixes/round33/
01-stale-process-map-overwrite.md``). A hash mismatch says "different",
never "older", so neither side could tell it was the outdated one.

This module answers that question by asking the disk. Two halves:

- **Loaded identity**, frozen here at import. ``importlib.metadata.
  version`` reads the installed dist-info *at call time*, so calling it
  later in a long-lived process reports whatever is installed now, not
  what this process is running. ``LOADED_VERSION`` is read once, at
  import, and never again.
- **Installed identity**, asked of a child interpreter
  (``installed_identity``), because this process's own imports can't
  be trusted to reflect the disk.

Only a process that called ``mark_long_lived`` ever consults the
installed identity. A one-shot CLI process loaded its code a moment
ago, is current by construction, and pays nothing.
"""

import subprocess
import sys
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

from dekko.core.languages import spec_fingerprint

# Verdicts ``classify`` can return. Plain strings rather than an Enum so
# they can sit on ``mapfile.Freshness`` and in JSON without conversion.
MAP_OLD = "map-old"
PROCESS_OUTDATED = "process-outdated"
BOTH_OLD = "both-old"
UNPROVEN = "unproven"

_IDENTITY_TIMEOUT = 10.0
_IDENTITY_SNIPPET = (
    "from importlib.metadata import version;"
    "from dekko.core.languages import spec_fingerprint;"
    "print(version('dekko'));"
    "print(spec_fingerprint())"
)


def _read_version() -> str:
    """Installed dekko version right now, or a placeholder."""
    try:
        return _pkg_version("dekko")
    except PackageNotFoundError:
        return "unknown"


# Frozen at import: the version of the code this process is running,
# however long it lives and whatever gets installed underneath it.
LOADED_VERSION: str = _read_version()

_long_lived = False
_identity_memo: tuple[tuple, tuple[str, str] | None] | None = None


def loaded_version() -> str:
    """dekko version this process imported, not what's on disk now."""
    return LOADED_VERSION


@cache
def loaded_spec() -> str:
    """Extractor spec hash of the code this process imported.

    Computed from in-memory spec objects, which never change after
    import, so caching it is exact (and saves rehashing every Tier-1
    query on every freshness check).
    """
    return spec_fingerprint()


def mark_long_lived() -> None:
    """Declare this process long-lived (MCP server, daemon).

    Turns on the installed-identity check. Never called by a one-shot
    CLI process, which is current by construction.
    """
    global _long_lived
    _long_lived = True


def is_long_lived() -> bool:
    """Whether ``mark_long_lived`` was called in this process."""
    return _long_lived


def _stat_sig(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` for ``path``, or ``None`` if unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None

    return (st.st_mtime_ns, st.st_size)


def _dist_info_metadata(package_dir: Path) -> Path | None:
    """The installed dist-info ``METADATA`` next to the package, if any."""
    matches = sorted(package_dir.parent.glob("dekko-*.dist-info/METADATA"))
    return matches[-1] if matches else None


def install_signature() -> tuple:
    """Cheap fingerprint of "has the installed code changed".

    Stats the two files that decide identity: ``core/languages.py``
    (every spec and ``_VERSION`` constant lives there) and the
    dist-info ``METADATA`` (the version). Used as the memo key for
    ``installed_identity`` so a second upgrade mid-lifetime is seen,
    and by ``doctor`` as the "when did the install last change" clock.
    """
    package_dir = Path(__file__).resolve().parent
    languages = package_dir / "core" / "languages.py"
    metadata = _dist_info_metadata(package_dir)
    return (
        _stat_sig(languages),
        _stat_sig(metadata) if metadata is not None else None,
    )


def install_mtime() -> float | None:
    """Most recent mtime among the identity files, in epoch seconds."""
    stamps = [sig[0] for sig in install_signature() if sig is not None]
    if not stamps:
        return None

    return max(stamps) / 1e9


def _ask_child() -> tuple[str, str] | None:
    """Run the identity snippet in a fresh interpreter."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _IDENTITY_SNIPPET],
            capture_output=True,
            text=True,
            timeout=_IDENTITY_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.split()
    if len(lines) != 2:
        return None

    return (lines[0], lines[1])


def installed_identity() -> tuple[str, str] | None:
    """``(version, spec_hash)`` of the dekko code currently on disk.

    Asked of a child interpreter, because this process's own imports
    can't be trusted to reflect the disk. Memoized on
    ``install_signature`` so the ~30 ms child only runs when the
    installed files actually changed. ``stdin`` is detached and output
    captured: inside the MCP server, stdout is the protocol channel.

    Returns:
        The installed identity, or ``None`` when the child fails or
        times out (callers treat that as "can't prove I'm current").
    """
    global _identity_memo
    sig = install_signature()
    if _identity_memo is not None and _identity_memo[0] == sig:
        return _identity_memo[1]
    identity = _ask_child()
    _identity_memo = (sig, identity)
    return identity


def process_outdated() -> bool:
    """Whether this long-lived process is running code no longer on disk.

    ``False`` for any process that never called ``mark_long_lived``. An
    unprovable identity (the child failed) counts as outdated: a
    process that can't show it's current must not author shared state.
    """
    if not _long_lived:
        return False

    return installed_identity() != (LOADED_VERSION, loaded_spec())


def known_outdated() -> bool:
    """``process_outdated``, but never spawns the child interpreter.

    Reads the memo only: ``True`` when an earlier check already proved
    this process outdated and the install hasn't changed since. For
    latency-sensitive paths (``daemon status`` answers from a side
    thread under a short probe timeout, and round 14 was all about
    that probe timing out under CPU contention) where "not known yet"
    is an acceptable answer and a ~30 ms child process is not.
    """
    if not _long_lived or _identity_memo is None:
        return False
    sig, identity = _identity_memo
    if sig != install_signature():
        return False

    return identity != (LOADED_VERSION, loaded_spec())


def classify(built_version: str | None, built_spec: str | None) -> str:
    """Decide who is out of date when a map's identity isn't ours.

    Only meaningful for a long-lived process looking at a map whose
    ``(tool_version, spec_hash)`` differs from its own loaded identity.

    Args:
        built_version: The map's recorded ``tool_version``.
        built_spec: The map's recorded ``spec_hash``.

    Returns:
        ``MAP_OLD`` when the disk agrees with this process (the map is
        the stale party, regenerate as always), ``PROCESS_OUTDATED``
        when the disk agrees with the map (leave it alone),
        ``BOTH_OLD`` when the disk agrees with neither, or
        ``UNPROVEN`` when the installed identity couldn't be read.
    """
    installed = installed_identity()
    if installed is None:
        return UNPROVEN
    if installed == (LOADED_VERSION, loaded_spec()):
        return MAP_OLD
    if installed == (built_version, built_spec):
        return PROCESS_OUTDATED

    return BOTH_OLD


def outdated_note(kind: str = "server") -> str:
    """One-line disclosure for a process that found itself outdated.

    Args:
        kind: What to call this process and its restart action:
            ``"server"`` (MCP) or ``"daemon"``.

    Returns:
        The note text, no leading ``note:`` and no trailing newline.
    """
    installed = installed_identity()
    if installed is None:
        on_disk = "installed code could not be identified"
    else:
        on_disk = f"installed {installed[0]} spec {installed[1][:12]}"
    restart = (
        "restart the dekko MCP server"
        if kind == "server"
        else "run `dekko daemon stop` then `dekko daemon start`"
    )
    return (
        f"this dekko {kind} is running outdated code (loaded "
        f"{LOADED_VERSION} spec {loaded_spec()[:12]}, {on_disk}); "
        "answers come from the on-disk map and any regen is delegated "
        f"to the installed dekko -- {restart} to pick up the upgrade"
    )


def _reset_for_tests() -> None:
    """Clear process-level state between tests."""
    global _long_lived, _identity_memo
    _long_lived = False
    _identity_memo = None
