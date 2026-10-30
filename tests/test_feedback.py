"""The feedback report renders from measurements without any network calls."""
from switchyard import feedback


def _data():
    ids = ["nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/Nemotron-3_5-Lightning",
           "nvidia/nemotron-3-super-120b-a12b", "Qwen/Qwen3-Embedding-8B"]
    nem = [{"id": i, "case": "lower" if i == i.lower() else "mixed", "vendor_prefix": "/NVIDIA-" in i,
            "version": "underscore" if "3_5" in i else "plain"} for i in ids if "emotron" in i]
    return {
        "measured_at": "2026-10-30 10:00 UTC", "base_url": "x", "samples": 3, "calls": 42, "cost_usd": 0.03,
        "ids": {"all": ids, "nemotron": nem, "embedding": ["Qwen/Qwen3-Embedding-8B"]},
        "latency": [{"model": "lightning", "id": ids[1], "n": 3, "errors": 0, "median_s": 7.1, "p90_s": 9.0,
                     "median_tokens": 80, "tok_per_s": 11.0},
                    {"model": "super", "id": ids[2], "n": 3, "errors": 1, "median_s": 4.9, "p90_s": 5.2,
                     "median_tokens": 82, "tok_per_s": 17.0}],
        "schema": [{"model": "super", "id": ids[2], "variant": "only", "n": 3, "both_ok": 0, "name_ok": 0, "time_ok": 3,
                    "example": '{"name": "create_calendar_event"}'},
                   {"model": "super", "id": ids[2], "variant": "prompt", "n": 3, "both_ok": 3, "name_ok": 3, "time_ok": 3,
                    "example": ""}],
        "runaway": [{"model": "ultra", "id": "u", "n": 3, "errors": 0, "hit_cap": 1, "invalid_json": 1,
                     "median_tokens": 2000, "max_tokens": 4096}],
        "embeddings": [{"id": "Qwen/Qwen3-Embedding-8B", "n": 6, "errors": 0, "dim": 4096, "min_s": 1.0,
                        "median_s": 2.0, "max_s": 12.0}],
    }


def test_report_has_every_issue_with_repro_numbers_impact_and_fix():
    md = feedback.markdown(_data())
    for n in range(1, 7):
        assert f"## {n}. " in md
    assert md.count("**Repro**") + md.count("**Repro** (") >= 6
    assert md.count("**Impact.**") == 6 and md.count("**Suggested fix.**") == 6
    assert "0/3 correct" in md and "3/3 correct" in md          # schema only vs schema in prompt
    assert "Lightning slower" in md                              # 7.1 s vs 4.9 s
    assert "(1 failed)" in md
    assert "`3_5` (underscore)" in md and "1/3" in md
    assert "Qwen/Qwen3-Embedding-8B" in md and "12.0 s" in md


def test_event_check_needs_a_readable_name_and_a_time():
    assert feedback._event_ok('{"name": "Team Meeting", "date": "2026-11-03T14:00:00"}')["name_ok"]
    bad = feedback._event_ok('{"name": "create_calendar_event", "date": "2026-11-03"}')
    assert not bad["name_ok"] and not bad["time_ok"]
    assert not feedback._event_ok("not json")["valid"]


def test_nemotron_sections_render_when_measured():
    d = _data()
    d["thinking"] = [
        {"model": "lightning", "id": "nvidia/Nemotron-3_5-Lightning", "setting": s, "n": 1, "errors": 0, "correct": 1,
         "median_tokens": t, "median_reasoning": r, "median_s": 1.0}
        for s, t, r in [("no setting (default)", 212, 205), ("`chat_template_kwargs.enable_thinking: false`", 6, 0),
                        ('`reasoning_effort: "none"`', 6, 0), ('`reasoning_effort: "low"`', 307, 300),
                        ("`/no_think` system message", 467, 460)]]
    d["by_task"] = {"runs": ["a", "b"], "sites": 10, "tasks": [{"task": "classification", "sites": 4, "switched": 4},
                                                              {"task": "generation", "sites": 6, "switched": 1}],
                    "chosen": {"lightning": 4, "super": 1}, "size_inversions": 5, "lightning_over_super": 3,
                    "never_switched": [{"function": "get_plan", "task": "generation", "runs": 2, "kept": 2, "best": 0.75}]}
    md = feedback.markdown(d)
    assert "## 7. Nemotron thinks by default" in md and "## 8. Nemotron model feedback" in md
    assert "billed 212 output tokens for an answer that needs 6" in md and "Lightning 307 vs 212" in md
    assert "so it is ignored" in md and "`get_plan` (best 75%)" in md and "On 5 of 10 call sites" in md
    assert md.count("**Suggested fix.**") == 8
