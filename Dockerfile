FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright CLIPFORGE_CONTAINER=1
WORKDIR /app
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg xvfb xauth \
    && python -m playwright install chrome \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 clipforge \
    && useradd --uid 10001 --gid 10001 --create-home clipforge
COPY requirements.media.lock ./requirements.media.lock
RUN pip install --no-cache-dir --no-deps -r requirements.media.lock
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY deploy ./deploy
COPY tests ./tests
COPY compose.yml ./compose.yml
# Source archives may be extracted with umask 077 on a private host directory.
# Image source is immutable but must remain readable by the runtime UID 10001.
RUN find /app -type d -exec chmod 0755 {} + \
    && find /app -type f -exec chmod 0644 {} +
ENTRYPOINT ["python", "-m", "deploy.runtime"]
CMD ["web"]
