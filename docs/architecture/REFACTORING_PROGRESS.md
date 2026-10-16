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
2. **Plano de instalação** — separar a montagem do `InstallPlan` das consultas
   de filesystem, mantendo `Provisioner.plan()` como API compatível.
3. **Execução e adapters** — delimitar instalação de componentes, runtime,
   modelos e workflows em adapters pequenos, sem alterar seus contratos.
4. **Estado e validação** — centralizar transições persistentes e revalidação
   de ownership/integridade, preservando o formato de estado existente.
5. **Caracterização contínua** — ampliar testes de falhas parciais, caminhos
   Windows/Linux, reexecução e compatibilidade de CLI.

## Marco executado

### Marco 1 — catálogo declarativo isolado

Implementado: `installer.catalog.ManifestCatalog` e `ProfileResolution` foram
extraídos sem modificar manifestos, workflows, CLI, capabilities públicas ou
execução de instalação. `Provisioner` continua expondo os métodos existentes e
mantém aliases mutáveis para os registries, preservando integrações e testes.

Validações executadas:

- testes sintéticos de resolução sem ComfyUI, downloads ou subprocessos;
- regressão dos testes de provisionamento diretamente afetados;
- import/sintaxe dos módulos alterados;
- CLI `--list` e `--dry-run` em diretório temporário;
- regressão específica de MiniMax H3, Krea e validação de modelos;
- revisão de diff garantindo que MiniMax H3 e workflows não foram alterados.

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

Extrair a resolução do plano declarativo de recursos do `InstallPlan`, usando a
resolução agora isolada e mantendo as verificações de filesystem no executor.
