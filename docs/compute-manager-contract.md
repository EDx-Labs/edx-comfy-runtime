# Contrato com o Compute Manager (lado do Runtime)

O Runtime **consome** tres contratos do Compute Manager e nunca os redefine:

1. o **manifesto de bootstrap, schema 2** (`/workspace/.edx/bootstrap.json`), lido por
   `installer.remote_bootstrap.Schema2ManifestParser`;
2. o **callback de readiness** (`POST /api/v1/internal/instances/{instance_id}/readiness`), enviado por
   `ReadinessCallbackClient`;
3. (CM5/RT1) a **entrega do bootstrap por ambiente da instancia**, convertida em manifesto e arquivo de credencial 0600 por
   `installer.remote_env`; as fixtures `fixtures/bootstrap-env` sao reproduzidas contra esse codigo real.

A fonte canônica é `edx-compute-manager/contracts/` (schemas JSON, unidades, estados, fixtures). Aqui existe só
um **snapshot somente leitura**, travado por hash, em `tests/contracts/compute-manager/` (sem schemas: o
Runtime usa apenas a biblioteca padrão). Ele é regenerado no Compute Manager com `contracts/tools/vendor.py`,
nunca editado à mão, e `tests/test_compute_manager_contract.py` falha se divergir.

## O que os testes provam

* Cada fixture de manifesto é reproduzida pelo **parser real**, e o veredito (aceita/rejeita) tem de coincidir
  com o registrado no contrato. O Runtime é um *leitor tolerante*: ignora chaves desconhecidas, aceita
  `provider_instance_id` inteiro, normaliza capabilities para minúsculas. Produtores (Compute Manager) devem
  ser estritos; isso não vira promessa do Runtime.
* O corpo do callback enviado é exatamente `{"provider_instance_id":"…"}`, e o segredo viaja só no cabeçalho.
* Os ids de capability do Control Plane (`minimax`, `krea2`, `krea2-reference`, `seedvr2`) resolvem no
  `Provisioner` real.

## Incompatibilidades conhecidas que tocam o Runtime (não corrigidas no C0)

| Id | Fato | Teste |
|---|---|---|
| I-1 | Manifesto com `capabilities: []` é recusado (o Compute Manager ainda pode emiti-lo) | `test_known_incompatibility_i1_empty_capabilities_is_refused` |
| I-2 | O id `minimax-h3` (exemplo da documentação do Compute Manager) não resolve | `test_known_incompatibility_i2_minimax_h3_does_not_resolve` |
| I-3 | A checagem de VRAM é estrita em `min_vram × 1024² bytes` sobre o que o ComfyUI reporta; se um cartão de 12 GiB reporta um pouco menos, o perfil o recusa (hipótese; exige GPU real) | `test_known_incompatibility_i3_vram_boundary_is_strict` |
| I-4 | A gramática de ids do Runtime é mais permissiva (até 128 caracteres; capability em maiúsculas) que a do contrato | `test_identifier_grammars_agree_except_for_the_documented_tolerances` |
| I-5 | Esta documentação (`docs/vast-runtime.md`) cita readiness de 600 s; o Compute Manager usa 3600 s por padrão | documentado no contrato do Compute Manager |

## Como rodar

```
python -B -m unittest discover -s tests -p test_compute_manager_contract.py
```

Offline, sem instalar, baixar ou iniciar nada.
