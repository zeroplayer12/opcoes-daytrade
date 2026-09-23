# -*- coding: utf-8 -*-
"""Define a senha de acesso do painel.

Grava `%LOCALAPPDATA%\\PainelDayTrade\\acesso.json` (fora do OneDrive e do Git) com o hash da senha
— nunca a senha em si. Sem esse arquivo, o painel abre direto, como sempre abriu no PC.

    python configurar_senha.py            define ou troca a senha
    python configurar_senha.py --tirar    volta a abrir sem senha

No fim ele mostra as duas linhas para colar em Settings → Secrets do Streamlit Cloud, se você quiser
a mesma senha na cópia da nuvem.
"""
import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

ARQ = Path(os.environ.get("LOCALAPPDATA") or Path(__file__).resolve().parent) / "PainelDayTrade" / "acesso.json"
ITERACOES = 200_000


def main() -> int:
    if "--tirar" in sys.argv:
        if ARQ.exists():
            ARQ.unlink()
            print(f"senha removida ({ARQ}); o painel volta a abrir sem login")
        else:
            print("não havia senha configurada")
        return 0

    senha = getpass.getpass("Senha do painel (a digitação não aparece): ")
    if len(senha) < 8:
        print("mínimo de 8 caracteres.")
        return 1
    if senha != getpass.getpass("Repita a senha: "):
        print("as duas digitações não bateram.")
        return 1

    salt = secrets.token_bytes(16)
    dados = {"salt": salt.hex(),
             "hash": hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, ITERACOES).hex()}
    ARQ.parent.mkdir(parents=True, exist_ok=True)
    ARQ.write_text(json.dumps(dados, indent=2), encoding="utf-8")
    print(f"\nsenha gravada em {ARQ}")
    print("recarregue o painel (F5) para a tela de login aparecer.\n")
    print("Para usar a mesma senha no Streamlit Cloud, cole em Settings → Secrets:\n")
    print("[painel]")
    print(f'salt = "{dados["salt"]}"')
    print(f'hash = "{dados["hash"]}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
