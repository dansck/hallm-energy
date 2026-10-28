"""Audit outbox: stamped immutable events, delivery with resend, removal only on ack,
restart, MQTT failure, disabled audit and the capacity gate for NEW grants."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.hallm_energy.audit_outbox import (
    AUDIT_CAPACITY,
    BATCH_SIZE,
    RESEND_AFTER,
    AuditOutbox,
)
from custom_components.hallm_energy.const import STORAGE_KEY
from custom_components.hallm_energy.energy_core.audit import (
    build_ack,
    parse_batch,
)
from custom_components.hallm_energy.lifecycle import set_reconcile
from custom_components.hallm_energy.runtime import EnergyRuntime
from custom_components.hallm_energy.state_schema import PendingAuditEvent
from custom_components.hallm_energy.store import LocalStateStore
from tests.test_lifecycle import T0, empty_state, prepared

TOPIC = "test/audit"


class Bus:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.fail = False

    async def publish(self, topic, payload):
        if self.fail:
            raise ConnectionError("mqtt down")
        self.sent.append((topic, json.loads(payload)))

    def ids(self):
        return [e["event_id"] for _, b in self.sent for e in b["events"]]


async def _runtime(hass, state=None):
    runtime = EnergyRuntime(hass, LocalStateStore(hass, version=1, key=STORAGE_KEY))
    await runtime.async_commit(state or set_reconcile(prepared(), "ready", None))
    return runtime


async def _deliver(box, now):
    await box.async_deliver(asyncio.Lock(), lambda: now)


def _ack(*ids):
    return json.dumps(build_ack(list(ids), T0))


# --- stamped, immutable events -------------------------------------------------------------


async def test_new_events_are_stamped_once_with_what_a_receiver_needs(storage_hass):
    runtime = EnergyRuntime(storage_hass, LocalStateStore(storage_hass, version=1,
                                                         key=STORAGE_KEY))
    runtime.mode_provider = lambda: "shadow"
    await runtime.async_commit(set_reconcile(empty_state(), "ready", None))
    state = prepared(runtime.state)
    await runtime.async_commit(state)
    event = runtime.state.pending_audit[0]
    assert event.created_at == T0
    assert event.payload["kind"] == "grant_prepared"
    assert event.payload["mode"] == "shadow" and event.payload["installation_id"] == "inst-1"
    assert event.payload["policy_version"] == "p1" and "state_revision" in event.payload
    first = dict(event.payload)
    runtime.mode_provider = lambda: "live"  # a later commit must not restamp it
    await runtime.async_commit(replace(runtime.state, state_revision=99))
    assert runtime.state.pending_audit[0].payload == first


async def test_event_content_is_identical_on_every_delivery(storage_hass):
    runtime = await _runtime(storage_hass)
    bus = Bus()
    box = AuditOutbox(runtime, TOPIC, bus.publish)
    await _deliver(box, T0)
    await _deliver(box, T0 + RESEND_AFTER)
    first, second = (parse_batch(b) for _, b in bus.sent)
    assert first.batch_id != second.batch_id and first.sent_at != second.sent_at
    assert [e.canonical_content() for e in first.events] == [
        e.canonical_content() for e in second.events]


# --- delivery, resend, acknowledgement ------------------------------------------------------


async def test_delivery_resend_and_removal_only_on_ack(storage_hass):
    runtime = await _runtime(storage_hass)
    bus = Bus()
    box = AuditOutbox(runtime, TOPIC, bus.publish)
    pending = [e.event_id for e in runtime.state.pending_audit]
    await _deliver(box, T0)
    assert bus.ids() == pending and bus.sent[0][0] == TOPIC
    await _deliver(box, T0 + timedelta(minutes=1))
    assert len(bus.sent) == 1  # not resent too early, and publishing is not an acknowledgement
    assert [e.event_id for e in runtime.state.pending_audit] == pending
    await _deliver(box, T0 + RESEND_AFTER)
    assert bus.ids() == pending * 2  # still unacknowledged: delivered again, same ids
    await box.async_handle_ack(_ack(pending[0], "unknown-id"), T0)
    assert [e.event_id for e in runtime.state.pending_audit] == pending[1:]
    assert box.last_ack_at == T0


async def test_invalid_or_foreign_ack_removes_nothing(storage_hass):
    runtime = await _runtime(storage_hass)
    box = AuditOutbox(runtime, TOPIC, Bus().publish)
    before = runtime.state
    for payload in ("{broken", "[]", json.dumps({"schema_version": 2, "event_ids": ["x"]}),
                    json.dumps({"schema_version": 1, "event_ids": []}), _ack("not-mine")):
        await box.async_handle_ack(payload, T0)
        assert runtime.state == before


async def test_unacknowledged_events_survive_a_restart_and_are_delivered_again(storage_hass):
    runtime = await _runtime(storage_hass)
    pending = [e.event_id for e in runtime.state.pending_audit]
    restarted = EnergyRuntime(storage_hass, LocalStateStore(storage_hass, version=1,
                                                          key=STORAGE_KEY))
    await restarted.async_start(known_installation_id="inst-1", new_installation_id=lambda: "x")
    bus = Bus()
    await _deliver(AuditOutbox(restarted, TOPIC, bus.publish), T0)
    assert bus.ids() == pending


async def test_broker_failure_keeps_the_events_and_retries(storage_hass):
    runtime = await _runtime(storage_hass)
    bus = Bus()
    bus.fail = True
    box = AuditOutbox(runtime, TOPIC, bus.publish)
    await _deliver(box, T0)
    assert "ConnectionError" in box.last_error and runtime.state.pending_audit
    bus.fail = False
    await _deliver(box, T0 + timedelta(seconds=30))  # not "sent": retried at once
    assert bus.ids() and box.last_error is None


async def test_batches_are_bounded(storage_hass):
    runtime = await _runtime(storage_hass, set_reconcile(empty_state(pending_audit=tuple(
        PendingAuditEvent(f"e{i}", {"kind": "x"}, T0) for i in range(BATCH_SIZE * 2 + 3))),
        "ready", None))
    bus = Bus()
    box = AuditOutbox(runtime, TOPIC, bus.publish)
    for _ in range(3):
        await _deliver(box, T0)
    assert [len(b["events"]) for _, b in bus.sent] == [BATCH_SIZE, BATCH_SIZE, 3]


# --- disabled and capacity ------------------------------------------------------------------


async def test_without_an_audit_topic_delivery_is_paused_and_the_backlog_is_kept(storage_hass):
    runtime = await _runtime(storage_hass)
    pending = [e.event_id for e in runtime.state.pending_audit]
    bus = Bus()
    box = AuditOutbox(runtime, None, bus.publish)
    assert box.label == "paused" and not box.full
    await _deliver(box, T0)
    assert bus.sent == [] and [e.event_id for e in runtime.state.pending_audit] == pending
    # a topic set later delivers what was kept
    await _deliver(AuditOutbox(runtime, TOPIC, bus.publish), T0)
    assert bus.ids() == pending


async def test_discarding_the_backlog_is_an_explicit_separate_act(storage_hass):
    runtime = await _runtime(storage_hass)
    box = AuditOutbox(runtime, None, Bus().publish)
    count = len(runtime.state.pending_audit)
    assert count and await box.async_discard() == count
    assert runtime.state.pending_audit == ()
    assert await box.async_discard() == 0  # nothing left


async def test_capacity_states_and_gate(storage_hass):
    def events(n):
        return tuple(PendingAuditEvent(f"e{i}", {"kind": "x"}, T0) for i in range(n))

    runtime = await _runtime(storage_hass, set_reconcile(empty_state(), "ready", None))
    box = AuditOutbox(runtime, TOPIC, Bus().publish)
    for count, label in ((0, "ok"), (int(AUDIT_CAPACITY * 0.8), "warning"),
                         (AUDIT_CAPACITY, "full")):
        await runtime.async_commit(replace(runtime.state, pending_audit=events(count)))
        assert box.label == label and box.full == (label == "full")


def test_the_full_outbox_blocks_only_a_new_grant():
    from tests.test_recovery_flow import _inputs, _plan

    inputs = replace(_inputs(pv=lambda _t: 0.0), audit_full=True)
    state, outcome = _plan(empty_state(), inputs)
    assert outcome.action == "blocked" and outcome.reason == "audit_outbox_full"
    assert state.active_grant is None and state.recovery.reason_code == "audit_outbox_full"
    free, outcome = _plan(empty_state(), replace(inputs, audit_full=False))
    assert outcome.action == "grant_prepared"


# --- in the loop and over MQTT ----------------------------------------------------------------


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_delivery_and_ack_over_mqtt_do_not_block_the_loop(storage_hass, mqtt_mock):
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_mqtt_message,
    )

    from custom_components.hallm_energy.const import DOMAIN
    from tests.test_executor import FakeInverter
    from tests.test_inputs import OPTIONS, set_telemetry

    hass = storage_hass
    hass.states.async_set("input_boolean.shadow", "on")
    hass.states.async_set("input_boolean.grid_recovery", "on")
    FakeInverter(hass)
    set_telemetry(hass, soc="50.0")
    entry = MockConfigEntry(domain=DOMAIN, options={**OPTIONS, "audit_topic": TOPIC})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    controller = entry.runtime_data.controller
    await hass.services.async_call(DOMAIN, "provision", blocking=True)
    await hass.services.async_call(DOMAIN, "reprovision", blocking=True)  # emits an event
    await controller.async_deliver_audit()  # the worker round (also driven by a 30 s interval)
    await hass.async_block_till_done(wait_background_tasks=True)
    published = [c for c in mqtt_mock.async_publish.call_args_list if c.args[0] == TOPIC]
    assert published, "the provisioning event should have been delivered"
    batch = parse_batch(json.loads(published[-1].args[1]))
    event = batch.events[0]
    assert event.payload["kind"] == "reprovisioned" and event.payload["mode"] == "shadow"
    ids = [e.event_id for e in batch.events]
    before = controller.outbox.pending
    async_fire_mqtt_message(hass, f"{TOPIC}/ack", _ack(*ids))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert controller.outbox.pending == before - len(ids)


async def test_a_failing_publisher_never_breaks_the_control_cycle(storage_hass):
    from tests.test_controller import _controller
    from tests.test_executor import FakeInverter
    from tests.test_inputs import NOW, set_telemetry

    FakeInverter(storage_hass)
    set_telemetry(storage_hass, soc="50.0")
    controller = await _controller(storage_hass, set_reconcile(prepared(), "ready", None))
    bus = Bus()
    bus.fail = True
    controller.outbox = AuditOutbox(controller.runtime, TOPIC, bus.publish)
    status = await controller.async_tick(NOW)
    assert status.result in ("cycle", "reconcile:ready") and status.error is None
    await controller.async_deliver_audit()
    assert controller.outbox.last_error is not None and controller.runtime.state.pending_audit


# --- R36-02: delivery never holds up the control loop --------------------------------------


class Hanging:
    """A publisher that waits for ever (or until released)."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, topic, payload):
        self.entered.set()
        await self.release.wait()


async def test_a_hanging_broker_does_not_delay_the_loop_a_stop_or_an_ack(storage_hass):
    from tests.test_controller import _controller
    from tests.test_executor import FakeInverter
    from tests.test_inputs import NOW, set_telemetry

    FakeInverter(storage_hass)
    set_telemetry(storage_hass, soc="50.0")
    controller = await _controller(storage_hass, set_reconcile(prepared(), "ready", None),
                                   live=True)
    pending = [e.event_id for e in controller.runtime.state.pending_audit]
    hanging = Hanging()
    controller.outbox = AuditOutbox(controller.runtime, TOPIC, hanging.publish)
    worker = asyncio.create_task(controller.async_deliver_audit())
    await asyncio.wait_for(hanging.entered.wait(), 2)  # the worker is now inside the publish
    # everything else proceeds: a control tick, the safety stop, an acknowledgement
    status = await asyncio.wait_for(controller.async_tick(NOW), 2)
    assert status.result in ("cycle", "reconcile:ready")
    await asyncio.wait_for(controller.async_safety_stop(), 2)
    await asyncio.wait_for(controller.async_handle_ack(_ack(pending[0])), 2)
    assert pending[0] not in [e.event_id for e in controller.runtime.state.pending_audit]
    assert not worker.done()  # still waiting for the broker
    hanging.release.set()
    await asyncio.wait_for(worker, 2)


async def test_a_publish_timeout_keeps_the_queue_and_retries(storage_hass, monkeypatch):
    from custom_components.hallm_energy import audit_outbox

    monkeypatch.setattr(audit_outbox, "PUBLISH_TIMEOUT_S", 0.05)
    runtime = await _runtime(storage_hass)
    pending = [e.event_id for e in runtime.state.pending_audit]
    hanging = Hanging()
    box = AuditOutbox(runtime, TOPIC, hanging.publish)
    await _deliver(box, T0)
    assert "TimeoutError" in box.last_error
    assert [e.event_id for e in runtime.state.pending_audit] == pending
    good = Bus()
    box._publish = good.publish  # the broker is back: the very next round delivers
    await _deliver(box, T0 + timedelta(seconds=1))
    assert good.ids() == pending and box.last_error is None


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_unloading_cancels_a_delivery_that_is_waiting_for_the_broker(
        storage_hass, mqtt_mock, monkeypatch):
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_time_changed,
    )

    from custom_components.hallm_energy import controller as controller_module
    from custom_components.hallm_energy.const import DOMAIN
    from tests.test_executor import FakeInverter
    from tests.test_inputs import OPTIONS, set_telemetry

    hass = storage_hass
    hass.states.async_set("input_boolean.shadow", "on")
    hass.states.async_set("input_boolean.grid_recovery", "on")
    FakeInverter(hass)
    set_telemetry(hass, soc="50.0")
    hanging = Hanging()
    monkeypatch.setattr(controller_module, "_mqtt_publisher", lambda _hass: hanging.publish)
    entry = MockConfigEntry(domain=DOMAIN, options={**OPTIONS, "audit_topic": TOPIC})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await hass.services.async_call(DOMAIN, "provision", blocking=True)
    await hass.services.async_call(DOMAIN, "reprovision", blocking=True)  # an event to deliver
    from homeassistant.util import dt as dt_util

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await asyncio.wait_for(hanging.entered.wait(), 3)  # the interval worker is in the publish
    async with asyncio.timeout(5):
        assert await hass.config_entries.async_unload(entry.entry_id)  # cancels the worker
