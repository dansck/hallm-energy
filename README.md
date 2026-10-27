# Hallm Energy

Home Assistant custom integration that owns grid-charging "grants" for a home
battery system: a single authority for deciding when and how much the battery
may charge from the grid, with its own persistent local state (so a restart
or a crash can't silently re-authorize energy that was already spent).

## Status: early

In place, shadow only (nothing writes to any device yet):

- durable local state via Home Assistant's own `Store` (fsynced writes, no
  255-character `input_text` limit), validated as a whole on every load;
- grant lifecycle with an episode ledger: enabling is only proposed and
  becomes active after a confirmed write; stopping never waits;
- startup: first run vs. lost state vs. restart, single installation, and a
  mandatory reconcile against a read-only snapshot before anything enables.

Not implemented yet: Home Assistant entities/services, the device adapter
with real readback, safety limits, and the history outbox.

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
