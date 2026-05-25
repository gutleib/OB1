# OB1 Russian Fork — Self-Hosted Server

> PostgreSQL + pgvector + локальные эмбеддинги (USER-bge-m3) + DeepSeek LLM.
> Полностью самостоятельный хостинг. Русский и английский языки.

## What It Does

Самостоятельный MCP-сервер для Open Brain, не требующий Supabase и OpenRouter.
Заменяет оригинальный `server/index.ts` (Supabase Edge Function) на Python/FastAPI-сервер,
работающий с локальным PostgreSQL + pgvector.

- Семантический поиск по мыслям (через Infinity + USER-bge-m3)
- Автоматическое извлечение метаданных (через DeepSeek)
- Полная схема Agent Memory с provenance tracking и audit trail
- Шесть MCP-инструментов: search, fetch, search_thoughts, list_thoughts, thought_stats, capture_thought

## Prerequisites

- Docker и docker-compose
- PostgreSQL 16 (или контейнер pgvector/pgvector:pg16 из compose)
- Infinity с загруженной моделью `deepvk/USER-bge-m3` (или любой 1024-dim совместимой)
- DeepSeek API ключ (или другой OpenAI-совместимый эндпоинт)
- 4 GB RAM (PostgreSQL + сервер)

## Step-by-Step Instructions

### 1. Настройка Infinity

Убедись, что Infinity запущен и модель загружена:

```bash
curl http://localhost:7997/models
```

Если модели нет — загрузи:

```bash
# Через Infinity API или предварительно скачай deepvk/USER-bge-m3
```

### 2. Настройка переменных окружения

Создай `.env` файл в корне проекта:

```env
DB_PASSWORD=your_secure_password
DEEPSEEK_API_KEY=sk-your-deepseek-key
MCP_ACCESS_KEY=your-random-access-key
INFINITY_URL=http://localhost:7997
```

Сгенерировать MCP_ACCESS_KEY:

```bash
openssl rand -hex 32
```

### 3. Запуск

```bash
docker-compose up -d
```

Проверить:

```bash
curl http://localhost:7981/health
# → {"ok": true, "version": "1.0.0"}
```

### 4. Подключение к Hermes

```bash
hermes mcp add ob1-ru --url http://localhost:7981/mcp?key=YOUR_ACCESS_KEY
hermes mcp test ob1-ru
```

После `/reset` появятся инструменты: `search`, `fetch`, `search_thoughts`, `list_thoughts`, `thought_stats`, `capture_thought`.

## Expected Outcome

- `GET /health` возвращает `{"ok": true}`
- `search_thoughts` находит релевантные мысли
- `capture_thought` сохраняет мысль с автоизвлечёнными метаданными
- В PostgreSQL видны таблицы: `thoughts`, `agent_memories`, `agent_memory_relations`, etc.

## Troubleshooting

**Infinity недоступен:**
Убедись, что Infinity запущен и `INFINITY_URL` правильный. Если Infinity на другом хосте — пропиши реальный IP, а не `host.docker.internal`.

**Ошибка подключения к PostgreSQL:**
Проверь `docker-compose logs postgres`. База должна быть готова (healthcheck `pg_isready`).

**DeepSeek не извлекает метаданные:**
Проверь `DEEPSEEK_API_KEY`. Без ключа метаданные будут `{"topics": ["uncategorized"], "type": "observation"}` — это не ошибка, сервер работает.

**MCP-инструменты не появляются в Hermes:**
Сделай `/reset` для перезагрузки тулсета. Проверь `hermes mcp test ob1-ru`.

## Architecture

```
Hermes (MCP client)
    ↕ MCP Streamable HTTP (порт 7981)
ob1-server (Python/FastAPI)
    ↕ asyncpg
PostgreSQL 16 + pgvector  ← схема thoughts + agent_memory
    ↕
Infinity (эмбеддинги)     ← deepvk/USER-bge-m3, 1024-dim
DeepSeek API (метаданные) ← deepseek-chat
```

## Differences from Upstream OB1

| Upstream | Этот форк |
|---|---|
| Supabase Edge Function (Deno) | Python/FastAPI |
| OpenRouter API (эмбеддинги) | Infinity (локально) |
| vector(1536) | vector(1024) |
| OpenRouter `gpt-4o-mini` (метаданные) | DeepSeek `deepseek-chat` |
| Supabase managed PostgreSQL | Собственный PostgreSQL |
| Slack capture | Не требуется (MCP из Hermes) |

## Next Steps

- [MCP Tool Audit & Optimization Guide](../docs/05-tool-audit.md) — управление тулсетом при добавлении новых расширений
- [Agent Memory Schema](../schemas/agent-memory/) — полная документация по схеме agent_memories
