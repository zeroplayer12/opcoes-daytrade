# -*- coding: utf-8 -*-
"""
Liga os avisos do painel no Telegram.

Antes, crie o seu bot (1 minuto, pelo próprio Telegram):
  1. procure @BotFather e mande /newbot
  2. escolha um nome e um usuário terminado em "bot"
  3. ele responde com o token (algo como 123456789:AAE...) — é a senha do bot

Depois rode, no terminal, dentro da pasta do painel:
  python configurar_telegram.py

O script pede o token (a digitação não aparece na tela), descobre o seu chat quando você
manda uma mensagem ao bot e grava tudo em %LOCALAPPDATA%\\PainelDayTrade\\avisos.json —
fora do OneDrive e do Git. Para desligar: apague esse arquivo, ou rode de novo com outro bot.
"""
from __future__ import annotations

import getpass
import sys
import time

import requests

import avisos


def _api(token: str, metodo: str, **params) -> dict:
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/{metodo}", params=params, timeout=40)
    except requests.RequestException as exc:
        sys.exit(f"Sem conexão com o Telegram ({type(exc).__name__}). Tente de novo.")
    dados = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not dados.get("ok"):
        sys.exit(f"O Telegram recusou: {dados.get('description') or r.status_code}. Confira o token.")
    return dados["result"]


def main() -> None:
    print(__doc__.split("Depois rode")[0])
    token = getpass.getpass("Cole o token do bot e tecle Enter (não aparece na tela): ").strip()
    if ":" not in token:
        sys.exit("Isso não parece um token do BotFather (formato 123456789:AAE...).")
    bot = _api(token, "getMe")["username"]
    print(f"\nBot @{bot} encontrado. Agora abra o Telegram, procure @{bot}, toque em Iniciar")
    print("(ou mande qualquer mensagem). Esperando até 3 minutos…")
    ultimo = 0
    for antigo in _api(token, "getUpdates", timeout=0):
        ultimo = max(ultimo, antigo["update_id"])          # ignora mensagens antigas
    chat = None
    fim = time.time() + 180
    while chat is None and time.time() < fim:
        for u in _api(token, "getUpdates", offset=ultimo + 1, timeout=25):
            ultimo = u["update_id"]
            msg = u.get("message") or u.get("edited_message") or {}
            if (msg.get("chat") or {}).get("type") == "private":
                chat = msg["chat"]
    if chat is None:
        sys.exit("Nenhuma mensagem chegou ao bot. Rode de novo e mande a mensagem assim que ele pedir.")
    cfg = avisos.carregar()
    cfg["telegram"] = {"token": token, "chat_id": str(chat["id"])}
    avisos.salvar(cfg)
    avisos.telegram("Painel Day Trade", "Avisos ligados: os sinais das estratégias chegam aqui.", cfg)
    print(f"\nPronto: mensagem de teste enviada para {chat.get('first_name', 'você')}.")
    print(f"Configuração gravada em {avisos.CONFIG}")
    print("O vigia (vigia.py) passa a mandar os avisos no próximo ciclo, sem reiniciar nada.")


if __name__ == "__main__":
    main()
