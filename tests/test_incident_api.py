"""M8 故障改派的幂等落库与 HTTP 接口。"""
from concurrent.futures import ThreadPoolExecutor

from app.incidents_store import IncidentStore
from app.errors import InputError


def _store(tmp_path):
    return IncidentStore(tmp_path / "incidents.sqlite3")


def test_retry_returns_same_ledger_without_double_deduction(tmp_path, clean_data, incident):
    store = _store(tmp_path)
    first, reused_first = store.submit(clean_data, incident)
    second, reused_second = store.submit(clean_data, incident)
    assert reused_first is False and reused_second is True
    assert first == second
    assert first["ledger"]["summary"] == second["ledger"]["summary"]


def test_concurrent_retries_commit_once(tmp_path, clean_data, incident):
    store = _store(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.submit(clean_data, incident), range(8)))
    assert sum(not reused for _, reused in results) == 1
    assert len({record["id"] for record, _ in results}) == 1


def test_same_incident_id_with_changed_baseline_is_rejected(tmp_path, clean_data, dirty_data, incident):
    store = _store(tmp_path)
    store.submit(clean_data, incident)
    try:
        store.submit(dirty_data, incident)
    except InputError as exc:
        assert exc.code == "INCIDENT_CONFLICT"
    else:  # pragma: no cover - 必须拒绝覆盖
        raise AssertionError("同一故障号换底账应被拒绝")


def test_get_unknown_incident_returns_404(client):
    response = client.get("/api/incidents/NOPE")
    assert response.status_code == 404
    assert response.json()["code"] == "INCIDENT_NOT_FOUND"


def test_api_submit_is_idempotent(client, clean_data, incident):
    payload = {"data": clean_data, "incident": incident}
    first = client.post("/api/incidents", json=payload)
    assert first.status_code == 200 and first.json()["reused"] is False
    second = client.post("/api/incidents", json=payload)
    assert second.json()["reused"] is True
    assert second.json()["ledger"] == first.json()["ledger"]
    fetched = client.get(f"/api/incidents/{incident['incident']}")
    assert fetched.json()["id"] == incident["incident"]


def test_api_requires_single_source(client, incident):
    response = client.post("/api/incidents", json={"incident": incident})
    assert response.json()["code"] == "CHOOSE_SOURCE"
