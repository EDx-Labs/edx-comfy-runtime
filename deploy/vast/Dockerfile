# A imagem base e um argumento deliberado: producao deve fornecer uma imagem
# Python 3.11 aprovada por digest. CUDA/PyTorch continuam declarados pelo
# installer/runtime.json, evitando uma segunda fonte de versoes.
ARG EDX_BASE_IMAGE=python:3.11-slim-bookworm
FROM ${EDX_BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COMFYUI_PATH=/workspace/ComfyUI \
    EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE=/run/secrets/edx-compute-manager-token \
    HF_HOME=/workspace/.cache/huggingface \
    TORCH_HOME=/workspace/.cache/torch \
    PIP_CACHE_DIR=/workspace/.cache/pip

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        ffmpeg \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        pkg-config \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /opt/edx-comfy-runtime /workspace /workspace/.edx /run/secrets

WORKDIR /opt/edx-comfy-runtime

# Copias explicitas evitam incluir artefatos locais ou credenciais no contexto
# final. Os submodulos integrations/* precisam estar inicializados no build.
COPY installer ./installer
COPY integrations ./integrations
COPY workflows ./workflows
COPY install_workflow.py README.md ./

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "installer.remote_supervisor"]
