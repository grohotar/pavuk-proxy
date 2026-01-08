# Subscription Proxy with Balancer

Прослойка для Remnawave панели, которая объединяет несколько Xray конфигов в один с балансировкой нагрузки.

## Что делает

- Получает подписку от Remnawave (несколько серверов)
- Объединяет их в один конфиг с `balancer` и `observatory`
- Клиент (Happ) видит одну локацию вместо нескольких
- Xray на клиенте сам выбирает лучший сервер и переключается при проблемах

## Установка

### Docker Compose

Добавь в `docker-compose.yml`:

```yaml
subscription-proxy:
  image: ghcr.io/grohotar/pavuk-proxy:latest
  container_name: subscription-proxy
  restart: always
  environment:
    - UPSTREAM_URL=http://127.0.0.1:3011
    - BALANCER_NAME=🇵🇱 Польша
    - APP_PORT=3020
  ports:
    - '127.0.0.1:3020:3020'
  network_mode: host
```

### Nginx

Добавь внутренний endpoint для получения оригинальных данных:

```nginx
# Internal endpoint for subscription-proxy
server {
    listen 3011;
    server_name _;

    location / {
        proxy_http_version 1.1;
        proxy_pass http://json;  # remnawave-subscription-page
        proxy_set_header Host subs.your-domain.com;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host subs.your-domain.com;
        proxy_set_header X-Forwarded-Port 443;
    }
}
```

Измени основной endpoint чтобы шёл через прослойку:

```nginx
upstream subscription_proxy {
    server 127.0.0.1:3020;
}

server {
    server_name subs.your-domain.com;
    
    location / {
        proxy_pass http://subscription_proxy;
        # ... остальные настройки
    }
}
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `UPSTREAM_URL` | `http://127.0.0.1:3011` | URL внутреннего nginx endpoint |
| `BALANCER_NAME` | `🇵🇱 Польша` | Название локации в клиенте |
| `APP_PORT` | `3020` | Порт прослойки |
| `PROBE_URL` | `https://www.google.com/generate_204` | URL для проверки доступности |
| `PROBE_INTERVAL` | `1m` | Интервал проверки |

## Как работает

```
Happ → subs.domain.com → nginx → subscription-proxy
                                        ↓
                              nginx:3011 (internal)
                                        ↓
                           remnawave-subscription-page
                                        ↓
                              [Poland1, Poland2, ...]
                                        ↓
                           subscription-proxy собирает
                           один конфиг с balancer
                                        ↓
                                     Happ
                              видит одну "Польша"
```

## License

MIT
