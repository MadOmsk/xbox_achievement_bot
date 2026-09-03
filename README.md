# Xbox Achievement Bot

Telegram-бот, который публикует ачивки Xbox участников чата: фильтр по
редкости, личная статистика, ежедневный итог дня, админ-панель, поиск
времени прохождения игр через HowLongToBeat. Некоммерческий проект на
20–30 человек.

Полное техническое задание — в [SPEC.md](SPEC.md), правила разработки и
структура кода — в [CLAUDE.md](CLAUDE.md), дерево файлов с пояснениями —
в [STRUCTURE.md](STRUCTURE.md), текущие открытые задачи — в [TODO.md](TODO.md).

## Стек

Python 3.12+, aiogram 3, xbox-webapi-python, httpx, aiohttp, aiosqlite,
APScheduler, pydantic v2, cryptography (Fernet), howlongtobeatpy.

## Что нужно до старта

Без этих трёх вещей бот не запустится или не сможет пускать людей через
Xbox-логин — готовятся один раз, до первого запуска:

1. **Токен Telegram-бота** — создать у [@BotFather](https://t.me/BotFather),
   получить `BOT_TOKEN`.
2. **Регистрация приложения на [portal.azure.com](https://portal.azure.com)
   — обязательна**, без неё Xbox-логин не работает вообще. Нужны:
   scope `XboxLive.signin XboxLive.offline_access`, redirect URI — тот же
   адрес, что в `OAUTH_REDIRECT_URL` (см. пункт 3), из регистрации берутся
   `AZURE_CLIENT_ID` и `AZURE_CLIENT_SECRET`.
3. **Домен с HTTPS**, куда указывает `OAUTH_REDIRECT_URL` (например
   `https://ваш-домен/auth/callback`). Microsoft принимает в качестве
   redirect URI только `https://`-адрес — ни `localhost`, ни голый `http://`
   не подходят. Для локальной разработки — свой домен на туннеле вроде
   Cloudflare Tunnel (даёт `https://` сразу); в проде — обычный домен с
   сертификатом (nginx + Let's Encrypt/certbot, как на боевом сервере этого
   проекта).

## Локальная разработка

```bash
git clone <репозиторий>
cd xbox_achievement_bot
python -m venv .venv
.venv\Scripts\pip install -e .[dev]

copy .env.example .env
# заполнить BOT_TOKEN, AZURE_CLIENT_ID/SECRET, OAUTH_REDIRECT_URL, FERNET_KEY
```

`FERNET_KEY` — сгенерировать:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Дальше процессом на своей машине управляет `manage.ps1` (бот сам себя
запустить не может):

```powershell
.\manage.ps1 start | stop | restart | status | logs [-Lines N]
.\manage.ps1 dashboard   # живой статус с автообновлением и горячими клавишами
```

Двойной клик по `manage.bat` открывает тот же dashboard. Подробности —
в разделе «Запуск» [CLAUDE.md](CLAUDE.md).

Тесты и линт:

```bash
pytest
ruff check . && ruff format --check .
```

## Продакшен

Бот развёрнут на VPS под systemd (юнит `xbox-bot.service`, отдельный
непривилегированный пользователь), за nginx с Let's Encrypt. На боевом
сервере `manage.ps1` не используется — им управляет systemd:

```bash
systemctl {start|stop|restart|status} xbox-bot
journalctl -u xbox-bot -f
```

Деплой — `git pull` в `/opt/xbox_achievement_bot` от имени сервисного
пользователя, затем `systemctl restart xbox-bot`. Детали инфраструктуры —
в разделе «Запуск» [CLAUDE.md](CLAUDE.md).

**`manage.ps1` на домашнем ПК и боевой сервер не запускаются одновременно**
— два процесса с одним `BOT_TOKEN` конфликтуют за обновления Telegram.
`manage.ps1` — инструмент для локальной разработки, а не пережиток: он
никуда не делся и нужен ровно для того же, для чего был нужен раньше.
