FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# 프롬프트 파일(app/prompts/*.md)은 패키지 안에 같이 설치된다. 프롬프트 버전·키·예산은
# 환경변수로 주입한다 — 이미지에 굽지 않는다 (#31). 예산 장부·리플레이 캐시는 /srv/.cache.
RUN useradd --system --uid 1001 --home /srv app \
    && mkdir -p /srv/.cache \
    && chown -R app:app /srv
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
