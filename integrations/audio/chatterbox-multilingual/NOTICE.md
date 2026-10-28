# ComfyUI-Chatterbox-Multilingual (vendored)

Cópia de https://github.com/filliptm/ComfyUI_Fill-ChatterBox, commit f7d7a16187430abcaf91a3039b9c83aa9960816a
(README declara licença MIT; o repositório não traz arquivo LICENSE). Modelos: ResembleAI/chatterbox (MIT).

Alteração do EDx: o modelo multilíngue carrega `t3_mtl23ls_v3.safetensors` em vez de `t3_mtl23ls_v2.safetensors`
(`chatterbox_node.py` e `local_chatterbox/chatterbox/mtl_tts.py`). O upstream só troca o arquivo de pesos do T3
(mesma arquitetura, `T3Config.multilingual()`). Removidos: assets, tests, workflows de exemplo.
