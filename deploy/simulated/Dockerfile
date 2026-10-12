FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EDX_SIMULATED_RUNTIME_SPOOL_DIR=/run/edx-runtime-bootstrap \
    EDX_SIMULATED_RUNTIME_STATE_DIR=/workspace/.edx/simulated \
    EDX_SIMULATED_RUNTIME_SECRETS_DIR=/run/secrets/edx-simulated-runtime \
    NVIDIA_VISIBLE_DEVICES=void \
    CUDA_VISIBLE_DEVICES=-1

RUN addgroup --system edxruntime \
    && adduser --system --ingroup edxruntime --home /opt/edx-comfy-runtime edxruntime \
    && install -d -o edxruntime -g edxruntime -m 0700 \
       /run/edx-runtime-bootstrap /run/secrets/edx-simulated-runtime /workspace/.edx/simulated

WORKDIR /opt/edx-comfy-runtime
COPY --chown=edxruntime:edxruntime installer ./installer

USER edxruntime
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "installer.simulated_host", "--healthcheck"]
ENTRYPOINT ["python", "-m", "installer.simulated_host"]
