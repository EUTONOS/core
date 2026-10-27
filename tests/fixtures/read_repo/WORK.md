# Native fixture work owner

This is synthetic fixture data. The following block is the sole selected-work
record for this fixture. Its fields are readable directly without EUTONOS.

```tokenslang-work
{
  "profile": "eutonos.tokenslang.work.v1",
  "work_id": "EHF2-DEMO",
  "goal": "Inspect the fixture greeting and preserve its required constraints.",
  "status": "selected",
  "required": [
    {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "required-rules"},
    {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "SRC-DEMO"}
  ],
  "optional": [
    {"target": {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "NAV-DEMO"}, "why": "A fixture route identifies the first implementation source."},
    {"target": {"scope_id": "fixture.scope", "namespace": "fixture.native", "resource_id": "RAM-DEMO"}, "why": "Prior fixture experience explains the Unicode requirement."}
  ]
}
```
