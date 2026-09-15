# -*- coding: utf-8 -*-
"""
Avisos das estratégias: o que mudou em cada operação (sinal, parcial, zero a zero, stop
tocado, saída) e o envio para o Windows e para o Telegram.

Quem usa: o vigia.py (Windows + Telegram; roda em segundo plano, com o navegador fechado
também) e a aba Operações (aviso dentro do navegador, com som).

A configuração fica FORA da pasta do painel, em %LOCALAPPDATA%\\PainelDayTrade\\avisos.json:
a pasta do painel está no OneDrive e no Git, e o token do bot do Telegram não deve ir para
nenhum dos dois. Para ligar o Telegram: python configurar_telegram.py
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
from datetime import datetime, timedelta
from html import escape
from xml.sax.saxutils import escape as xml_escape

import pandas as pd

RAIZ = pathlib.Path(__file__).resolve().parent
CONFIG = pathlib.Path(os.environ.get("LOCALAPPDATA") or RAIZ) / "PainelDayTrade" / "avisos.json"
# as 5 da carteira recomendada (15/09/2026); a BBAS3 continua no painel, sem aviso
PADRAO = {"ativos": ["VALE3", "PETR4", "BPAC11", "ITUB4", "BOVA11"], "windows": True,
          "telegram": {"token": "", "chat_id": ""}}
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def carregar() -> dict:
    cfg = json.loads(json.dumps(PADRAO))
    try:
        dados = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    cfg.update({k: v for k, v in dados.items() if k != "telegram"})
    cfg["telegram"].update(dados.get("telegram") or {})
    return cfg


def salvar(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def telegram_ligado(cfg: dict) -> bool:
    tg = cfg.get("telegram") or {}
    return bool(tg.get("token") and tg.get("chat_id"))


# ---- o que aconteceu em cada operação ------------------------------------------------

def _r(v: float) -> str:
    return "R$ " + f"{v:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _rs(v: float) -> str:
    return ("+" if v > 0.004 else "−" if v < -0.004 else "") + _r(abs(v))


def eventos(ativo: str, est, res, agora: datetime, dias: int = 5) -> list[dict]:
    """Um item por acontecimento das operações dos últimos `dias`, cada um com uma chave
    estável — quem avisa guarda as chaves já avisadas e manda só as novas.

    `hora` é quando aconteceu (None se não dá para saber, como a subida do stop para o zero
    a zero, que o motor não data); `base` identifica a operação."""
    lista = []

    def ev(op, sufixo, tipo, hora, titulo, texto):
        base = f"{ativo}|{op.hora_sinal:%Y%m%d%H%M}"
        lista.append({"chave": f"{base}|{sufixo}", "base": base, "tipo": tipo, "ativo": ativo, "op": op,
                      "lado": op.lado, "hora": hora, "titulo": titulo, "texto": texto})

    for op in res.operacoes:
        fim_sinal = op.hora_sinal + pd.Timedelta(minutes=est.minutos)   # o sinal sai no fechamento
        if fim_sinal < agora - timedelta(days=dias):
            continue
        lado = "COMPRA" if op.lado > 0 else "VENDA"
        niveis = f"alvo {_r(op.alvo2)} · stop {_r(op.stop_inicial)}"
        if not pd.isna(op.alvo1) and getattr(est, "parcial_no_profit", True):
            niveis = f"parcial {_r(op.alvo1)} · " + niveis
        ent = op.entrada
        if ent is not None:
            quando = f"Entrada a {_r(ent.preco)} ({ent.hora:%H:%M})"
        else:
            quando = f"Entra a mercado na abertura do próximo candle (sinal no fechamento a {_r(op.preco_sinal)})"
        ev(op, "sinal", "sinal", fim_sinal, f"{ativo} · sinal de {lado}", f"{quando} · {niveis}")
        parcial = next((e for e in op.execucoes if e.rotulo == "parcial"), None)
        if parcial is not None:
            ev(op, "parcial", "parcial", parcial.hora, f"{ativo} · parcial executada",
               f"{abs(parcial.qtd):.0f} ações a {_r(parcial.preco)} ({parcial.hora:%H:%M}); stop no zero a zero")
        elif op.stop_movido and op.aberta:
            ev(op, "zero", "zero", None, f"{ativo} · stop no zero a zero",
               f"Stop movido para {_r(op.stop)}: a operação não perde mais")
        if op is res.aberta and any(o.rotulo == "stop" and o.tipo == "mercado" for o in res.pendentes):
            ev(op, "stop_tocado", "stop_tocado", agora, f"{ativo} · tocou o stop",
               f"Stop {_r(op.stop)} tocado; a estratégia sai a mercado na abertura do próximo candle")
        if not op.aberta and op.execucoes:
            s = op.execucoes[-1]
            como = {"alvo": "no alvo", "zeragem": "na zeragem"}.get(
                s.rotulo, "no zero a zero" if op.stop_movido else "no stop")
            ev(op, "saida", "saida", s.hora, f"{ativo} · saída {como}",
               f"Saiu a {_r(s.preco)} ({s.hora:%H:%M}) · resultado {_rs(op.resultado())} com {est.lote} ações")
    return lista


# ---- envio -----------------------------------------------------------------------------

def windows(titulo: str, texto: str) -> None:
    """Notificação do Windows (central de notificações) pelo PowerShell, sem instalar nada.
    O modo Não perturbe / Assistente de foco do Windows pode segurar a notificação."""
    xml = (f'<toast duration="long" scenario="reminder"><visual><binding template="ToastGeneric">'
           f"<text>{xml_escape(titulo)}</text><text>{xml_escape(texto)}</text></binding></visual>"
           f'<actions><action content="OK" arguments="ok" activationType="system"/></actions>'
           f'<audio src="ms-winsoundevent:Notification.Reminder"/></toast>')
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
          "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
          "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:AVISO_XML);"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:AVISO_APP)"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(ps.encode("utf-16-le")).decode()],
                       env={**os.environ, "AVISO_XML": xml, "AVISO_APP": APP_ID},
                       capture_output=True, text=True, errors="replace", timeout=40,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "PowerShell falhou").strip()[:300])


def telegram(titulo: str, texto: str, cfg: dict) -> None:
    import requests
    tg = cfg["telegram"]
    try:
        r = requests.post(f"https://api.telegram.org/bot{tg['token']}/sendMessage", timeout=20,
                          json={"chat_id": tg["chat_id"], "parse_mode": "HTML", "disable_web_page_preview": True,
                                "text": f"<b>{escape(titulo)}</b>\n{escape(texto)}"})
    except requests.RequestException as exc:
        # a mensagem de erro do requests traz a URL, e a URL traz o token: não repassar
        raise RuntimeError(f"sem conexão com o Telegram ({type(exc).__name__})") from None
    if not r.ok:
        raise RuntimeError(f"o Telegram recusou ({r.status_code}): {r.text[:200]}")


def enviar(titulo: str, texto: str, cfg: dict) -> dict:
    """Manda pelos canais ligados; devolve {canal: 'ok' | 'erro: …'}. Um canal falhar não
    impede o outro."""
    feito = {}
    if cfg.get("windows", True):
        try:
            windows(titulo, texto)
            feito["windows"] = "ok"
        except Exception as exc:
            feito["windows"] = f"erro: {exc}"
    if telegram_ligado(cfg):
        try:
            telegram(titulo, texto, cfg)
            feito["telegram"] = "ok"
        except Exception as exc:
            feito["telegram"] = f"erro: {exc}"
    return feito
