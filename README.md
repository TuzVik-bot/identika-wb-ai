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

## Production

Репозиторий: `https://github.com/TuzVik-bot/identika-wb-ai`

### VPS (текущий прод)

- URL: `https://eurasia-transline.online/identika/` (nginx → `127.0.0.1:8787`, единый Basic Auth WB Tool на весь сайт).
- Код на сервере: `/home/tbot/identika`, systemd: `identika.service`.
- БД и ассеты: `/home/tbot/.identika/`, `/home/tbot/.identika/assets/` (см. `.env` на сервере).
- Обязательно: `IDENTIKA_PUBLIC_BASE_PATH=/identika` в `/home/tbot/identika/.env`.

Деплой с локальной машины:

```bash
export SSHPASS='<tbot password>'
./scripts/deploy_vps.sh
```

Ручной рестарт на сервере: `sudo systemctl restart identika`.

Nginx для `/identika/`: не кэшировать динамику (`proxy_no_cache 1; proxy_cache_bypass 1;`), не использовать отдельный `auth_basic "Identika"` — только общий вход сайта. Статика `/identika/static/` без пароля. В `.env` не задавать `IDENTIKA_UI_PASSWORD`.

### Render (альтернатива)

1. [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint** → подключить репозиторий.
2. Используется `render.yaml` (Docker + диск `/data` для SQLite).
3. В Render Environment задать `IDENTIKA_PROVIDER=mock` (или `openrouter` + `OPENROUTER_API_KEY`).
4. После деплоя проверка: `GET /health`.

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
