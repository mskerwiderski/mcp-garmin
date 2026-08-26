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
# The server keeps three things under /data: the Garmin tokens (unless they
# come in via GARMIN_TOKENS), the registered OAuth clients, and the FIT cache.
ENV GARMIN_TOKENS_FILE=/data/tokens.json \
    MCP_STATE_FILE=/data/oauth.json \
    GARMIN_MCP_CACHE=/data/cache
COPY --from=build /opt/venv /opt/venv
RUN useradd -u 1000 -m app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"
CMD ["garmin-mcp", "serve", "--http", "--host", "0.0.0.0", "--port", "8000"]
