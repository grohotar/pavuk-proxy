# Subscription Proxy with Selective Balancer

Прослойка для Remnawave subscription-page.

Что делает:
- работает как отдельный Docker-контейнер;
- для Happ может объединять только выбранные ноды (например `Germany1 + Germany2 -> Германия`);
- не объединенные ноды отдает как есть (например `Poland` остается отдельной);
- стандартную страницу подписки в браузере не ломает (HTML идет как раньше);
- распределяет пользователей по нодам внутри группы (sticky-назначение) примерно поровну.

## Как это работает

Поток:

`Happ -> subs.domain -> nginx -> subscription-proxy -> remnawave-subscription-page`

- Для `User-Agent` с `Happ` прокси читает JSON подписки и применяет правила группировки.
- Для остальных клиентов/браузера прокси просто отдает upstream-ответ без трансформации.

## Конфигурация

Скопируй `.env.example` в `.env`:

```bash
cp .env.example .env
```

Основные переменные:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `UPSTREAM_URL` | `http://127.0.0.1:3010` | remnawave subscription-page |
| `APP_PORT` | `3020` | порт прокси |
| `FORWARDED_HOST` | `subs.pavuka.cv` | `X-Forwarded-Host` для upstream |
| `BALANCER_NAME` | `🇵🇱 Польша` | имя группы в legacy режиме (когда нет rules файла) |
| `DEFAULT_GROUP_MODE` | `sticky` | режим группировки по умолчанию: `sticky` или `xray_balancer` |
| `DEFAULT_BALANCER_STRATEGY` | `random` | стратегия балансировки |
| `PROBE_URL` | `https://www.google.com/generate_204` | URL проверки доступности |
| `PROBE_INTERVAL` | `10s` | интервал проверки |
| `GROUP_RULES_PATH` | `/app/group-rules.json` | путь к JSON-правилам группировки |

## Правила группировки

Формат файла `group-rules.json`:

```json
{
  "groups": [
    {
      "name": "🇩🇪 Германия",
      "mode": "sticky",
      "remarks": ["Germany1", "Germany2"],
      "remark_regex": ["^Germany\\d+$"],
      "address_regex": ["^de\\d+\\.pavuka\\.cv$"],
      "strategy": "random",
      "probe_url": "https://www.google.com/generate_204",
      "probe_interval": "10s"
    }
  ]
}
```

Правила:
- `remarks`/`remark_regex`/`address_regex` работают по логике OR.
- Если в группе найдено меньше 2 нод, группа не собирается.
- Ноды, не попавшие ни в одну группу, остаются отдельными.
- `mode`:
  - `sticky` (по умолчанию): каждому пользователю назначается одна нода из группы детерминированно (по UUID), поэтому "Germany" в Happ остается одной записью, а нагрузка делится по пользователям.
  - `xray_balancer`: генерируется один конфиг с несколькими outbounds и `routing.balancers` (может увеличивать online-счетчики и давать `n/a` в ping в некоторых клиентах).
- Если `GROUP_RULES_PATH` задан, прокси работает только в rules-режиме (селективная группировка).
- Если `GROUP_RULES_PATH` пустой, включается legacy-режим: объединяются все ноды в одну запись `BALANCER_NAME`.

## Запуск отдельного контейнера

```bash
cd /opt
git clone https://github.com/grohotar/pavuk-proxy.git subscription-proxy
cd subscription-proxy
cp .env.example .env
cp group-rules.example.json group-rules.json
```

Пример запуска:

```bash
docker build -t subscription-proxy:latest .
docker run -d --name subscription-proxy --network host --restart always \
  --env-file .env \
  -v /opt/subscription-proxy/group-rules.json:/app/group-rules.json:ro \
  subscription-proxy:latest
```

## Интеграция с nginx Remnawave (через отдельный override-файл)

Чтобы не держать всю кастомизацию в основном `nginx.conf`:

1. Создай `docker-compose.override.yml` в `/opt/remnawave` (можно взять `remnawave-docker-compose.override.example.yml`):

```yaml
services:
  remnawave-nginx:
    volumes:
      - ./nginx.subscription-proxy.conf:/etc/nginx/conf.d/nginx.subscription-proxy.conf:ro
```

2. Создай `/opt/remnawave/nginx.subscription-proxy.conf` (можно взять `nginx.subscription-proxy.conf.example`):

```nginx
upstream subscription_proxy {
    server 127.0.0.1:3020;
}
```

3. В `/opt/remnawave/nginx.conf`:
- добавь include рядом с upstream-блоками:

```nginx
include /etc/nginx/conf.d/nginx.subscription-proxy.conf;
```

- в `server_name subs...` поменяй только `proxy_pass`:

```nginx
proxy_pass http://subscription_proxy;
```

4. Примени изменения:

```bash
cd /opt/remnawave
docker compose up -d remnawave-nginx
```

После этого:
- браузерная страница подписки продолжит открываться как раньше;
- Happ будет получать трансформированную выдачу по правилам группировки.

## Проверка

```bash
curl -sS http://127.0.0.1:3020/health
```

Проверка Happ-выдачи:

```bash
curl -k -sS -A 'Happ/4.2.5/ios' "https://subs.your-domain.com/<short_uuid>"
```

Проверка браузерной выдачи:

```bash
curl -k -sS -A 'Mozilla/5.0' "https://subs.your-domain.com/<short_uuid>"
```

## License

MIT
