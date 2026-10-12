"""Launcher do ENTRYPOINT remoto (RT2.1): materializa o bootstrap e so entao executa o tini e o supervisor com ambiente sanitizado.

O RT2 mediu em Linux real que o ambiente original de um processo continua legivel em ``/proc/<pid>/environ`` por processos root do container
mesmo depois de ``os.environ`` ser limpo, e que isso inclui o PID 1 (tini) e o supervisor. A correcao minima e nao deixar nenhum processo
DEFINITIVO nascer com o ambiente original: este launcher e o ENTRYPOINT (PID 1 por alguns milissegundos), faz o trabalho que precisa dos segredos e
substitui a propria imagem por ``tini -- python -m installer.remote_supervisor`` via ``execve`` com um ambiente novo. Um ``execve`` cria um bloco de
ambiente novo, entao ``/proc/1/environ`` (agora o tini), o do supervisor e o de todo filho (ComfyUI, pip, git) nunca contem os segredos.

    ENTRYPOINT ["python", "-m", "installer.remote_launcher"]   (deploy/vast/Dockerfile)

Com variaveis ``EDX_BOOTSTRAP_*`` (entrega por ambiente, CM5): ``installer.remote_env.prepare_from_environment`` valida TODOS os campos
(o ``Schema2ManifestParser`` existente confere o manifesto), publica manifesto e credencial (0600, escrita atomica, diretorios novos 0700),
e devolve o ambiente sanitizado: sem o token, sem token de servico administrativo, sem ``CONTAINER_API_KEY``/``JUPYTER_TOKEN`` da Vast e sem as variaveis
ja consumidas, mas COM o caminho do arquivo de credencial e a origem confiavel, que o supervisor continua lendo do arquivo restrito.
Sem essas variaveis o ambiente segue exatamente como chegou (inicializacao anterior inalterada).

Nao ha segundo mecanismo: e a mesma materializacao do RT1 (``remote_env``), executada antes do supervisor em vez de dentro dele. O supervisor passou a
recusar a entrega por ambiente quando iniciado diretamente, para que a garantia acima nao possa ser contornada sem querer.

Limite que nenhum codigo daqui remove: o host do provedor ve o ambiente fornecido na criacao da instancia (``docker inspect``), e variaveis de
conta da Vast sao injetadas antes de qualquer codigo nosso; so se limita o que os processos do container conseguem enxergar depois.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .provisioning import ProvisioningError
from .remote_bootstrap import DEFAULT_MANIFEST, RemoteBootstrapError
from .remote_env import delivery_requested, prepare_from_environment

TINI = "/usr/bin/tini"
SUPERVISOR_COMMAND = ("python", "-m", "installer.remote_supervisor")
EXIT_FAILED = 2


def supervisor_argv(forwarded: Sequence[str], tini: str = TINI) -> list[str]:
    """O comando definitivo: so constantes e os argumentos que o operador passou ao ENTRYPOINT. Nunca um segredo."""
    return [tini, "--", *SUPERVISOR_COMMAND, *forwarded]


def manifest_path(forwarded: Sequence[str]) -> Path:
    """O mesmo ``--manifest`` que o supervisor vai ler (o padrao e o do supervisor), para que ambos olhem o mesmo arquivo."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    known, _ = parser.parse_known_args(list(forwarded))
    return known.manifest


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: MutableMapping[str, str] | None = None,
    execve: Callable[[str, list[str], Mapping[str, str]], Any] = os.execve,
    tini: str = TINI,
) -> int:
    """Devolve um codigo de saida apenas quando NAO executa o supervisor (falha ou encerramento antes do exec); no sucesso nao retorna."""
    forwarded = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environment is None else environment

    def stop(signum, frame) -> None:  # um SIGTERM durante a materializacao encerra limpo: os temporarios sao removidos pelo finally dela
        del signum, frame
        raise SystemExit(0)

    if environment is None:
        for name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(name, stop)  # o exec reinstala a disposicao padrao: o tini define as dele
    try:
        if delivery_requested(env):
            child_environment = prepare_from_environment(env, manifest_path(forwarded))
        else:
            child_environment = dict(env)
    except SystemExit as exc:
        return int(exc.code or 0)
    except (RemoteBootstrapError, ProvisioningError) as exc:
        print(f"[EDx] launcher remoto falhou: {exc}", file=sys.stderr, flush=True)
        return EXIT_FAILED
    try:
        execve(tini, supervisor_argv(forwarded, tini), child_environment)
    except OSError as exc:
        print(f"[EDx] launcher remoto nao conseguiu executar {tini}: {type(exc).__name__}", file=sys.stderr, flush=True)
        return EXIT_FAILED
    return 0  # so alcancavel com um ``execve`` de teste


if __name__ == "__main__":
    raise SystemExit(main())
