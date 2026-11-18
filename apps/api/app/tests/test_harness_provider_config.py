"""Harness配置选择测试：保留原API，env与CLI优先级明确，配置错误不能静默外发。"""

import pytest

from app.modules.evidence_harness.config import validation_provider_options


CODEX_ENV = {"EVIDENCE_HARNESS_PROVIDER": "codex-login", "EVIDENCE_HARNESS_CODEX_MODEL": "fake-model",
    "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE": "true"}


def test_no_configuration_keeps_original_api_and_strict_profile():
    """新功能不能把未选择Codex的原API使用者自动切换认证或放开配置变更。"""
    options, sources = validation_provider_options(environ={})
    assert options == {"provider": "openai-compatible", "codex_model": None, "allow_model_profile_change": False}
    assert sources == {"provider": "DEFAULT", "codex_model": "UNUSED", "allow_model_profile_change": "DEFAULT"}


def test_env_selects_codex_and_keeps_credentials_out_of_options():
    """只传有效选择，不把API key、数据库或认证资料交给候选参数。"""
    options, sources = validation_provider_options(environ={**CODEX_ENV, "LLM_API_KEY": "PRIVATE_SENTINEL", "DATABASE_URL": "PRIVATE_SENTINEL"})
    assert options == {"provider": "codex-login", "codex_model": "fake-model", "allow_model_profile_change": True}
    assert set(sources.values()) == {"ENV"}
    assert "PRIVATE_SENTINEL" not in str(options)


def test_cli_can_temporarily_switch_to_api_without_deleting_codex_model():
    """切回API不需删掉env中的Codex模型，已有API配置继续由原Settings读取。"""
    options, sources = validation_provider_options(environ=CODEX_ENV, provider="openai-compatible")
    assert options["provider"] == "openai-compatible" and options["codex_model"] is None
    assert sources["provider"] == "CLI" and sources["codex_model"] == "UNUSED"


def test_api_env_ignores_saved_codex_model():
    """只修改一个Provider配置项就可以切回原API。"""
    options, _ = validation_provider_options(environ={**CODEX_ENV, "EVIDENCE_HARNESS_PROVIDER": "openai-compatible"})
    assert options["provider"] == "openai-compatible" and options["codex_model"] is None


def test_cli_explicit_false_and_model_override_env():
    """显式关闭必须覆盖env中的true；命令指定的模型也优先于env。"""
    options, sources = validation_provider_options(environ=CODEX_ENV, codex_model="other-fake-model", allow_model_profile_change=False)
    assert options["codex_model"] == "other-fake-model" and options["allow_model_profile_change"] is False
    assert sources["codex_model"] == sources["allow_model_profile_change"] == "CLI"


@pytest.mark.parametrize("env,kwargs,code", [
    ({"EVIDENCE_HARNESS_PROVIDER": "typo"}, {}, "HARNESS_PROVIDER_INVALID"),
    ({**CODEX_ENV, "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE": "yes"}, {}, "HARNESS_MODEL_PROFILE_CHANGE_INVALID"),
    ({**CODEX_ENV, "EVIDENCE_HARNESS_CODEX_MODEL": ""}, {}, "CODEX_MODEL_REQUIRED"),
    ({"EVIDENCE_HARNESS_PROVIDER": "codex-login"}, {}, "CODEX_MODEL_REQUIRED"),
    ({}, {"codex_model": "fake-model"}, "HARNESS_CODEX_MODEL_WITH_API"),
    ({}, {"allow_model_profile_change": 1}, "HARNESS_MODEL_PROFILE_CHANGE_INVALID"),
])
def test_invalid_configuration_fails_closed(env, kwargs, code):
    """配置有误直接拒绝，不能偷偷改用另一个模型接口或默许变更。"""
    with pytest.raises(ValueError, match=code):
        validation_provider_options(environ=env, **kwargs)


def test_cli_has_priority_over_invalid_unused_env_values():
    """显式参数能覆盖同项错误env，不读取未使用模型或其他目录中的配置。"""
    options, _ = validation_provider_options(environ={"EVIDENCE_HARNESS_PROVIDER": "typo",
        "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE": "typo"}, provider="openai-compatible", allow_model_profile_change=False)
    assert options["provider"] == "openai-compatible" and options["allow_model_profile_change"] is False


def test_https_env_and_cli_override_do_not_change_api_selection():
    """HTTPS只属于Codex，原API保留；命令可临时恢复auto，错误值不能静默忽略。"""
    env = {**CODEX_ENV, "EVIDENCE_HARNESS_CODEX_TRANSPORT": "https"}
    options, sources = validation_provider_options(environ=env)
    assert options["codex_transport"] == "https" and sources["codex_transport"] == "ENV"
    options, sources = validation_provider_options(environ=env, codex_transport="auto")
    assert options["codex_transport"] == "auto" and sources["codex_transport"] == "CLI"
    options, _ = validation_provider_options(environ=env, provider="openai-compatible")
    assert "codex_transport" not in options
    with pytest.raises(ValueError, match="CODEX_TRANSPORT_INVALID"):
        validation_provider_options(environ={**env, "EVIDENCE_HARNESS_CODEX_TRANSPORT": "typo"})
