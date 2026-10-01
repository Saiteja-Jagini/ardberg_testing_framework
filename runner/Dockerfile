FROM mcr.microsoft.com/playwright:v1.63.0-noble

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       python3 python3-pip python3-venv curl git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash runner
RUN python3 -m venv /opt/ardberg-python
ENV PATH="/opt/ardberg-python/bin:${PATH}"
RUN corepack enable
WORKDIR /workspace
