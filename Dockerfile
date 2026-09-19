FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends tzdata     && ln -snf /usr/share/zoneinfo/ /etc/localtime && echo  > /etc/timezone     && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir     "aiogram>=3.17.0"     "aiohttp>=3.10.0"     "asyncpg>=0.30.0"

COPY . /app

CMD ["python", "bot.py"]
