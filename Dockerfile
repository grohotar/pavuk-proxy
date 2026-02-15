FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY group-rules.example.json /app/group-rules.example.json

ENV APP_PORT=3020
ENV UPSTREAM_URL=http://127.0.0.1:3010
ENV BALANCER_NAME="🇵🇱 Польша"
ENV PROBE_URL="https://www.google.com/generate_204"
ENV PROBE_INTERVAL="10s"
ENV FORWARDED_HOST="subs.pavuka.cv"
ENV DEFAULT_BALANCER_STRATEGY="random"
ENV GROUP_RULES_PATH=""

EXPOSE 3020

CMD ["python", "app.py"]
