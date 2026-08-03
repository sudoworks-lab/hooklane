ARG PYTHON_IMAGE=python:3.12-alpine3.23@sha256:601d3d3797e90e2534782e69c85fafb7971b43f24c7b1b079b7e48dd435e458d

FROM ${PYTHON_IMAGE} AS builder

WORKDIR /build
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN python -m pip install \
    --no-cache-dir \
    --prefix=/install \
    --constraint=requirements.lock \
    .

FROM ${PYTHON_IMAGE} AS runtime

ENV PATH=/usr/local/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=builder /install /usr/local
WORKDIR /app
USER 10001:10001

FROM runtime AS api
LABEL org.opencontainers.image.version="0.1.1" \
    io.hooklane.role="api"
EXPOSE 8080
ENTRYPOINT ["uvicorn"]
CMD ["hooklane.api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "warning"]

FROM runtime AS worker
LABEL org.opencontainers.image.version="0.1.1" \
    io.hooklane.role="worker"
EXPOSE 9090
ENTRYPOINT ["python", "-m", "hooklane.worker.main"]

FROM runtime AS mock-sink
LABEL org.opencontainers.image.version="0.1.1" \
    io.hooklane.role="mock-sink"
EXPOSE 8080
ENTRYPOINT ["uvicorn"]
CMD ["hooklane.mock_sink.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "warning"]
