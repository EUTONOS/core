"""Complete current-agent CLI protocol in a disposable deterministic fixture."""
from test_nav import NavTests
from eutonos import tokenslang as ts
from eutonos import nav


def main():
    fixture = NavTests()
    fixture.setUp()
    try:
        args = ["--fixture", "--coordinator", str(fixture.coordinator.path), "--actor", "fixture-agent", "--attempt", "nav-test"]
        fixture.cli("start", *args, "--run-id", "cli-demonstration")
        packets = 0
        for _ in range(64):
            issued = fixture.cli("next", *args)
            if issued["outcome"] != "packet":
                break
            fixture.cli("submit", *args, payload=fixture.provider(issued["packet"]))
            packets += 1
        assert issued["outcome"] == "queue-complete", issued
        revision = fixture.commit()
        read = fixture.cli("read", "--revision", revision, payload={"profile": nav.READ, "operation": "search", "query": "auth"})
        assert read["items"], read
        (fixture.root / "src/auth.py").write_bytes(b"def login(user):\n    return bool(user)\n")
        impact = fixture.cli("maintenance")
        assert "SRC-auth" in impact["affectedRecords"] and "GAP-bootstrap" not in impact["affectedRecords"]
        print(ts.encoded({"demo": "EHF4 NAV", "construction": issued["outcome"], "closedSweeps": len(issued["closedSweeps"]),
                          "cliPackets": packets, "searchRecords": len(read["items"]), "maintenance": impact,
                          "provider": "deterministic source-reading fixture; zero model calls", "semanticAcceptance": "unproven"}).decode())
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
