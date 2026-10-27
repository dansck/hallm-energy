# Hallm Energy

Home Assistant custom integration that owns grid-charging "grants" for a home
battery system: a single authority for deciding when and how much the battery
may charge from the grid, with its own persistent local state (so a restart
or a crash can't silently re-authorize energy that was already spent).

## Status: early

In place:

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
  enabling sequence;
- a control loop every 30 s, with inputs taken from Home Assistant states:
  battery SOC and charge counter (unknown/unavailable = invalid, so the
  device integration must not keep a stale value), slot starts read from the
  inverter, and off-peak only when the measured tariff relay AND a current
  tariff schedule both say so.

Nothing creates grants yet; the loop keeps the stored state reconciled and,
in live mode, turns grid charging off whenever no grant allows it.

Not implemented yet: grant creation, restoring normal floors after a grant,
and the history outbox.

## Configuration

Settings → Devices & services → Add integration → Hallm Energy, then map the
entities (all of them can be changed later in the integration's options):

| Role | Example |
| --- | --- |
| Battery SOC, charge counter (total DC charge, kWh) | sensors |
| Off-peak relay, tariff schedule | binary sensor, sensor with `schedule` and `last_update` attributes |
| Grid charge master switch | switch |
| Per slot (6): start (`HHMM`), capacity (%), charge enable | number, number, switch |
| Shadow switch, feature switch | input booleans |

**Writes happen only while the shadow switch is explicitly `off` and the
feature switch explicitly `on`.** Anything else (including unavailable) only
reports the intended writes. Leaving live mode turns grid charging off once.

After mapping, verify the charge counter and call `hallm_energy.provision`
once: from then on the counter is trusted. A changed counter entity or epoch,
or a lost stored state, blocks; `hallm_energy.reprovision` trusts the
configured counter again (only with no grant, step or episode in flight;
recorded in the audit). `hallm_energy.safety_stop`
turns grid charging off immediately (live mode only).

The diagnostic sensor `sensor.hallm_energy_status` shows the overall state,
the stored grant, the last tick's result, input problems, intended writes
and write outcomes.

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
