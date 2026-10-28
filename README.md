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
  tariff schedule both say so;
- recovery below the reserve: an episode starts when SOC drops below the
  reserve while the current slot floor is at least the reserve (a lower
  floor means the battery may deliberately go lower). Waiting for PV is
  decided by a step simulation of the PV production forecast minus the house
  load profile until a deadline anchored at entry; PV is credited only while
  both snapshots (received over MQTT) are current, complete and good quality. Otherwise a grid grant to the
  recovery target is prepared over the next off-peak steps, one per episode;
  an expired window is extended with the same identity and budget;
- after the grant, decided again every cycle: the floors stay at the target
  while tomorrow's corrected PV minus the house load is negative or cannot be
  judged (no repeated discharge and re-buy); otherwise the floors found when
  the grant was prepared are written back — never below the reserve — with
  charging verifiably off, and the episode ends. Losing the reason raises an
  already restored floor back; a floor changed from outside blocks all floor
  writes (diagnosed, not overwritten).

- the floor table: the discharge floor of every slot is worked out from the
  tariff schedule (not from the slot index) — high tariff: reserve + comfort,
  off-peak during the day: that plus what the remaining high-tariff blocks need
  beyond the PV forecast, off-peak after the last high tariff: the reserve —
  with the morning exception and grants overlaid. It is always REPORTED next to
  the actual floors in the status sensor (`floors`). Behind a second activation
  gate (`Manage slot floors`, off by default) and only in live mode the
  integration keeps the floors at the plan: raising always, lowering only
  while the schedule and telemetry are trustworthy, never below the reserve
  without the morning exception, never while a grant, a pending step or
  charging is in play, every write confirmed by readback. With the gate on, the
  plan (not the floors found earlier) is the authority after a grant, and a
  recovery episode no longer waits for a deliberately low floor;
- the morning exception: before sunrise the battery may fall to a lower floor
  when PV is forecast to restore the reserve by the end of useful PV. It is
  behind an activation gate (`Enable morning exception`, off by default), needs
  the grid present, a current profile and forecast covering the whole window,
  and never runs together with a recovery grant. SOC below the reserve inside
  it is not a recovery trigger.

In shadow, grants are created and tracked like in live mode, but nothing is
written to the inverter.

Not implemented yet: planned loads such as an EV with a deadline, and the
history outbox.

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
| PV forecast topic (optional) | retained MQTT topic with the PV production snapshot: times of retrieval and (if the source states it) issue, validity, 15-minute kW with the correction already applied |
| House load profile topic (optional) | retained MQTT topic |
| Grid present relay (optional), enable morning exception | binary sensor on while the grid is present (without it the morning exception never starts); gate, off by default |
| Manage slot floors | gate, off by default: write the planned floors (live mode only) |

Recovery parameters (reserve, target, wait for PV, battery capacity and
efficiency, budget factor, …) have defaults and can be changed
in the options.

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
