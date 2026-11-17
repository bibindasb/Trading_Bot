FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# state/ and logs/ become volumes; they start owned by the unprivileged bot user.
RUN useradd --uid 1000 --create-home bot && mkdir -p state logs && chown bot:bot state logs
USER bot

CMD ["python", "main.py"]
