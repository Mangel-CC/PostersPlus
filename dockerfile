FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --gecos '' appuser

# Copy app files and set ownership on everything except the cache dir,
# which is a runtime volume mount — permissions are fixed by entrypoint.sh.
COPY . .
RUN chown -R appuser:appuser /app

# Run as root so entrypoint.sh can fix cache volume permissions at startup,
# then it drops to appuser via gosu before exec-ing uvicorn.
CMD ["/bin/sh", "entrypoint.sh"]