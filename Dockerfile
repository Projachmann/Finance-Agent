FROM python:3.11-slim-bookworm

# WeasyPrint system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    libffi-dev libcairo2 libjpeg62-turbo-dev fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so the apt/pip layers are cached across code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p output

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "main:app"]
