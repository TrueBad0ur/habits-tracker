# Habits Tracker

Telegram Mini App для отслеживания привычек в групповых чатах. Участники группы ведут общий календарь, смотрят стрики, ачивки и рейтинг.

---

## Требования

- Docker + Docker Compose
- Домен с HTTPS (mini app работает только по HTTPS)
- Обратный прокси (nginx, Caddy и т.д.), проксирующий домен на порт контейнера

---

## Настройка бота в BotFather

### 1. Создать бота

```
/newbot
```

Сохрани токен — он пойдёт в `BOT_TOKEN`.

### 2. Отключить Group Privacy

```
/mybots → выбрать бота → Bot Settings → Group Privacy → Turn off
```

Без этого бот не видит сообщения в группах.

### 3. Настроить Mini App

```
/mybots → выбрать бота → Bot Settings → Configure Main Mini App → Enable
→ Enter the URL: https://your-domain.com
```

### 4. Заполнить описание (опционально)

```
/mybots → выбрать бота → Edit Bot → Edit About
```

Пример:
```
🏋️ Трекер привычек / Habits tracker

Добавь в группу → /start → мини-приложение
Привычки, стрики, рейтинг

Add to a group → /start → mini app
Habits, streaks, leaderboard
```

Картинку для приветствия при первом `/start` положи в `assets/hello.jpg`.

---

## Установка

### 1. Клонировать репозиторий

```bash
git clone <repo-url>
cd habits-tracker/telegram-bot-miniapp
```

### 2. Настроить переменные окружения

Открой `docker-compose.yml` и заполни:

```yaml
x-common-env: &common-env
  BOT_TOKEN: "токен от BotFather"
  WEBAPP_URL: "https://your-domain.com"
  ENABLE_LOGGING: "true"   # false — отключить логи
  TZ: "Europe/Moscow"      # часовой пояс для логов
```

Порт фронтенда (по умолчанию 8092) — настраивается в секции `frontend → ports`.

### 3. Настроить обратный прокси

Проксируй `https://your-domain.com` → `http://localhost:8092`.

### 4. Запустить

```bash
docker compose up
```

---

## Использование

### Подключение группы

1. Добавь бота в группу
2. Напиши `/start` в группе — бот пришлёт кнопку **Открыть трекер**
3. Нажми кнопку — Mini App откроется прямо в Telegram

При первом `/start` бот пришлёт приветственное сообщение с картинкой.

### В Mini App

**Календарь** — основной экран. Нажми на любой день, чтобы отметить выполнение привычек (✅ / ❌) для каждого участника.

**Ачивки** — стрики за каждую привычку. Значки разблокируются при достижении серий: 🔥 2 · 💪 3 · ⚡ 5 · 🌟 7 · 🏆 14 · 👑 30 · 💎 60 дней подряд.

**Рейтинг** — участники отсортированы по % выполнения за выбранный месяц. 🥇🥈🥉 для топ-3.

### Настройки (кнопка ⚙)

- **Участники** — добавить / удалить участников
- **Привычки** — добавить / удалить привычки для каждого участника
- **База данных** — экспорт и импорт данных в формате JSON
- **Язык** — Русский / English

---

## Данные и логи

```
telegram-bot-miniapp/
├── data/
│   ├── habits.db       # база данных привычек
│   └── bot.db          # состояние бота (группы, кеш)
└── logs/
    ├── group_<id>.log  # лог групповых чатов
    └── direct_<id>.log # лог личных чатов
```

Формат лога:
```
[2026-03-07 12:00:00 MSK] Иван (@ivan, id=12345): added habit "Спорт" for person "Иван" in group 'My Group'
[2026-03-07 12:01:00 MSK] Иван (@ivan, id=12345): marked "Спорт" on 2026-03-07 as ✅ in group 'My Group'
```

---

## Обновление

```bash
docker compose down
git pull
docker compose up --build
```
