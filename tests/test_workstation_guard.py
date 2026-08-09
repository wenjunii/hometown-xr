from datetime import datetime, timedelta, timezone

import pytest

from workstation_guard import (
    WorkstationGuardError,
    WorkstationLease,
    lease_is_active,
)


class FakeLease(WorkstationLease):
    def __init__(self, shared, *args, **kwargs):
        self.shared = shared
        super().__init__(*args, **kwargs)

    def preflight(self, *, allow_uncheckpointed_state=False):
        return {
            "ready": True,
            "head": "abc123",
            "branch": "main",
            "allows_uncheckpointed_state": allow_uncheckpointed_state,
        }

    def _read_remote(self):
        return self.shared.get("commit"), self.shared.get("payload")

    def _publish(self, payload, parent):
        assert parent == self.shared.get("commit")
        commit = f"lease-{self.shared.get('sequence', 0) + 1}"
        self.shared["sequence"] = self.shared.get("sequence", 0) + 1
        self.shared["commit"] = commit
        self.shared["payload"] = dict(payload)
        return commit


def test_cross_pc_lease_blocks_competing_owner_and_releases(tmp_path):
    now = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    shared = {}
    first = FakeLease(
        shared,
        "3080",
        root=tmp_path,
        state_path=tmp_path / "first.json",
        host="pc-3080",
        now=lambda: now[0],
    )
    second = FakeLease(
        shared,
        "4090",
        root=tmp_path,
        state_path=tmp_path / "second.json",
        host="pc-4090",
        now=lambda: now[0],
    )

    claimed = first.claim()
    assert claimed["state"] == "active"
    assert claimed["profile"] == "3080"
    assert lease_is_active(shared["payload"], now[0])
    with pytest.raises(WorkstationGuardError, match="owned by pc-3080"):
        second.claim()

    released = first.release()
    assert released["released"]
    assert not lease_is_active(shared["payload"], now[0])
    assert second.claim()["profile"] == "4090"


def test_context_releases_when_durable_state_remains_synchronized(tmp_path):
    shared = {}
    lease = FakeLease(
        shared,
        "3080",
        root=tmp_path,
        state_path=tmp_path / "owner.json",
        host="pc-3080",
    )

    with lease:
        assert shared["payload"]["state"] == "active"

    assert shared["payload"]["state"] == "released"
    assert not lease.state_path.exists()


def test_expired_lease_can_be_recovered_and_current_owner_can_renew(tmp_path):
    now = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    shared = {}
    first = FakeLease(
        shared,
        "3080",
        root=tmp_path,
        state_path=tmp_path / "first.json",
        host="pc-3080",
        lease_hours=1,
        renew_minutes=10,
        now=lambda: now[0],
    )
    first.claim()
    original_expiry = shared["payload"]["expires_at"]
    now[0] += timedelta(minutes=11)
    renewed = first.renew_if_due()
    assert renewed is not None
    assert renewed["expires_at"] > original_expiry

    now[0] += timedelta(hours=2)
    second = FakeLease(
        shared,
        "5090",
        root=tmp_path,
        state_path=tmp_path / "second.json",
        host="pc-5090",
        now=lambda: now[0],
    )
    assert second.claim()["host"] == "pc-5090"


def test_uncheckpointed_lease_requires_same_owner_or_explicit_recovery(tmp_path):
    now = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    shared = {}
    first = FakeLease(
        shared,
        "3080",
        root=tmp_path,
        state_path=tmp_path / "first.json",
        host="pc-3080",
        lease_hours=1,
        now=lambda: now[0],
    )
    second = FakeLease(
        shared,
        "4090",
        root=tmp_path,
        state_path=tmp_path / "second.json",
        host="pc-4090",
        now=lambda: now[0],
    )

    first.claim()
    retained = first.retain()
    assert retained["lease"]["checkpoint_state"] == "uncheckpointed"
    now[0] += timedelta(hours=2)

    with pytest.raises(WorkstationGuardError, match="uncheckpointed work"):
        second.claim()
    assert first.claim()["owner_id"] == retained["lease"]["owner_id"]

    first.state_path.unlink()
    now[0] += timedelta(hours=2)
    recovered = second.claim(force_recovery=True)
    assert recovered["force_recovered_from"]["host"] == "pc-3080"
