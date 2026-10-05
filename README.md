# EDx ComfyUI Runtime

## Runtime local gerenciado

`installer/runtime.json` declara a origem e a revisão do ComfyUI base. A API
`BaseRuntimeInstaller` prepara uma instalação isolada e retomável somente
quando o Studio solicita uma capability local. `Provisioner` continua sendo a
autoridade para resolver capability → profile → components → models →
workflow sets. Abrir o Studio não chama nenhuma dessas operações e não baixa
pesos.

Downloads de modelos usam `.partial`, HTTP Range, retry com backoff,
validação de tamanho/SHA-256 quando declarados e rename atômico. Dependências
dos custom nodes são instaladas com o Python isolado do ComfyUI, nunca com o
Python do Studio.

O Studio pode cancelar cooperativamente uma instalação ativa. Processos de
clone/pip são encerrados, downloads param entre chunks e o `.partial` é
preservado para HTTP Range no retry. Assets já validados não são removidos.
Locks por destino impedem que operações concorrentes baixem o mesmo arquivo.

O grupo de instalação `krea2-full` resolve `krea2` e `krea2-reference` como
capabilities distintas sobre uma união incremental de assets físicos.

## Pré-requisito

Informe a raiz de uma instalação válida do ComfyUI (`models/` deve existir):

```powershell
$env:COMFYUI_PATH = "C:\caminho\para\ComfyUI"
```

Ou acrescente `--comfyui "C:\caminho\para\ComfyUI"` a cada comando.

## Consultar capacidades

```powershell
python install_workflow.py --list
python install_workflow.py minimax-h3-fast --comfyui "C:\caminho\para\ComfyUI" --dry-run
```

## Instalar perfis

```powershell
# Vídeo
python install_workflow.py minimax-h3-fast
python install_workflow.py minimax-h3-high
python install_workflow.py minimax-h3-full
python install_workflow.py minimax-h3-easy-continuation
python install_workflow.py qwen-h3
python install_workflow.py seedvr2
python install_workflow.py edx-video-full

# Imagem
python install_workflow.py krea2
python install_workflow.py krea2-reference
python install_workflow.py krea2-full

# Música
python install_workflow.py minimax-music3
python install_workflow.py minimax-music3-low-vram

# Voz e transcrição — providers alternativos
python install_workflow.py qwen3-tts
python install_workflow.py chatterbox-tts
python install_workflow.py chatterbox-voice-clone
python install_workflow.py qwen-asr
```

## Opções

```powershell
# Mostra o plano sem alterar arquivos
python install_workflow.py krea2-reference --dry-run

# Instala nodes e workflows, sem baixar modelos
python install_workflow.py minimax-music3 --skip-models

# Não executa pip para requirements dos nodes
python install_workflow.py chatterbox-tts --skip-deps

# Alias compatível para --skip-deps
python install_workflow.py chatterbox-tts --no-pip
```

Os workflows são instalados em `ComfyUI/user/default/workflows/EDx/`.
