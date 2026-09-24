"""Canonical callee text: a call's receiver rendered without its arguments.

``RawCall.text``/``receiver`` used to be the receiver's source text
verbatim, so ``expect(result.foo).toBe`` or a thirty-line array
literal's ``.join`` became an unbounded external callee id.
``extractor._canonical_expr`` renders the receiver structurally
instead. The resolver only ever reads the head token, the second
segment of a parameter-rooted chain and the last joiner of that text,
so the invariant tested last is the one that matters: every head
that was an identifier before is byte-identical after.
"""

import re
from pathlib import Path

from dekko.core import languages
from dekko.core.extractor import (
    _RECEIVER_FIELDS,
    _callee_parts,
    _cap_callee,
    _one,
    _run_query,
    _text,
    extract_file,
)
from dekko.core.grammars import get_grammar
from tree_sitter import Parser

# The resolver's head split (``resolver._PATH_SPLIT``).
_PATH_SPLIT = re.compile(r"::|\.|/")


def _calls(tmp_path: Path, filename: str, source: str) -> dict[str, str]:
    """``name -> text`` for every call in ``source``, last one wins."""
    spec = languages.spec_for_path(filename)
    assert spec is not None
    (tmp_path / filename).write_text(source)
    fm = extract_file(tmp_path, filename, spec)
    assert fm.error is None
    return {c.name: c.text for c in fm.calls}


def test_ts_call_chains_drop_arguments(tmp_path: Path) -> None:
    texts = _calls(
        tmp_path,
        "a.ts",
        "expect(result.foo).toBe(1);\n"
        "chalk.hex('#fff').bold('x');\n"
        "new Date(meta.at).getTime();\n"
        "create<T>(x).y();\n",
    )
    assert texts["toBe"] == "expect().toBe"
    assert texts["bold"] == "chalk.hex().bold"
    assert texts["getTime"] == "new Date().getTime"
    # A generic call is a plain call with type arguments to the TS
    # grammar; the head stays ``create``.
    assert texts["y"] == "create().y"


def test_ts_literal_and_wrapper_receivers_become_markers(
    tmp_path: Path,
) -> None:
    texts = _calls(
        tmp_path,
        "a.ts",
        '[1, 2].join("\\n");\n'
        '({ a: 1 }).hasOwnProperty("a");\n'
        '"a,b".split(",");\n'
        "`t${x}`.trim();\n"
        "/^x$/.test(s);\n"
        "arr[0].map(x => x);\n"
        "map.get(k)!.run();\n"
        "a?.b?.c();\n"
        "(await fetch(url)).json();\n",
    )
    assert texts["join"] == "[].join"
    assert texts["hasOwnProperty"] == "(…).hasOwnProperty"
    assert texts["split"] == '"".split'
    assert texts["trim"] == '"".trim'
    assert texts["test"] == "/…/.test"
    assert texts["map"] == "arr[].map"
    assert texts["run"] == "map.get()!.run"
    assert texts["c"] == "a?.b?.c"
    assert texts["json"] == "(…).json"


def test_ts_multiline_chain_with_comment_has_no_residue(
    tmp_path: Path,
) -> None:
    texts = _calls(
        tmp_path,
        "a.ts",
        "export const S = z\n"
        "  // the schema\n"
        "  .object({ a: z.string() })\n"
        "  .strict();\n",
    )
    assert texts["object"] == "z.object"
    assert texts["strict"] == "z.object().strict"


def test_plain_member_chains_are_verbatim(tmp_path: Path) -> None:
    ts = _calls(tmp_path, "a.ts", "this.options.authToken.trim();\n")
    assert ts["trim"] == "this.options.authToken.trim"
    py = _calls(tmp_path, "b.py", "os.path.join(a, b)\nself.x.y()\n")
    assert py["join"] == "os.path.join"
    assert py["y"] == "self.x.y"
    rs = _calls(tmp_path, "c.rs", "fn f() { a::b::c(); self.a.b(); }")
    assert rs["c"] == "a::b::c"
    assert rs["b"] == "self.a.b"
    cpp = _calls(tmp_path, "d.cpp", "void f() { p.q.r(); a->b()->c(); }")
    assert cpp["r"] == "p.q.r"
    assert cpp["c"] == "a->b()->c"


def test_python_receiver_shapes(tmp_path: Path) -> None:
    texts = _calls(
        tmp_path,
        "b.py",
        '", ".join(xs)\nfoo(x)[0].bar()\n(a + b).c()\nd["k"].strip()\n',
    )
    assert texts["join"] == '"".join'
    assert texts["bar"] == "foo()[].bar"
    assert texts["c"] == "(…).c"
    assert texts["strip"] == "d[].strip"


def test_rust_receiver_shapes(tmp_path: Path) -> None:
    texts = _calls(
        tmp_path,
        "c.rs",
        "fn f() {\n"
        "  x.iter().map(|a| a).collect();\n"
        "  locations.get(ix)?.run();\n"
        "  fut.await.poll();\n"
        "  MentionUri::File { a: 1 }.as_str();\n"
        "  Vec::<u8>::new();\n"
        "  resolve::<usize, _>(a, &b).zip();\n"
        "  std::fs::read_to_string(p).unwrap();\n"
        '  format!("x").len();\n'
        "}\n",
    )
    assert texts["collect"] == "x.iter().map().collect"
    assert texts["run"] == "locations.get()?.run"
    assert texts["poll"] == "fut.await.poll"
    assert texts["as_str"] == "MentionUri::File {}.as_str"
    assert texts["len"] == "format!().len"
    assert texts["new"] == "Vec::<u8>::new"
    # A turbofish call keeps ``::`` so the head token stays ``resolve``.
    assert texts["zip"] == "resolve::<>().zip"
    assert texts["unwrap"] == "std::fs::read_to_string().unwrap"


def test_rust_turbofish_call_is_named_after_its_real_callee(
    tmp_path: Path,
) -> None:
    # ``generic_function`` has no name field; the raw-text fallback
    # used to name ``xs.iter().map(f).collect::<Vec<_>>()`` ``iter``.
    texts = _calls(
        tmp_path,
        "t.rs",
        "fn f() {\n"
        "  let v = xs.iter().map(f).collect::<Vec<_>>();\n"
        "  let n = s.parse::<u32>();\n"
        "  resolve::<usize, _>(a);\n"
        "}\n",
    )
    assert texts["collect"] == "xs.iter().map().collect"
    assert texts["parse"] == "s.parse"
    assert texts["resolve"] == "resolve"
    assert "iter" in texts  # the inner ``.iter()`` call, on its own


def test_go_java_receiver_shapes(tmp_path: Path) -> None:
    go = _calls(
        tmp_path, "d.go", "package p\nfunc f() { foo(x).Bar(); T{}.M() }"
    )
    assert go["Bar"] == "foo().Bar"
    assert go["M"] == "T{}.M"
    java = _calls(
        tmp_path,
        "E.java",
        "class E { void f() { new Foo(x).bar(); a().b().c(); } }",
    )
    assert java["bar"] == "new Foo().bar"
    assert java["c"] == "a().b().c"


def test_cpp_template_member_name_elides_its_arguments(
    tmp_path: Path,
) -> None:
    # ``template_method`` names carry the whole argument list as text
    # (2,350 characters on one tensorflow call). A short one is kept:
    # ``v.Get<int>()`` resolves by exact name to a specialization
    # symbol ``Get<int>``. A long list is elided to ``add<>``.
    long_args = ", ".join(f"ConvertOp{i}" for i in range(12))
    texts = _calls(
        tmp_path,
        "t.cc",
        f"void f() {{ patterns->add<{long_args}>(ctx); v.Get<int>(); }}",
    )
    assert texts["add<>"] == "patterns->add<>"
    assert texts["Get<int>"] == "v.Get<int>"


def test_fallback_branch_is_untouched(tmp_path: Path) -> None:
    # A callee with no name field (calling the result of a call) still
    # goes through ``_split_callee_text`` on its raw text.
    spec = languages.spec_for_path("a.ts")
    assert spec is not None
    (tmp_path / "a.ts").write_text("make(x)();\n")
    fm = extract_file(tmp_path, "a.ts", spec)
    assert sorted(c.text for c in fm.calls) == ["make", "make(x)"]


def test_cap_keeps_head_joiner_and_name() -> None:
    receiver = "server." + ".".join(f"add_handler{i}()" for i in range(60))
    text = receiver + ".finish"
    capped_text, capped_receiver = _cap_callee(text, "finish", receiver)
    # The head keeps its separator: ``server.…`` splits to ``server``
    # for the resolver, ``server…`` would not.
    assert capped_text == "server.….finish"
    assert capped_receiver == "server.…"
    assert len(capped_text) < 200
    assert _cap_callee("a.b.c", "c", "a.b") == ("a.b.c", "a.b")
    scoped = "ListItem::" + "::".join(f"h{i}()" for i in range(60))
    assert _cap_callee(scoped + ".on_click", "on_click", scoped)[1] == (
        "ListItem::…"
    )


def test_unknown_shapes_keep_an_identifier_head(tmp_path: Path) -> None:
    java = _calls(
        tmp_path,
        "F.java",
        "class F { void f() { ObjectNode.class.isAssignableFrom(t); } }",
    )
    assert java["isAssignableFrom"] == "ObjectNode.class.isAssignableFrom"
    go = _calls(tmp_path, "g.go", "package p\nfunc f() { x.(T).M() }")
    assert go["M"].startswith("x.")
    rs = _calls(tmp_path, "h.rs", "fn f() { (a..b).len(); true.then(g); }")
    assert rs["len"] == "(…).len"
    assert rs["then"] == "true.then"


def _resolver_head(receiver: str) -> str:
    return _PATH_SPLIT.split(receiver)[0]


def test_identifier_heads_are_byte_identical_to_verbatim(
    tmp_path: Path,
) -> None:
    # The invariant the resolver relies on: whenever the verbatim
    # receiver's head token was a plain identifier (an import binding,
    # ``this``, a parameter name), the canonical receiver has the same
    # head; a head that was already garbage may become other garbage.
    sources = {
        "a.ts": (
            "chalk.red('x').bold();\n"
            "this.items.filter(f).map(g);\n"
            "input.client.getSchedule(id).run();\n"
            "expect(result.foo).toBe(1);\n"
            "arr[0].map(x => x);\n"
            "z\n  .object({}).strict();\n"
        ),
        "c.rs": (
            "fn f() { std::fs::read_to_string(p).unwrap();"
            " gpui::px(1.0).floor(); resolve::<usize, _>(a).zip();"
            " self.state.lock().unwrap(); }"
        ),
        "b.py": (
            "subprocess.run(c, check=True).stdout.strip()\nself.a[0].b()\n"
        ),
    }
    checked = 0
    for filename, source in sources.items():
        spec = languages.spec_for_path(filename)
        assert spec is not None
        (tmp_path / filename).write_text(source)
        root = Parser(get_grammar(spec.grammar)).parse(
            (tmp_path / filename).read_bytes()
        )
        matches = _run_query(spec.grammar, spec.call_query, root.root_node)
        for _, caps in matches:
            callee = _one(caps, "callee")
            if callee is None:
                continue
            _, name, canonical = _callee_parts(callee)
            verbatim = None
            for field_name in _RECEIVER_FIELDS:
                recv_node = callee.child_by_field_name(field_name)
                if recv_node is not None:
                    verbatim = _text(recv_node)
                    break
            if verbatim is None or canonical is None:
                continue
            old_head = _resolver_head(verbatim)
            if old_head.isidentifier():
                assert _resolver_head(canonical) == old_head, (name, verbatim)
                checked += 1
    assert checked >= 10
