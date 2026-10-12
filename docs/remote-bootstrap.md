# Bootstrap remoto do EDx Runtime

O bootstrap remoto e um fluxo estritamente opt-in. Ele nao e importado nem
executado por `install_workflow.py`, nao altera os defaults do desktop e nao
detecta modo remoto por sistema operacional, container ou GPU.

## Arquitetura

`python -m installer.remote_bootstrap` apenas coordena APIs existentes:

1. `BaseRuntimeInstaller` instala ou valida o ComfyUI fixado em
   `installer/runtime.json`, incluindo o venv e o backend NVIDIA.
2. `Provisioner` resolve capability para profile, dependencias recursivas,
   components, modelos e workflow sets. Downloads, `.partial`, locks, cache de
   SHA-256, deduplicacao e estado continuam pertencendo ao provisionador.
3. `ComfyUIController` reutiliza uma API ja saudavel ou um PID previamente
   rastreado. Um lock atomico impede duas inicializacoes concorrentes.
4. A validacao operacional confere assets, runtime de components, backend,
   VRAM minima declarada, `/system_stats`, `/object_info` e os modulos dos
   custom nodes obrigatorios.
5. So entao `ReadinessCallbackClient` envia o callback autenticado.

Essa validacao e de readiness operacional. O bootstrap nao executa um workflow
de inferencia e registra explicitamente `inference_validated: false`.

## Contrato de manifesto isolado

O schema 2 continua confinado a `Schema2ManifestParser`, evitando espalhar o
contrato do Compute Manager pelo provisionador. O envelope Runtime-side abaixo
foi alinhado pelo Compute Manager no commit informado `81eed78`:

```json
{
  "schema_version": 2,
  "instance_id": "instance-uuid",
  "provider_instance_id": "5000",
  "capabilities": ["minimax-h3-fast"],
  "callback": {
    "readiness_url": "https://compute.example/api/v1/internal/instances/instance-uuid/readiness"
  }
}
```

O contrato nao e duplicado em outro arquivo neste repositorio. Se uma versao
futura mudar nomes ou o formato de `capabilities`, somente esse adaptador e
seus testes devem ser ajustados; nao se deve adicionar aliases especulativos.

Validacoes aplicadas:

- arquivo regular, nao simbolico, JSON UTF-8 e no maximo 1 MiB;
- `schema_version` exatamente 2;
- IDs nao vazios e sem separadores de caminho;
- lista de capabilities nao vazia;
- rota de readiness exatamente vinculada a `instance_id`;
- callback sem credenciais, query ou fragmento;
- origem presente em `EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS`;
- HTTPS obrigatorio, exceto loopback explicitamente habilitado para testes.

O manifesto e configuracao: seus valores nunca viram comandos, nomes de
pacotes, URLs de modelos ou scripts. Capabilities sao resolvidas apenas pelos
manifests confiaveis do Runtime.

## Segredo e execucao

O token nao pertence ao manifesto e nao e aceito por argumento de linha de
comando. Defina exatamente uma fonte:

```bash
export EDX_COMPUTE_MANAGER_SERVICE_TOKEN='...'
```

ou:

```bash
export EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE=/run/secrets/edx-compute-manager-token
```

Em POSIX, o arquivo deve ser regular, nao simbolico e sem permissoes para grupo
ou outros (por exemplo, modo `0600`). A imagem/gerenciador da instancia
precisa fornecer um desses canais de segredo; este repositorio nao distribui o
token. Desde o RT1 o proprio entrypoint pode gerar o arquivo a partir do ambiente
da instancia (secao seguinte), que e o caminho usado pelo Compute Manager (CM5).

A origem autorizada e configuracao separada e obrigatoria:

```bash
export EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS=https://compute.example
python -m installer.remote_bootstrap \
  --manifest /workspace/.edx/bootstrap.json \
  --comfyui /workspace/ComfyUI
```

`--comfyui` pode ser omitido quando `COMFYUI_PATH` estiver definido; sem ambos,
o entrypoint remoto usa `/workspace/ComfyUI`. Isso nao muda o comportamento do
entrypoint desktop.

HTTP so e aceito em loopback, para testes locais, quando a origem tambem estiver
na allowlist e `EDX_ALLOW_INSECURE_LOCAL_CALLBACK=1` estiver definido.

## Entrega por ambiente da instancia (RT1) e o launcher (RT2.1)

Quando o Compute Manager cria a instancia (CM5), ele entrega tudo no **ambiente por instancia** da propria chamada de criacao. O ENTRYPOINT da imagem e o
**launcher** (`python -m installer.remote_launcher`): ele detecta a entrega (qualquer variavel `EDX_BOOTSTRAP_*`) e, **antes** de existir qualquer processo
definitivo, usa `installer/remote_env.py` para:

1. ler somente estas variaveis (e `CONTAINER_ID`, injetada pelo provedor):

   | Variavel | Conteudo | Segredo |
   |---|---|---|
   | `EDX_BOOTSTRAP_SCHEMA_VERSION` | `2` | nao |
   | `EDX_BOOTSTRAP_INSTANCE_ID` | UUID do slot no Compute Manager | nao |
   | `EDX_BOOTSTRAP_CAPABILITIES` | ids separados por virgula | nao |
   | `EDX_BOOTSTRAP_READINESS_URL` | URL do callback desta instancia | nao |
   | `EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS` | origem autorizada (variavel que ja existia) | nao |
   | `EDX_BOOTSTRAP_READINESS_TOKEN` | token escopado desta instancia | **sim** |
   | `CONTAINER_ID` | id da instancia no provedor (Vast) = `provider_instance_id` | nao |

2. validar os campos obrigatorios: variavel ausente ou vazia, `schema_version` diferente de 2, `CONTAINER_ID` ausente ou invalida (aceita o
   identificador seguro do contrato; a forma Vast `C.<numero>` e normalizada para `<numero>`), capability vazia, token com espaco/controle. As mensagens citam **nomes** de variaveis, nunca valores;
3. gerar o manifesto schema 2 e conferi-lo com o **`Schema2ManifestParser` existente** (URL https, rota exata da instancia, origem na allow-list,
   identificadores): um manifesto invalido nao deixa arquivo algum;
4. gravar manifesto (`/workspace/.edx/bootstrap.json`) e credencial (`EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE`, padrao
   `/run/secrets/edx-compute-manager-token`) em **arquivos separados**, modo `0600`, por **escrita atomica** (temporario no mesmo diretorio, `fsync`,
   `rename`; o manifesto por ultimo, pois e o sinal de que a configuracao esta completa; destino simbolico e recusado; diretorios novos nascem `0700`);
5. remover de `os.environ` o token, qualquer token de servico administrativo (`EDX_COMPUTE_MANAGER_SERVICE_TOKEN`, ignorado e avisado sem o valor), a
   `CONTAINER_API_KEY` da Vast e as variaveis ja consumidas. ComfyUI, pip e git herdam um ambiente sem eles. O bootstrap recebe um mapeamento privado
   com apenas o necessario (origens confiaveis e o caminho do arquivo de credencial);
6. **substituir a propria imagem** (`execve`) por `tini -- python -m installer.remote_supervisor` com o ambiente sanitizado do passo 5, repassando ao supervisor
   os argumentos recebidos. O `execve` cria um bloco de ambiente novo: `/proc/1/environ` (agora o tini), o do supervisor e o de todo filho nunca contem os segredos
   (o RT2 mediu que, sem isso, o ambiente original ficava legivel em `/proc` por processos root do container). O tini continua PID 1 depois do exec. O
   `RemoteSupervisor`/`RemoteBootstrap` existentes seguem como estavam e leem a credencial do arquivo restrito; nao ha segunda infraestrutura de execucao.

A materializacao e **uma so**: o supervisor nao a faz mais; se encontrar `EDX_BOOTSTRAP_*` (ou seja, foi iniciado fora do launcher e nasceu com o ambiente
original) ele **recusa** (exit 2, nada escrito) e aponta o launcher. Uma entrega invalida termina o launcher com exit 2 **antes** de qualquer arquivo ser publicado e
sem iniciar o supervisor. SIGTERM durante a materializacao encerra limpo (exit 0, temporarios removidos).

Sem nenhuma variavel `EDX_BOOTSTRAP_*` o comportamento anterior (arquivos criados por outro produtor) e identico. Uma reinicializacao do container
regrava os mesmos arquivos; um volume reutilizado por outra instancia recebe o manifesto novo.

Limite que permanece (veja `docs/rt2-linux-validation.md`): **o operador do host continua podendo acessar o ambiente fornecido pela Vast no momento da criacao**
(`docker inspect`, o painel/API da Vast, o proprio daemon), e a Vast injeta variaveis de conta e do provedor (`CONTAINER_API_KEY`, `JUPYTER_TOKEN`...) antes de qualquer
codigo nosso: o launcher so limita o que os **processos do container** conseguem enxergar depois. O RT2 mediu o vazamento em `/proc/1/environ` e no supervisor; o RT2.1 o
elimina. Por isso o token continua escopado a uma instancia, expira com o TTL dela e so serve para o callback de readiness.

O contrato (`tests/contracts/compute-manager/fixtures/bootstrap-env`) e reproduzido por `tests/test_compute_manager_contract.py` contra este codigo real;
`tests/test_remote_env.py` cobre permissoes, atomicidade, remocao de segredos (com um processo filho de verdade) e o fluxo ate o callback com os fakes do bootstrap.
O RT2.2 reutiliza a mesma materializacao para o identificador provider-agnostic do CM6 (`fake-...`); detalhes em
[`docs/simulated-runtime-host.md`](simulated-runtime-host.md).

## Callback e reinicializacao

O corpo enviado e exatamente:

```json
{"provider_instance_id":"5000"}
```

O cliente envia `Authorization: Bearer <token>`, nunca registra o token e
nao segue redirects (para nao encaminhar a credencial a outra origem) e
considera qualquer 2xx como confirmacao. Timeout/conexao, 408, 425, 429 e 5xx
recebem no maximo quatro tentativas com backoff de 1, 2 e 4 segundos. Erros
3xx, 400, 401, 403, 404 e 409 nao sao repetidos.

O estado sem credenciais fica ao lado do manifesto em
`bootstrap-state.json`. Uma reinicializacao repete as validacoes, reaproveita
runtime/assets validos e pode repetir o callback, que e idempotente no Compute
Manager. Readiness nunca e enviada em estado `FAILED`.

## Timeout e limitacoes operacionais

O bootstrap nao envia READY antecipadamente. Perfis grandes podem exceder os
600 segundos atuais apenas em downloads, principalmente sem volume de cache
persistente. O Compute Manager precisa aumentar o timeout por perfil ou adotar
progresso/heartbeat em um checkpoint futuro; isso nao e implementado aqui.

Modelos marcados como `node-managed` continuam sob o Loader do custom node. Se
o snapshot obrigatorio ainda nao existir, o bootstrap falha fechado em vez de
declarar a capability pronta. Um mecanismo confiavel para preaquecer esses
snapshots deve ser acrescentado ao provisionador antes de usar tais profiles
remotamente.

Teste real em Vast.ai ainda depende de: Compute Manager implantado numa revisao
que contenha o contrato alinhado, o provedor honrar o ambiente por instancia (e
injetar `CONTAINER_ID` com o id numerico da instancia), a origem confiavel,
acesso aos hosts de modelos, volume de cache persistente, GPU/driver compativeis
e timeout do Compute Manager adequado.
