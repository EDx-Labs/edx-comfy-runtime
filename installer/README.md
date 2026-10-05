# Provisionamento modular EDx

`install_workflow.py` carrega manifests independentes em `profiles/`,
`components/`, `models/` e `workflow_sets/`. A API é `Provisioner` em
`installer.provisioning`; ela separa resolução, plano e instalação.

Os custom nodes são sempre copiados de fontes locais. Um node já presente sem
`.edx-component.json` é considerado externo e nunca é sobrescrito. Nodes
gerenciados recebem marcador com fingerprint; requirements só são executados
quando o fingerprint muda. Workflows são instalados por `workflow_set` em
`user/default/workflows/EDx/<set>/`, não por perfil.

Modelos com URL usam `.partial`, HTTP Range e validação SHA-256 quando o
manifest a fornece. Modelos `node-managed` (TTS/ASR) preservam o mecanismo de
snapshot existente dos nodes e são baixados pelo Loader na primeira execução.

Krea 2 usa os assets oficiais declarados em `models/registry.json`. O perfil
`krea2-reference` herda o modelo de difusão e o VAE de `krea2`, acrescentando
somente o encoder BF16 e seu workflow de referência. O grupo `krea2-full`
instala essa união de forma incremental, sem duplicar arquivos físicos.

```powershell
python install_workflow.py --list
python install_workflow.py minimax-h3-fast --comfyui C:\ComfyUI --dry-run
python install_workflow.py minimax-h3-full --comfyui C:\ComfyUI --skip-models
```
