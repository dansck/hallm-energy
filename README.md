# Hallm Energy

Home Assistant custom integration that owns grid-charging "grants" for a home
battery system: a single authority for deciding when and how much the battery
may charge from the grid, with its own persistent local state (so a restart
or a crash can't silently re-authorize energy that was already spent).

## Status: early

In place, shadow only (nothing writes to any device yet):

- durable local state in Home Assistant's `.storage/` (fsynced atomic writes,
  confirmed by reading back; no 255-character `input_text` limit), validated
  as a whole on every load; a failed write blocks enabling until storage is
  verified again;
- grant lifecycle with an episode ledger: enabling is only proposed and
  becomes active after a confirmed write; stopping never waits;
- startup: first run vs. lost state vs. restart, single installation, and a
  mandatory reconcile against a read-only snapshot before anything enables;
- executor for an inverter with time-of-use slots: prepare (permissions off,
  every floor raised) → enable in the current slot with the master switch
  last → stop with the master switch first; every write confirmed by
  readback, one write arbitration point, and a safety stop that preempts any
  enabling sequence. Writes only when a shadow switch is explicitly off AND a
  feature switch explicitly on — anything else only reports intents.

Not implemented yet: configuration (entity mapping) and the periodic loop,
Home Assistant entities/services for diagnostics, restoring normal floors
after a grant, and the history outbox.

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
