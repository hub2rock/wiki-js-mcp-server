FROM python:3.12-slim

LABEL org.opencontainers.image.title="wiki-js-mcp-server" \
      org.opencontainers.image.description="MCP server for Wiki.js" \
      org.opencontainers.image.source="https://github.com/hub2rock/wiki-js-mcp-server" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=http

RUN groupadd -r mcp && useradd -r -g mcp mcp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/example.env ./config/example.env

RUN mkdir -p /app/data && chown -R mcp:mcp /app

USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

CMD ["python", "src/server.py", "--http"]
