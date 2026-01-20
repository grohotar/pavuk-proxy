# Subscription Proxy with Balancer

Прослойка для Remnawave панели, которая объединяет несколько Xray конфигов в один с балансировкой нагрузки.

## Что делает

- Получает подписку от Remnawave (несколько серверов)
- Объединяет их в один конфиг с `balancer` и `observatory`
- Клиент (Happ) видит одну локацию вместо нескольких
- Xray на клиенте сам выбирает лучший сервер и переключается при проблемах

## Быстрая установка

**1. Клонируй репо на сервер с панелью:**
```bash
cd /opt
git clone https://github.com/grohotar/pavuk-proxy.git subscription-proxy
cd subscription-proxy
```

**2. Создай .env файл:**
```bash
cp .env.example .env
nano .env  # отредактируй если нужно
```

**3. Собери и запусти:**
```bash
docker build -t subscription-proxy:latest .
docker run -d --name subscription-proxy --network host --restart always \
  --env-file .env \
  subscription-proxy:latest
```

**4. Обнови nginx конфиг** (см. ниже)

## Конфигурация

Скопируй `.env.example` в `.env` и настрой:

```bash
cp .env.example .env
```

Параметры в `.env`:

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `UPSTREAM_URL` | `http://127.0.0.1:3010` | URL remnawave subscription-page |
| `BALANCER_NAME` | `🇵🇱 Польша` | Название локации в клиенте |
| `APP_PORT` | `3020` | Порт прослойки |
| `PROBE_URL` | `https://www.google.com/generate_204` | URL для проверки доступности |
| `PROBE_INTERVAL` | `10s` | Интервал проверки (5s-10s рекомендуется) |

## Nginx конфигурация

Добавь в `/opt/remnawave/nginx.conf`:

```nginx
# Upstream для subscription-proxy
upstream subscription_proxy {
    server 127.0.0.1:3020;
}

# Измени server для subs.your-domain.com
server {
    server_name subs.your-domain.com;
    listen 443 ssl;
    http2 on;

    ssl_certificate "/etc/nginx/ssl/subs.your-domain.com/fullchain.pem";
    ssl_certificate_key "/etc/nginx/ssl/subs.your-domain.com/privkey.pem";

    location / {
        proxy_http_version 1.1;
        proxy_pass http://subscription_proxy;  # ← через прослойку
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        # ... остальные заголовки
    }
}
```

Перезапусти nginx:
```bash
docker restart remnawave-nginx
```

## Обновление

```bash
cd /opt/subscription-proxy
git pull
docker build -t subscription-proxy:latest .
docker stop subscription-proxy && docker rm subscription-proxy
docker run -d --name subscription-proxy --network host --restart always \
  --env-file .env \
  subscription-proxy:latest
```

## Как работает

```
Happ → subs.domain.com → nginx → subscription-proxy
                                        ↓
                           remnawave-subscription-page:3010
                                        ↓
                              [Poland1, Poland2, ...]
                                        ↓
                           subscription-proxy объединяет
                           в один конфиг с balancer
                                        ↓
                                     Happ
                              видит одну "Польша"
```

## License

MIT
