FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /src
COPY pyproject.toml README.md ./
COPY garmin_mcp ./garmin_mcp
RUN pip install --upgrade pip && pip install .

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Everything the server keeps lives under /data: the SQLite database (accounts,
# encrypted Garmin tokens, OAuth clients) and the per-account FIT cache.
ENV MCP_DB=/data/app.db \
    GARMIN_MCP_CACHE=/data/cache
COPY --from=build /opt/venv /opt/venv
RUN useradd -u 1000 -m app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"
CMD ["garmin-mcp", "serve", "--http", "--host", "0.0.0.0", "--port", "8000"]
