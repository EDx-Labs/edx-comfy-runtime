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

Não há integração local Krea2, nem pesos MiniMax HIGH separados neste checkout;
por isso não existe perfil `krea2` e `minimax-h3-high` informa a pendência sem
inventar modelos ou URLs.

```powershell
python install_workflow.py --list
python install_workflow.py minimax-h3-fast --comfyui C:\ComfyUI --dry-run
python install_workflow.py minimax-h3-full --comfyui C:\ComfyUI --skip-models
```
