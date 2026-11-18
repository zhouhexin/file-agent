"""Harness启动配置：在宿主一次性选择C的模型代理，不向候选暴露环境或认证。"""

import os
from collections.abc import Mapping


def validation_provider_options(*, provider=None, codex_model=None, allow_model_profile_change=None,
                                codex_transport=None, environ: Mapping[str, str] | None = None):
    """复用CLI已加载的env，按显式参数、环境、默认排序；不自行查找其他配置文件。"""
    env = os.environ if environ is None else environ
    sources = {}

    def choose(name, explicit, default):
        """None表示未传参数；显式false不能被env中的true覆盖。"""
        if explicit is not None:
            sources[name] = "CLI"
            return explicit
        key = "EVIDENCE_HARNESS_" + name.upper()
        sources[name] = "ENV" if key in env else "DEFAULT"
        return env.get(key, default)

    selected = choose("provider", provider, "openai-compatible")
    if not isinstance(selected, str) or selected.strip().lower() not in {"codex-login", "openai-compatible"}:
        raise ValueError("HARNESS_PROVIDER_INVALID")
    selected = selected.strip().lower()
    allowed = choose("allow_model_profile_change", allow_model_profile_change, False)
    if isinstance(allowed, str):
        if allowed.strip().lower() not in {"true", "false"}:
            raise ValueError("HARNESS_MODEL_PROFILE_CHANGE_INVALID")
        allowed = allowed.strip().lower() == "true"
    if type(allowed) is not bool:
        raise ValueError("HARNESS_MODEL_PROFILE_CHANGE_INVALID")
    if selected == "codex-login":
        model = choose("codex_model", codex_model, None)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("CODEX_MODEL_REQUIRED")
        model = model.strip()
    else:
        # env保留Codex模型便于切换；显式矛盾参数继续拒绝，保护原API调用的可解释性。
        if codex_model is not None:
            raise ValueError("HARNESS_CODEX_MODEL_WITH_API")
        model, sources["codex_model"] = None, "UNUSED"
    options = {"provider": selected, "codex_model": model, "allow_model_profile_change": allowed}
    if selected == "codex-login" and (codex_transport is not None or "EVIDENCE_HARNESS_CODEX_TRANSPORT" in env):
        transport = choose("codex_transport", codex_transport, "auto")
        if not isinstance(transport, str) or transport.strip().lower() not in {"auto", "https"}:
            raise ValueError("CODEX_TRANSPORT_INVALID")
        options["codex_transport"] = transport.strip().lower()
    elif selected != "codex-login" and codex_transport is not None:
        raise ValueError("HARNESS_CODEX_TRANSPORT_WITH_API")
    return options, sources
