FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    gnupg \
    lsb-release \
    gcc \
    && install -d /etc/apt/keyrings \
    && wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

# Opt-in extra for the offline scanner's dense-embedding path. Empty by
# default, so the image is byte-for-byte what it always was; docker-compose
# sets it from LOCAL_SCANNER_MODEL, so pointing at a model is what installs
# the runtime that reads it. See backend/services/card_embedding.py.
ARG INSTALL_SCANNER_MODEL=

COPY backend/requirements.txt backend/requirements-scanner.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ -n "$INSTALL_SCANNER_MODEL" ]; then \
         pip install --no-cache-dir -r requirements-scanner.txt; \
       fi

COPY VERSION /app/VERSION
COPY backend/ .

RUN mkdir -p /app/backups /app/debug /app/data/pokedex-images/sprites /app/data/pokedex-images/artwork

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
