FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY bot.py conversation_handlers.py README.md ./
COPY dataset ./dataset

# Non-root runtime user
RUN useradd -m vera
USER vera
ENV LLM_CACHE_PATH=/home/vera/llm_cache.json

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/v1/healthz',timeout=4)"

# ONE worker on purpose: all state is in-process memory and must persist across calls.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 75 --log-level info"]
