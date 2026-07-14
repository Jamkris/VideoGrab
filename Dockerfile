FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py resolver.py .

ENV DOWNLOAD_DIR=/data
EXPOSE 8000

# yt-dlp breaks whenever sites change their internals; self-update on every
# container start so a simple restart fixes most extraction failures.
CMD ["sh", "-c", "pip install --no-cache-dir -U yt-dlp -q && uvicorn main:app --host 0.0.0.0 --port 8000"]
