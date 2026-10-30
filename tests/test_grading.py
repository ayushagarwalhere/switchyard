"""Grading rules learned from the first real Token Factory run."""
from switchyard import evaluate


def test_fields_compare_like_a_person_would():
    ok = evaluate._field_ok
    assert ok(1.0, 0.95) and ok(0.8, 0.9) and not ok(0.4, 0.9)          # confidence scores: judgement calls
    assert ok(12, 12) and not ok(11, 12)                                 # counts and amounts: exact
    assert ok(True, True) and not ok(False, True) and not ok(1, True)    # booleans: exact, 1 isn't True
    assert ok(" Billing ", "billing")                                    # labels: normalized
    assert ok("anything", "A long description of what the user is asking for in their message") is None


def _ev(model, rows):
    return evaluate.summarize(model, [{"case": c, "passed": p, "score": s, "latency": 1.0, "tokens_in": 1,
                                       "tokens_out": 1, "cost": 0.0, "error": None,
                                       "checks": [{"name": "field is_event", "ok": p, "got": got}]}
                                      for c, p, s, got in rows])


def test_a_reference_every_model_contradicts_the_same_way_is_set_aside():
    evals = {"lightning": _ev("lightning", [(1, False, 0.5, False), (2, True, 0.9, True), (3, False, 0.4, True)]),
             "super": _ev("super", [(1, False, 0.6, False), (2, True, 0.9, True), (3, True, 0.9, False)]),
             "ultra": _ev("ultra", [(1, False, 0.6, False), (2, True, 0.9, True), (3, True, 0.9, False)])}
    disputed = evaluate.disputed_cases(evals)
    assert disputed == [1]                    # case 3: models disagree with each other, so it still counts
    ultra = evaluate.exclude(evals["ultra"], disputed)
    assert ultra["pass_rate"] == 1.0 and ultra["disputed"] == 1 and len(ultra["results"]) == 3
    assert evaluate.exclude(evals["lightning"], disputed)["pass_rate"] == 0.5


def test_dates_names_scores_and_tool_calls():
    ok = evaluate._field_ok
    # "next Friday" with no date in the input: any valid date is a fair reading, but it must be a date
    assert ok("2025-04-18T10:30:00Z", "2024-06-14T10:30:00", "date", '{"q": "next friday"}')
    assert not ok("tomorrow", "2024-06-14T10:30:00", "date", '{"q": "next friday"}')
    # an absolute date in the input must be read exactly (formats may differ)
    assert ok("2024-06-14 10:30", "2024-06-14T10:30:00", "date", '{"q": "June 14 2024 at 10:30"}')
    assert not ok("2024-06-15T10:30:00", "2024-06-14T10:30:00", "date", '{"q": "June 14 2024"}')
    assert ok(7, 8, "cohesion_score") and not ok(4, 8, "cohesion_score")
    assert ok("team sync meeting", "Team meeting", "name") and not ok("create_calendar_event", "Team meeting", "name")
    # the app reads a tool call's arguments, not the wrapper with the function's name
    assert evaluate._tool_output([{"id": "1", "name": "create_event", "arguments": '{"name": "x"}'}]) == '{"name": "x"}'


def test_schema_descriptions_are_shown_to_nemotron_but_not_the_baseline():
    from switchyard import config, llm
    seen = {}

    class TF:
        usage = llm.Usage()

        def complete(self, model_key, messages, **kw):
            seen[model_key] = messages
            return llm.Completion('{"name": "Team Meeting"}', None, 1, 1, 0.1, 0.0, model_key)

        def json(self, *a, **k):
            return {"score": 9, "pass": True, "reason": "ok"}, None

    schema = {"type": "object", "properties": {"name": {"type": "string", "description": "Name of the event"}}}
    site = {"messages": [{"role": "user", "content": "{{q}}"}], "params": {"output_schema": {"name": "E", "schema": schema}}}
    case = {"id": 1, "inputs": {"q": "meet"}, "reference": '{"name": "Team Meeting"}', "expect": {}}
    evaluate.run_case(TF(), site, {"output": {"format": "json"}}, case, "lightning")
    evaluate.run_case(TF(), site, {"output": {"format": "json"}}, case, config.SOURCE_PREFIX + "gpt-4o")
    assert seen["lightning"][0]["content"].startswith(evaluate.SCHEMA_PREFIX) and "Name of the event" in seen["lightning"][0]["content"]
    assert not any(m["content"].startswith(evaluate.SCHEMA_PREFIX) for m in seen[config.SOURCE_PREFIX + "gpt-4o"])


def test_never_switch_to_a_pricier_model():
    from switchyard import pipeline
    site = {"model": "gpt-4o-mini"}
    ev = {"ultra": {"results": [{}] * 4, "tokens_in": 4000, "tokens_out": 4000, "latency_p50": 1.0}}
    d = {"action": "migrate", "choice": "ultra", "model": "ultra", "pass_rate": 0.88, "hint": ""}
    sav = pipeline.savings(site, ev, d)
    assert sav["saving_pct"] < 0
    kept, sav2 = pipeline.never_pricier(site, ev, d, sav)
    assert kept["action"] == "keep" and "costs more than gpt-4o-mini" in kept["reason"] and sav2["saving_pct"] == 0


def test_the_original_model_baseline_must_also_disagree_before_a_case_is_set_aside():
    from switchyard import config
    fam = {k: _ev(k, [(1, False, 0.5, False), (2, False, 0.5, False), (3, False, 0.5, False), (4, False, 0.5, False)])
           for k in ("lightning", "super", "ultra")}
    base = _ev(config.SOURCE_PREFIX + "gpt-4o", [(1, True, 0.9, True), (2, False, 0.5, None), (3, False, 0.5, False),
                                                 (4, False, 0.5, True)])
    base["results"][3]["checks"] = [{"name": "matches Pydantic schema", "ok": False}]   # wrong, but not on that field
    # the Nemotron sizes are one family: without a baseline their agreement is all there is
    assert evaluate.disputed_cases(dict(fam)) == [1, 2, 3, 4]
    # with one, it decides: case 1 the original model got right, so the reference stands
    assert evaluate.disputed_cases({**fam, config.SOURCE_PREFIX + "gpt-4o": base}) == [2, 3]
    base["results"][1]["error"] = "timeout"
    assert evaluate.disputed_cases({**fam, config.SOURCE_PREFIX + "gpt-4o": base}) == [3]
    # a hinted re-run isn't a separate opinion
    assert evaluate.disputed_cases({"lightning": fam["lightning"], "lightning+hint": fam["super"]}) == []


def test_a_patched_schema_message_is_replaced_but_its_merged_hint_kept():
    sp = evaluate.SCHEMA_PREFIX
    msgs = [{"role": "system", "content": sp + "{{json_dumps_Event_model_json_schema}}\n\nUse 24-hour times."},
            {"role": "system", "content": sp + "{{json_dumps_getattr_Event_model_json_sc}}"},
            {"role": "user", "content": "{{text}}"}]
    assert evaluate._without_schema(msgs) == [{"role": "system", "content": "Use 24-hour times."},
                                              {"role": "user", "content": "{{text}}"}]
