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
│   ├── config.py              — настройки из окружения (pydantic-settings)
│   ├── lock.py                 — блокировка «один процесс на .env» (single_instance)
│   ├── util.py                 — мелкие общие помощники (время UTC, маскирование секретов)
│   │
│   ├── handlers/               — роутеры aiogram, только UI-слой
│   │   ├── connect.py           — /start, /connect, /disconnect
│   │   ├── panel.py             — личная панель пользователя
│   │   ├── admin.py             — админ-панель (/admin)
│   │   ├── chat.py              — команды группового чата: /subscribe, /stats,
│   │   │                          /online, /who, /recent, /summary, хаб группы
│   │   ├── hltb.py              — /hltb, поиск времени прохождения (SPEC 6.6)
│   │   └── keyboards.py         — инлайн-клавиатуры, общие для панелей и онбординга
│   │
│   ├── services/                — бизнес-логика, не знает про Telegram/aiogram
│   │   ├── achievements.py       — фильтрация ачивок и формирование сообщений
│   │   ├── connect.py            — одноразовый state для OAuth, завершение входа
│   │   ├── stats.py              — агрегаты для панелей, /stats, итога дня
│   │   ├── tables.py             — общий рендерер моноширинных таблиц (SPEC 1.6)
│   │   ├── hltb.py               — обёртка над howlongtobeatpy, кэш в hltb_cache
│   │   ├── message_log.py        — request-мидлварь: лог исходящих сообщений в группы
│   │   ├── notify.py             — уведомления администратору
│   │   ├── crypto.py             — шифрование refresh-токенов (Fernet)
│   │   └── xbox/                 — всё про Xbox Live, ничего про Telegram
│   │       ├── auth.py            — обёртка над xbox-webapi-python: токены, обновление
│   │       ├── client.py          — запросы к Xbox Live, лимитер, retry, backoff
│   │       └── models.py          — pydantic-модели ответов (в т.ч. контракт 4 с редкостью)
│   │
│   ├── poller/                  — фоновые задачи (APScheduler)
│   │   ├── scheduler.py           — тики, сборка джобов
│   │   ├── presence.py            — шаг 1: presence, интервалы по состоянию
│   │   ├── fetcher.py             — шаг 2: ачивки по игре, история игр, бэкфил
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
│           ├── 002_rarity_hidden.sql       — third rarity_mode state, пересборка таблицы
│           ├── 003_chat_seen.sql           — кто писал в чате, не только подписчики публикации
│           ├── 004_hltb_cache.sql
│           ├── 005_bot_messages.sql        — лог исходящих сообщений бота (для очистки чата)
│           └── 006_hltb_platforms.sql      — платформы игры в hltb_cache
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
│   ├── test_filters.py            — фильтры публикации (редкость, Xbox 360, мьюты)
│   ├── test_poller.py             — дедуп, бэкфил, догон после простоя, исключённые юзеры
│   ├── test_stats.py              — счётчики, окна «сегодня»/«за месяц»
│   ├── test_stats_display.py      — гeймерскор из профиля, лимит игр и кнопка «показать все»
│   ├── test_tables.py             — рендерер таблиц, обрезка длинных имён
│   ├── test_daily.py              — ежедневный итог, лимит строк и «показать всех»
│   ├── test_recent.py             — /recent как таблица
│   ├── test_chat_online.py        — /online, hub-клавиатура, chat_seen
│   ├── test_keyboards.py          — клавиатуры панели, цикл редкости
│   ├── test_admin.py              — форматирование лимитов API, границы настроек
│   ├── test_connect_service.py    — сквозной проброс origin_chat_id при входе
│   ├── test_connect_payload.py    — разбор deep-link пейлоада /start
│   ├── test_notify.py             — уведомления администратору
│   ├── test_message_log.py        — лог сообщений бота, фильтр по типу чата
│   ├── test_hltb.py               — поиск/кэш HLTB, пагинация, очистка запроса
│   ├── test_rate_limiter.py       — снимок использования лимитера без учёта самого себя
│   └── test_util.py               — маскирование секретов, форматирование чисел
│
├── data/                        — bot.db, в git не попадает
└── logs/                        — bot.log, bot.err.log, в git не попадает
```
