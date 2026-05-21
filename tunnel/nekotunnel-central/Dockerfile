FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl ca-certificates git openssh-client tar gzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://railway.com/install.sh | bash -s -- -y \
    && ln -sf /root/.railway/bin/railway /usr/local/bin/railway \
    && railway --version

ENV RAILWAY_CLI_PATH=/usr/local/bin/railway
ENV RAILWAY_HOME_DIR=/var/data/railway
ENV PATH="/usr/local/bin:/root/.railway/bin:${PATH}"

RUN mkdir -p /var/data/railway

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY client ./client

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
