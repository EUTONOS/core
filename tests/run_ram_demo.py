"""Actual RAM CLI capture/read/correct/archive with no model or live consumer."""
from test_ram import RamTests
from eutonos import ram, tokenslang as ts


def main():
    fixture = RamTests()
    fixture.setUp()
    try:
        def write(request):
            value = fixture.cli("plan", request, "--actor", "recorder")
            fixture.admit(value)
            return fixture.cli("apply", value, "--fixture", "--actor", "recorder", "--attempt", "ram-attempt",
                               "--coordinator", str(fixture.coordinator.path), "--fingerprint", value["fingerprint"])
        write(fixture.request)
        event = fixture.cli("read", {"profile": ram.READ, "operation": "browse", "from": "2026-09-07T00:00:00-05:00",
                                     "to": "2026-09-08T00:00:00-05:00"})
        assert len(event["items"]) == 1
        write(fixture.correction())
        write(fixture.control("archived"))
        archived = fixture.cli("read", {"profile": ram.READ, "operation": "get", "identity": fixture.ident, "include_archived": True})
        item = archived["items"][0]
        assert item["experience"]["revision"] == 2 and item["controls"]["state"] == "archived"
        print(ts.encoded({"demo": "EHF4R RAM", "capture": "guarded CLI", "event_matches": len(event["items"]),
                          "record_revision": item["record_revision"], "content_revision": item["experience"]["revision"],
                          "state": item["controls"]["state"], "native_work_link": "preserved", "model_calls": 0,
                          "scope": "disposable fixture; no live installation"}).decode())
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
