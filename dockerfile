FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --gecos '' appuser

# Copy app files and create cache dir while still root, then hand ownership over
COPY . .
RUN mkdir -p /app/cache /app/cache/tmdb_posters /app/cache/tmdb_logos \
    && chown -R appuser:appuser /app

USER appuser

# entrypoint.sh reads the WORKERS env var and passes it to uvicorn.
# Using CMD (not ENTRYPOINT) so operators can still override with a shell command.
CMD ["/bin/sh", "entrypoint.sh"]