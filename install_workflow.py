"""CLI pequena para o provisionador modular do EDx."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from installer.provisioning import Provisioner, ProvisioningError


def main() -> None:
    parser = argparse.ArgumentParser(description="Provisionador sob demanda de workflows EDx para ComfyUI")
    parser.add_argument("profile", nargs="?", help="capacidade, por exemplo minimax-h3-fast")
    parser.add_argument("--comfyui", type=Path, help="raiz da instalação ComfyUI")
    parser.add_argument("--list", action="store_true", help="lista as capacidades disponíveis")
    parser.add_argument("--dry-run", action="store_true", help="mostra o plano sem alterar arquivos")
    parser.add_argument("--skip-models", action="store_true", help="não baixa pesos")
    parser.add_argument("--skip-deps", action="store_true", help="não instala dependências Python")
    parser.add_argument("--no-pip", action="store_true", help="compatível com versões anteriores; igual a --skip-deps")
    args = parser.parse_args()
    provisioner = Provisioner()
    if args.list:
        for profile_id, profile in provisioner.list_profiles().items():
            print(f"{profile_id:24} {profile.get('description', '')}")
        return
    if not args.profile:
        parser.error("informe um perfil ou use --list")
    comfy = args.comfyui or (Path(os.environ["COMFYUI_PATH"]) if os.environ.get("COMFYUI_PATH") else None)
    if comfy is None:
        parser.error("informe --comfyui ou defina COMFYUI_PATH")
    try:
        plan = provisioner.plan(args.profile, comfy.expanduser().resolve())
        print(plan.format())
        if not args.dry_run:
            provisioner.install(plan, skip_models=args.skip_models, skip_deps=args.skip_deps or args.no_pip)
    except ProvisioningError as exc:
        parser.exit(2, f"[EDx] erro: {exc}\n")


if __name__ == "__main__":
    main()
