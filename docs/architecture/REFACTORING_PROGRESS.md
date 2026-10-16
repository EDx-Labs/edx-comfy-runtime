# Progresso da refatoração arquitetural do runtime

## Estado atual

- Repositório: `edx-comfy-runtime`
- Branch: `develop`
- HEAD de início: `c71343e30096aeb2354aade17c72d7aced3ce3b5`
- Alteração preexistente preservada: `AGENTS.md` não rastreado.
- O provisionador atual mantém boa cobertura para downloads retomáveis, SHA-256,
  locks, ownership, idempotência e desinstalação segura.
- A principal fronteira ainda ausente era entre catálogo/resolução declarativa e
  a classe que executa filesystem, subprocessos, downloads e estado.

## Backlog priorizado

1. **Catálogo e resolução declarativa** — extrair leitura dos manifestos,
   resolução de profiles/capabilities e requisitos para uma unidade pura,
   preservando as fachadas da API existente.
2. **Plano de instalação** — em andamento: separar o contrato de dados do
   `InstallPlan` e validar referências declarativas antes das consultas de
   filesystem, mantendo `Provisioner.plan()` como API compatível.
3. **Execução e adapters** — em andamento: delimitar instalação de workflows,
   componentes, runtime e modelos em adapters pequenos, sem alterar seus
   contratos.
4. **Estado e validação** — em andamento: centralizar o adapter de persistência
   atômica sem alterar transições, ownership ou formato do estado existente.
5. **Caracterização contínua** — ampliar testes de falhas parciais, caminhos
   Windows/Linux, reexecução e compatibilidade de CLI.

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

Extrair a execução de componentes/modelos/workflows em adapters explícitos,
mantendo o `InstallPlan` como entrada somente leitura do executor e sem alterar
o estado persistente ou os contratos de progresso.
