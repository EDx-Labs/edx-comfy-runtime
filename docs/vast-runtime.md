# Ambiente remoto Vast.ai

Este ambiente e opt-in. A imagem inicia `installer.remote_launcher`, que grava o bootstrap e executa `tini -- python -m installer.remote_supervisor`; os
entrypoints desktop (`install_workflow.py` e a integracao local do Studio) nao
importam nem executam o supervisor.

## Arquitetura reutilizada

O repositorio nao possuia Dockerfile, unidade systemd ou configuracao de
supervisor. O script historico `install_minimax_h3.sh` instala uma capacidade
especifica com uma pilha propria e, portanto, nao e usado aqui. A imagem chama
o bootstrap schema 2 existente, que por sua vez reutiliza:

- `BaseRuntimeInstaller` e `installer/runtime.json` para a revisao do ComfyUI,
  o venv e o backend PyTorch;
- `Provisioner` para capability, profile, dependencias recursivas, custom
  nodes, modelos, workflows, `.partial`, checksum, locks e estado persistente;
- `ComfyUIController` para o processo, `/system_stats` e `/object_info`;
- o callback autenticado somente depois de toda a validacao operacional.

O `remote_supervisor` acrescenta apenas ciclo de vida de container: espera
configuracao, exclusao mutua por PID, propagacao de shutdown, monitoramento do
PID do ComfyUI e encerramento TERM/KILL com prazo. `tini` e o PID 1 e recolhe
processos encerrados.

## Build reproduzivel

Inicialize os submodulos e construa a partir da raiz do repositorio:

```bash
git submodule update --init --recursive
docker build \
  -f deploy/vast/Dockerfile \
  --build-arg EDX_BASE_IMAGE='python:3.11-slim-bookworm@sha256:<digest-aprovado>' \
  -t registry.example/edx-comfy-runtime:<commit> .
```

O default sem digest e conveniente para desenvolvimento, mas um template de
producao deve usar digest e publicar a imagem com tag imutavel. Nao ha versao
CUDA duplicada no Dockerfile: `installer/runtime.json` e a autoridade existente
e atualmente instala wheels PyTorch do indice `cu130`.

## Template Vast.ai

Use uma imagem customizada no modo **Entrypoint**. Os modos SSH e Jupyter da
Vast substituem o entrypoint original da imagem; caso sejam usados para
diagnostico, o comando abaixo precisa ser configurado como on-start:

```bash
python -m installer.remote_launcher
```

Monte armazenamento persistente em `/workspace`. Um volume Vast local e
persistente entre instancias que reutilizem o mesmo volume, mas fica vinculado
a maquina fisica e nao pode ser movido entre hosts. Reserve disco suficiente
antes da criacao da instancia, pois o tamanho do disco/volume nao e expansivel
in-place.

O usuario do container precisa escrever em:

- `/workspace/ComfyUI` (runtime, modelos, nodes, workflows e estado);
- `/workspace/.edx` (manifesto, estado e lock do supervisor);
- `/workspace/.cache` (pip, Torch e Hugging Face);
- `/run/secrets` somente para a entrega do segredo.

Variaveis obrigatorias. Com a entrega por ambiente (CM5 + RT1) o **Compute Manager as define por instancia** na chamada de criacao (veja
`docs/remote-bootstrap.md`, 'Entrega por ambiente da instancia'); o template nao deve guardar nenhum segredo:

```text
EDX_BOOTSTRAP_SCHEMA_VERSION, EDX_BOOTSTRAP_INSTANCE_ID, EDX_BOOTSTRAP_CAPABILITIES, EDX_BOOTSTRAP_READINESS_URL,
EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS, EDX_BOOTSTRAP_READINESS_TOKEN   (todas por instancia, do Compute Manager)
CONTAINER_ID                                                         (injetada pela Vast)
EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE=/run/secrets/edx-compute-manager-token   (padrao da imagem; o entrypoint cria o arquivo 0600)
```

Sem as variaveis `EDX_BOOTSTRAP_*` o fluxo anterior continua valendo: outro produtor cria o manifesto e o arquivo de credencial.

Variaveis opcionais:

```text
COMFYUI_PATH=/workspace/ComfyUI
EDX_BOOTSTRAP_WAIT_TIMEOUT=0
EDX_BOOTSTRAP_POLL_INTERVAL=1
EDX_COMFYUI_SHUTDOWN_TIMEOUT=20
```

`EDX_BOOTSTRAP_WAIT_TIMEOUT=0` espera indefinidamente. Isso permite que o
container inicie antes de `/workspace/.edx/bootstrap.json`; nenhuma readiness
e enviada enquanto manifesto, segredo, provisionamento e validacoes nao forem
concluidos.

## Entrega do manifesto e do segredo

O produtor deve criar os dois arquivos de forma atomica, escrevendo um arquivo
temporario no mesmo filesystem e fazendo rename somente depois de completo:

```bash
install -d -m 0700 /workspace/.edx /run/secrets
install -m 0600 /origem/edx-compute-manager-token.tmp \
  /run/secrets/edx-compute-manager-token.tmp
mv /run/secrets/edx-compute-manager-token.tmp \
  /run/secrets/edx-compute-manager-token
install -m 0600 /origem/bootstrap.json.tmp /workspace/.edx/bootstrap.json.tmp
mv /workspace/.edx/bootstrap.json.tmp /workspace/.edx/bootstrap.json
```

Nao passe o token em argumento, manifesto, imagem ou log. O bootstrap ainda
aceita `EDX_COMPUTE_MANAGER_SERVICE_TOKEN` por compatibilidade de contrato, mas
o template remoto deve usar o arquivo. Em Linux, o Runtime rejeita arquivo de
segredo acessivel pelo grupo/outros e links simbolicos.

A Vast oferece variaveis de conta criptografadas em repouso e injetadas como
variaveis de ambiente. A documentacao publica consultada nao oferece um mount
nativo equivalente a Docker Secrets nas opcoes do template. Por isso o RT1
usa o ambiente por instancia como canal e **minimiza o que ele carrega**: so o
token escopado da propria instancia (declara READY para ela, ate o TTL, e nada
mais), que o entrypoint grava em arquivo 0600 e remove do ambiente antes de
qualquer processo filho. O ambiente continua legivel pelo host do provedor e,
dentro do container, por `/proc/1/environ`; um token administrativo nunca deve
ser entregue assim (e, se aparecer no ambiente, o entrypoint o ignora e remove).
Variaveis de conta da Vast sao injetadas em **todo** container: nao guarde ali
segredos administrativos.

## GPU, rede e persistencia

A instancia precisa de GPU NVIDIA exposta ao container pelo runtime NVIDIA da
Vast e de driver do host compativel com os wheels CUDA declarados no
`runtime.json`. Nao se fixa aqui uma versao de driver: a matriz oficial de
compatibilidade CUDA e as release notes do wheel/imagem aprovados devem ser
verificadas ao selecionar a oferta. O bootstrap valida de fato
`torch.cuda.is_available()` e a VRAM reportada por `/system_stats`; sem GPU ele
falha antes do callback.

Egress DNS/HTTPS e necessario para o repositorio ComfyUI, repositorios de
custom nodes, PyPI/files.pythonhosted.org, download.pytorch.org, provedores dos
modelos (incluindo Hugging Face/CDNs quando aplicavel) e a origem HTTPS do
Compute Manager. A API do ComfyUI escuta somente em `127.0.0.1:8189`; exposicao
publica nao faz parte deste checkpoint.

Modelos, workflows e caches ficam sob `/workspace`, evitando downloads validos
em reinicializacoes. O volume Vast local reduz o cold start, mas nao sobrevive
a perda/migracao da maquina que o hospeda.

## Ciclo de vida

1. O launcher materializa o bootstrap (se entregue por ambiente) e executa `tini`, que inicia o supervisor com o ambiente sanitizado.
2. O supervisor adquire `/workspace/.edx/remote-supervisor.lock`.
3. Ele espera manifesto completo e credencial valida.
4. O bootstrap instala/reutiliza o Runtime e valida as capabilities.
5. O callback de readiness e enviado.
6. Somente entao o supervisor registra sucesso e monitora o PID do ComfyUI.
7. SIGTERM/SIGINT cancela operacoes cooperativas e encerra o grupo ComfyUI.

Um lock vivo (PID mais identidade de inicio do processo Linux) impede dois
entrypoints sem confundir um PID reutilizado apos recriacao do container. Locks
e estados stale sao recuperados; o controlador existente reutiliza um ComfyUI
saudavel em vez de iniciar outro.
Se o ComfyUI morrer depois da readiness, o supervisor encerra com falha para
que a politica externa de restart possa recriar o container.

## Primeiro smoke em GPU

Sem contratar GPU, os testes cobrem espera, falhas, lock, reinicio e shutdown,
mas nao validam driver/CUDA, tempo real de download, carga de custom nodes ou
inferencia. O primeiro smoke precisa confirmar tambem:

- publicacao e pull da imagem imutavel;
- que o `env` por instancia enviado na criacao com `template_hash_id` chega ao entrypoint (o formato do campo, a mesclagem por chave com o ambiente do
  template, limite de tamanho) e que `CONTAINER_ID` e o id numerico devolvido pela criacao;
- oferta Vast com driver compativel e disco/volume suficientes;
- allowlist de egress e certificado do callback;
- tempo de cold start versus o timeout atual de readiness de 600 segundos.

READY continua significando readiness operacional, nao inferencia validada. Se
downloads frios ultrapassarem 600 segundos, o Compute Manager precisara ampliar
o timeout ou adicionar progresso/heartbeat em outro checkpoint; o Runtime nao
envia READY antecipadamente.

## Referencias operacionais

- [Vast.ai: Docker environment](https://docs.vast.ai/guides/instances/docker-environment)
- [Vast.ai: advanced template setup](https://docs.vast.ai/guides/templates/advanced-setup)
- [Vast.ai: volumes](https://docs.vast.ai/guides/instances/storage/volumes)
- [Vast.ai API: account environment variables](https://docs.vast.ai/api-reference/accounts/create-env-var)
- [NVIDIA Container Toolkit: install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [NVIDIA CUDA compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)
