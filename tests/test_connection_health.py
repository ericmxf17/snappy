from datetime import datetime, timedelta, timezone

import snaptrade_client_wrapper as st


def authorization(updated=None, *, disabled=False):
    return {
        "id": "conn-1",
        "brokerage": {"name": "Example Broker"},
        "disabled": disabled,
        "disabled_date": "2026-08-01T00:00:00Z" if disabled else None,
        "type": "read",
        "updated_date": updated,
    }


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_connection_health_classifies_recent_and_aging_syncs():
    assert st._connection_health(authorization(ago(2)))['status'] == 'healthy'
    assert st._connection_health(authorization(ago(6.1)))['status'] == 'aging'


def test_connection_health_classifies_stale_after_24_hours():
    health = st._connection_health(authorization(ago(24.1)))

    assert health["status"] == "stale"
    assert health["stale"] is True


def test_disabled_takes_precedence_over_stale():
    health = st._connection_health(authorization(ago(48), disabled=True))

    assert health["status"] == "disabled"
    assert health["disabled_since"]


def test_missing_or_malformed_sync_time_is_safe_and_needs_attention():
    for updated in (None, "not-a-date"):
        health = st._connection_health(authorization(updated))

        assert health["status"] == "aging"
        assert health["hours_since_sync"] is None
        assert health["stale"] is False


def test_list_connections_exposes_health_fields(monkeypatch):
    class Response:
        body = [authorization(ago(2))]

    class Connections:
        def list_brokerage_authorizations(self, **kwargs):
            return Response()

    class Client:
        connections = Connections()

    monkeypatch.setattr(st, "_client", Client())
    monkeypatch.setattr(st, "_USER", {})

    connection = st.list_connections()[0]

    assert connection["connection_id"] == "conn-1"
    assert connection["status"] == "healthy"
    assert connection["last_synced"]
    assert connection["created"] is None
