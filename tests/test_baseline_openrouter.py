from switchyard import config


def test_openai_key_uses_openai(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-abc")
    assert config.baseline_key() == "sk-proj-abc"
    assert config.baseline_base_url() is None
    assert config.baseline_model_id("gpt-4o") == "gpt-4o"


def test_openrouter_key_routes_to_openrouter(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    assert config.baseline_key() == "sk-or-v1-abc"
    assert config.baseline_base_url() == config.OPENROUTER_BASE_URL
    assert config.baseline_model_id("gpt-4o") == "openai/gpt-4o"
    assert config.baseline_model_id("openai/gpt-4o-mini") == "openai/gpt-4o-mini"


def test_openrouter_key_in_openai_var(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-v1-abc")
    assert config.baseline_base_url() == config.OPENROUTER_BASE_URL


def test_baseline_off(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    monkeypatch.setenv("SWITCHYARD_BASELINE", "0")
    assert config.baseline_key() is None


def test_baseline_budget_stops_calls(monkeypatch):
    from switchyard import llm
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    monkeypatch.setenv("NEBIUS_API_KEY", "test-key")
    monkeypatch.setenv("SWITCHYARD_BASELINE_BUDGET", "0.5")
    tf = llm.TokenFactory()
    tf.baseline_usage.cost = 0.5
    out = tf.complete("source:gpt-4o", [{"role": "user", "content": "hi"}])
    assert out.error and "budget" in out.error


def test_github_models_token(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-abc")
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "github_pat_abc")
    assert config.baseline_key() == "github_pat_abc"   # preferred over OpenRouter
    assert config.baseline_base_url() == config.GITHUB_MODELS_BASE_URL
    assert config.baseline_model_id("gpt-4o-mini") == "openai/gpt-4o-mini"
