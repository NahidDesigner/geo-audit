FROM python:3.12-slim

# System libraries WeasyPrint needs for PDF generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi8 shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py geo_audit.py prospects.py remediation.py ./

# Reports + database live here - mount a Coolify volume at this path
ENV GEO_DATA_DIR=/app/data
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "300", \
     "--bind", "0.0.0.0:8080", "app:app"]
