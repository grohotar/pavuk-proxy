FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENV APP_PORT=3020
ENV UPSTREAM_URL=http://127.0.0.1:3011
ENV BALANCER_NAME="🇵🇱 Польша"
ENV PROBE_URL="https://www.google.com/generate_204"
ENV PROBE_INTERVAL="1m"

EXPOSE 3020

CMD ["python", "app.py"]
