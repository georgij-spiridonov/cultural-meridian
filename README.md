# Культурный Меридиан — Афиша культуры Башкортостана

Информационный сервис для жителей и гостей Республики Башкортостан: поиск культурных мероприятий, планировщик «Выходной день», ИИ-ассистент в Telegram, открытый версионированный REST API, встраиваемый виджет и автоматизированный конвейер сбора и проверки афиш.

- **Выбранный уровень по ТЗ:** **Уровень 4 «Контур» (100 базовых баллов)** с кумулятивным выполнением требований Уровней 1 («Бот»), 2 («Сайт»), 3 («Агент») и 4 («Контур»).
- **Официальный дисклеймер (ТЗ п. 8.2, 11):** проект работает в изолированном контуре, взаимодействует с внешними порталами только через открытые интерфейсы (отдельный поддомен, JSON API `/api/v1`, виджет `/widget` / `/widget.js`), является информационным сервисом и **не подменяет официальный портал Министерства культуры Республики Башкортостан**, не внося изменений в действующие системы министерства.

---

## 1. Матрица соответствия требованиям ТЗ (Уровни 1–4)

| Уровень | Требование ТЗ | Реализация в проекте | Модули и эндпоинты |
| :--- | :--- | :--- | :--- |
| **1. «Бот»** | Telegram-бот, поиск, карточка, подписка, импорт, ошибки | Меню на инлайн-кнопках, фильтры по городу/категории/дате/цене, пагинация, `/start`, `/help`, `/cancel`, `/subscribe`, `/unsubscribe`, `/plan` | `src/entrypoints/telegram_bot/`, `src/entrypoints/command_line/seed.py` |
| **2. «Сайт»** | Адаптивный веб-каталог, REST API + OpenAPI 3.1, виджет, админка, дедупликация | SSR + HTMX каталог (от 360 px), URL-фильтры, форма обратной связи с honeypot, `/admin` (импорт CSV/JSON, модерация, аудит), `/api/v1/*` с ETag, Shadow DOM виджет, сведение дублей по `dedup_key` | `src/entrypoints/web/{pages,api,widget,administration}.py`, `/docs`, `/redoc`, `/openapi.json`, `/health`, `/metrics` |
| **3. «Агент»** | Свободный запрос, инструменты, память ≥ 5 реплик, уточнения, трассировка, резервный режим | OpenAI Agents SDK + 7 формальных инструментов, история диалога (`TELEGRAM_DIALOG_HISTORY_LIMIT=10`), пост-валидация UUID (`allowed_event_ids`), трассы в `agent_traces`, автопереключение на `FallbackSearchService` | `src/application/agent_dialog.py`, `src/infrastructure/language_model/`, `src/application/fallback_search.py` |
| **4. «Контур»** | Сбор из внешних источников, планировщик «Выходной день», оценка качества, защита и лимиты | Воркер сбора RSS/JSON/HTML/локальных фидов + очередь модерации, планировщик маршрута 2–4 событий без пересечений + гаверсинус/OSRM, 80 контрольных кейсов (70+10), защита от инъекций, лимиты шагов/бюджета/частоты | `src/entrypoints/background_worker/runner.py`, `src/application/weekend_planner.py`, `src/entrypoints/command_line/evaluate.py`, `evaluation/` |

---

## 2. Быстрый старт за ≤ 10 минут

### Вариант А. Запуск одной командой в Docker Compose (рекомендуется)

Требуется только установленный Docker / Docker Compose. На чистой машине **даже без файла `.env`** стек запускается в безопасном рабочем режиме (Telegram-бот в режиме ожидания без падений, поиск работает через детерминированный резервный контур).

1. (Опционально) Создайте `.env` из шаблона для подключения ключей OpenRouter и Telegram:
   ```bash
   cp .env.example .env
   ```
2. Поднимите весь стек одной командой:
   ```bash
   docker compose up -d --build
   ```
   Compose автоматически:
   - запустит `database` (`postgres:17-alpine`, часовой пояс `Asia/Yekaterinburg`) и дождётся `healthy`;
   - выполнит одноразовый init-сервис `migrator` (`alembic upgrade head && python -m src.entrypoints.command_line.seed` — повторный запуск идемпотентен и не создаёт дублей);
   - поднимет `web_api` (FastAPI + Uvicorn на порту `8000` со встроенным `healthcheck`), `telegram_bot` и `background_worker`.
3. Проверьте готовность:
   ```bash
   curl http://localhost:8000/health
   ```

**Основные адреса после запуска:**
- Веб-каталог: `http://localhost:8000/`
- Планировщик «Выходной день»: `http://localhost:8000/planner`
- Обзор сценариев ассистента: `http://localhost:8000/assistant`
- Конструктор встраиваемого виджета: `http://localhost:8000/widget/demo`
- Панель модерации (демо-вход по `.env.example`: `moderator` / `meridian_admin_dev`): `http://localhost:8000/admin`
- OpenAPI 3.1 (Swagger UI / ReDoc / JSON): `http://localhost:8000/docs`, `http://localhost:8000/redoc`, `http://localhost:8000/openapi.json`
- Наблюдаемость: `http://localhost:8000/health`, `http://localhost:8000/metrics`

### Вариант Б. Локальный запуск для разработки (`uv`)

Требования: Python 3.14+, [`uv`](https://docs.astral.sh/uv/), PostgreSQL 17.

```bash
uv sync --locked
docker compose up -d database
uv run alembic upgrade head
uv run python -m src.entrypoints.command_line.seed
```

Отдельные процессы (в других терминалах при необходимости):
```bash
uv run uvicorn src.entrypoints.web.application:create_default_application --factory --host 0.0.0.0 --port 8000
uv run python -m src.entrypoints.telegram_bot.runner
uv run python -m src.entrypoints.background_worker.runner
```

---

## 3. Архитектура, ИИ-агент и защита

Подробное описание слоёв (`domain`, `application`, `infrastructure`, `entrypoints`), схемы БД и правил разработки приведено в [ARCHITECTURE.md](ARCHITECTURE.md).

### Контракт ИИ-агента и 7 инструментов (`src/infrastructure/language_model/tools.py`)
1. `search_events` — фильтрация по городу (с учётом опечаток и словоформ), категории, периоду/датам, времени начала, цене, бесплатному входу, возрастному цензу, длительности, языку и близости к центру.
2. `get_event` — полная карточка события по UUID (включая пометку об отмене, если событие отменено).
3. `find_similar` — подбор похожих опубликованных мероприятий.
4. `get_venue_schedule` — афиша конкретной культурной площадки.
5. `plan_weekend` — вызов планировщика «Выходной день» с расчётом маршрута и рекомендациями по смягчению ограничений.
6. `prepare_subscribe` — подготовка отложенного подтверждения (`pending_actions`) на оформление подписки.
7. `prepare_feedback` — подготовка отложенного подтверждения (`pending_actions`) на отправку обращения организатору.

### Механизмы достоверности и безопасности
- **Запрет выдумок (0 галлюцинаций):** пост-валидация всех UUID ответа по множеству `allowed_event_ids`, собранному из фактических результатов инструментов текущего хода; при расхождении ответ блокируется/пересобирается.
- **Изоляция недоверенных данных:** описания и названия событий из внешних источников передаются модели строго в тегах `<untrusted_event_data>...</untrusted_event_data>` с проверкой утечки системного промпта (`contains_system_prompt_leak`).
- **Двухшаговое подтверждение:** изменяющие действия (`prepare_subscribe`, `prepare_feedback`) не выполняются из текста напрямую — создаётся `pending_action` со сроком жизни `TELEGRAM_PENDING_ACTION_LIFETIME_SECONDS=600`, исполняемый только после явного подтверждения пользователем.
- **Лимиты и трассировка:** не более `LANGUAGE_MODEL_MAX_AGENT_STEPS=5` вызовов инструментов за ход; для каждого ответа в таблице `agent_traces` фиксируются запрос, план, инструменты, аргументы, UUID оснований, задержка, токены и стоимость.
- **Автоматический резервный режим:** при отсутствии ключа, сбое провайдера или исчерпании дневного бюджета (`llm_usage_daily`) сервис автоматически переключается на `FallbackSearchService` с понятным уведомлением пользователю.

### Планировщик «Выходной день» и сбор данных
- **Геолокация (`src/infrastructure/geolocation/`):** локальный справочник `configuration/venues_coordinates.yaml` (**18 городов и 44 площадки** Башкортостана) + `HybridGeolocationResolver` (локальный каталог → опционально Nominatim/OSRM при `GEOLOCATION_EXTERNAL_ENABLED=true` → резервный расчёт по формуле гаверсинуса со средней скоростью `25 км/ч` и `7 мин` на пересадку). При несовместимости ограничений планировщик предлагает конкретное минимальное ослабление (сдвинуть окно, поднять бюджет, увеличить время на дорогу).
- **Сбор данных (`configuration/sources.yaml`, `src/application/event_ingestion.py`):** 8 настроенных источников (RSS, JSON, HTML, локальные фиды `data/local_feeds/`). Сырые записи сохраняются в `raw_events` отдельно от нормализованных для повторного переигрывания, дедупликация сводит дубли с сохранением всех источников в `event_sources`, а записи с битой ссылкой или уверенностью ниже `INGESTION_CONFIDENCE_THRESHOLD=0.75` направляются в очередь модерации `/admin`.

---

## 4. Модель, расчёт стоимости диалога и переключение провайдера

- **Провайдер и модели:** OpenAI Agents SDK поверх OpenRouter (`LANGUAGE_MODEL_BASE_URL=https://openrouter.ai/api/v1`).
  - Основная модель (`LANGUAGE_MODEL_PRIMARY`): `google/gemini-4-hydrogen` — разбор многокритериальных ограничений, планирование вызовов инструментов.
  - Быстрая модель (`LANGUAGE_MODEL_FAST`): `google/gemini-3.5-flash-lite` — двухэтапный синтез финального ответа (`LANGUAGE_MODEL_USE_FAST_SYNTHESIS_MODEL=true`) и гибридное дозаполнение сложных полей при сборе (`INGESTION_HYBRID_LLM_ENABLED=true`).
  - **Смена провайдера или модели:** выполняется заменой `LANGUAGE_MODEL_BASE_URL`, `LANGUAGE_MODEL_API_KEY` и `LANGUAGE_MODEL_PRIMARY` / `LANGUAGE_MODEL_FAST` в `.env` без изменения кода.

### Формула расчёта стоимости (`src/infrastructure/language_model/cost_calculator.py`)
$$\text{cost}_{\text{RUB}} = \frac{\text{tokens}_{\text{in}}}{1\,000\,000} \cdot \text{tariff}_{\text{in}} + \frac{\text{tokens}_{\text{out}}}{1\,000\,000} \cdot \text{tariff}_{\text{out}}$$

Тарифы по умолчанию из `LANGUAGE_MODEL_PRICING_JSON` (в рублях за 1M токенов):
- `google/gemini-4-hydrogen`: `input = 135.0 ₽`, `output = 675.0 ₽`
- `google/gemini-3.5-flash-lite`: `input = 24.0 ₽`, `output = 200.0 ₽`

Дневные бюджетные квоты: `LANGUAGE_MODEL_USER_DAILY_BUDGET_RUBLES=15.0` на пользователя, `LANGUAGE_MODEL_GLOBAL_DAILY_BUDGET_RUBLES=250.0` на весь сервис.

**Фактические живые замеры через OpenRouter (`google/gemini-4-hydrogen` + `google/gemini-3.5-flash-lite`):**
- Точечная выборка (3 запроса `external_holdout.yaml`): средняя стоимость **`1.0924 ₽/ход`**, задержка p50 / p95 **`3887 / 4217 мс`** (`≤ 6000 мс`), pass rate `100.0% (3/3)`.
- Полный прогон `external_holdout.yaml` (10 запросов, `evaluation/reports/baseline_live_report.md`): средняя стоимость **`1.1052 ₽/ход`** (`11.0516 ₽` всего, в пределах порога Уровня 4 `≤ 3.00 ₽`), задержка p50 / p95 **`4033 / 5397 мс`**, pass rate `90.0% (9/10)` (`0` галлюцинаций, `0` утечек отмен).

---

## 5. Бенчмарки и отчёт оценки качества (ТЗ раздел 9)

В репозитории содержатся два независимых набора контрольных запросов:
- `evaluation/golden_queries.yaml` — **70** запросов (простые, многокритериальные, опечатки и разговорные формулировки, площадки/похожие, планировщик, честный отказ и отмены, многошаговые сессии ≥ 5 реплик, подтверждения, 13 кейсов инъекций);
- `evaluation/external_holdout.yaml` — **10** изолированных внешних запросов.

### Сводка воспроизводимых замеров (код текущей ветки)

| Метрика (порог Уровня 4) | Offline Golden (70) | Offline Holdout (10) | Offline Worst-of-two | Offline Baseline (80) | Live OpenRouter (10, факт) | Live OpenRouter (80, проекция) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Pass rate** | **100.0%** (70/70) | **100.0%** (10/10) | **100.0%** | 16.2% (13/80) | **90.0%** (9/10) | ~90–95% |
| **Точность выдачи** (`catalog_accuracy`) | 87.8% | 91.7% | 87.8% | 44.2% | **86.7%** | ~87–95% |
| **Полнота** (`recall_at_k`, ≥ 90%) | **99.3%** | **100.0%** | **99.3%** | 22.5% | **90.0%** | ~90–95% |
| **Выдуманные UUID** (0) | **0** | **0** | **0** | 0 | **0** | **0** |
| **Утечка отмен** (0) | **0** | **0** | **0** | 0 | **0** | **0** |
| **Устойчивость к инъекциям** (100%) | **100.0%** (13/13) | — | **100.0%** | 100.0% | **100.0%** (1/1) | **100.0%** |
| **Точность инструментов** (`tool_match_rate`, ≥ 92%) | **100.0%** | **100.0%** | **100.0%** | 21.2% | **100.0%** | **100.0%** |
| **Безопасность подтверждений** (100%) | **100.0%** (4/4) | — | **100.0%** | 0.0% (0/4) | — | **100.0%** |
| **Задержка p50 / p95** (≤ 6000 мс) | 42 / 82 мс | 50 / 76 мс | 50 / 82 мс | 8 / 15 мс | **4033 / 5397 мс** | ~4.0 / 5.4 с |
| **Стоимость / ход** (≤ 3.00 ₽) | 0.0000 ₽ | 0.0000 ₽ | 0.0000 ₽ | 0.0000 ₽ | **1.1052 ₽** (11.05 ₽/10) | **~1.11 ₽** (~88.4 ₽/80) |

> **Аналитическая заметка (офлайн-режим и проекция онлайн-запуска):**
> 1. **Проекция на 80 онлайн-запросов:** показатели столбца «Live OpenRouter (80, проекция)» являются аналитической проекцией, построенной на базе фактического живого прогона всех 10 изолированных запросов `external_holdout.yaml` (`evaluation/reports/baseline_live_report.md`: `90.0%` pass rate, `100.0%` `tool_match_rate`, `100.0%` защита от инъекций, `0` выдуманных UUID, `0` утечек отмен, `1.1052 ₽/ход`, p50 `4033 мс`, p95 `5397 мс`, единственный непройденный кейс `eh-004` из-за 0 карточек при опечатке/разговорной формулировке), а также точечной 3-запросной выборки (`100.0%` pass rate, `1.0924 ₽/ход`, p95 `4217 мс`). Такой подход даёт честную картину без лишнего расхода токенов на повторный прогон всех 70 базовых запросов.
> 2. **Офлайн-поправка:** в режиме `--mode offline` роль оркестратора выполняет детерминированный эвристический парсер `OfflineDeterministicAgentOrchestrator` (без LLM-NLU), из-за чего `catalog_accuracy` на сложных и неявных ограничениях составляет `87.8%` на `golden` (`88.2%` в среднем по 80 запросам). При использовании связки `google/gemini-4-hydrogen` + `google/gemini-3.5-flash-lite` сохраняются все детерминированные гарантии безопасности (`allowed_event_ids`, `guardrails`, `pending_actions`) при стоимости около `1.1 ₽` за ход.

### Команды воспроизведения
```bash
# Полный воспроизводимый офлайн-прогон (80 запросов, 0 ₽)
uv run python -m src.entrypoints.command_line.evaluate --mode offline --dataset all --seed

# Полный прогон на реальной модели через OpenRouter
uv run python -m src.entrypoints.command_line.evaluate --mode live --dataset all

# Полный прогон holdout-набора на реальной модели через OpenRouter (10 запросов, ~11 ₽)
uv run python -m src.entrypoints.command_line.evaluate --mode live --dataset holdout --max-budget-rubles 15.0
```
Отчёты сохраняются в `evaluation/reports/` в форматах `.json` и `.md`.

---

## 6. Записка о решении, ограничения и политика ПДн

### Почему выбран текущий стек и какие альтернативы отклонены
- **OpenAI Agents SDK + OpenRouter (`gemini-4-hydrogen` / `gemini-3.5-flash-lite`)** — строгие типизированные инструменты, простой контроль шагов и биллинга, смена модели одной строкой в `.env`.
- **PostgreSQL 17 (`pg_trgm` + полнотекстовый поиск)** — единое транзакционное хранилище для каталога, `raw_events`, `agent_traces`, `llm_usage_daily` и гибридного нечёткого поиска.
- **Отказ от `strict_json_schema=True` на `gemini-3.5-flash-lite`:** используется `AgentOutputSchema(..., strict_json_schema=False)` с локальной Pydantic-валидацией, чтобы избежать ошибок провайдера при опциональных полях.

### Границы применимости и известные ограничения
- Вне рамок сервиса: интеграция с билетными шлюзами и эквайрингом, мобильные нативные приложения, полный охват всех учреждений РБ без согласования источников.
- Интерфейс на башкирском языке и локализованный словарь поиска — отдельный этап развития (ТЗ раздел 14); сейчас поддерживается фильтрация по метаданному полю `language` («башкирский»).
- Офлайн-оценка качества (`--mode offline`) оценивает контур инструментов и безопасности детерминированно, без семантики LLM.

### Минимизация ПДн и сроки хранения (ТЗ п. 10)
- Таблица `subscriptions` хранит только технический идентификатор (`subscriber_identifier`, например ID чата Telegram или анонимный ключ веб-сессии), канал и фильтры (`city`, `category`) — **без телефонов, ФИО и домашних адресов**.
- Отписка (`/unsubscribe` в боте, кнопки в `/subscriptions` или `DELETE /api/v1/subscriptions/{id}`) выполняет **физическое удаление (`DELETE`)** записи из БД.
- Форма обратной связи защищена скрытым антиспам-полем `honeypot_website` и rate limiter (`WEB_FEEDBACK_RATE_LIMIT_PER_MINUTE=10`), а контактная подсказка (`contact_hint`) необязательна.
- Встраиваемый виджет использует Shadow DOM и работает **без сторонних cookies**.
- **Сроки хранения:** активные подписки хранятся до отписки пользователем; обезличенные диалоговые сессии, трассы `agent_traces` и технические журналы логов (не содержащие ПДн) подлежат операционной ротации (рекомендуемый срок хранения — 30–90 дней).

---

## 7. Открытый REST API и встраиваемый виджет

### Примеры запросов `curl`

```bash
# 1. Поиск бесплатных выставок в Уфе
curl -s "http://localhost:8000/api/v1/events?city=Уфа&category=exhibition&only_free=true&page_size=5"

# 2. Карточка события (поддерживает ETag / If-None-Match)
curl -i "http://localhost:8000/api/v1/events/<EVENT_UUID>"

# 3. Справочник городов (с ETag)
curl -s "http://localhost:8000/api/v1/references/cities"

# 4. Планировщик «Выходной день»
curl -s -X POST "http://localhost:8000/api/v1/weekend-plan" \
  -H "Content-Type: application/json" \
  -d '{"city":"Уфа","target_date":"2026-11-21","window_start_time":"11:00","window_end_time":"20:00","budget_rubles":2000,"companions_count":2,"maximum_travel_minutes":30}'

# 5. Оформление и физическое удаление подписки
curl -s -X POST "http://localhost:8000/api/v1/subscriptions" \
  -H "Content-Type: application/json" \
  -d '{"channel":"web","subscriber_identifier":"demo-visitor-1","city":"Уфа","category":"concert"}'

# 6. Отправка заявки организатором (honeypot_website должен быть пустым)
curl -s -X POST "http://localhost:8000/api/v1/feedback" \
  -H "Content-Type: application/json" \
  -d '{"channel":"web","message_text":"Просим добавить концерт симфонического оркестра в ГКЗ Башкортостан."}'

# 7. Наблюдаемость
curl -s "http://localhost:8000/health"
curl -s "http://localhost:8000/metrics"
```

### Встраивание виджета на сайт Министерства культуры РБ

**Через `<iframe>`:**
```html
<iframe
  src="http://localhost:8000/widget?city=Уфа&category=concert&period=weekend"
  title="Афиша культуры Башкортостана"
  style="width:100%;height:560px;border:0;border-radius:12px;"
  loading="lazy">
</iframe>
```

**Через изолированный JS-виджет (Shadow DOM, без конфликта стилей и без cookies):**
```html
<div id="cultural-meridian-widget"
     data-city="Уфа"
     data-category="concert"
     data-period="weekend"
     data-height="560px"></div>
<script src="http://localhost:8000/widget.js" defer></script>
```
