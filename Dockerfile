FROM python:3.12-slim

# System libraries: WeasyPrint (PDF fallback) + font coverage
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi8 shared-mime-info fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium for pixel-identical PDF rendering. --with-deps pulls the exact
# system libraries the bundled build needs. If this layer is removed the app
# still runs: pdf_render falls back to WeasyPrint automatically.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app.py geo_audit.py prospects.py remediation.py mcp_server.py \
     deep_scan.py manual_audit.py pdf_render.py ./

# Reports + database live here - mount a Coolify volume at this path
ENV GEO_DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "300", \
     "--bind", "0.0.0.0:8080", "app:app"]
