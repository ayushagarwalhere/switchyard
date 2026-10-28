import json

from switchyard import scanner, traces
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "sample_app"


def test_production_calls_become_cases(tmp_path):
    scan = scanner.scan_repo(FIX)
    triage = next(c for c in scan["calls"] if c["function"] == "triage")
    system = "You are a support triage assistant. Reply with one label: billing, bug, feature, other."
    lines = [
        {"messages": [{"role": "system", "content": system}, {"role": "user", "content": "Ticket:\nI was charged twice"}],
         "output": "billing", "model": "gpt-4o-mini"},
        {"request": {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": system},
                                                          {"role": "user", "content": "Ticket:\nApp crashes on login"}]},
         "response": {"choices": [{"message": {"content": "bug"}}]}},
        {"messages": [{"role": "user", "content": "unrelated prompt"}], "output": "x", "model": "gpt-4o-mini"},
    ]
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines))
    recs = traces.load(f)
    assert len(recs) == 3
    cases = traces.cases_for_site(triage, recs)
    assert [c["inputs"]["ticket"] for c in cases] == ["I was charged twice", "App crashes on login"]
    assert [c["reference"] for c in cases] == ["billing", "bug"]
    assert all(c["kind"] == "production" for c in cases)


def test_opaque_template_is_not_matched():
    site = {"messages": [{"role": "user", "content": "{{messages}}"}], "variables": ["messages"], "model": None}
    assert traces.cases_for_site(site, [{"messages": [{"role": "user", "content": "hi"}], "output": "x"}]) == []
