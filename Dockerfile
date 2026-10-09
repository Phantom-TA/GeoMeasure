FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# pyogrio, shapely and pyproj wheels bundle GDAL, GEOS and PROJ: no system packages needed
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY samples ./samples

RUN useradd --create-home --uid 10001 geo \
    && mkdir -p /data \
    && chown geo /data
USER geo

ENV GEO_DATA_DIR=/data \
    GEO_LOG_FORMAT=json
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status != 200)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
