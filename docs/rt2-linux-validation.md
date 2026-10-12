# RT2 — validação do Runtime em Linux real

Executado em 2026-10-10 com Docker Desktop 28.0.4 (WSL2, kernel `5.15.167.4-microsoft-standard-WSL2`), **sem GPU, sem rede (`--network none`), sem baixar
modelos e com segredos sintéticos**. A imagem é a do `deploy/vast/Dockerfile` **sem nenhuma alteração**; o harness fica numa camada separada, só de teste
(`tests/linux/rt2/`, veja o README de la). Nenhum codigo de produto foi alterado no RT2.

## Resultado em uma frase

O ENTRYPOINT real (`tini -- python -m installer.remote_supervisor`) processa as variaveis `EDX_BOOTSTRAP_*`, gera manifesto schema 2 e credencial 0600 de forma
atomica, remove os segredos do ambiente dos filhos, roda o `RemoteSupervisor`/`RemoteBootstrap` reais ate um readiness simulado, e falha fechado em todos os
estados de erro testados (95 verificacoes, 0 falhas). Dois pontos ficam documentados como limites (ambiente legivel em `/proc` por processos root; o host do provedor ve o ambiente).

## 1. Imagem

| Item | Valor |
|---|---|
| Build | ok (`docker build -f deploy/vast/Dockerfile .`), 963 MB, ~9 min (apt: `build-essential`, `ffmpeg`, `git`, `tini`...) |
| Base | `python:3.11-slim-bookworm@sha256:0a310eee...` → Debian 12, Python 3.11.17 |
| ENTRYPOINT | `["/usr/bin/tini","--","python","-m","installer.remote_supervisor"]`, `CMD` vazio, `STOPSIGNAL SIGTERM`, WORKDIR `/opt/edx-comfy-runtime`, usuario root |
| Diretorios | `/workspace` 755, `/workspace/.edx` 700, `/run/secrets` 700 (root) |
| Dependencias | so a biblioteca padrao (pip lista apenas `pip/setuptools/wheel/packaging`); `git 2.39.5`, `ffmpeg 5.1.9`, `tini 0.19.0` |
| Aviso do build | `SecretsUsedInArgOrEnv` sobre `ENV EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE`: falso positivo (o valor e um **caminho**, nao um segredo) |
| Nao validado | GPU/driver, `nvidia-smi` (ausente de proposito), CUDA e wheels PyTorch `cu130` de `runtime.json`, download do ComfyUI e de modelos |

## 2. Testes unitarios no Linux (imagem + camada de teste)

* `tests/test_remote_env.py` (RT1): **34/34, 0 skipped** (no Windows eram 3 skipped: modos 0600/0700 reais, umask, symlink real; agora executados).
* `tests/test_remote_supervisor.py`: **11/11** (inclui o lock por identidade `/proc` e o teste que le o Dockerfile).
* `tests/test_remote_bootstrap.py`: **41/41**; `tests/test_compute_manager_contract.py`: **19 OK** (1 skipped: o snapshot irmao so existe no checkout lado a lado).
* Suite inteira do Runtime (386 testes): **na execucao final 1 falha e 4 skipped; em execucoes anteriores 3 falhas**. Nenhuma tem relacao com RT1/RT2:

| Falha | Causa (medida) |
|---|---|
| `test_krea2_reference_contract…test_component_source_is_pinned_real_and_not_empty` | Artefato do harness (deterministica): o teste confere o `HEAD` git do submodulo, e a imagem de producao (corretamente) nao carrega `.git`. |
| `test_model_validation_cache…test_same_size_corruption_detected…` e `test_uninstall…test_model_with_different_content_is_kept` | **Intermitentes** e pre-existentes, sensiveis ao relogio do sistema de arquivos: o cache de validacao usa `(tamanho, mtime_ns)` e, neste kernel, duas escritas seguidas do mesmo tamanho tem o **mesmo** `st_mtime_ns` em 1872 de 2000 tentativas. Isoladas, falharam em 11/12 e 8/12 execucoes; na execucao final da suite passaram. Nao tocam o bootstrap remoto. Nao foram alteradas. |

> Observacao de produto: o mesmo comportamento de `mtime` significa que uma corrupcao de modelo **do mesmo tamanho dentro da mesma fracao de tempo** de uma validacao anterior
> pode passar despercebida pelo cache. Na pratica a janela e de milissegundos; fica registrado, nao corrigido (fora do escopo do RT2).

## 3. Bootstrap pelo ENTRYPOINT real (cenarios)

Todos os cenarios usam o ENTRYPOINT sem alteracao; so o ComfyUI, o instalador/provisionador e o Compute Manager sao simulados (README do harness).

| # | Cenario | Resultado |
|---|---|---|
| S2 | Sucesso: variaveis `EDX_BOOTSTRAP_*` + `CONTAINER_ID` → ate `bootstrap confirmado; supervisionando ComfyUI` | PID 1 = tini, supervisor = filho do tini, ComfyUI simulado = filho do supervisor. Manifesto == o schema 2 esperado (provider id vindo de `CONTAINER_ID`). Callback unico `200`, `Authorization: Bearer <token da instancia>`, corpo `{"provider_instance_id":"5000"}`, validado pelo oraculo (mesmas regras do Compute Manager). |
| S2 | Permissoes reais | manifesto e credencial **0600**; `/workspace/.edx` e `/run/secrets` **0700**; nenhum `*.tmp` restante. |
| S2 | Credencial | o arquivo tem exatamente o token da instancia (nunca o token administrativo que tambem foi injetado no ambiente). Manifesto e `bootstrap-state.json` nao contem nenhum segredo. |
| S2 | Ambiente dos filhos | o filho (ComfyUI simulado, criado como o controlador real: herda `os.environ`) **nao** ve token, token administrativo, `CONTAINER_API_KEY` nem as variaveis consumidas; ve `CONTAINER_ID`, `COMFYUI_PATH`, `HF_HOME`, `PATH`. |
| S2 | Parada | `docker stop`: exit 0 em ~0,5 s, `encerrando ComfyUI`, lock liberado. |
| S2 | Reinicio | mesmo container: arquivos regerados com **os mesmos hashes**, ainda 0600, sem temporarios, callback idempotente repetido (2). `SIGKILL`: o lock velho fica e o proximo start o **recupera**. |
| S2 | Volume reutilizado por outra instancia | o manifesto e o estado novos substituem os antigos; a credencial **nao** persiste no volume `/workspace`. |
| S2b | Sem nenhuma simulacao, sem rede | materializa, e depois **falha fechado** no primeiro passo de rede (`BaseRuntimeInstaller`): exit 2, `phase=FAILED`, nada baixado, sem segredo em logs/estado. |
| S2c | Sem `EDX_BOOTSTRAP_*` | o comportamento anterior e identico (espera manifesto e credencial); SIGTERM encerra com exit 0. |
| S2 | Raiz somente leitura | `--read-only` + volume em `/workspace` + tmpfs em `/run/secrets` e `/tmp`: chega ao readiness. O Runtime escreve **so** em `/workspace` e `/run/secrets`. |
| S12 | Credencial em tmpfs | `/run/secrets` como tmpfs (so RAM), arquivo 0600; apos `docker restart` e regerado do ambiente entregue. |
| S4 | Entregas recusadas (9) | variavel ausente, `CONTAINER_ID` ausente, schema 3, origem nao confiavel, URL de outra instancia, URL com query, http externo sem flag, token com espaco: **exit 2, nada escrito, a mensagem nomeia a variavel/campo sem valor**. |
| S5 | Estados de falha (8) | token de **outra instancia** → 403; **expirado** → 401; **adulterado** → 401; `kid` desconhecido → 401; readiness rejeitada → 409; callback 5xx → 4 tentativas e falha; erro de provisionamento e ComfyUI indisponivel → **nenhum callback**. Todos: exit 2, `phase=FAILED`, sem segredo em stdout/estado. |
| S6 | Symlink no destino do manifesto | recusado (exit 2), alvo do link intacto, nenhuma credencial publicada. |
| S7 | Atomicidade sob concorrencia | 3 leitores × 1 escritor (volume + tmpfs): ~6,6 milhoes de leituras durante 1500 reescritas: **0 leituras parciais ou misturadas, 0 arquivo ausente, 0 temporarios**. |
| S8 | Logs | nenhum segredo em `docker logs` em nenhum cenario (3 starts do mesmo container incluidos); o token administrativo no ambiente e reportado apenas pelo **nome**. |

## 4. O que o container garante e o que ninguem protege do operador do host

**Depende das garantias do container / do Runtime (medido)**

* Arquivos: modos 0600/0700 reais, publicacao por rename atomico, recusa de symlink, recuperacao de lock, regeneracao idempotente.
* Processos filhos (ComfyUI, pip, git, custom nodes): ambiente limpo.
* Isolamento entre usuarios: um uid sem privilegio no container recebe `Permission denied` ao ler `/proc/1/environ`.
* Raiz somente leitura e credencial em tmpfs sao compativeis.

**Nao pode ser protegido do operador do host (e dos processos root do container)**

* **O host do provedor ve o ambiente**: `docker inspect` mostra `EDX_BOOTSTRAP_READINESS_TOKEN` e `CONTAINER_API_KEY` em texto. Isso e inerente a entregar segredo por ambiente; a mitigacao e o desenho do token (uma instancia, uma rota, expira com o TTL) e a rotacao de chaves.
* **Achado do RT2 (medido), corrigido no RT2.1**: sem o launcher, o ambiente original ficava legivel em `/proc/1/environ` (tini) e em `/proc/<supervisor>/environ` por qualquer processo **root** do container (o `os.environ.pop` limpa so o que os filhos herdam, nao a memoria que o kernel expoe de um processo ja iniciado). Detalhes e evidencias na secao 7.
* O token administrativo/`CONTAINER_API_KEY`/`JUPYTER_TOKEN` da Vast e as variaveis de conta sao injetados pelo provedor antes de qualquer codigo nosso: o Runtime so limita o que os **processos do container** enxergam depois. **O operador do host continua podendo acessar o ambiente fornecido pela Vast no momento da criacao.**

## 5. Ainda so uma GPU/instancia Vast real prova

Formato e mesclagem do `env` por instancia com `template_hash_id`; se o container recebe as variaveis no template oficial; que `CONTAINER_ID` e o id numerico da criacao;
limite de tamanho do `env`; visibilidade do ambiente ao host; pull da imagem imutavel; driver/CUDA/PyTorch `cu130`; egress DNS/HTTPS; cold start real vs prazo de readiness;
ComfyUI real (custom nodes, VRAM). Nada disso foi exercitado: a rede era `none` e nao havia GPU.

## 6. Como reproduzir

`python tests/linux/rt2/rt2_driver.py build` e `... all` (README em `tests/linux/rt2/`). Exige Docker e o repositorio `infra/edx-compute-manager` ao lado.

## 7. RT2.1 — o ambiente deixa de existir nos processos definitivos

**Correcao minima** (`installer/remote_launcher.py`, `ENTRYPOINT ["python", "-m", "installer.remote_launcher"]`): o launcher valida todos os campos, publica manifesto e
credencial (a mesma materializacao do RT1: 0600/0700, rename atomico) e entao faz `execve` de `tini -- python -m installer.remote_supervisor` com um ambiente
**novo e sanitizado**. Como o `execve` cria um bloco de ambiente novo, `/proc/1/environ` (agora o tini), o do supervisor e o de todo filho nao contem os segredos. O supervisor
nao materializa mais: se encontrar `EDX_BOOTSTRAP_*` (foi iniciado fora do launcher) ele recusa (exit 2, nada escrito). Sem `EDX_BOOTSTRAP_*` o ambiente segue intacto. SIGTERM durante
a materializacao encerra limpo (exit 0, temporarios removidos). `JUPYTER_TOKEN` entrou na lista de segredos removidos, ao lado de `CONTAINER_API_KEY`.

**Evidencias (Linux, ENTRYPOINT real, 95 verificacoes, 0 falhas)**

| Criterio | Resultado |
|---|---|
| `/proc/<pid>/environ` de **todo** processo da arvore do container (tini PID 1, supervisor, ComfyUI) | nenhum segredo (token, MAC do token, token administrativo, `CONTAINER_API_KEY`, `JUPYTER_TOKEN`) |
| `/proc/<pid>/cmdline` dos mesmos | nenhum segredo; PID 1 = `/usr/bin/tini -- python -m installer.remote_supervisor` |
| Variaveis entregues/consumidas que sobram | so as nao secretas que o supervisor precisa: `EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS`, `EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE` (um caminho) e `CONTAINER_ID` |
| Credencial | lida do arquivo 0600 (`EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE`); callback `200` com `Bearer <token da instancia>` |
| Supervisor iniciado SEM o launcher e com a entrega presente | recusado, exit 2, nada escrito, a mensagem aponta o launcher |
| Argumentos do ENTRYPOINT | atravessam launcher e tini ate o supervisor (`--wait-timeout 2` → o supervisor desiste com exit 2) |
| Sem `EDX_BOOTSTRAP_*` | comportamento anterior: espera o manifesto e a credencial; SIGTERM → exit 0 |
| Entrega invalida | exit 2 **antes** de publicar qualquer arquivo, sem iniciar o supervisor, sem segredo na mensagem |
| Reinicio, SIGKILL, SIGTERM, permissoes, atomicidade, symlink, volume reutilizado, logs | os mesmos cenarios da secao 3, todos verdes com o novo ENTRYPOINT |

Unitarios no Linux: `test_remote_launcher` 16/16, `test_remote_env` 34/34, `test_remote_supervisor` 11/11, `test_remote_bootstrap` 41/41, contrato 19 OK. Suite completa: 402 testes, as mesmas 3
falhas nao relacionadas da secao 2 (1 artefato do harness e 2 sensiveis ao `mtime`). Mutacoes: 13/13 detectadas.

**O que continua fora do alcance do Runtime**: o operador do host (e quem controla o daemon/painel da Vast) ve o ambiente fornecido na criacao; a Vast injeta variaveis de conta e do
provedor antes de qualquer codigo nosso; e um processo com acesso ao daemon pode ler o ambiente de qualquer container. A defesa e o desenho do token: uma instancia, uma rota,
expira com o TTL, rotacionavel por `kid`.
