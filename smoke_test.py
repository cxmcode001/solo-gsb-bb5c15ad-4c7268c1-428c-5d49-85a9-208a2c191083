"""End-to-end smoke test using FastAPI TestClient."""
import asyncio
import hashlib
import io
import json
import os
import tempfile
import time
import zipfile

_tmp = tempfile.mkdtemp()
os.environ["REPLAY_DB"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.dispatcher import dispatcher  # noqa: E402
from app.main import app  # noqa: E402


def wait_until(predicate, timeout=20, label="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {label}")


def run(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}

    # receiver: first 2 calls per idem key fail with 500
    settings = {
        "target_url": "http://testserver/recv?fail=500&times=2",
        "hmac_secret": "topsecret",
        "max_retries": 3,
        "timeout_seconds": 5.0,
    }
    r = client.post("/api/settings", json=settings)
    assert r.status_code == 200, r.text
    assert r.json()["target_url"].endswith("times=2")

    # import: array form with event_id / id / auto-generated id
    events = [
        {"event_id": "evt-001", "type": "a"},
        {"event_id": "evt-002", "type": "b"},
        {"id": "evt-003", "type": "c"},
        {"type": "no-id", "x": 1},
    ]
    r = client.post("/api/events/import", json=events)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 4, body
    assert len(body["created"]) == 4

    # re-import same id -> update, not create
    r = client.post("/api/events/import", json=[{"event_id": "evt-001", "type": "a2"}])
    assert r.json()["created"] == [] and r.json()["updated"] == ["evt-001"]

    # invalid JSON
    r = client.post("/api/events/import", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400

    # replay all
    r = client.post("/api/replay", json={})
    assert r.status_code == 200
    queued = r.json()["queued"]
    assert len(queued) == 4, r.text

    wait_until(
        lambda: client.get("/api/status").json()["counts"]["success"] == 4,
        timeout=60, label="all 4 delivered")
    st = client.get("/api/status").json()
    assert st["counts"]["failed"] == 0, st
    print("status after replay:", st)

    # timeline: each event had 500,500,200 with a shared idem key
    detail = client.get("/api/events/evt-001").json()
    attempts = detail["timeline"]
    assert len(attempts) == 3, [(a["outcome"], a["http_status"]) for a in attempts]
    assert [a["http_status"] for a in attempts] == [500, 500, 200]
    assert [a["outcome"] for a in attempts] == ["retriable", "retriable", "delivered"]
    keys = {a["idem_key"] for a in attempts}
    assert len(keys) == 1, "retries within one replay must share idempotency key"
    hdr = json.loads(attempts[0]["req_headers"])
    assert hdr["X-Webhook-Event"] == "evt-001"
    assert hdr["Idempotency-Key"] == list(keys)[0]
    assert "t=" in hdr["X-Webhook-Signature"] and "v1=" in hdr["X-Webhook-Signature"]
    assert detail["payload_parsed"]["type"] == "a2"
    print("evt-001 attempts:", [(a["http_status"], a["elapsed_ms"]) for a in attempts])

    # signature verifies against the canonical body
    from app.signing import canonical_body, verify
    raw = canonical_body(detail["payload_parsed"])
    assert verify(raw, "topsecret", hdr["X-Webhook-Signature"])
    assert not verify(raw, "wrong", hdr["X-Webhook-Signature"])

    # receiver logged one 200 per unique event
    logs = client.get("/recv/logs?limit=200").json()["logs"]
    delivered = [l for l in logs if l["status_returned"] == 200]
    assert {l["event"] for l in delivered} == set(queued), [l["event"] for l in delivered]
    print("receiver log entries:", len(logs), "200s:", len(delivered))

    # 4xx -> non_retriable, fail immediately without retry
    client.post("/api/settings", json={**settings,
                "target_url": "http://testserver/recv?fail=422&times=9"})
    client.post("/api/events/import", json=[{"event_id": "evt-bad", "type": "x"}])
    client.post("/api/events/evt-bad/replay")
    wait_until(lambda: client.get("/api/events/evt-bad").json()["status"] == "failed",
               timeout=10, label="evt-bad failed")
    d = client.get("/api/events/evt-bad").json()
    assert len(d["timeline"]) == 1 and d["timeline"][0]["http_status"] == 422
    assert d["timeline"][0]["outcome"] == "non_retriable"

    # manual replay against healthy target -> success with a NEW idem key
    client.post("/api/settings", json={**settings, "target_url": "http://testserver/recv"})
    client.post("/api/events/evt-bad/replay")
    wait_until(lambda: client.get("/api/events/evt-bad").json()["status"] == "success",
               timeout=10, label="evt-bad replay success")
    d = client.get("/api/events/evt-bad").json()
    assert len(d["timeline"]) == 2
    k1, k2 = d["timeline"][0]["idem_key"], d["timeline"][1]["idem_key"]
    assert k1 != k2, "manual replay must use a fresh idempotency key"

    # timeout -> retriable, recorded without an http status
    client.post("/api/settings", json={**settings,
                "target_url": "http://testserver/recv?sleep=2",
                "timeout_seconds": 0.3, "max_retries": 0})
    client.post("/api/events/import", json=[{"event_id": "evt-slow", "type": "x"}])
    t0 = time.time()
    client.post("/api/events/evt-slow/replay")
    wait_until(lambda: client.get("/api/events/evt-slow").json()["status"] == "failed",
               timeout=15, label="evt-slow failed")
    a = client.get("/api/events/evt-slow").json()["timeline"][0]
    assert a["http_status"] is None and a["outcome"] == "retriable"
    assert "Timeout" in a["resp_excerpt"]
    print("timeout path ok in %.1fs, excerpt:" % (time.time() - t0),
          a["resp_excerpt"][:60])

    # detail for missing event
    assert client.get("/api/events/nope").status_code == 404

    # ---------- snapshot backup / atomic restore ----------
    # healthy settings, then import a few events + attempts so the archive
    # has all three tables
    client.post("/api/settings", json={**settings, "target_url": "http://testserver/recv"})
    client.post("/api/events/import", json=[
        {"event_id": "snap-1", "type": "s"},
        {"event_id": "snap-2", "type": "s"},
    ])
    client.post("/api/replay", json={})
    wait_until(lambda: client.get("/api/events/snap-2").json()["status"] == "success",
               timeout=30, label="snap events delivered")
    before_events = {e["event_id"]: e for e in client.get("/api/events").json()["events"]}
    before_attempts = client.get("/api/timeline?limit=500").json()["attempts"]

    r = client.get("/api/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    blob = r.content
    zf = zipfile.ZipFile(io.BytesIO(blob))
    assert zf.namelist() == ["manifest.json", "settings.jsonl",
                             "events.jsonl", "attempts.jsonl"], zf.namelist()
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "webhook-replay-lab-snapshot"
    assert manifest["db_version"] == 1
    for name in ("settings.jsonl", "events.jsonl", "attempts.jsonl"):
        buf = zf.read(name)
        assert hashlib.sha256(buf).hexdigest() == manifest["files"][name]["sha256"]
        assert len(buf.splitlines()) == manifest["files"][name]["rows"]
    # fixed field order: JSONL rows are arrays, not objects
    first_event = json.loads(zf.read("events.jsonl").splitlines()[0])
    assert isinstance(first_event, list) and first_event[0] in before_events
    print("snapshot backup:", {k: v["rows"] for k, v in manifest["files"].items()})

    def restore(bad_blob):
        return client.post("/api/snapshot/restore",
                           files={"file": ("snap.zip", bad_blob, "application/zip")})

    def rezip(*, mutate=None, manifest_override=None, keep=None):
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(blob)) as zin, \
             zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            names = keep if keep is not None else zin.namelist()
            for name in names:
                if name == "manifest.json" and manifest_override is not None:
                    zout.writestr(name, manifest_override)
                elif name in zin.namelist():
                    data = zin.read(name)
                    if mutate and name in mutate:
                        data = mutate[name](data)
                    zout.writestr(name, data)
        return out.getvalue()

    # corrupt payload but hash made consistent -> fails event payload parse, no writes
    bad_payload = b'["snap-1",1,"{not-json","success","t","t"]\n'
    # build a minimal valid archive (deterministic) for the rejection cases
    minimal_events = (
        json.dumps(["snap-x", 1, json.dumps({"event_id": "snap-x"}),
                    "pending", "t", "t"]) + "\n"
    ).encode()
    minimal_settings = zf.read("settings.jsonl")
    minimal_attempts = b""
    m3 = {
        "format": "webhook-replay-lab-snapshot",
        "db_version": 1,
        "created_at": "2026-09-22T00:00:00+00:00",
        "files": {
            "settings.jsonl": {"rows": 1, "bytes": len(minimal_settings),
                               "sha256": hashlib.sha256(minimal_settings).hexdigest()},
            "events.jsonl": {"rows": 1, "bytes": len(minimal_events),
                             "sha256": hashlib.sha256(minimal_events).hexdigest()},
            "attempts.jsonl": {"rows": 0, "bytes": 0,
                               "sha256": hashlib.sha256(minimal_attempts).hexdigest()},
        },
    }

    def build_archive(meta, settings_b, events_b, attempts_b):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.writestr("manifest.json", json.dumps(meta, sort_keys=True))
            zout.writestr("settings.jsonl", settings_b)
            zout.writestr("events.jsonl", events_b)
            zout.writestr("attempts.jsonl", attempts_b)
        return out.getvalue()

    # foreign-key violation: attempt references unknown event
    fk_att = (json.dumps([1, "ghost-event", 1, 1, "idem", "t", None, 500,
                          "delivered", 5, json.dumps({}), None]) + "\n").encode()
    mfk = json.loads(json.dumps(m3))
    mfk["files"]["attempts.jsonl"] = {"rows": 1, "bytes": len(fk_att),
                                      "sha256": hashlib.sha256(fk_att).hexdigest()}
    r = restore(build_archive(mfk, minimal_settings, minimal_events, fk_att))
    assert r.status_code == 422, r.text
    failed = [c["name"] for c in r.json()["detail"]["checks"] if not c["ok"]]
    assert failed == ["attempts.semantic"], failed

    # unparseable event payload with a matching hash -> rejected
    m_bad = json.loads(json.dumps(m3))
    m_bad["files"]["events.jsonl"] = {"rows": 1, "bytes": len(bad_payload),
                                      "sha256": hashlib.sha256(bad_payload).hexdigest()}
    r = restore(build_archive(m_bad, minimal_settings, bad_payload, b""))
    assert r.status_code == 422
    assert any(not c["ok"] and c["name"] == "events.semantic"
               for c in r.json()["detail"]["checks"])

    # hash mismatch (tamper file, keep manifest) -> rejected at sha256
    r = restore(rezip(mutate={"events.jsonl": lambda d: d + b" "}))
    assert r.status_code == 422
    assert any(not c["ok"] and c["name"].endswith(".sha256")
               for c in r.json()["detail"]["checks"])

    # db version mismatch -> rejected
    mver = json.loads(json.dumps(m3)); mver["db_version"] = 999
    r = restore(build_archive(mver, minimal_settings, minimal_events, b""))
    assert r.status_code == 422
    assert any(not c["ok"] and c["name"] == "db.version"
               for c in r.json()["detail"]["checks"])

    # not even a zip -> rejected cleanly
    r = restore(b"definitely not a zip")
    assert r.status_code == 422
    assert any(not c["ok"] and c["name"] == "archive.zip"
               for c in r.json()["detail"]["checks"])

    # rejected restores must not have changed anything
    current = {e["event_id"]: e for e in client.get("/api/events").json()["events"]}
    assert set(current) == set(before_events), set(current) ^ set(before_events)
    assert client.get("/api/timeline?limit=500").json()["attempts"] == before_attempts

    # change settings + wipe data, then restore the valid snapshot:
    # data comes back, but current settings are preserved.
    client.post("/api/settings", json={**settings,
                "target_url": "http://testserver/recv", "hmac_secret": "restored-era"})
    client.delete("/api/events")
    assert client.get("/api/events").json()["events"] == []
    r = restore(blob)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["restored"] is True
    assert summary["counts"]["events"] == len(before_events)
    assert summary["counts"]["attempts"] == len(before_attempts)
    assert summary["settings_preserved"]["hmac_secret"] == "restored-era"
    assert all(c["ok"] for c in summary["checks"])
    after = {e["event_id"]: e for e in client.get("/api/events").json()["events"]}
    assert set(after) == set(before_events)
    # payloads and statuses are byte-identical
    for eid, e in before_events.items():
        assert after[eid]["status"] == e["status"]
        assert after[eid]["payload"] == e["payload"]
    assert len(client.get("/api/timeline?limit=500").json()["attempts"]) == len(before_attempts)
    print("restore summary:", summary["counts"])

    # delivery still works after restore (signing uses the *current* secret)
    client.post("/api/events/import", json=[{"event_id": "snap-after", "type": "z"}])
    client.post("/api/events/snap-after/replay")
    wait_until(lambda: client.get("/api/events/snap-after").json()["status"] == "success",
               timeout=15, label="post-restore delivery")
    hdr = json.loads(
        client.get("/api/events/snap-after").json()["timeline"][0]["req_headers"])
    from app.signing import canonical_body, verify
    raw = canonical_body({"event_id": "snap-after", "type": "z"})
    assert verify(raw, "restored-era", hdr["X-Webhook-Signature"])
    print("post-restore delivery + signature verified")

    # clear history: events/attempts gone, settings preserved
    assert client.delete("/api/events").json() == {"ok": True}
    assert client.get("/api/events").json()["events"] == []
    assert client.get("/api/timeline").json()["attempts"] == []
    assert client.get("/api/settings").json()["hmac_secret"] == "restored-era"

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    import httpx

    # ASGITransport (intentionally) does not enforce httpx timeouts, so wrap
    # it to emulate the real httpcore transport's read-timeout behaviour.
    class TimedASGITransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            timeout = request.extensions.get("timeout", {})
            deadline = timeout.get("read") if timeout else None
            try:
                if deadline is not None:
                    return await asyncio.wait_for(
                        super().handle_async_request(request), timeout=deadline)
                return await super().handle_async_request(request)
            except asyncio.TimeoutError:
                raise httpx.ReadTimeout("simulated read timeout", request=request)

    # Make the background worker's async httpx client talk to this ASGI app
    # instead of performing real DNS for the "testserver" hostname.
    dispatcher._client_factory = lambda: httpx.AsyncClient(
        transport=TimedASGITransport(app=app),
        base_url="http://testserver",
    )
    with TestClient(app) as client:
        run(client)
