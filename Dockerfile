FROM python:3.12-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir build hatchling
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim
# Non-root, and the app writes nothing to disk — avatars live in the database
# precisely so the container can run read_only.
RUN useradd --system --uid 10001 --create-home doser
WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl asyncpg \
 && rm -f /tmp/*.whl
USER doser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "doser.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
