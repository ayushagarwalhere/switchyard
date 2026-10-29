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
