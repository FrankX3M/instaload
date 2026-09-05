# instaload

Telegram-бот, который по ссылке присылает в чат видео и фото из Instagram (Reels, посты, карусели), TikTok и YouTube. Версия 2 — переработка под слабый VPS (1 vCPU, 512 МБ–1 ГБ RAM) по результатам аудита: **без Docker**, systemd + venv, очередь с одним воркером, yt-dlp и ffmpeg в подпроцессах, потоковая отправка файлов, автообновление yt-dlp, **cookies Instagram у каждого пользователя свои**.

Пользовательская инструкция по cookies — [docs/COOKIES.md](docs/COOKIES.md).

## Что изменилось по сравнению с v1

| Было | Стало |
|---|---|
| Docker-образ ~700 МБ, пересборка на сервере при каждом деплое | systemd-сервис, venv ~60 МБ + статический ffmpeg ~80 МБ + бинарник yt-dlp ~30 МБ; деплой = `git pull` + рестарт |
| yt-dlp и instaloader импортированы в процесс бота (RSS 50 МБ на старте, 120–200 МБ через сутки) | В процессе бота только `python-telegram-bot` (~30 МБ). yt-dlp — отдельный бинарник, instaloader — helper-скрипт; оба живут ровно столько, сколько идёт задача |
| Синхронные загрузки внутри `async`-хэндлера — бот «замирал» на время каждой загрузки | `asyncio.Queue` + воркер; yt-dlp/ffmpeg через `create_subprocess_exec` с таймаутом и `killpg`. Бот отвечает мгновенно, `/stats` работает во время загрузки |
| PTB 20.7 читает файл целиком в память при отправке (OOM, exit 137) | PTB 22.x, `InputFile(read_file_handle=False)` — httpx стримит файл с диска |
| Нарезка: отдельный `ffmpeg -ss` на каждую часть (N проходов по файлу) | Один проход `-f segment`, проверка размеров частей, дорезка при VBR |
| Селектор `best[height<=1080]` — прогрессивные форматы, по факту 360p | `bestvideo+bestaudio` с ремуксом (`-c copy`) и **фильтром по размеру**: сначала качество, которое влезет в 50 МБ целиком, потом нарезка. 1080p убран |
| Один общий файл cookies на всех | **Cookies у каждого пользователя свои** (`/cookies`), права 0600, используются только для его ссылок; ротация записывается обратно |
| Обновление yt-dlp = пересборка образа | systemd-таймер раз в сутки: `yt-dlp --update-to`, smoke-тест, откат при регрессе. `/update` из бота |
| Состояние в памяти, терялось при рестарте | `state.json` с атомарной записью |
| Без лимитов ресурсов | `MemoryMax=300M`, `TasksMax=64`, `CPUWeight=50`, `ProtectSystem=strict` и прочая изоляция systemd |

Также исправлено по мелочам из аудита: двойной `query.answer()` в callback (не-админ теперь видит «только администратор»), дубль проверки размера, ложный `_part` в id ролика, устаревший User-Agent (убран — у yt-dlp свой), несуществующий extractor-arg TikTok, `/tmp` на tmpfs (временные файлы теперь в `/var/lib/instaload/tmp`).

## Установка (Debian 12 / Ubuntu 22.04+, root)

```bash
git clone https://github.com/FrankX3M/instaload.git
cd instaload
sudo bash scripts/install.sh
sudo nano /etc/instaload/instaload.env     # BOT_TOKEN, ADMIN_ID
sudo systemctl start instaload
journalctl -u instaload -f
```

`install.sh` идемпотентен: создаёт пользователя `instaload`, venv, скачивает бинарник yt-dlp и статический ffmpeg под архитектуру (x86_64 / aarch64 / armv7l), ставит systemd-юниты и включает таймер обновления. Повторный запуск обновляет код и зависимости, не трогая настройки и данные.

Флаги: `--system-ffmpeg` (использовать ffmpeg из apt, если он уже есть на сервере), `--force-binaries` (перекачать yt-dlp и ffmpeg).

Раскладка на диске:

```
/opt/instaload/app          код                       root, read-only для бота
/opt/instaload/venv         PTB + instaloader         root, read-only для бота
/opt/instaload/bin          ffmpeg, ffprobe           root, read-only для бота
/var/lib/instaload/         состояние (0700, instaload)
  ├── state.json            подпись, качество по чатам
  ├── cookies/<user_id>.txt cookies пользователей (0600) + <user_id>.json (аккаунт, даты)
  ├── bin/yt-dlp            бинарник yt-dlp (обновляется сам)
  ├── cache/                кэш yt-dlp
  └── tmp/                  временные файлы задач
/etc/instaload/instaload.env настройки (0640 root:instaload)
```

### Обновление кода

```bash
cd instaload && sudo bash scripts/deploy.sh    # git pull → install.sh → restart
```

### Ручной запуск без systemd (для отладки)

```bash
set -a; source /etc/instaload/instaload.env; set +a
INSTALOAD_DATA_DIR=/tmp/il /opt/instaload/venv/bin/python -m instaload
```

## Настройка

Все параметры — в `/etc/instaload/instaload.env` (пример с комментариями: [instaload.env.example](instaload.env.example)). Главное:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `BOT_TOKEN` | — | токен от @BotFather |
| `ADMIN_ID` | — | user_id администратора: `/caption`, `/stats`, `/update` |
| `WORKERS` | 1 | одновременных загрузок. 1 для 1 vCPU; 2 — если 2 vCPU и ≥ 1 ГБ |
| `QUEUE_MAX` / `QUEUE_PER_USER` | 20 / 3 | длина очереди и лимит задач одного пользователя |
| `DOWNLOAD_TIMEOUT_SEC` / `FFMPEG_TIMEOUT_SEC` | 240 / 180 | таймауты подпроцессов; по истечении группа процессов убивается |
| `MAX_DOWNLOAD_MB` | 400 | `--max-filesize` yt-dlp, защита диска |
| `YT_MERGE` | 1 | `bestvideo+bestaudio` (настоящие 480/720). `0` — только прогрессивные форматы |
| `IG_COOKIES_SHARED` | — | общий файл cookies Instagram для тех, у кого нет своих (и для постов каналов). Необязательно |
| `YT_COOKIES` | — | cookies YouTube против «Sign in to confirm you're not a bot» |
| `YTDLP_CHANNEL` | stable | канал обновления: `stable` / `nightly` / `master` |
| `SMOKE_URLS` | одно видео YouTube | ссылки для smoke-теста после обновления yt-dlp |

## Команды бота

| Команда | Кто | Что делает |
|---|---|---|
| ссылка в чат | все | ставит в очередь; бот отвечает «в очереди, впереди N» и редактирует статус по ходу |
| `/cookies` | все, только в личке | статус своих cookies Instagram и инструкция, как добавить |
| `/cookies_delete` | все | удалить свои cookies |
| `/quality` | все | качество YouTube для чата: 360 / 480 / 720p |
| `/caption` | админ | подпись под медиа (`/caption off` — убрать) |
| `/stats` | админ | очередь, активные задачи, RSS, свободный диск, версия yt-dlp, пользователей с cookies |
| `/update` | админ | запустить `update_ytdlp.sh` сейчас и показать результат |

Файл `.txt`/`.json` или строка `sessionid=…; csrftoken=…`, присланные боту в личку, воспринимаются как cookies Instagram (см. [docs/COOKIES.md](docs/COOKIES.md)).

## Cookies Instagram: как это устроено

- Пользователь присылает cookies боту в личку (файл Netscape/JSON или строку). Бот оставляет только cookies `instagram.com`, требует наличие `sessionid`, сохраняет в `/var/lib/instaload/cookies/<user_id>.txt` с правами 0600, удаляет сообщение пользователя из чата и проверяет сессию через instaloader (`test_login` → `@username`).
- Для ссылки Instagram бот берёт cookies **по `from_user.id` отправителя**. Нет своих → `IG_COOKIES_SHARED`, если задан → без cookies. В группе каждый участник скачивает своими cookies; чужие не используются никогда.
- yt-dlp работает с копией файла в tmp задачи (он перезаписывает cookiefile); после задачи обновлённая копия валидируется и записывается обратно — так переживается ротация `csrftoken`/`sessionid`.
- Значения cookies в логи не попадают; в журнале только `user=<id>` и `@username`.

## Автообновление yt-dlp

`instaload-ytdlp-update.timer` запускает `scripts/update_ytdlp.sh` ежедневно (~05:30 ± 1 ч, плюс через 10 минут после загрузки сервера). Скрипт:

1. сохраняет копию бинарника;
2. `yt-dlp --update-to $YTDLP_CHANNEL`;
3. если версия сменилась — `scripts/smoke_test.sh` прогоняет `SMOKE_URLS` в режиме `--simulate`;
4. ссылка падает на новой версии, но работает на старой → **откат**; падает на обеих → это сеть/блокировка IP, новая версия остаётся.

Перезапуск бота не нужен — yt-dlp запускается отдельным процессом на каждую задачу. Команды:

```bash
systemctl list-timers 'instaload*'          # когда следующий запуск
sudo systemctl start instaload-ytdlp-update # запустить сейчас
journalctl -u instaload-ytdlp-update -n 50  # лог последнего обновления
```

Если Instagram/YouTube что-то сломали, а фикс есть только в master yt-dlp — поставьте `YTDLP_CHANNEL=nightly` (аналог хака из старого Dockerfile, но без пересборки).

Зависимости venv (PTB, instaloader) обновляются при `deploy.sh`/`install.sh`; ffmpeg статический и обновления не требует (`install.sh --force-binaries` перекачает).

## Архитектура

```
Telegram ──long polling──► PTB Application (один процесс, ~30 МБ)
                             │  хэндлеры: мгновенный ответ, задача → asyncio.Queue
                             ▼
                        worker (1..N)
                             │  на задачу: mkdtemp в /var/lib/instaload/tmp
                             ├─► yt-dlp (бинарник, subprocess, таймаут, --cookies копия, --max-filesize,
                             │            --print after_move:filepath, формат с фильтром по размеру)
                             ├─► ig_fetch.py (instaloader, subprocess) — fallback для фото/каруселей IG
                             ├─► ffmpeg -f segment (один проход) — если файл > 50 МБ
                             ├─► отправка: InputFile(read_file_handle=False) — стриминг с диска
                             └─► write-back cookies, rmtree(tmp)
```

Файлы:

- `instaload/bot.py` — хэндлеры, очередь, воркер, отправка;
- `instaload/media.py` — подпроцессы: yt-dlp, ffmpeg, helper; объяснение ошибок;
- `instaload/ig_fetch.py` — helper на instaloader (запускается как отдельный процесс);
- `instaload/cookies.py` — парсинг (Netscape / JSON / строка), фильтрация, per-user хранилище;
- `instaload/state.py` — JSON-состояние с атомарной записью;
- `instaload/config.py` — все настройки из env;
- `scripts/` — `install.sh`, `deploy.sh`, `update_ytdlp.sh`, `smoke_test.sh`;
- `systemd/` — сервис бота, сервис и таймер обновления;
- `tests/` — тесты чистых функций (`BOT_TOKEN=x python -m pytest -q`).

## Бюджет ресурсов (VPS 1 vCPU / 1 ГБ)

| Метрика | v1 | v2 |
|---|---|---|
| RSS бота в простое | 50 МБ на старте, 120–200 МБ через сутки | ~30 МБ стабильно |
| Пик RAM на задачу | 100–150 МБ в процессе бота | 40–80 МБ в подпроцессах, освобождается по завершении |
| Диск под установку | ~700 МБ образ + build-cache | ~200 МБ |
| Реакция бота во время загрузки | нет | мгновенно |
| Деплой | пересборка образа | секунды |

## Диагностика

```bash
journalctl -u instaload -f                    # логи; каждая строка задачи содержит [id chat= user=]
journalctl -u instaload | grep '\[a1b2c3'     # вся история одной задачи
systemctl status instaload                    # состояние, RSS (Memory:), рестарты
sudo -u instaload /var/lib/instaload/bin/yt-dlp -F 'https://youtu.be/…'   # какие форматы видит сервер
```

Типичные ситуации:

- **«Instagram требует авторизацию»** — у пользователя нет cookies или они протухли → `/cookies`.
- **«YouTube требует подтверждения»** — IP датацентра под подозрением; `YT_COOKIES` с cookies залогиненного аккаунта YouTube помогает, но не гарантированно.
- **Бот убит по памяти** — `systemctl status` покажет `oom-kill`; поднимите `MemoryMax` в юните (300 МБ хватает для 1 воркера) или снизьте `WORKERS`/`MAX_DOWNLOAD_MB`.
- **Загрузки медленные при `WORKERS=2` на 1 vCPU** — так и будет: yt-dlp+ffmpeg дерутся за CPU и диск, ставьте 1.

## Лицензия

Как в исходном репозитории.
