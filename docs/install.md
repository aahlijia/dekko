# Install

```sh
uv tool install dekko     # or: pip install dekko / pipx install dekko
```

The default install bundles nine Tier-1 languages (Python, Rust, C, C++,
JavaScript, TypeScript/TSX, Go, Java) as offline grammar packages — no
network call at parse time. For ~55 additional languages (parsed
generically), add the extra:

```sh
pip install dekko[all]
```

`dekko search` works out of the box (BM25 lexical scoring, no
dependencies). For its optional embedding-based scorer
(`--scorer embedding` / `search_code`'s `scorer: "embedding"`) — a
deterministic, fully-offline hashing-trick embedding, not a
downloaded model — add:

```sh
pip install dekko[search]
```

Then add the `/map` command + MCP server to Claude Code:

```sh
dekko --claude-install     # restart Claude Code afterward
```

## From a local clone

```sh
git clone https://github.com/aahlijia/dekko.git
cd dekko
./install.sh               # installs the CLI and registers the plugin
```

## Uninstall

```sh
dekko --claude-uninstall   # remove the /map plugin (and its MCP server)
uv tool uninstall dekko    # or: pip uninstall dekko / pipx uninstall dekko
```

Next: [quick start](../README.md#quick-start), or the full
[CLI reference](cli.md).
