# Antigravity Handoff: Identika WB AI

Дата среза: 2026-06-02  
Рабочая папка: `/Users/home/Downloads/Identika`  
Статус: рабочий MVP в стиле кабинета, с API/UI флоу generate → approve → export/rich-export → upload.

## Назначение и scope

Identika WB AI - локальный FastAPI-сервис для генерации медиа карточки Wildberries:

- 10 слайдов по контексту товара: SVG остаётся для UI-preview, скачиваемый export содержит PNG 900x1200.
- Rich-пакет (PNG-блоки 1440x900 + PDF preview + отдельный rich ZIP; HTML остаётся только внутренним preview в UI).
- ZIP-экспорт финального пакета с `manifest.json`.
- История jobs и ассетов в SQLite + файловом хранилище.
- UI-кабинет: dashboard, настройки, создание, страница job.

По умолчанию работает в `mock`, без внешних зависимостей.

## Карта архитектуры

- **Backend core**
  - `app/identika/app.py` - сборка FastAPI, шаблоны, статика, middleware.
  - `app/identika/api/routes.py` - HTML + JSON эндпоинты, no-cache для динамики.
  - `app/identika/models.py` - Pydantic-модели job/result/slide/rich/export.
  - `app/identika/storage.py` - SQLite + asset storage, удаление jobs.
- **Services**
  - `app/identika/services/jobs.py` - оркестрация generation/approve/re-render/export/upload.
  - `app/identika/services/rendering.py` - рендер SVG/HTML/PDF/ZIP и профили quality (`preview`/`final`).
  - `app/identika/services/product_images.py` - загрузка фото товара, warnings, fallback.
  - `app/identika/services/uploads.py` - валидация multipart фото.
  - `app/identika/services/wb_tool.py` и `wb_cdn.py` - интеграция WB Tool/CDN.
- **Providers**
  - `app/identika/providers/mock.py` - офлайн генерация.
  - `app/identika/providers/openrouter.py` + `image_gen.py` + `prompts.py` - OpenRouter text/image слой.
- **UI/templates**
  - `app/identika/templates/index.html` - dashboard.
  - `app/identika/templates/create.html` - создание + upload/режим без фото.
  - `app/identika/templates/job.html` - approve/export/rich/upload/delete + редактирование слайдов.
  - `app/identika/static/app.css` - дизайн-система кабинета.
- **Tests**
  - `tests/test_api.py`, `tests/test_generation.py`, `tests/test_rebrand_settings.py`, `tests/test_source_photos.py` и др.

## Ключевые маршруты и workflow

### HTML/UI

- `GET /` - dashboard.
- `GET /create` - запуск генерации.
- `GET /jobs/{job_id}` - рабочая страница job.
- `GET /settings`, `POST /settings`, `POST /settings/test` - провайдер/ключ/модели.
- `POST /jobs/{job_id}/delete` - удаление job.
- `POST /jobs/{job_id}/slides/{slide_index}/text` + `/reset` - правка/сброс текста.
- `POST /jobs/{job_id}/slides/{slide_index}/image/clear` - очистка изображения слайда.
- `POST /jobs/{job_id}/source-images` - дозагрузка фото в существующий job.
- `POST /jobs/{job_id}/upload-to-wb` - отправка approved пакета в WB Tool.

### JSON/API

- `GET /health`.
- `POST /v1/uploads/source-images`.
- `POST /v1/generation/jobs`, `GET /v1/generation/jobs`, `GET /v1/generation/jobs/{job_id}`.
- `GET /v1/generation/jobs/{job_id}/result`.
- `PATCH /v1/generation/jobs/{job_id}/result/text`.
- `POST /v1/generation/jobs/{job_id}/source-images`.
- `POST /v1/generation/jobs/{job_id}/re-render`.
- `POST /v1/generation/jobs/{job_id}/approve`.
- `GET /v1/generation/jobs/{job_id}/export`.
- `GET /v1/generation/jobs/{job_id}/rich-export`.
- `GET /v1/assets/{asset_id}`.

### Бизнес-флоу

1. **Generate**: создать job через `/create` или `/v1/generation/jobs` (+ фото или режим без фото).  
2. **Preview/Edit**: отредактировать тексты/картинки, rerender.  
3. **Approve**: `POST /v1/generation/jobs/{job_id}/approve` переключает профиль качества в `final`.  
4. **Export**: `export` и `rich-export` доступны после approve.  
5. **Upload**: `POST /jobs/{job_id}/upload-to-wb`; при `501` во внешнем WB Tool переход в staging-сценарий.

## Прод/деплой и caveats

- Текущий прод: `https://eurasia-transline.online/identika/` (nginx reverse proxy).
- Деплой: `SSHPASS=... ./scripts/deploy_vps.sh` (rsync, reinstall, restart systemd, правка nginx).
- Важное:
  - Нужен `IDENTIKA_PUBLIC_BASE_PATH=/identika`.
  - Динамика должна быть без кеша (`proxy_no_cache`, `proxy_cache_bypass` + app headers `no-store`).
  - Используется общий Basic Auth WB Tool; отдельный `IDENTIKA_UI_PASSWORD` обычно не задается.
  - `scripts/deploy_vps.sh` использует `SSHPASS`; держать только в env, не коммитить.

## Свежие крупные изменения (актуально для этой ветки)

- UI-редизайн кабинета (dashboard/create/job) и улучшенная навигация действий.
- Добавлены delete-действия для job и действия редактирования/сброса контента слайдов.
- Guard для генерации без фото и явные подсказки/валидации при загрузке source images.
- Выделен rich-export (`/v1/generation/jobs/{job_id}/rich-export`) и rich-зона в UI.
- Финализация качества: после approve используется `final` quality profile для экспорта.

## Известные проблемы, TODO и ближайшие приоритеты

- Внешний `POST /api/ai/jobs/{id}/upload` в WB Tool может возвращать `501`; upload ограничен staging-сценарием.
- Нужен полноценный E2E с реальным OpenRouter (latency/cost/стабильность изображений).
- Нужны стабильные UI visual regression tests (desktop + mobile).
- Подчистить и стабилизировать текущие изменения в рабочих файлах перед релизом.

## Локальный запуск и тесты

```bash
cd /Users/home/Downloads/Identika
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
uvicorn identika.app:create_app --factory --app-dir app --host 127.0.0.1 --port 8787
```

Проверка:

```bash
curl http://127.0.0.1:8787/health
pytest
```

Docker:

```bash
docker compose up --build
```

## Правила для следующего ИИ

- Не удалять `data/identika.sqlite` и `assets/`.
- Не коммитить секреты (`.env`, API keys, пароли, `SSHPASS`).
- Не ломать offline `mock` сценарий.
- Изменения storage/schema делать с совместимостью по старым данным.
- После кода прогонять `pytest`; после UI-правок проверять в браузере.
