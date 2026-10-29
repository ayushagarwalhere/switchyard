"""Call-site detection on patterns seen in real repositories."""
from switchyard import scanner

SRC = '''
from anthropic import Anthropic
from openai import OpenAI
from pydantic import BaseModel

client = OpenAI()
haiku = Anthropic()


class Intent(BaseModel):
    label: str


def classify(user_input):
    return client.responses.parse(model="gpt-4.1", input=f"Classify: {user_input}", text_format=Intent).output_parsed


def draft(ticket):
    return haiku.messages.create(model="claude-3-5-haiku-latest", max_tokens=200,
                                 messages=[{"role": "user", "content": ticket}])


def not_llm(queue):
    return queue.messages.create(body="hi")   # some other library's messages.create
'''


def test_responses_parse_and_named_anthropic_clients(tmp_path):
    (tmp_path / "app.py").write_text(SRC, encoding="utf-8")
    calls = {c["function"]: c for c in scanner.scan_repo(tmp_path)["calls"]}
    assert set(calls) == {"classify", "draft"}
    assert calls["classify"]["method"] == "responses.parse"
    assert calls["classify"]["params"]["output_schema"]["name"] == "Intent"
    assert calls["classify"]["variables"] == ["user_input"]
    assert calls["draft"]["sdk"] == "anthropic" and calls["draft"]["model"] == "claude-3-5-haiku-latest"


def test_cli_scan_runs(capsys):
    """Importing and running the CLI catches syntax errors the module-level tests can't see."""
    from pathlib import Path

    from switchyard import cli
    assert cli.main(["scan", str(Path(__file__).parent / "fixtures" / "sample_app")]) == 0
    out = capsys.readouterr().out
    assert "3 LLM call site(s)" in out and "triage" in out


CONCAT = '''
import os
from openai import OpenAI

client = OpenAI()
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
SYSTEM = {"role": "system", "content": "You are terse."}


def chat(history, question):
    msgs = [SYSTEM] + history + [{"role": "user", "content": question}]
    return client.chat.completions.create(model=MODEL, messages=msgs)


def chat2(question):
    base = [{"role": "system", "content": "Be kind."}]
    return client.chat.completions.create(model=os.environ.get("M", "gpt-4.1"),
                                          messages=[*base, {"role": "user", "content": question}])
'''


def test_concatenated_messages_and_env_var_models(tmp_path):
    (tmp_path / "app.py").write_text(CONCAT, encoding="utf-8")
    calls = {c["function"]: c for c in scanner.scan_repo(tmp_path)["calls"]}
    roles = [m["role"] for m in calls["chat"]["messages"]]
    assert roles == ["system", "history", "user"]
    assert calls["chat"]["messages"][0]["content"] == "You are terse."
    assert calls["chat"]["messages"][-1]["content"] == "{{question}}"
    assert calls["chat"]["model"] == "gpt-4o-mini"
    assert calls["chat2"]["model"] == "gpt-4.1"
    assert calls["chat2"]["messages"][0] == {"role": "system", "content": "Be kind."}


SHADOW = '''
from openai import OpenAI
client = OpenAI()


def extract(user_input):
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": user_input}])


user_input = "Can you send an email to Alice?"     # demo code at the bottom of the file
print(extract(user_input))
'''


def test_parameters_shadow_module_level_demo_values(tmp_path):
    (tmp_path / "app.py").write_text(SHADOW, encoding="utf-8")
    call = scanner.scan_repo(tmp_path)["calls"][0]
    assert call["messages"] == [{"role": "user", "content": "{{user_input}}"}] and call["variables"] == ["user_input"]
