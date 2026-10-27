# Hallm Energy

Home Assistant custom integration that owns grid-charging "grants" for a home
battery system: a single authority for deciding when and how much the battery
may charge from the grid, with its own persistent local state (so a restart
or a crash can't silently re-authorize energy that was already spent).

## Status: early

The installable shape (manifest, HACS metadata, CI/validation) and a real
local persistence layer (`store.py`, a confirmed-write wrapper around Home
Assistant's own `Store` — no 255-character `input_text` limit) are in place
and loaded on setup. The actual grant decision logic, restart reconcile,
safety limits and services are not implemented yet.

## Install

Via [HACS](https://hacs.xyz/): HACS → Custom repositories → add this
repository URL, category "Integration".

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

Validation: `.github/workflows/validate.yml` runs [hassfest](https://github.com/home-assistant/actions)
and the [HACS validation action](https://github.com/hacs/action) on every push.

## License

[MIT](LICENSE)

## Next implementation step

Next: a validated state schema, episode ledger and shared pure core; then restart reconciliation in shadow mode.
The existing Store wrapper does not implement grant accounting or authorize physical writes.
