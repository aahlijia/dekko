# Codex hook output schemas

Copied unchanged from `openai/codex` at commit `6e5f665`
(`codex-rs/hooks/schema/generated/*.command.output.schema.json`).

Codex parses a command hook's stdout against these schemas and rejects
any key they don't list (`additionalProperties: false`): the hook run is
marked failed, its `additionalContext` is dropped, and the tool call
runs anyway. `tests/integrations/test_hooks_codex.py` checks every
output dekko's hooks give Codex against them.

To refresh, copy the same three files from a newer Codex checkout and
update the commit above.
