FROM ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:5268283a8d6510d167364f19aee93e98180d8eb0cac4b7edb20af7e3edf40c17

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    python3 \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/model-manager-venv && \
    /opt/model-manager-venv/bin/pip install --no-cache-dir \
    fastapi==0.141.1 \
    httpx==0.28.1 \
    uvicorn==0.35.0

COPY packaging/model_manager/app.py /opt/model-manager/app.py

WORKDIR /app
EXPOSE 8090

ENTRYPOINT ["/opt/model-manager-venv/bin/python", "/opt/model-manager/app.py"]
