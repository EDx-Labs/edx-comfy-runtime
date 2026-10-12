# RT2 — validação do Runtime em Linux real (Docker), sem GPU

Roda a **imagem do `deploy/vast/Dockerfile` como ela é**, pelo ENTRYPOINT real (`python -m installer.remote_launcher`, que executa `tini -- python -m installer.remote_supervisor`), com `--network none`,
sem GPU, sem baixar modelo e com segredos sintéticos. Resultados e análise: `docs/rt2-linux-validation.md`.

```bash
python tests/linux/rt2/rt2_driver.py build      # imagem 1: o Dockerfile intacto; imagem 2: camada SOMENTE de teste
python tests/linux/rt2/rt2_driver.py all        # todos os cenarios (ou: success tmpfs failures refused symlink real legacy proc readonly atomic)
```

Requer um daemon Docker em execucao e o repositorio `infra/edx-compute-manager` ao lado deste (o oraculo do token e a chave sintetica dos vetores sao
reaproveitados, para que o Compute Manager falso verifique tokens pelas mesmas regras do real). A evidencia fica em `$RT2_WORK/evidence`
(padrao: `<tmp>/edx-rt2/evidence`), fora do repositorio.

## O que e real e o que e simulado

| Real | Simulado (`support/sitecustomize.py`, so com `RT2_SMOKE=1`) |
|---|---|
| o launcher, tini como PID 1, `remote_supervisor.main`, `remote_env` (materializacao e limpeza do ambiente), `RemoteSupervisor`, `RemoteBootstrap`, `Schema2ManifestParser`, `load_service_token`, `ReadinessCallbackClient`, arquivos e permissoes do kernel Linux | o instalador do ComfyUI, o provisionador (nenhum modelo), o processo do ComfyUI (um filho que apenas grava o proprio ambiente) e o Compute Manager (callback em loopback que valida o token com o oraculo de referencia) |

A camada de teste (`Dockerfile.smoke`) so **acrescenta** arquivos (testes unitarios, harness); o ENTRYPOINT e todo o resto da imagem ficam intactos.
Cenarios: `success tmpfs failures refused symlink real legacy proc direct readonly atomic`. `proc` varre `/proc/<pid>/environ` e `cmdline` de todo processo da arvore do container; `direct` prova que o supervisor iniciado fora do launcher recusa a entrega.

## Higiene

Todo objeto Docker criado se chama `edx-rt2-*` e tem o rotulo `edx.rt2=1`; o driver nunca toca em outro container, imagem ou volume do daemon e remove
os containers e volumes que criou. As imagens ficam: `docker rmi edx-rt2/runtime-smoke:validate edx-rt2/runtime:validate`.
