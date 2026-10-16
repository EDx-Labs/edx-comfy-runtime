# Progresso da refatoração arquitetural do runtime

## Estado atual

- Repositório: `edx-comfy-runtime`
- Branch: `develop`
- HEAD de início da sessão R2: `b431ca1e300ca74391ad5c0ce7d1d295387901ff`
- Alteração preexistente preservada: `AGENTS.md` não rastreado.
- O provisionador atual mantém boa cobertura para downloads retomáveis, SHA-256,
  locks, ownership, idempotência e desinstalação segura.
- As fronteiras de catálogo, plano, estado, workflows, componentes, runtime de
  backend e modelos agora possuem adapters explícitos; o orquestrador conserva
  a coordenação e o ownership persistido.

## Backlog priorizado

1. **Catálogo e resolução declarativa** — concluído: extrair leitura dos
   manifestos, resolução de profiles/capabilities e requisitos para uma
   unidade pura, preservando as fachadas da API existente.
2. **Plano de instalação** — concluído: separar o contrato de dados do
   `InstallPlan` e validar referências declarativas antes das consultas de
   filesystem, mantendo `Provisioner.plan()` como API compatível.
3. **Execução e adapters** — concluído para workflows, componentes, runtime de
   backend e modelos; permanece a revisão de composição do orquestrador sem
   alterar contratos.
4. **Estado e validação** — adapter de persistência concluído; permanece a
   separação futura das regras de validação/ownership sem alterar o formato.
5. **Caracterização contínua** — ampliar testes de falhas parciais, caminhos
   Windows/Linux, reexecução e compatibilidade de CLI.

## Matriz da auditoria de adapters

| Operação | Implementação atual | Dependências | Efeitos colaterais | Estado acessado | Testes |
| --- | --- | --- | --- | --- | --- |
| Componentes/custom nodes | `installer/component_adapter.py`; `Provisioner._component()` coordena o registro | catálogo de components, fonte local, funções de fingerprint/link/cópia, runner de pip e callbacks de cancelamento/progresso | cria ou atualiza `custom_nodes/<name>`, marcador `.edx-component.json`, executa pip quando declarado; preserva node externo | somente o orquestrador grava `state.components` | adapter + `test_component_install.py` + `test_local_component_idempotence.py`; 17 testes diretos |
| Runtime base ComfyUI | `installer/runtime.py:BaseRuntimeInstaller`, consumido por `remote_bootstrap.py` | `runtime.json`, runner injetável, git, venv/pip, marcador `.edx-runtime.json` | clone/fetch/checkout, criação de venv, instalação de backend e requirements | marcador do runtime; não usa `ProvisioningStateStore` | `test_managed_runtime.py`, cobertura sintética com runner falso |
| Runtime de backend de componente | `installer/runtime_adapter.py:BackendRuntimeAdapter`; fachadas antigas do `Provisioner` apenas injetam dependências e aplicam o resultado | manifesto do component, `install_runtime.py`, hardware/plataforma, downloader e callbacks de progresso/cancelamento | downloads de runtime em cache, staging/extração controlada pelo installer e marcador/configuração do backend | `state.components[ident].backend_runtime` é salvo pelo orquestrador após validação | adapter + `test_llama_runtime_provisioning.py` + `test_managed_runtime.py`; 36 testes direcionados |
| Modelos | `installer/model_adapter.py:ModelAdapter`; fachadas `_valid_model()`/`_download()` preservam consumidores legados | manifestos de models, opener HTTP injetado, locks injetados, hash, cache de validação e callback de ownership | cria `.partial`, retoma ou reinicia downloads, promove arquivo validado e atualiza cache; não conhece o schema de estado | cache de validação é do adapter; `state.models`/ownership continuam no callback do `Provisioner` | adapter + `test_install_robustness.py`, `test_download_416.py`, `test_model_validation_cache.py`, integridade MiniMax/Music/Krea; 68 testes direcionados |
| Workflows | `installer/workflow_adapter.py:WorkflowInstaller`; `_workflows()` apenas delega | catálogo de workflow sets, fonte local, digest | copia JSON ao destino ComfyUI, preserva edição do usuário, registra hash | `state.workflows` | `test_workflow_adapter.py` e regressões de perfis |

Leitura arquitetural: os três adapters de execução recebem operações externas
por injeção, não importam o orquestrador e devolvem resultados para a
coordenação. O `ProvisioningStateStore` continua com proprietário claro:
componentes, runtime e workflows são registrados pelo provisionador, enquanto
o adapter de modelos recebe apenas o callback que materializa ownership no
momento de promoção do arquivo validado.

## Marco executado

### Marco 1 — catálogo declarativo isolado

Implementado: `installer.catalog.ManifestCatalog` e `ProfileResolution` foram
extraídos sem modificar manifestos, workflows, CLI, capabilities públicas ou
execução de instalação. `Provisioner` continua expondo os métodos existentes e
mantém aliases mutáveis para os registries, preservando integrações e testes.

Commit: `49f4c18 refactor(runtime): isolate manifest catalog resolution`.

Validações executadas:

- testes sintéticos de resolução sem ComfyUI, downloads ou subprocessos;
- regressão dos testes de provisionamento diretamente afetados;
- import/sintaxe dos módulos alterados;
- CLI `--list` e `--dry-run` em diretório temporário;
- regressão específica de MiniMax H3, Krea e validação de modelos;
- revisão de diff garantindo que MiniMax H3 e workflows não foram alterados.

### Marco 2 — contrato de plano e referências declarativas

Implementado: `InstallPlan` agora vive em `installer.plans`, enquanto
`ManifestCatalog.resolve_installation()` valida referências de components,
models e workflow sets antes de o `Provisioner` observar ou alterar qualquer
instalação. A classe `Provisioner` continua reexportando `InstallPlan` por
compatibilidade do import existente.

Commit: `14eeb29 refactor(runtime): isolate installation plan contract`.

Validações executadas:

- 50 testes de provisionamento, retry, idempotência e MiniMax Music 3;
- import de compatibilidade `installer.provisioning.InstallPlan`;
- compilação dos módulos alterados.

### Marco 3 — adapter de estado persistente

Implementado: `ProvisioningStateStore` centraliza o caminho, defaults e escrita
atômica do estado do provisionador. O store é neutro quanto a ownership e
mantém registros desconhecidos, enquanto `Provisioner` e `Uninstaller` retêm
as mesmas regras de validação e o mesmo schema JSON. A fachada também mantém
sincronizadas reatribuições dos registries legados usadas por adapters e testes.

Commit: `97cdee2 refactor(runtime): centralize provisioning state storage`.

Validações executadas:

- 88 testes de estado, provisionamento, desinstalação, integridade e retry;
- compilação/import dos módulos alterados;
- regressão de reatribuição dos registries legados, corrigida antes do commit.

### Marco 4 — adapter de workflow sets

Implementado: `WorkflowInstaller` isola a cópia de workflow sets no destino
ComfyUI e mantém o registro de hash/edição do usuário. Ele recebe o digest e o
catálogo por injeção, não faz downloads, não interpreta JSON e não cria um novo
contrato público.

Commit: `d84277b refactor(runtime): isolate workflow installation adapter`.

Validações executadas:

- 88 testes de provisionamento, desinstalação e contratos MiniMax H3/Krea;
- testes próprios de idempotência e preservação de edição do usuário;
- compilação e import do adapter.

### Marco 5 — adapter de componentes

Implementado: `ComponentAdapter` recebeu a instalação de custom nodes,
preservação de componentes externos, cópia/link idempotente, marcador local e
seleção dos comandos de dependência. O adapter recebe runners e funções de
filesystem por injeção e não acessa o estado persistente; `Provisioner` mantém
a coordenação, a compatibilidade da API privada e o registro de
`state.components`.

Commit: `8fb96bd refactor(runtime): isolate component installation adapter`.

Validações executadas:

- baseline antes da extração: 13 testes de componentes, 1 skip de symlink;
- adapter novo + regressão de componentes: 17 testes, 1 skip de symlink;
- regressão ampliada de provisionamento, estado, desinstalação e perfis: 94
  testes, 1 skip;
- regressão ampliada de downloads, integridade, MiniMax e Krea: 70 testes;
- compilação e `git diff --check`.

Nenhum manifesto, workflow, modelo, URL, hash ou parâmetro MiniMax H3 foi
alterado.

### Marco 6 — adapter de runtime de backend

Implementado: `BackendRuntimeAdapter` isolou a resolução do installer por
componente, detecção de plataforma/hardware, seleção de candidatos, adaptação
do downloader comum, tratamento de cancelamento/falha e verificação final.
`Provisioner` continua expondo as fachadas existentes e grava o registro
`backend_runtime` somente depois de o adapter confirmar readiness.

Commit: `b1f33ae refactor(runtime): isolate backend runtime adapter`.

Validações executadas:

- baseline antes da extração: 35 testes de runtime base e runtimes de backend;
- adapter novo + regressão: 36 testes;
- nenhuma instalação real, download real ou execução de `llama-server`.

### Marco 7 — adapter de modelos

Implementado: `ModelAdapter` isolou resolução de destino, cache de validação,
single-flight de SHA-256, promoção de `.partial`, retomada, retry, tratamento
HTTP 416 e emissão de progresso. Locks do filesystem, opener, hash e ownership
são dependências explícitas; o callback de ownership mantém o
`ProvisioningStateStore` sob o `Provisioner`.

Commit: `527e5b1 refactor(runtime): isolate model validation and downloads`.

Validações executadas:

- adapter novo + validação, retry, HTTP 416, locks, integridade MiniMax e Music:
  68 testes;
- regressão ampliada sequencial de componentes, runtime, modelos, estado,
  workflows, desinstalação, MiniMax e Krea: 213 testes, 1 skip;
- compilação, imports e `git diff --check`.

Uma tentativa de executar dois processos de teste em paralelo exibiu uma
exceção de remoção concorrente de lock temporário no Windows; os mesmos
conjuntos passaram isoladamente e na regressão sequencial, sem falha funcional.

Nenhum manifesto, workflow, modelo, URL, hash ou parâmetro MiniMax H3 foi
alterado.

### Checkpoint de compatibilidade após os adapters

- `--list` e `--dry-run minimax-h3-fast` passaram em diretório temporário;
- a bateria histórica direcionada passou: 88 testes;
- a regressão ampliada sequencial passou: 213 testes, 1 skip conhecido de
  symlink;
- compilação de `installer/`, imports dos adapters e `git diff --check`
  passaram;
- a inspeção de `Provisioner.plan()`/`install()` não justificou um executor
  genérico adicional: suas responsabilidades restantes são resolução de
  diagnóstico, agregação de progresso, ordenação e commit do estado;
- `AGENTS.md` não rastreado preexistente permanece preservado e fora do
  staging.

## Contratos preservados

- comandos e argumentos de `install_workflow.py`;
- formato dos manifestos e IDs de profiles, capabilities, components, models e
  workflow sets;
- `Provisioner` e seus métodos consumidos pelo backend/compute manager;
- formato de `InstallPlan`, estado persistente, downloads `.partial`, hashes,
  ownership e progressos;
- workflows, modelos, parâmetros e lógica funcional do MiniMax H3.

## R2 — resiliência e compatibilidade

### R2.1 — caracterização de downloads resilientes

Adicionados testes determinísticos em `tests/test_model_resilience.py` para:

- timeout durante streaming, preservação do `.partial` e retomada por `Range`;
- resposta com `Content-Length` inconsistente, mantendo tamanho/hash do corpo
  como fonte de verdade antes da promoção;
- falha transitória em `Path.replace`, com retry da promoção sem repetir a
  requisição HTTP;
- duas requisições concorrentes para o mesmo modelo, verificando lock,
  materialização única e retorno idempotente do segundo consumidor.

O conjunto direcionado de modelos, downloads, locks e cache passou de 51 para
55 testes nesta sessão. A implementação existente já cobria os demais casos
R2.1 (200/206/416, parcial incompleto/completo, mismatch de `Content-Range`,
hash inválido, retries finitos, cancelamento e arquivo final válido/ inválido).

Não foi reproduzido defeito de produção neste marco; portanto nenhuma lógica
de download foi alterada. `Content-Length` não é tratado como prova de
integridade: o contrato atual valida os bytes efetivamente gravados por tamanho
esperado e SHA-256 quando declarados. Os testes executados são Windows; Linux
continua não validado nesta sessão.

Commit do marco: pendente nesta atualização do checkpoint.

### Matriz de estados R2.1

| Estado anterior | Evento | Resultado observado | Persistência | Recuperação |
| --- | --- | --- | --- | --- |
| ausente | resposta completa e hash válido | arquivo final promovido | cache/ownership após promoção | não baixa novamente |
| `.partial` incompleto | 206 compatível | bytes anexados e validados | `.partial` permanece até promoção | retoma por `Range` |
| `.partial` incompleto | timeout/truncamento | erro transitório e parcial preservado | sem sucesso prematuro | retry retoma do offset |
| `.partial` | 200 ignorando Range ou 206 incompatível | parcial descartado antes de recomeçar | arquivo final intacto | download completo sem concatenação |
| `.partial` completo | 416 com tamanho remoto igual | valida e promove sem novo corpo | cache/ownership após promoção | idempotente |
| qualquer | hash/tamanho inválido | não promove; parcial inválido é removido | sem ownership | nova execução parte de estado seguro |
| final válido | nova solicitação concorrente | segundo consumidor retorna reutilização | nenhum overwrite | lock thread/processo |
| final válido | falha transitória na promoção | retry da promoção, sem HTTP extra | sucesso só após promoção | retry local |

## Limitações de ambiente

- `python.exe`/`py.exe` disponíveis no host apontam para WindowsApps e falham
  com acesso negado. A validação foi executada com o interpretador local
  `C:\Users\Eduardo\.local\bin\python3.11.exe`, sem instalar dependências.

## Próxima tarefa segura

Próxima evolução segura: ampliar caracterização de falhas parciais e caminhos
Windows/Linux, sem criar novos adapters até surgir uma responsabilidade
concreta fora das fronteiras atuais. Manter a revisão dos contratos
compartilhados e a regressão de MiniMax H3 antes de qualquer mudança futura.
