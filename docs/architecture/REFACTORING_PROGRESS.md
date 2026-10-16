# Progresso da refatoração arquitetural do runtime

## Estado atual

- Repositório: `edx-comfy-runtime`
- Branch: `develop`
- HEAD de início: `c71343e30096aeb2354aade17c72d7aced3ce3b5`
- Alteração preexistente preservada: `AGENTS.md` não rastreado.
- O provisionador atual mantém boa cobertura para downloads retomáveis, SHA-256,
  locks, ownership, idempotência e desinstalação segura.
- As fronteiras de catálogo, plano, estado e workflows já estão isoladas; a
  instalação de componentes agora também possui um adapter explícito.

## Backlog priorizado

1. **Catálogo e resolução declarativa** — concluído: extrair leitura dos
   manifestos, resolução de profiles/capabilities e requisitos para uma
   unidade pura, preservando as fachadas da API existente.
2. **Plano de instalação** — concluído: separar o contrato de dados do
   `InstallPlan` e validar referências declarativas antes das consultas de
   filesystem, mantendo `Provisioner.plan()` como API compatível.
3. **Execução e adapters** — em andamento: workflows e componentes concluídos;
   runtime e modelos ainda precisam de uma fronteira segura, sem alterar seus
   contratos.
4. **Estado e validação** — adapter de persistência concluído; permanece a
   separação futura das regras de validação/ownership sem alterar o formato.
5. **Caracterização contínua** — ampliar testes de falhas parciais, caminhos
   Windows/Linux, reexecução e compatibilidade de CLI.

## Matriz da auditoria de adapters

| Operação | Implementação atual | Dependências | Efeitos colaterais | Estado acessado | Testes |
| --- | --- | --- | --- | --- | --- |
| Componentes/custom nodes | `installer/component_adapter.py`; `Provisioner._component()` coordena o registro | catálogo de components, fonte local, funções de fingerprint/link/cópia, runner de pip e callbacks de cancelamento/progresso | cria ou atualiza `custom_nodes/<name>`, marcador `.edx-component.json`, executa pip quando declarado; preserva node externo | somente o orquestrador grava `state.components` | adapter + `test_component_install.py` + `test_local_component_idempotence.py`; 17 testes diretos |
| Runtime base ComfyUI | `installer/runtime.py:BaseRuntimeInstaller`, consumido por `remote_bootstrap.py` | `runtime.json`, runner injetável, git, venv/pip, marcador `.edx-runtime.json` | clone/fetch/checkout, criação de venv, instalação de backend e requirements | marcador do runtime; não usa `ProvisioningStateStore` | `test_managed_runtime.py`, cobertura sintética com runner falso |
| Runtime de backend de componente | `Provisioner._backend_runtime()` e `_runtime_downloader()`; installers específicos carregados por caminho | manifesto do component, `install_runtime.py`, hardware/plataforma, `_download()` e state store | downloads de runtime em cache, staging/extração controlada pelo installer e marcador/configuração do backend | `state.components[ident].backend_runtime` é salvo após validação | `test_llama_runtime_provisioning.py`, `test_managed_runtime.py` |
| Modelos | `Provisioner._model_path()`, `_valid_model()`, `_download()` e `_finalize_partial()` | manifestos de models, opener HTTP injetado, locks, cache SHA-256, state store | cria `.partial`, retoma ou reinicia downloads, promove arquivo validado, atualiza cache/ownership | cache de validação e `state.models`; ownership é decidido pelo orquestrador | `test_install_robustness.py`, `test_download_416.py`, `test_model_validation_cache.py`, integridade MiniMax/Music/Krea |
| Workflows | `installer/workflow_adapter.py:WorkflowInstaller`; `_workflows()` apenas delega | catálogo de workflow sets, fonte local, digest | copia JSON ao destino ComfyUI, preserva edição do usuário, registra hash | `state.workflows` | `test_workflow_adapter.py` e regressões de perfis |

Leitura arquitetural: o adapter de componentes possui uma fronteira segura
porque suas entradas e efeitos são locais e o estado pode continuar sendo
propriedade do orquestrador. O runtime de backend ainda mistura seleção de
hardware, download adaptado e persistência, enquanto o gerenciamento de
modelos concentra integridade, locks, retry e ownership em `Provisioner`; são
as próximas áreas que exigem caracterização antes de uma extração.

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

## Contratos preservados

- comandos e argumentos de `install_workflow.py`;
- formato dos manifestos e IDs de profiles, capabilities, components, models e
  workflow sets;
- `Provisioner` e seus métodos consumidos pelo backend/compute manager;
- formato de `InstallPlan`, estado persistente, downloads `.partial`, hashes,
  ownership e progressos;
- workflows, modelos, parâmetros e lógica funcional do MiniMax H3.

## Limitações de ambiente

- `python.exe`/`py.exe` disponíveis no host apontam para WindowsApps e falham
  com acesso negado. A validação foi executada com o interpretador local
  `C:\Users\Eduardo\.local\bin\python3.11.exe`, sem instalar dependências.

## Próxima tarefa segura

Caracterizar primeiro `Provisioner._backend_runtime()` e `BaseRuntimeInstaller`
para decidir se o runtime de backend pode ser extraído como adapter sem
duplicar o downloader, o state store ou os contratos de progresso. Se a
fronteira não for segura, registrar a decisão e avançar para a validação
isolada de modelos.
