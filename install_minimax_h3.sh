#!/usr/bin/env bash
set -Eeuo pipefail

echo "============================================================"
echo " MiniMax H3 Easy - Provisioning Vast.ai"
echo "============================================================"

WORKSPACE="${WORKSPACE:-/workspace}"
COMFY_DIR="${WORKSPACE}/ComfyUI"
BENCH_DIR="${WORKSPACE}/benchmarks"

# ============================================================
# 0. AMBIENTE PYTHON DA IMAGEM VAST
# ============================================================

echo ""
echo "============================================================"
echo "Ativando ambiente Python"
echo "============================================================"

if [ -f /venv/main/bin/activate ]; then
    . /venv/main/bin/activate
else
    echo "[ERRO] /venv/main/bin/activate não encontrado."
    exit 1
fi

echo "[OK] Python:"
which python
python --version


# ============================================================
# 1. VERIFICAR COMFYUI
# ============================================================

echo ""
echo "============================================================"
echo "Verificando ComfyUI"
echo "============================================================"

if [ ! -d "$COMFY_DIR" ]; then
    echo "[ERRO] ComfyUI não encontrado:"
    echo "       $COMFY_DIR"
    exit 1
fi

echo "[OK] ComfyUI encontrado:"
echo "     $COMFY_DIR"

cd "$COMFY_DIR"


# ============================================================
# 2. CUSTOM NODES
# ============================================================

echo ""
echo "============================================================"
echo "Instalando/verificando Custom Nodes"
echo "============================================================"

mkdir -p "$COMFY_DIR/custom_nodes"
cd "$COMFY_DIR/custom_nodes"


clone_if_missing() {

    local url="$1"
    local destino="$2"

    if [ -d "$destino/.git" ]; then

        echo ""
        echo "[OK] Custom Node já instalado:"
        echo "     $destino"

    elif [ -d "$destino" ]; then

        echo ""
        echo "[AVISO] Diretório existe mas não contém .git:"
        echo "        $destino"

    else

        echo ""
        echo "[CLONE] $url"

        git clone "$url" "$destino"

    fi
}


clone_if_missing \
    "https://github.com/EDx-Labs/comfyui-minimaxh3-easy-optimized.git" \
    "ComfyUI-MiniMaxH3-Easy"

clone_if_missing \
    "https://github.com/nicolab28/ComfyUI-ClipProj.git" \
    "ComfyUI-ClipProj"

clone_if_missing \
    "https://github.com/kijai/ComfyUI-KJNodes.git" \
    "ComfyUI-KJNodes"

clone_if_missing \
    "https://github.com/LAOGOU-666/Comfyui-Memory_Cleanup.git" \
    "Comfyui-Memory_Cleanup"

clone_if_missing \
    "https://github.com/yolain/ComfyUI-Easy-Use.git" \
    "ComfyUI-Easy-Use"

clone_if_missing \
    "https://github.com/rgthree/rgthree-comfy.git" \
    "rgthree-comfy"


# ============================================================
# 3. DEPENDÊNCIAS DOS CUSTOM NODES
# ============================================================

cd "$COMFY_DIR"

echo ""
echo "============================================================"
echo "Instalando dependências Python"
echo "============================================================"


install_requirements() {

    local arquivo="$1"
    local nome="$2"

    if [ -f "$arquivo" ]; then

        echo ""
        echo "[PIP] $nome"

        python -m pip install -r "$arquivo"

    else

        echo ""
        echo "[INFO] Sem requirements.txt:"
        echo "       $nome"

    fi
}


install_requirements \
    "custom_nodes/ComfyUI-KJNodes/requirements.txt" \
    "ComfyUI-KJNodes"

install_requirements \
    "custom_nodes/ComfyUI-Easy-Use/requirements.txt" \
    "ComfyUI-Easy-Use"


echo ""
echo "[PIP] OpenCV"

python -m pip install opencv-python


# ============================================================
# 4. DIRETÓRIOS DOS MODELOS
# ============================================================

echo ""
echo "============================================================"
echo "Preparando diretórios dos modelos"
echo "============================================================"

mkdir -p "$COMFY_DIR/models/diffusion_models"
mkdir -p "$COMFY_DIR/models/clip_projections"
mkdir -p "$COMFY_DIR/models/text_encoders"
mkdir -p "$COMFY_DIR/models/vae"


# ============================================================
# 5. URLs DOS MODELOS
# ============================================================

URL_T2V="https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/main/minimax_h3_fl2va_pruned_w4a8_mixed.safetensors"

URL_REF2V="https://huggingface.co/Kijai/MiniMax-H3-experimental/resolve/main/minimax_h3_ref2va_pruned_w4a8_mixed.safetensors"

URL_CLIP_PROJ="https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3/resolve/main/mmh3-4b-ClipProj-celeb-mlp.safetensors"

URL_TEXT_ENC="https://huggingface.co/Comfy-Org/Krea-2/resolve/main/text_encoders/qwen3vl_4b_fp8_scaled.safetensors"

URL_AUDIO_VAE="https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors"

URL_VIDEO_VAE="https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors"


# ============================================================
# 6. FUNÇÃO DE DOWNLOAD
# ============================================================

baixar_se_nao_existir() {

    local url="$1"
    local destino="$2"

    if [ -s "$destino" ]; then

        echo ""
        echo "[OK] Modelo já existe:"
        echo "     $destino"

        ls -lh "$destino"

        return 0

    fi

    echo ""
    echo "============================================================"
    echo "[DOWNLOAD]"
    echo "$destino"
    echo "============================================================"

    mkdir -p "$(dirname "$destino")"

    wget \
        --continue \
        --content-disposition \
        --tries=10 \
        --timeout=30 \
        "$url" \
        -O "$destino"

    if [ ! -s "$destino" ]; then

        echo ""
        echo "[ERRO] Download não produziu arquivo válido:"
        echo "       $destino"

        rm -f "$destino"

        exit 1

    fi

    echo ""
    echo "[OK] Download concluído:"

    ls -lh "$destino"
}


# ============================================================
# 7. DOWNLOAD DOS MODELOS
# ============================================================

echo ""
echo "============================================================"
echo "Baixando/verificando modelos MiniMax H3"
echo "============================================================"


baixar_se_nao_existir \
    "$URL_T2V" \
    "$COMFY_DIR/models/diffusion_models/minimax_h3_fl2va_pruned_w4a8_mixed.safetensors"


baixar_se_nao_existir \
    "$URL_REF2V" \
    "$COMFY_DIR/models/diffusion_models/minimax_h3_ref2va_pruned_w4a8_mixed.safetensors"


baixar_se_nao_existir \
    "$URL_CLIP_PROJ" \
    "$COMFY_DIR/models/clip_projections/mmh3-4b-ClipProj-celeb-mlp.safetensors"


baixar_se_nao_existir \
    "$URL_TEXT_ENC" \
    "$COMFY_DIR/models/text_encoders/qwen3vl_4b_fp8_scaled.safetensors"


baixar_se_nao_existir \
    "$URL_AUDIO_VAE" \
    "$COMFY_DIR/models/vae/minimax_h3_audio_vae_fp32.safetensors"


baixar_se_nao_existir \
    "$URL_VIDEO_VAE" \
    "$COMFY_DIR/models/vae/minimax_h3_video_vae_fp16.safetensors"


# ============================================================
# 8. VERIFICAR CUSTOM NODES
# ============================================================

echo ""
echo "============================================================"
echo "Verificando Custom Nodes"
echo "============================================================"


NODES=(

    "ComfyUI-MiniMaxH3-Easy"

    "ComfyUI-ClipProj"

    "ComfyUI-KJNodes"

    "Comfyui-Memory_Cleanup"

    "ComfyUI-Easy-Use"

    "rgthree-comfy"

)


ERRO=0


for NODE in "${NODES[@]}"
do

    if [ -d "$COMFY_DIR/custom_nodes/$NODE" ]; then

        echo "[OK] $NODE"

    else

        echo "[ERRO] $NODE"
        ERRO=1

    fi

done


# ============================================================
# 9. VERIFICAR MODELOS
# ============================================================

echo ""
echo "============================================================"
echo "Verificando Modelos"
echo "============================================================"


MODELOS=(

    "$COMFY_DIR/models/diffusion_models/minimax_h3_fl2va_pruned_w4a8_mixed.safetensors"

    "$COMFY_DIR/models/diffusion_models/minimax_h3_ref2va_pruned_w4a8_mixed.safetensors"

    "$COMFY_DIR/models/clip_projections/mmh3-4b-ClipProj-celeb-mlp.safetensors"

    "$COMFY_DIR/models/text_encoders/qwen3vl_4b_fp8_scaled.safetensors"

    "$COMFY_DIR/models/vae/minimax_h3_audio_vae_fp32.safetensors"

    "$COMFY_DIR/models/vae/minimax_h3_video_vae_fp16.safetensors"

)


for MODELO in "${MODELOS[@]}"
do

    if [ -s "$MODELO" ]; then

        echo ""
        echo "[OK] $MODELO"

        ls -lh "$MODELO"

    else

        echo ""
        echo "[ERRO] Modelo ausente ou vazio:"
        echo "       $MODELO"

        ERRO=1

    fi

done


if [ "$ERRO" -ne 0 ]; then

    echo ""
    echo "============================================================"
    echo "[ERRO] Provisioning incompleto"
    echo "============================================================"

    exit 1

fi


# ============================================================
# 10. INFORMAÇÕES DA GPU
# ============================================================

echo ""
echo "============================================================"
echo "GPU detectada"
echo "============================================================"

nvidia-smi

echo ""

nvidia-smi \
    --query-gpu=index,name,driver_version,memory.total,power.limit \
    --format=csv


# ============================================================
# 11. PREPARAR BENCHMARK
# ============================================================

echo ""
echo "============================================================"
echo "Configurando benchmark"
echo "============================================================"

mkdir -p "$BENCH_DIR"


RUN_ID="$(date +%Y%m%d_%H%M%S)"

GPU_LOG="$BENCH_DIR/gpu_${RUN_ID}.csv"

GPU_ERROR_LOG="$BENCH_DIR/gpu_${RUN_ID}.err"

GPU_PID_FILE="$BENCH_DIR/gpu_${RUN_ID}.pid"

SYSTEM_INFO="$BENCH_DIR/system_${RUN_ID}.txt"


# ============================================================
# 12. REGISTRAR INFORMAÇÕES DO SISTEMA
# ============================================================

{

    echo "============================================================"
    echo "MINIMAX H3 BENCHMARK"
    echo "============================================================"

    echo ""

    echo "Data:"
    date

    echo ""

    echo "Hostname:"
    hostname

    echo ""

    echo "GPU:"

    nvidia-smi \
        --query-gpu=index,name,driver_version,memory.total,power.limit \
        --format=csv

    echo ""

    echo "CPU:"
    lscpu

    echo ""

    echo "RAM:"
    free -h

    echo ""

    echo "DISCO:"
    df -h

    echo ""

    echo "PYTHON:"
    python --version

    echo ""

    echo "PYTORCH:"

    python - <<'PY'

import torch

print("PyTorch:", torch.__version__)
print("CUDA runtime:", torch.version.cuda)
print("CUDA disponível:", torch.cuda.is_available())

if torch.cuda.is_available():

    print("Quantidade de GPUs:", torch.cuda.device_count())

    for i in range(torch.cuda.device_count()):

        p = torch.cuda.get_device_properties(i)

        print()
        print("GPU:", i)
        print("Nome:", p.name)
        print(
            "VRAM:",
            round(p.total_memory / 1024**3, 2),
            "GiB"
        )

PY

} > "$SYSTEM_INFO" 2>&1


echo "[OK] Informações salvas:"
echo "     $SYSTEM_INFO"


# ============================================================
# 13. INICIAR MONITOR DA GPU
# ============================================================

echo ""
echo "============================================================"
echo "Iniciando monitor da GPU"
echo "============================================================"


nohup nvidia-smi \
    --query-gpu=timestamp,index,name,memory.used,memory.total,utilization.gpu,utilization.memory,power.draw,temperature.gpu \
    --format=csv \
    -l 1 \
    > "$GPU_LOG" \
    2> "$GPU_ERROR_LOG" \
    < /dev/null &


GPU_MONITOR_PID=$!


echo "$GPU_MONITOR_PID" > "$GPU_PID_FILE"


echo ""
echo "[OK] Monitor iniciado"

echo "PID:"
echo "  $GPU_MONITOR_PID"

echo ""

echo "CSV:"
echo "  $GPU_LOG"

echo ""

echo "Erros:"
echo "  $GPU_ERROR_LOG"


# ============================================================
# 14. FINALIZAÇÃO
# ============================================================

echo ""
echo "============================================================"
echo " MiniMax H3 - Provisioning concluído"
echo "============================================================"

echo ""
echo "Custom Nodes:"
echo "  - MiniMax H3 Easy"
echo "  - ClipProj"
echo "  - KJNodes"
echo "  - Memory Cleanup"
echo "  - Easy-Use"
echo "  - rgthree-comfy"

echo ""
echo "Modelos:"
echo "  - MiniMax H3 FL2VA"
echo "  - MiniMax H3 REF2VA"
echo "  - MiniMax H3 ClipProj"
echo "  - Qwen3-VL 4B"
echo "  - MiniMax H3 Audio VAE"
echo "  - MiniMax H3 Video VAE"

echo ""
echo "Benchmark:"
echo "  $BENCH_DIR"

echo ""
echo "IMPORTANTE:"
echo "  O provisioning terminou."
echo "  O entrypoint da Vast continuará o boot."
echo "  O ComfyUI será liberado após /.provisioning ser removido."

echo ""
echo "============================================================"
