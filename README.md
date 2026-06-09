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

- URL: `https://eurasia-transline.online/identika/` (nginx → `127.0.0.1:8787`, без Basic Auth на разделе Identika).
- Код на сервере: `/home/tbot/identika`, systemd: `identika.service`.
- БД и ассеты: `/home/tbot/.identika/`, `/home/tbot/.identika/assets/` (см. `.env` на сервере).
- Обязательно: `IDENTIKA_PUBLIC_BASE_PATH=/identika` в `/home/tbot/identika/.env`.

Деплой с локальной машины:

```bash
export SSHPASS='<tbot password>'
./scripts/deploy_vps.sh
```

Проверка команд без подключения к серверу и без копирования файлов:

```bash
DRY_RUN=1 ./scripts/deploy_vps.sh
```

Ручной рестарт на сервере: `sudo systemctl restart identika`.

Nginx для `/identika/`: не кэшировать динамику (`proxy_no_cache 1; proxy_cache_bypass 1;`) и явно отключать Basic Auth (`auth_basic off;`) для `/identika/`, `/identika/static/` и redirect `/identika`. В `.env` не задавать `IDENTIKA_UI_PASSWORD`.

### Render (альтернатива)

1. [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint** → подключить репозиторий.
2. Используется `render.yaml` (Docker + диск `/data` для SQLite).
3. В Render Environment задать `IDENTIKA_PROVIDER=mock` (или `openrouter` + `OPENROUTER_API_KEY`).
4. После деплоя проверка: `GET /health`.

Без OpenRouter-ключа при `IDENTIKA_PROVIDER=openrouter` сервис автоматически работает как `mock`.

## Проверки

Базовый локальный прогон:

```bash
pytest -q
```

Live-smoke реального OpenRouter выключен по умолчанию, чтобы не делать платные
внешние вызовы в обычном CI. Запуск текстовой генерации + проверки ZIP:

```bash
export OPENROUTER_API_KEY='<openrouter key>'
RUN_OPENROUTER_E2E=1 pytest -q tests/test_openrouter_live.py
```

Отдельная проверка image-модели дороже и включается вторым флагом:

```bash
RUN_OPENROUTER_E2E=1 RUN_OPENROUTER_IMAGE_E2E=1 pytest -q tests/test_openrouter_live.py
```

## API

- `GET /health`
- `GET /`, `GET /create`, `GET /jobs/{job_id}` — HTML-кабинет.
- `GET /settings`, `POST /settings`, `POST /settings/test` — провайдер и OpenRouter.
- `GET /templates`, `POST /templates`, `POST /templates/{template_id}/delete` — шаблоны категорий.
- `POST /v1/uploads/source-images` — загрузка исходных фото.
- `POST /v1/generation/jobs`
- `GET /v1/generation/jobs`
- `GET /v1/generation/jobs/{job_id}`
- `GET /v1/generation/jobs/{job_id}/result`
- `PATCH /v1/generation/jobs/{job_id}/result/text`
- `POST /v1/generation/jobs/{job_id}/re-render`
- `POST /v1/generation/jobs/{job_id}/source-images`
- `POST /v1/generation/jobs/{job_id}/approve`
- `DELETE /v1/generation/jobs/{job_id}`
- `DELETE /v1/generation/jobs/{job_id}/slides/{slide_index}/text`
- `DELETE /v1/generation/jobs/{job_id}/slides/{slide_index}/image`
- `GET /v1/generation/jobs/{job_id}/export`
- `GET /v1/generation/jobs/{job_id}/rich-export`
- `GET /v1/assets/{asset_id}`

## Безопасность

Секреты не сохраняются в БД, manifest, ZIP или логах. AI-сервис получает только
очищенный `ProductContext`, а не WB/B2B токены.
