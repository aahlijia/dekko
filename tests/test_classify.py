"""Shared path classification: test code vs production code."""

from dekko.classify import is_test_path


def test_is_test_path_is_cached() -> None:
    # Round-12 §1 fix: is_test_path is called ~7.9M times per search
    # on a large repo (all rediscovering the same ~2K distinct paths),
    # so it is lru_cache-wrapped. Build a distinct-but-equal string
    # (not the same object) so a hit can only come from the cache
    # comparing by value, not from Python's small-string interning.
    is_test_path.cache_clear()
    path = "".join(["tests/", "test_cache", "_probe.py"])
    other = "tests/test_cache_probe.py"
    assert path is not other
    assert path == other

    first = is_test_path.cache_info()
    assert is_test_path(path) is True
    assert is_test_path(other) is True

    info = is_test_path.cache_info()
    assert info.hits == first.hits + 1
    assert info.misses == first.misses + 1


def test_plain_test_dir_is_test() -> None:
    assert is_test_path("tests/test_cli.py") is True
    assert is_test_path("src/app.py") is False


def test_multi_level_test_dir_still_matches() -> None:
    # A `tests`/`test` segment anywhere in the path (not just the last
    # directory component) must keep matching — this is the common,
    # explicitly-supported shape the src/main fix must not regress.
    assert is_test_path("apps/foo/tests/unit/test_thing.py") is True


def test_basename_glob_without_test_dir() -> None:
    assert is_test_path("src/app.test.js") is True
    assert is_test_path("src/app_test.go") is True
    assert is_test_path("src/AppTests.java") is True
    assert is_test_path("src/app.js") is False


def test_maven_src_test_java_layout_is_test() -> None:
    # The normal, correct Maven/Gradle case: src/test/... is really a
    # test root and must keep classifying as test code.
    assert (
        is_test_path("core/src/test/java/com/example/WidgetTest.java") is True
    )


def test_maven_src_main_java_package_named_test_is_not_test() -> None:
    # Round-11 §3 regression: a Java *package* segment literally named
    # `test` (org.springframework.boot.test) under src/main/ is
    # production code, not a test directory, even though `test` is one
    # of TEST_DIR_PARTS. This is the exact spring-boot repro path.
    path = (
        "core/spring-boot-test/src/main/java/org/springframework/boot/"
        "test/context/runner/AbstractApplicationContextRunner.java"
    )
    assert is_test_path(path) is False


def test_maven_src_main_java_actual_test_filename_still_matches() -> None:
    # Even under src/main/, a filename that itself matches a test glob
    # (e.g. shipped as a testing-support library's own *Test.java) is
    # still classified as test code — only the mid-path package-name
    # false positive is fixed, not the basename check.
    path = "core/src/main/java/org/example/testing/WidgetTest.java"
    assert is_test_path(path) is True


def test_src_without_main_or_test_segment_falls_through() -> None:
    # A `src/` root that isn't followed by `main`/`test` at all (e.g. a
    # flat src/ layout) falls through to the existing directory-part
    # and basename checks unchanged.
    assert is_test_path("src/lib/testing/helper.py") is True
    assert is_test_path("src/lib/helper.py") is False


def test_repos_without_src_root_are_unaffected() -> None:
    # The overwhelming majority of Python/Go/JS/Rust repos have no
    # src/ root at all — the new branch must never trigger for them.
    assert is_test_path("pkg/handler.go") is False
    assert is_test_path("pkg/handler_test.go") is True
