"""LiteLLM and LangChain apps: found, tested, and rewritten like plain SDK calls."""
import ast

from switchyard import config, rewrite, scanner

LITELLM = '''
import litellm
from litellm import completion as llm_call


def tag(text):
    return llm_call(model="gpt-4o-mini", messages=[{"role": "user", "content": f"Tag: {text}"}], temperature=0)


async def tag_async(text):
    return await litellm.acompletion(model="claude-3-5-haiku-latest", messages=[{"role": "user", "content": text}])


def positional(text):
    return litellm.completion("gpt-4o", [{"role": "user", "content": text}])
'''

LANGCHAIN = '''
import os
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel

llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=os.environ["OPENAI_API_KEY"], max_retries=3)
writer = ChatAnthropic(model="claude-3-5-sonnet-latest")

PROMPT = ChatPromptTemplate.from_messages([
    ("system", "You route support tickets for {company}. Answer with one team name."),
    MessagesPlaceholder("history"),
    ("human", "{ticket}"),
])


class Verdict(BaseModel):
    team: str
    urgent: bool


def route(ticket, history):
    chain = PROMPT | llm.with_structured_output(Verdict)
    return chain.invoke({"company": "Acme", "ticket": ticket, "history": history})


def draft(topic):
    return writer.invoke(f"Write a haiku about {topic}").content
'''


def _scan(tmp_path, name, src):
    (tmp_path / name).write_text(src, encoding="utf-8")
    return {c["function"]: c for c in scanner.scan_repo(tmp_path)["calls"]}


def test_litellm_calls_are_found(tmp_path):
    calls = _scan(tmp_path, "app.py", LITELLM)
    assert set(calls) == {"tag", "tag_async", "positional"}
    assert calls["tag"]["sdk"] == "litellm" and calls["tag"]["model"] == "gpt-4o-mini"
    assert calls["tag"]["messages"] == [{"role": "user", "content": "Tag: {{text}}"}]
    assert calls["tag_async"]["model"] == "claude-3-5-haiku-latest"


def test_langchain_models_follow_their_prompts(tmp_path):
    (tmp_path / "chains.py").write_text(LANGCHAIN, encoding="utf-8")
    found = scanner.scan_repo(tmp_path)["calls"]
    by_ctor = {c["method"]: c for c in found}
    oa = by_ctor["ChatOpenAI"]
    assert oa["sdk"] == "langchain-openai" and oa["model"] == "gpt-4o" and oa["function"] == "route"
    assert [m["role"] for m in oa["messages"]] == ["system", "history", "user"]
    assert oa["messages"][0]["content"] == "You route support tickets for {{company}}. Answer with one team name."
    assert set(oa["variables"]) == {"company", "history", "ticket"}
    assert oa["params"]["temperature"] == 0
    assert oa["params"]["output_schema"]["name"] == "Verdict"
    an = by_ctor["ChatAnthropic"]
    assert an["sdk"] == "langchain-anthropic" and an["function"] == "draft"
    assert an["messages"] == [{"role": "user", "content": "Write a haiku about {{topic}}"}]


def _migrate(tmp_path, src, choose):
    repo, out = tmp_path / "repo", tmp_path / "run" / "migrated"
    repo.mkdir()
    (repo / "app.py").write_text(src, encoding="utf-8")
    scan = scanner.scan_repo(repo)
    sites = [{"site": c, "status": "done",
              "decision": {"action": "migrate", "model": choose, "choice": choose, "pass_rate": 1.0, "hint": ""}}
             for c in scan["calls"]]
    res = rewrite.apply(repo, out, scan, sites)
    return res, (out / "app.py").read_text(encoding="utf-8")


def test_litellm_rewrite_points_at_token_factory(tmp_path):
    res, code = _migrate(tmp_path, LITELLM, "lightning")
    assert res["ok"], res["compile"]
    ast.parse(code)
    mid = config.MODELS["lightning"].id
    assert code.count(f'"openai/{mid}"') == 2 and code.count("api_base=os.environ.get(") == 2
    assert [m["line"] for m in res["manual"]] and res["manual"][0]["reason"].startswith("model passed positionally")
    env = (tmp_path / "run" / "migration.env").read_text()
    assert f"=openai/{mid}" in env


def test_langchain_rewrite(tmp_path):
    res, code = _migrate(tmp_path, LANGCHAIN, "super")
    assert res["ok"], res["compile"]
    tree = ast.parse(code)
    ctor = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ChatOpenAI")
    kws = {k.arg: ast.unparse(k.value) for k in ctor.keywords}
    assert config.MODELS["super"].id in kws["model"] and "NEBIUS_API_KEY" in kws["api_key"] and "base_url" in kws
    assert kws["temperature"] == "0" and kws["max_retries"] == "3"
    assert "OPENAI_API_KEY" not in ast.unparse(ctor)       # the old key is dropped, not sent to Nebius
    # ChatAnthropic can't be pointed at an OpenAI-compatible endpoint: reported, not rewritten
    assert any(m["sdk"] == "langchain-anthropic" for m in res["manual"])
    assert 'writer = ChatAnthropic(model="claude-3-5-sonnet-latest")' in code


AGENT = """
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

llm = ChatOpenAI(model="gpt-4o")
agent = create_react_agent(llm, tools=[])
"""


def test_models_driven_by_agent_frameworks_are_skipped_with_a_reason(tmp_path):
    (tmp_path / "agent.py").write_text(AGENT, encoding="utf-8")
    c = scanner.scan_repo(tmp_path)["calls"][0]
    assert c["skip"] and "create_react_agent()" in c["skip"]
    from switchyard import cli
    assert cli.main(["run", str(tmp_path), "--dry-run"]) == 1


TRICKY = '''
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain.chains import LLMChain
from litellm import completion

PARAMS = {"api_key": "x", "temperature": 0}


def grade(doc):
    llm = ChatOpenAI(model="gpt-4o")
    return (ChatPromptTemplate.from_template("Grade this: {doc}. Reply as {{\\"score\\": n}}") | llm).invoke({"doc": doc})


def rewrite(text):
    llm = ChatOpenAI(model="gpt-4o-mini")
    return llm.invoke(f"Rewrite: {text}")


def legacy(q):
    chain = LLMChain(llm=ChatOpenAI(model="gpt-4o"), prompt=ChatPromptTemplate.from_template("Answer: {q}"))
    return chain.run(q=q)


def spread(q):
    return completion(model="gpt-4o", messages=[{"role": "user", "content": q}], **PARAMS)
'''


def test_local_models_get_their_own_prompts_and_star_kwargs_go_manual(tmp_path):
    (tmp_path / "app.py").write_text("\ufeff" + TRICKY, encoding="utf-8")      # saved with a BOM
    calls = {c["function"]: c for c in scanner.scan_repo(tmp_path)["calls"]}
    assert calls["grade"]["messages"][0]["content"] == 'Grade this: {{doc}}. Reply as {"score": n}'
    assert calls["rewrite"]["messages"][0]["content"] == "Rewrite: {{text}}"
    assert calls["legacy"]["messages"][0]["content"] == "Answer: {{q}}" and not calls["legacy"].get("skip")
    (tmp_path / "m").mkdir()
    res, _ = _migrate(tmp_path / "m", TRICKY, "lightning")
    spread = next(m for m in res["manual"] if m["line"] == calls["spread"]["line"])
    assert "**kwargs" in spread["reason"]
