# Структура проекта

Полное дерево файлов под git (`git ls-files`), с пояснениями. Курс на
«что и почему» — в [CLAUDE.md](CLAUDE.md) (структура верхнего уровня,
правила слоёв) и [SPEC.md](SPEC.md) (полное техзадание). Этот файл — просто
актуальный снимок дерева, обновляется по мере роста проекта.

```
.
├── .env.example              — шаблон переменных окружения, .env в git не попадает
├── .gitattributes
├── .gitignore
├── CLAUDE.md                 — правила разработки, структура, стиль
├── SPEC.md                   — полное техническое задание
├── TODO.md                   — открытые задачи между заходами
├── README.md                  — этот же обзор, но для стороннего читателя
├── STRUCTURE.md               — этот файл
├── pyproject.toml            — зависимости, ruff, pytest
├── manage.ps1                — управление процессом бота на машине разработчика
├── manage.bat                — двойной клик → manage.ps1 dashboard
│
├── bot/                       — приложение
│   ├── main.py                — точка входа, сборка приложения, регистрация роутеров
│   ├── config.py               — настройки из окружения (pydantic-settings)
│   ├── lock.py                 — блокировка «один процесс на .env» (single_instance)
│   ├── util.py                 — мелкие общие помощники (время UTC, маскирование секретов)
│   │
│   ├── handlers/               — роутеры aiogram, только UI-слой
│   │   ├── connect.py           — /start, /connect_xbox, /disconnect_xbox
│   │   ├── panel.py             — личная панель пользователя, «Мои чаты»
│   │   ├── admin.py             — админ-панель (/admin), стереть сообщения бота за 24ч
│   │   ├── chat.py              — команды группового чата: /subscribe, /stats,
│   │   │                          /online, /who, /recent, /summary, /delete_last, хаб группы
│   │   ├── hltb.py              — /hltb, поиск времени прохождения (SPEC 6.6)
│   │   ├── steam.py             — /connect_steam, /disconnect_steam (M-Steam-1)
│   │   └── keyboards.py         — инлайн-клавиатуры, общие для панелей и онбординга
│   │
│   ├── services/                — бизнес-логика, не знает про Telegram/aiogram
│   │   ├── achievements.py       — фильтрация ачивок, формирование сообщений, platform_tag
│   │   ├── connect.py            — одноразовый state для OAuth, завершение входа
│   │   ├── stats.py              — агрегаты для панелей, /stats, итога дня
│   │   ├── models.py             — ParsedAchievement/Platform, общие для Xbox и Steam (M-Steam-2a.5)
│   │   ├── tables.py             — общий рендерер моноширинных таблиц (SPEC 1.6)
│   │   ├── hltb.py               — обёртка над howlongtobeatpy, кэш в hltb_cache
│   │   ├── message_log.py        — request-мидлварь: лог исходящих сообщений в группы
│   │   ├── notify.py             — уведомления администратору
│   │   ├── crypto.py             — шифрование refresh-токенов (Fernet)
│   │   ├── xbox/                 — всё про Xbox Live, ничего про Telegram
│   │   │   ├── auth.py            — обёртка над xbox-webapi-python: токены, обновление
│   │   │   ├── client.py          — запросы к Xbox Live, лимитер, retry, backoff
│   │   │   └── models.py          — pydantic-модели ответов (в т.ч. контракт 4 с редкостью)
│   │   └── steam/                — официальный Steam Web API, без OAuth (M-Steam-1, верифицирован вживую)
│   │       ├── client.py          — резолв профиля, видимость, презенс, ачивки, редкость
│   │       └── achievements.py    — fetch_unlocked() + кэш схемы/редкости (M-Steam-2b)
│   │
│   ├── poller/                  — фоновые задачи (APScheduler)
│   │   ├── scheduler.py           — тики, сборка джобов
│   │   ├── cadence.py             — общая математика интервалов/дебаунса для обоих presence-поллеров
│   │   ├── presence.py            — шаг 1: Xbox presence, интервалы по состоянию
│   │   ├── steam_presence.py      — Steam-презенс, тот же шаг 1, свой batch-запрос (M-Steam-2c)
│   │   ├── fetcher.py             — шаг 2: ачивки Xbox по игре, история игр, бэкфил
│   │   ├── steam_fetcher.py       — шаг 2: ачивки Steam по игре, бэкфил при привязке (M-Steam-2d)
│   │   ├── publisher.py           — шаг 3: публикация, дайджест, очередь Telegram
│   │   ├── daily.py               — ежедневный итог + /summary по требованию
│   │   └── reminders.py           — напоминания о протухшем входе (SPEC 5.1.1)
│   │
│   ├── web/
│   │   └── oauth.py               — aiohttp-колбэк Microsoft
│   │
│   └── db/
│       ├── schema.sql             — полная DDL для новой базы (SPEC, раздел 3)
│       ├── repo.py                — весь доступ к данным, единственное место с SQL
│       └── migrations/            — по одному файлу на изменение схемы, применяются по порядку
│           ├── 001_daily_reports.sql
│           ├── 002_rarity_hidden.sql            — third rarity_mode state (сейчас — no-op, колонка убрана)
│           ├── 003_chat_seen.sql                — кто писал в чате, не только подписчики публикации
│           ├── 004_hltb_cache.sql
│           ├── 005_bot_messages.sql             — лог исходящих сообщений бота (для очистки чата)
│           ├── 006_hltb_platforms.sql           — платформы игры в hltb_cache
│           ├── 007_platform_links.sql           — привязка Steam/PSN аккаунтов (M-Steam-1)
│           ├── 008_chat_overrides.sql           — свои порог редкости/время итога на чат
│           ├── 009_chat_settings_mandatory.sql  — обязательные per-chat настройки, без общего fallback
│           ├── 010_secret_achievements.sql      — is_secret на seen_achievements
│           ├── 011_seen_achievements_tg_id.sql  — ключ по tg_id, не по xuid (M-Steam-2a)
│           ├── 012_hltb_url_and_image.sql
│           ├── 013_steam_achievement_cache.sql  — кэш схемы/редкости Steam (M-Steam-2b)
│           ├── 014_steam_presence_state.sql     — presence-таблица Steam (M-Steam-2c)
│           ├── 015_drop_show_x360.sql           — show_x360 исчезает, общий rarity_mode (M-Steam-2e)
│           ├── 016_rarity_mode_per_chat.sql     — rarity_mode: subscriptions, не user_settings
│           ├── 017_steam_presence_grace.sql     — last_active_* для grace-периода поллера
│           └── 018_titles_icon_url.sql          — обложка игры как иконка для ачивок Xbox 360
│
├── scripts/                    — вспомогательные скрипты вне приложения, разовые/ручные
│   ├── db_status.py              — сводка по базе для `manage.ps1 status` (без зависимостей)
│   ├── reconcile_achievements.py — разовый полный бэкфил истории ачивок
│   └── backfill_hltb_platforms.py — разовое дозаполнение platforms в уже закэшированных играх
│
├── tests/                      — pytest + pytest-asyncio, реальные запросы запрещены
│   ├── conftest.py                — фикстуры (репозиторий на временной базе и т.п.)
│   ├── test_auth.py               — обновление токена, сериализация, invalid_grant
│   ├── test_client.py             — Xbox-клиент: лимитер, retry, парсинг ответов
│   ├── test_models.py             — pydantic-модели ответов Xbox Live
│   ├── test_crypto.py             — шифрование токенов
│   ├── test_lock.py               — блокировка одного процесса
│   ├── test_filters.py            — фильтры публикации (редкость, Xbox 360, мьюты), format_single/digest
│   ├── test_poller.py             — дедуп, бэкфил, догон после простоя, исключённые юзеры
│   ├── test_cadence.py            — общая математика интервалов/дебаунса (poller/cadence.py)
│   ├── test_presence_title.py     — резолв названия игры из presence (Xbox)
│   ├── test_stats.py              — счётчики, окна «сегодня»/«за месяц»
│   ├── test_stats_display.py      — гeймерскор из профиля, лимит игр и кнопка «показать все»
│   ├── test_tables.py             — рендерер таблиц, обрезка длинных имён
│   ├── test_daily.py              — ежедневный итог, лимит строк и «показать всех»
│   ├── test_recent.py             — /recent как таблица
│   ├── test_chat_online.py        — /online, hub-клавиатура, chat_seen
│   ├── test_keyboards.py          — клавиатуры панели, цикл редкости
│   ├── test_panel.py              — личная панель, «Мои чаты»
│   ├── test_user_chats.py         — «Мои чаты»: список, подписка/отписка, rarity_mode по чату
│   ├── test_admin.py              — форматирование лимитов API, границы настроек
│   ├── test_connect_service.py    — сквозной проброс origin_chat_id при входе
│   ├── test_connect_payload.py    — разбор deep-link пейлоада /start
│   ├── test_notify.py             — уведомления администратору
│   ├── test_message_log.py        — лог сообщений бота, фильтр по типу чата, last_bot_message
│   ├── test_hltb.py               — поиск/кэш HLTB, пагинация, очистка запроса
│   ├── test_steam.py              — разбор ссылки/ID, привязка аккаунта (M-Steam-1)
│   ├── test_steam_achievements.py — fetch_unlocked(), кэш схемы/редкости Steam (M-Steam-2b)
│   ├── test_steam_fetcher.py      — опрос ачивок Steam по игре, бэкфил (M-Steam-2d)
│   ├── test_steam_presence.py     — Steam-презенс, финальный опрос, grace-период
│   ├── test_rate_limiter.py       — снимок использования лимитера без учёта самого себя
│   └── test_util.py               — маскирование секретов, форматирование чисел
│
├── data/                        — bot.db, в git не попадает
└── logs/                        — bot.log, bot.err.log, в git не попадает
```
