# Qwen3-14B Q4_K_M para geração textual local (`qwen-text`)

Resumo da avaliação que levou o Qwen3-14B Q4_K_M a ser o modelo padrão de `qwen-text` /
`text-generation`. O `qwen-h3` (multimodal, `QwenH3PromptLocal`) continua com o Qwen3.8-27B + mmproj.

Hardware dos testes: RTX 3050 Laptop 6 GB, ~24 GB de RAM, llama.cpp b11115 (CUDA 13), Windows.
Os números são desta máquina e variam com a carga de fundo (a geração é limitada pela CPU).

## Artefato

| | |
|---|---|
| Arquivo | `Qwen3-14B-Q4_K_M.gguf` (oficial, `Qwen/Qwen3-14B-GGUF`, Apache-2.0) |
| Tamanho | 9.001.752.960 bytes (~8,38 GiB) |
| SHA-256 | `500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0` |
| Registry | `qwen3-14b-q4-k-m`, em `models/LLM/Qwen3-14B/`, revisão fixada |
| Visão | não tem (só texto); roda sem mmproj |

## Resultados

**Pipeline textual de 3 cenas** (Understanding, Visual Plan, Image Prompt Writer; cada modelo com a sua cadeia):
14B ≈ 2,1× mais rápido (386 s contra 806 s), ~29% menos RAM do servidor, load 13,7 s contra 24,9 s.
Understanding e Image Prompt Writer com perda pequena a relevante de detalhe/explicitude; contratos
estruturados (TextTask JSON) equivalentes.

**H3 Prompt Compiler** (caminho de texto, `first_last`, 3 casos reais do smoke, mesmas entradas, seed 0):

| | Instrução original | Instrução com bloco OUTPUT FORMAT |
|---|---|---|
| Válidos no validador estrito | 1/3 (2 casos devolveram as seções como JSON serializado dentro de `prompt`) | **3/3** |
| START→END preservado | 3/3 | 3/3 |
| Alucinação | sem aumento relevante (ambos inventam sons ambientes diegéticos) | idem |
| Tempo médio por caso | 67,1 s | **58,3 s** |
| Câmera | deficiente | deficiente (principal ponto restante) |

Para comparação, o 27B no mesmo teste: 3/3 válidos, 139,3 s em média.

A correção é só de formato: o `FIRST_LAST_INSTRUCTIONS` do Studio (`studio/h3_compiler.py`) troca a
última linha (`Return only JSON: ...`) por um bloco que explicita que `prompt` é UMA string de texto
(nunca objeto/array/JSON serializado), os rótulos exatos na ordem do validador, o término em
`non_diegetic_music: N/A`, um exemplo correto e um incorreto. O `chained` não foi alterado.

## Limitações (não afirmar mais do que isto)

- Teste pequeno: 3 casos, 1 seed, só o caminho de texto (sem imagens START/END; o caminho LOCAL de produção
  usa a skill `h3-prompt-writing` com imagens e não foi exercitado com o 14B, que não tem visão).
- Ainda não houve comparação visual dos vídeos do MiniMax H3.
- **Não há equivalência definitiva ao 27B.** O 14B é um candidato validado para geração textual local.
- A deficiência de câmera pode envolver também o Visual Plan ambíguo ("Estática ou pan lateral suave"):
  o 14B tende a copiar a hesitação ou a omitir a câmera; o 27B citou câmera nos 3 casos. Não foi tratada.
- A instrução `chained` tem a mesma última linha e não foi validada para esse problema.

## ctx 8192 + fit-target 768 (`qwen-text`)

Descoberto no mesmo estudo (RTX 3050 6 GB, Qwen3-14B): `--ctx-size 32768 --fit-target 1536` offloadava 11/41
camadas; `8192` + `768` offloada 18/41 (margem real ~750 MiB, sem OOM), +17% a +23% de geração em
medições intercaladas. `--fit-target` é a margem de memória livre a deixar por dispositivo.

**Estado:** implementado no commit `5fb4ffe` (branch `feat/qwen-text-inference-defaults`, base desta
branch), somente no caminho `QwenTextTaskLocal`, com override por `EDX_QWEN_TEXT_CTX_SIZE` e
`EDX_QWEN_TEXT_FIT_TARGET_MIB` e proteção de contexto (`CONTEXT_EXCEEDED`). O `qwen-h3` mantém 32768 e o
`fit-target` do `runtime_config`. A promoção do 14B a modelo padrão (`7b21e73`) é independente desses valores.

## Pendências

- Teste ampliado (8 a 10 cenas, 3 seeds, câmera decisiva, caminho da skill com imagens): decidido não
  executar por ora.
- Melhorar a explicitude/decisão de câmera (instrução e/ou Visual Plan).
- Variantes LOW/MEDIUM/HIGH: não implementadas.
- `recommended_vram` do profile `qwen-text` continua 20 (herdado quando usava o 27B); a política de
  hardware não foi alterada.
- Cosmético: o `--alias` do llama-server segue `qwen3.8-27b` mesmo com o 14B (não afeta o resultado).
