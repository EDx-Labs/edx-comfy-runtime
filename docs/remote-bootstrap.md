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
ou outros (por exemplo, modo `0600`). A imagem/gerenciador da instancia ainda
precisa fornecer um desses canais de segredo; este repositorio nao distribui o
token.

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
que contenha o contrato alinhado, injecao segura do token e da origem confiavel,
acesso aos hosts de modelos, volume de cache persistente, GPU/driver compativeis
e timeout do Compute Manager adequado.
