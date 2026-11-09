FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py ./
COPY static/ ./static/
RUN useradd -r -u 10001 app && mkdir /config && chown -R app:app /config /app
USER app
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3)"
CMD ["python", "server.py"]
