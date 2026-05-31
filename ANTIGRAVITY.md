# Antigravity Handoff: Identika WB AI

Дата среза: 2026-05-31.
Рабочая папка: `/Users/home/Downloads/Identika`.
Статус: production-ready MVP с git, CI, upload фото, OpenRouter image layer, WB upload contract и optional auth.

## Что это за проект

Identika WB AI - локальный FastAPI-сервис для генерации комплекта медиа для карточки товара Wildberries:

- 10 SVG-слайдов для карточки товара.
- Rich package: HTML-превью, PDF-превью и 10 rich-блоков.
- ZIP-экспорт с `manifest.json`, слайдами и rich-файлами.
- Локальная история jobs в SQLite.
- UI-кабинет Identika (ребрендинг с Aidentika): dashboard, настройки, создание проекта, страница результата.
- Безопасная модель данных: WB/B2B/OpenRouter секреты не сохраняются в БД, manifest, ZIP или логах.

По умолчанию проект работает в `mock`-режиме, без внешних AI/WB-вызовов.

## Как запустить локально

```bash
cd /Users/home/Downloads/Identika
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
uvicorn identika.app:create_app --factory --app-dir app --host 127.0.0.1 --port 8787
```

UI: `http://127.0.0.1:8787`

Быстрая проверка:

```bash
curl http://127.0.0.1:8787/health
pytest
```

Docker-вариант:

```bash
docker compose up --build
```

## Текущее локальное состояние

На момент среза:

- Проект инициализирован как git-репозиторий (`.gitignore` исключает `data/`, `assets/`, `.env`).
- БД: `data/identika.sqlite`.
- Ассеты: `assets/`.
- `pytest`: 43 теста (generation, API, UI smoke, upload, OpenRouter mock, WB upload, settings/rebrand, WB CDN, rerender).
- GitHub Actions CI: `.github/workflows/ci.yml`.

Не удалять `data/` и `assets/`: это текущая рабочая история и экспортные файлы.

## Основная архитектура

Ключевые файлы:

- `app/identika/app.py` - создание FastAPI-приложения, templates/static, JobService.
- `app/identika/api/routes.py` - HTML и JSON API маршруты.
- `app/identika/models.py` - Pydantic-схема продукта, слайдов, rich-пакета, jobs.
- `app/identika/storage.py` - SQLite storage и файловые ассеты.
- `app/identika/services/jobs.py` - orchestration генерации, ререндер ассетов, approve/edit.
- `app/identika/services/rendering.py` - SVG/PDF/HTML/ZIP renderer.
- `app/identika/services/wb_tool.py` - клиент WB Tool.
- `app/identika/providers/mock.py` - локальный mock provider.
- `app/identika/providers/openrouter.py` - OpenRouter text provider поверх mock renderer.
- `app/identika/providers/prompts.py` - visual/text prompts для OpenRouter (text-free AI backgrounds).
- `app/identika/services/product_images.py` - скачивание WB CDN фото в локальные ассеты.
- `app/identika/services/wb_cdn.py` - URL-кандидаты для фото WB.
- `app/identika/ui_labels.py` - русские подписи статусов jobs.
- `app/identika/providers/image_gen.py` - OpenRouter image generation layer.
- `app/identika/services/uploads.py` - multipart upload validation.
- `app/identika/middleware.py` - optional API key + UI basic auth.
- `app/identika/templates/` - Jinja UI.
- `app/identika/static/app.css` - текущая дизайн-система.
- `tests/test_generation.py` - regression tests для генерации, ZIP, approve, secret hygiene и text patch.

## Текущий UI

Уже сделано:

- `base.html` с верхней навигацией, footer и общей оболочкой.
- `index.html` — dashboard: метрики (готово/в работе/ошибки), проекты с русскими статусами, quick actions.
- `settings.html` — провайдер mock/openrouter, маскированный ключ, модели, тест ключа (БД > env, без рестарта).
- `create.html` — drag-and-drop upload, loading на submit.
- `job.html` — блок «Информация о генерации» (warnings vs info), пересборка слайдов, scaled SVG preview.
- `app.css` — WB Tool стиль (indigo/slate/Inter), mobile burger, slide preview без crop.
- `routes.py` — `/settings`, `POST /jobs/{id}/re-render`, Cache-Control no-store на динамике.

Важно: продолжать дизайн как рабочий кабинет, а не маркетинговый лендинг.

## API surface

HTML:

- `GET /` - dashboard.
- `GET /create` - создание проекта.
- `GET /jobs/{job_id}` - страница результата.
- `POST /demo` - demo job.
- `POST /wb/generate` - создать job из WB Tool product context.
- `POST /jobs/{job_id}/slides/{slide_index}/text` - форма редактирования текста слайда.
- `GET /settings` — страница настроек (провайдер, ключ, модели).
- `POST /settings`, `POST /settings/test` — сохранение и проверка OpenRouter.
- `POST /jobs/{job_id}/re-render` — пересборка SVG с актуальными фото.

JSON/API:

- `GET /health`
- `POST /v1/uploads/source-images` — multipart, до 4 фото, max 10MB каждое
- `POST /v1/generation/jobs`
- `GET /v1/generation/jobs`
- `GET /v1/generation/jobs/{job_id}`
- `GET /v1/generation/jobs/{job_id}/result`
- `PATCH /v1/generation/jobs/{job_id}/result/text`
- `POST /v1/generation/jobs/{job_id}/approve`
- `GET /v1/generation/jobs/{job_id}/export`
- `POST /jobs/{job_id}/upload-to-wb` - upload в WB Tool.
- `POST /v1/generation/jobs/{job_id}/re-render` - JSON rerender.

## Настройки окружения

Все настройки читаются из env и `.env` через `pydantic-settings`.

Основные переменные (см. `.env.example`):

- `IDENTIKA_PROVIDER=mock` или `openrouter`.
- `IDENTIKA_HOST=127.0.0.1`
- `IDENTIKA_PORT=8787`
- `IDENTIKA_DB_PATH=./data/identika.sqlite`
- `IDENTIKA_ASSETS_DIR=./assets`
- `WB_TOOL_BASE_URL=http://127.0.0.1:8765`
- `IDENTIKA_PUBLIC_BASE_PATH=`
- `OPENROUTER_API_KEY=`
- `OPENROUTER_TEXT_MODEL=google/gemini-3.1-flash-lite-preview`
- `OPENROUTER_IMAGE_MODEL=google/gemini-3.1-flash-image-preview`
- `IDENTIKA_ENABLE_AI_IMAGES=` — пусто = auto (true при `openrouter`, false при `mock`)
- `IDENTIKA_API_KEY=` — если задан, все `/v1/*` требуют `X-API-Key` или `Authorization: Bearer`
- `IDENTIKA_UI_PASSWORD=` — optional basic auth для HTML UI

Runbook для заказчика:

1. Скопировать `.env.example` → `.env`, оставить `IDENTIKA_PROVIDER=mock` для офлайн-демо.
2. Для AI: `IDENTIKA_PROVIDER=openrouter`, задать `OPENROUTER_API_KEY`, при необходимости `IDENTIKA_ENABLE_AI_IMAGES=true`.
3. Для production на VPS: единый nginx Basic Auth (WB Tool), **не** задавать `IDENTIKA_UI_PASSWORD`; настройки OpenRouter можно править в UI `/settings`.
4. Деплой: `SSHPASS=... ./scripts/deploy_vps.sh` (rsync + restart + nginx no-cache для `/identika/`).

Не коммитить и не вписывать реальные секреты в документацию.

## Что было решено по продукту

- Цель - клон/аналог визуального направления АИдентика для WB product card generator.
- Внутреннюю валюту/балансы не делать.
- Главный сценарий: продавец выбирает товар WB, добавляет brief, получает 10 слайдов и rich-пакет.
- Секреты WB/B2B/OpenRouter не должны уходить в результат, ZIP, manifest или логи.
- UI должен быть похож на современный личный кабинет Aidentika, с реальными локальными метриками, а не декоративной промо-страницей.

## Известные ограничения

- WB upload зависит от внешнего WB Tool на `:8765`; без него redirect `?upload=error`.
- OpenRouter image generation требует API key и может частично падать → fallback на programmatic SVG с warning.
- Async jobs (BackgroundTasks) включаются только при `openrouter` + `IDENTIKA_ENABLE_AI_IMAGES`; mock остаётся синхронным.
- Browser/UI visual regression tests не автоматизированы.

## Рекомендуемый следующий шаг

1. Согласовать финальный контракт WB Tool upload с реальной загрузкой медиа на маркетплейс.
2. Прогнать end-to-end с живым OpenRouter и замерить latency/cost на 10 image calls.
3. Добавить browser visual regression для create/job flow.

## Правила для следующего инструмента

- Не удалять существующие `data/identika.sqlite` и `assets/`.
- Не переписывать проект с нуля: текущая архитектура уже рабочая.
- Сохранять plain local MVP: mock mode должен всегда работать без внешних сервисов.
- Любое подключение внешних провайдеров делать опциональным через env.
- Перед изменением storage/schema добавить миграционную логику или совместимость со старой БД.
- После изменений запускать `pytest`; для UI-изменений дополнительно проверять браузером desktop и mobile ширину.
