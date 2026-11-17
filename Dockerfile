FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
# Package index, e.g. a company mirror: set these in .env or the shell (see .env.example). Default: public PyPI.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# state/ and logs/ become volumes; they start owned by the unprivileged bot user.
RUN useradd --uid 1000 --create-home bot && mkdir -p state logs && chown bot:bot state logs
USER bot

CMD ["python", "main.py"]
