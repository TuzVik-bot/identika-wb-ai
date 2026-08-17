from __future__ import annotations

import httpx

CREDITS_HINT = "пополните баланс на https://openrouter.ai/settings/credits"


def describe_openrouter_error(exc: BaseException) -> str:
    """Human-readable OpenRouter failure cause for job warnings/UI."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return (
                f"OpenRouter отклонил API-ключ (HTTP {status}) — "
                "проверьте OPENROUTER_API_KEY в настройках"
            )
        if status == 402:
            return (
                f"Недостаточно кредитов OpenRouter (HTTP 402) — {CREDITS_HINT}"
            )
        if status == 429:
            return "Лимит запросов OpenRouter (HTTP 429) — повторите генерацию позже"
        if status in (400, 404):
            return (
                f"OpenRouter отклонил запрос (HTTP {status}) — проверьте имена моделей "
                f"в настройках: {exc.response.text[:160]}"
            )
        return f"OpenRouter вернул HTTP {status}: {exc.response.text[:160]}"
    if isinstance(exc, httpx.HTTPError):
        return f"сеть недоступна ({type(exc).__name__}) — проверьте подключение к openrouter.ai"
    return type(exc).__name__
