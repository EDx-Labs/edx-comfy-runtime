"""Bootstrap remoto por variaveis de ambiente da instancia (RT1).

O Compute Manager (CM5) entrega, no ambiente por instancia da propria chamada de criacao, tudo que o Runtime precisa: nenhum comando remoto
precisa alcancar um host semi-iniciado. Este modulo apenas converte esse ambiente nos DOIS arquivos que o fluxo remoto existente ja le, e
entrega o controle a ele:

    EDX_BOOTSTRAP_SCHEMA_VERSION        "2"
    EDX_BOOTSTRAP_INSTANCE_ID           UUID do slot no Compute Manager
    EDX_BOOTSTRAP_CAPABILITIES          ids separados por virgula
    EDX_BOOTSTRAP_READINESS_URL         URL do callback desta instancia
    EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS origem autorizada (variavel que o Runtime ja possuia)
    EDX_BOOTSTRAP_READINESS_TOKEN       SEGREDO: token escopado desta instancia (so declara ESTA instancia pronta)
    CONTAINER_ID                        injetada pelo provedor (Vast): o id da instancia no provedor

    -> manifesto schema 2          (default /workspace/.edx/bootstrap.json)   lido por Schema2ManifestParser
    -> arquivo de credencial 0600  (EDX_COMPUTE_MANAGER_SERVICE_TOKEN_FILE, default /run/secrets/edx-compute-manager-token) lido por load_service_token

Nao ha segunda infraestrutura de execucao: o RemoteSupervisor e o RemoteBootstrap existentes continuam sendo quem espera os arquivos,
provisiona, sobe o ComfyUI e envia o callback. O manifesto gerado e validado pelo PROPRIO ``Schema2ManifestParser`` antes de ser publicado.

Garantias
* le somente as variaveis acima; mensagens de erro citam NOMES de variaveis, nunca valores;
* manifesto e credencial em arquivos separados, criados com modo 0600, por escrita atomica (arquivo temporario no mesmo diretorio, fsync,
  rename); um manifesto invalido nao deixa nenhum arquivo para tras;
* depois de gravar, os segredos (token, token de servico administrativo eventualmente presente, ``CONTAINER_API_KEY`` e ``JUPYTER_TOKEN`` da Vast) e as
  variaveis ja consumidas saem de ``os.environ`` e o ambiente sanitizado e devolvido. Desde o RT2.1 quem o usa e o ``installer.remote_launcher`` (o
  ENTRYPOINT), que executa o tini e o supervisor com ele via ``execve``: nenhum processo definitivo nasce com o ambiente original.

Limite que permanece: o host do provedor ve o ambiente fornecido na criacao (``docker inspect``) e a Vast injeta variaveis (de conta e do proprio
provedor) antes de qualquer codigo nosso. Por isso o token e escopado a uma instancia, expira com o TTL e so serve para o callback de readiness.
(O RT2 mediu que, SEM o launcher, o ambiente original ficava legivel em /proc/1/environ e no do supervisor; o RT2.1 fecha isso.)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from .remote_bootstrap import (
    DEFAULT_MANIFEST,
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    TRUSTED_ORIGINS_ENV,
    RemoteBootstrapError,
    Schema2ManifestParser,
    load_service_token,
)

ENV_SCHEMA = "EDX_BOOTSTRAP_SCHEMA_VERSION"
ENV_INSTANCE_ID = "EDX_BOOTSTRAP_INSTANCE_ID"
ENV_CAPABILITIES = "EDX_BOOTSTRAP_CAPABILITIES"
ENV_READINESS_URL = "EDX_BOOTSTRAP_READINESS_URL"
ENV_READINESS_TOKEN = "EDX_BOOTSTRAP_READINESS_TOKEN"
PROVIDER_ID_ENV = "CONTAINER_ID"  # documentada pela Vast como o id unico da instancia
PROVIDER_SECRET_ENV = "CONTAINER_API_KEY"  # chave por instancia que a Vast injeta em todo container: nunca deve chegar a filhos
PROVIDER_SECRET_ENVS = (PROVIDER_SECRET_ENV, "JUPYTER_TOKEN")  # a Vast documenta ambas como injetadas em toda instancia (a segunda nao serve ao Runtime)
DEFAULT_TOKEN_FILE = Path("/run/secrets/edx-compute-manager-token")
SCHEMA_VERSION = "2"

REQUIRED = (ENV_SCHEMA, ENV_INSTANCE_ID, ENV_CAPABILITIES, ENV_READINESS_URL, ENV_READINESS_TOKEN)
CONSUMED = REQUIRED + (TRUSTED_ORIGINS_ENV,)
SECRETS = (ENV_READINESS_TOKEN, TOKEN_ENV, *PROVIDER_SECRET_ENVS)
MAX_TOKEN_BYTES = 4096
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class EnvDeliveryError(RemoteBootstrapError):
    """Entrega por ambiente invalida. A mensagem cita nomes de variaveis e nunca os valores."""


def delivery_requested(environment: Mapping[str, str]) -> bool:
    """True quando alguma variavel EDX_BOOTSTRAP_* foi entregue: o modo e opt-in, sem ela o comportamento anterior e inalterado."""
    return any(name in environment for name in REQUIRED)


def _present(environment: Mapping[str, str], name: str) -> str:
    return str(environment.get(name, "") or "").strip()


def provider_instance_id(environment: Mapping[str, str]) -> str:
    raw = _present(environment, PROVIDER_ID_ENV)
    if not raw:
        raise EnvDeliveryError(f"{PROVIDER_ID_ENV} (id da instancia no provedor) ausente")
    # Vast sometimes exposes its numeric id as ``C.<id>``.  Normalize that
    # provider-specific spelling, but otherwise retain the provider-agnostic
    # identifier grammar already enforced by Schema2ManifestParser.  RT2.2
    # needs this for CM6 ids (``fake-...``); correlation remains exact at the
    # authenticated readiness endpoint.
    if raw.startswith("C."):
        if re.fullmatch(r"C\.[0-9]{1,20}", raw):
            return raw[2:]
        raise EnvDeliveryError(f"{PROVIDER_ID_ENV} invalida")
    if not _PROVIDER_ID.fullmatch(raw):
        raise EnvDeliveryError(f"{PROVIDER_ID_ENV} invalida")
    return raw


def manifest_from_environment(environment: Mapping[str, str]) -> dict[str, Any]:
    """O manifesto schema 2 correspondente ao ambiente. Validacao estrutural apenas: o conteudo (URL, origem, identificadores) e conferido
    em seguida pelo Schema2ManifestParser, que continua sendo a unica autoridade sobre o contrato do manifesto."""
    missing = [name for name in REQUIRED if not _present(environment, name)]
    if missing:
        raise EnvDeliveryError("variaveis de bootstrap ausentes ou vazias: " + ", ".join(missing))
    if _present(environment, ENV_SCHEMA) != SCHEMA_VERSION:
        raise EnvDeliveryError(f"{ENV_SCHEMA} incompativel; esperado {SCHEMA_VERSION}")
    capabilities = [item.strip() for item in _present(environment, ENV_CAPABILITIES).split(",")]
    if not capabilities or any(not item for item in capabilities):
        raise EnvDeliveryError(f"{ENV_CAPABILITIES} deve ser uma lista nao vazia de ids separados por virgula")
    return {
        "schema_version": int(SCHEMA_VERSION),
        "instance_id": _present(environment, ENV_INSTANCE_ID),
        "provider_instance_id": provider_instance_id(environment),
        "capabilities": capabilities,
        "callback": {"readiness_url": _present(environment, ENV_READINESS_URL)},
    }


def validate_token(token: str) -> str:
    value = token.strip()
    if not value:
        raise EnvDeliveryError(f"{ENV_READINESS_TOKEN} vazio")
    if len(value.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise EnvDeliveryError(f"{ENV_READINESS_TOKEN} grande demais")
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise EnvDeliveryError(f"{ENV_READINESS_TOKEN} contem espacos ou caracteres de controle")
    return value


# ------------------------------------------------------------------------------------------------ escrita privada e atomica
def _ensure_private_directory(directory: Path) -> None:
    if directory.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)  # so diretorios criados aqui; os pre-existentes pertencem a quem os preparou
    except OSError:
        pass


def _stage(path: Path, data: bytes) -> Path:
    """Escreve ``data`` num arquivo temporario 0600 no MESMO diretorio (mesmo filesystem), com fsync. Nao publica ainda."""
    if path.is_symlink():
        raise EnvDeliveryError(f"destino nao pode ser link simbolico: {path.name}")
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))  # mkstemp cria com modo 0600
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)  # explicito: nao depende do umask nem do comportamento do mkstemp da plataforma
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except BaseException:
        _discard(temporary)
        raise
    return temporary


def _commit(temporary: Path, path: Path) -> None:
    os.replace(temporary, path)  # atomico no mesmo filesystem: um leitor ve o arquivo antigo ou o novo completo, nunca um pedaco
    if os.name != "nt":
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass


def _discard(temporary: Path) -> None:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass


@dataclass(frozen=True)
class Materialized:
    manifest_path: Path
    token_path: Path
    instance_id: str
    provider_instance_id: str
    capabilities: tuple[str, ...]


def materialize(environment: Mapping[str, str], manifest_path: Path, token_path: Path) -> Materialized:
    """Gera e publica os dois arquivos. Tudo ou nada: um manifesto invalido (conferido pelo parser real) ou uma credencial invalida nao deixa
    arquivo algum; um erro depois disso restaura a consistencia removendo apenas os temporarios."""
    manifest = manifest_from_environment(environment)
    token = validate_token(_present(environment, ENV_READINESS_TOKEN))
    body = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    staged_manifest = _stage(manifest_path, body)
    staged_token: Path | None = None
    try:
        parsed = Schema2ManifestParser(environment).load(staged_manifest)  # o contrato do manifesto continua sendo do parser existente
        staged_token = _stage(token_path, token.encode("utf-8"))
        _commit(staged_token, token_path)
        staged_token = None
        _commit(staged_manifest, manifest_path)  # o manifesto por ultimo: e o sinal de que a configuracao esta completa
        staged_manifest = None  # type: ignore[assignment]
    finally:
        if staged_token is not None:
            _discard(staged_token)
        if staged_manifest is not None:
            _discard(staged_manifest)
    load_service_token({TOKEN_FILE_ENV: str(token_path)})  # confere modo e conteudo do arquivo publicado, sem guardar o segredo
    return Materialized(manifest_path, token_path, parsed.instance_id, parsed.provider_instance_id, parsed.capabilities)


# ------------------------------------------------------------------------------------------------ ambiente dos processos filhos
def private_environment(environment: Mapping[str, str], token_path: Path) -> dict[str, str]:
    """O mapeamento que o bootstrap recebe: o ambiente sem segredos, com o caminho do arquivo de credencial e as origens confiaveis."""
    private = {name: value for name, value in environment.items() if name not in SECRETS and name not in REQUIRED}
    private[TOKEN_FILE_ENV] = str(token_path)  # as origens confiaveis e o restante do ambiente seguem; so os segredos e o consumido saem
    return private


def scrub_process_environment(environment: MutableMapping[str, str]) -> list[str]:
    """Remove de ``os.environ`` os segredos e as variaveis ja consumidas. Devolve apenas os NOMES removidos."""
    removed = []
    for name in dict.fromkeys(SECRETS + CONSUMED):
        if name in environment:
            del environment[name]  # os.environ.__delitem__ chama unsetenv: processos filhos nao herdam
            removed.append(name)
    return removed


def prepare_from_environment(
    environment: MutableMapping[str, str],
    manifest_path: Path = DEFAULT_MANIFEST,
) -> dict[str, str]:
    """Ponto unico usado pelo supervisor: materializa, remove os segredos do ambiente real e devolve o mapeamento privado do bootstrap."""
    token_path = Path(_present(environment, TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE).expanduser()
    snapshot = dict(environment)
    materialize(snapshot, Path(manifest_path).expanduser(), token_path)
    admin_token_ignored = bool(_present(snapshot, TOKEN_ENV))
    private = private_environment(snapshot, token_path)
    scrub_process_environment(environment)
    print("[EDx] bootstrap por ambiente: manifesto e credencial gravados (modo 0600); segredos removidos do ambiente dos processos filhos", flush=True)
    if admin_token_ignored:
        print(f"[EDx] aviso: {TOKEN_ENV} ignorada e removida do ambiente; usando somente a credencial por instancia", flush=True)
    return private
