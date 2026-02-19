FROM python:3.12-slim

# bust cache 2026-02-19-v4
RUN echo "fresh build v4"

RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcb-dri3-0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY PermitProject/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY PermitProject/ .

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080"]
