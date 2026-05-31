# Identika WB AI

Локальный MVP-сервис для генерации комплекта WB-медиа: 10 слайдов, rich-пакет,
PDF-превью и ZIP-экспорт. По умолчанию работает в `mock`-режиме и не делает
внешних AI/WB-вызовов.

## Контекст для продолжения

Для продолжения работы в Antigravity см. [ANTIGRAVITY.md](ANTIGRAVITY.md).
Там зафиксированы текущее состояние проекта, архитектура, готовый UI/API,
локальные данные и рекомендуемые следующие шаги.

## Запуск

```bash
cd /Users/home/Downloads/Identika
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
uvicorn identika.app:create_app --factory --app-dir app --host 127.0.0.1 --port 8787
```

Открыть UI: `http://127.0.0.1:8787`.

Docker:

```bash
docker compose up --build
```

## Production (Render)

Репозиторий: `https://github.com/TuzVik-bot/identika-wb-ai`

1. [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint** → подключить репозиторий.
2. Используется `render.yaml` (Docker + диск `/data` для SQLite).
3. В Render Environment задать `IDENTIKA_PROVIDER=mock` (или `openrouter` + `OPENROUTER_API_KEY`).
4. После деплоя открыть URL сервиса; проверка: `GET /health`.

Без OpenRouter-ключа при `IDENTIKA_PROVIDER=openrouter` сервис автоматически работает как `mock`.

## API

- `GET /health`
- `POST /v1/generation/jobs`
- `GET /v1/generation/jobs/{job_id}`
- `GET /v1/generation/jobs/{job_id}/result`
- `POST /v1/generation/jobs/{job_id}/approve`
- `GET /v1/generation/jobs/{job_id}/export`
- `GET /v1/assets/{asset_id}`

## Безопасность

Секреты не сохраняются в БД, manifest, ZIP или логах. AI-сервис получает только
очищенный `ProductContext`, а не WB/B2B токены.
