"""Extraction tests for statically-known environment-variable reads.

Design doc: config-constant-value-tracing-design.md — a scoped
detector, not the general config-value-flow feature. Covers every
Tier-1 language's curated call shape, the "default-value argument is
never captured" scope boundary, dynamic-key/f-string rejection, a
module-level read (no enclosing definition), the same key read via
two different call shapes in one file, and a near-miss-named call
(``my_getenv_wrapper``) that must not produce a false-positive match.
"""

from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import extract_file
from dekko.core.model import EnvRead


def _by_key(reads: list[EnvRead]) -> dict[str, list[EnvRead]]:
    out: dict[str, list[EnvRead]] = {}
    for r in reads:
        out.setdefault(r.key, []).append(r)
    return out


# ---------------------------------------------------------------------
# Python


def test_python_os_getenv(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import os\n\nDATABASE_URL = os.getenv('DATABASE_URL')\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert fm.error is None
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "DATABASE_URL"
    assert read.call == "os.getenv"
    assert read.caller_id is None  # module-level read
    assert read.line == 3


def test_python_os_environ_get_ignores_default_arg(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import os\n\n\ndef f():\n    return os.environ.get('PORT', '8080')\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "PORT"
    assert read.call == "os.environ.get"
    assert read.caller_id == "a.py::f"


def test_python_os_environ_subscript(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import os\n\n\ndef f():\n    return os.environ['LOG_LEVEL']\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert len(fm.env_reads) == 1
    assert fm.env_reads[0].key == "LOG_LEVEL"
    assert fm.env_reads[0].call == "os.environ[]"


def test_python_dynamic_key_not_captured(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import os\n\n\ndef f(name):\n    return os.getenv(name)\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert fm.env_reads == []


def test_python_fstring_key_not_captured(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import os\n\n\ndef f(suffix):\n"
        "    return os.getenv(f'APP_{suffix}')\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert fm.env_reads == []


def test_python_near_miss_name_not_captured(tmp_path: Path) -> None:
    # A user-defined function literally named my_getenv_wrapper must
    # not produce a false-positive match — exact-name filtering, not
    # substring, per the design doc's own precision requirement.
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "def my_getenv_wrapper(x):\n    return x\n\n\n"
        "def f():\n    return json.dumps('not_env')\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    assert fm.env_reads == []


def test_python_same_key_two_call_shapes(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.py")
    assert spec is not None
    (tmp_path / "a.py").write_text(
        "import os\n\n\ndef f():\n    return os.getenv('PORT')\n\n\n"
        "def g():\n    return os.environ['PORT']\n"
    )
    fm = extract_file(tmp_path, "a.py", spec)
    by_key = _by_key(fm.env_reads)
    assert len(by_key["PORT"]) == 2
    calls = {r.call for r in by_key["PORT"]}
    assert calls == {"os.getenv", "os.environ[]"}


# ---------------------------------------------------------------------
# JS/TS


def test_js_process_env_dot_access(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function f() {\n    return process.env.PORT;\n}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "PORT"
    assert read.call == "process.env"
    assert read.caller_id == "a.js::f"


def test_js_process_env_bracket_access(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function f() {\n    return process.env['DATABASE_URL'];\n}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    assert len(fm.env_reads) == 1
    assert fm.env_reads[0].key == "DATABASE_URL"
    assert fm.env_reads[0].call == "process.env[]"


def test_js_dynamic_bracket_key_not_captured(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.js")
    assert spec is not None
    (tmp_path / "a.js").write_text(
        "function f(name) {\n    return process.env[name];\n}\n"
    )
    fm = extract_file(tmp_path, "a.js", spec)
    assert fm.env_reads == []


def test_ts_process_env(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text(
        "const port: string | undefined = process.env.PORT;\n"
    )
    fm = extract_file(tmp_path, "a.ts", spec)
    assert len(fm.env_reads) == 1
    assert fm.env_reads[0].key == "PORT"
    assert fm.env_reads[0].call == "process.env"


# ---------------------------------------------------------------------
# Java


def test_java_system_getenv(tmp_path: Path) -> None:
    spec = languages.spec_for_path("A.java")
    assert spec is not None
    (tmp_path / "A.java").write_text(
        "public class A {\n"
        "  void m() {\n"
        '    String db = System.getenv("DATABASE_URL");\n'
        "  }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "A.java", spec)
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "DATABASE_URL"
    assert read.call == "System.getenv"
    assert read.caller_id == "A.java::A.m"


def test_java_dynamic_key_not_captured(tmp_path: Path) -> None:
    spec = languages.spec_for_path("A.java")
    assert spec is not None
    (tmp_path / "A.java").write_text(
        "public class A {\n"
        "  void m(String key) {\n"
        "    String db = System.getenv(key);\n"
        "  }\n"
        "}\n"
    )
    fm = extract_file(tmp_path, "A.java", spec)
    assert fm.env_reads == []


# ---------------------------------------------------------------------
# Rust


def test_rust_std_env_var(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        'fn main() {\n    let db = std::env::var("DATABASE_URL");\n}\n'
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "DATABASE_URL"
    assert read.call == "std::env::var"
    assert read.caller_id == "a.rs::main"


def test_rust_bare_env_var(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        'fn main() {\n    let port = env::var("PORT");\n}\n'
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    assert len(fm.env_reads) == 1
    assert fm.env_reads[0].key == "PORT"
    assert fm.env_reads[0].call == "env::var"


def test_rust_dynamic_key_not_captured(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.rs")
    assert spec is not None
    (tmp_path / "a.rs").write_text(
        'fn main() {\n    let key = "PORT";\n'
        "    let v = std::env::var(key);\n}\n"
    )
    fm = extract_file(tmp_path, "a.rs", spec)
    assert fm.env_reads == []


# ---------------------------------------------------------------------
# Go


def test_go_os_getenv(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.go")
    assert spec is not None
    (tmp_path / "a.go").write_text(
        "package main\n\nfunc main() {\n"
        '\tdb := os.Getenv("DATABASE_URL")\n\t_ = db\n}\n'
    )
    fm = extract_file(tmp_path, "a.go", spec)
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "DATABASE_URL"
    assert read.call == "os.Getenv"
    assert read.caller_id == "a.go::main"


def test_go_os_lookupenv(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.go")
    assert spec is not None
    (tmp_path / "a.go").write_text(
        "package main\n\nfunc main() {\n"
        '\tv, ok := os.LookupEnv("PORT")\n\t_, _ = v, ok\n}\n'
    )
    fm = extract_file(tmp_path, "a.go", spec)
    assert len(fm.env_reads) == 1
    assert fm.env_reads[0].key == "PORT"
    assert fm.env_reads[0].call == "os.LookupEnv"


# ---------------------------------------------------------------------
# C / C++


def test_c_getenv(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.c")
    assert spec is not None
    (tmp_path / "a.c").write_text(
        "int main() {\n"
        '    char *db = getenv("DATABASE_URL");\n'
        "    return 0;\n}\n"
    )
    fm = extract_file(tmp_path, "a.c", spec)
    assert len(fm.env_reads) == 1
    read = fm.env_reads[0]
    assert read.key == "DATABASE_URL"
    assert read.call == "getenv"
    assert read.caller_id == "a.c::main"


def test_c_near_miss_name_not_captured(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.c")
    assert spec is not None
    (tmp_path / "a.c").write_text(
        'int main() {\n    char *x = notgetenv("x");\n    return 0;\n}\n'
    )
    fm = extract_file(tmp_path, "a.c", spec)
    assert fm.env_reads == []


def test_cpp_getenv(tmp_path: Path) -> None:
    spec = languages.spec_for_path("a.cpp")
    assert spec is not None
    (tmp_path / "a.cpp").write_text(
        "int main() {\n"
        '    char *db = getenv("DATABASE_URL");\n'
        "    return 0;\n}\n"
    )
    fm = extract_file(tmp_path, "a.cpp", spec)
    assert len(fm.env_reads) == 1
    assert fm.env_reads[0].key == "DATABASE_URL"
    assert fm.env_reads[0].call == "getenv"
