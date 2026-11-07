# syntax=docker/dockerfile:1
FROM nvidia/cuda:12.8.2-devel-ubuntu24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates cmake git && rm -rf /var/lib/apt/lists/*
RUN git init /src && cd /src \
    && git remote add origin https://github.com/ServeurpersoCom/acestep.cpp.git \
    && git fetch --depth 1 origin 07abf07da3a9c1c45bd85aabed401644f8eb7c72 \
    && git checkout --detach FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON \
        '-DCMAKE_CUDA_ARCHITECTURES=90-real;100-real' \
    && cmake --build build --target ace-server -j 4

FROM ubuntu:24.04 AS weights
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/models
COPY model-sha256.txt .
RUN set -eu; \
    base=https://huggingface.co/Serveurperso/ACE-Step-1.5-GGUF/resolve/666ac70204440867d8c01ba4b119cc79c95b370a; \
    while read -r hash filename; do \
        curl --fail --location --silent --show-error --retry 4 --output "$filename" "$base/$filename"; \
    done < model-sha256.txt; \
    sha256sum -c model-sha256.txt; \
    curl --fail --location --silent --show-error --output LICENSE "$base/LICENSE"

FROM nvidia/cuda:12.8.2-runtime-ubuntu24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir fastapi==0.162.2 uvicorn==0.54.0 httpx==0.28.1
COPY --from=build /src/build/ace-server /opt/ace/
COPY --from=build /src/LICENSE /opt/ace/LICENSE
COPY --from=build /src/ggml/LICENSE /opt/ace/GGML-LICENSE
COPY --from=build /src/build/libggml*.so* /opt/ace/
COPY --from=weights /opt/models /opt/models
WORKDIR /opt/gateway
COPY gateway.py start.sh ./
ENV PATH=/opt/venv/bin:$PATH LD_LIBRARY_PATH=/opt/ace:/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/bin/bash", "/opt/gateway/start.sh"]
