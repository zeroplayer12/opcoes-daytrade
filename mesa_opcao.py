# -*- coding: utf-8 -*-
"""Mesa de testes: onde a opção teria saído se não fosse vendida junto com a ação.

Nada é operado aqui. A cada pregão o arquivo `dados_rt/mesa_opcao.json` ganha, para cada operação
encerrada, três números lado a lado, todos em % do prêmio:

  como é hoje   vende a opção junto com a ação, no alvo/stop da estratégia;
  ganhadora     perdedora sai junto com a ação; ganhadora fica, com stop móvel de X% do topo;
  solta         a opção manda desde a entrada, só com o stop móvel, até o vencimento.

Por que registrar em vez de já mudar a regra: na simulação de 2021–2026 as duas alternativas
melhoraram a mediana em ~60% e cortaram o número de operações de 1.452 para 250–600 — mas sem
platô nenhum (trail de 40% dava 489%, trail de 50% dava 2.888%) e com as 5 melhores operações
respondendo por 51% a 206% do lucro. Ou seja: a direção tem lastro, os parâmetros não. Três meses
de registro para a frente valem mais do que continuar torcendo a amostra antiga.

    python mesa_opcao.py            atualiza e mostra o placar
    python mesa_opcao.py --desde 2026-06-01   começa o registro numa data anterior
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd

RAIZ = pathlib.Path(__file__).resolve().parent
ARQ = RAIZ / "dados_rt" / "mesa_opcao.json"
TRAIL = 50.0          # stop móvel, em % abaixo do maior prêmio desde a entrada
JANELA = 120          # dias corridos recalculados a cada rodada (operação aberta ainda muda)


def _candles(app, ativo: str) -> pd.DataFrame:
    barras = app.barras_estrategia(ativo)
    antigas = app._barras_longas(ativo)
    if len(antigas):
        barras = pd.concat([antigas, barras])
        barras = barras[~barras.index.duplicated(keep="last")].sort_index()
    return barras


def _caminho(app, candles: pd.DataFrame, o: dict, ent_hora, ent_preco: float,
             saida_acao, ganhou: bool, so_ganhadora: bool, trail: float):
    """Percorre os candles precificando a opção; devolve (pct, quando, motivo) ou None se a posição
    ainda está viva (o vencimento não chegou e o stop móvel não foi tocado)."""
    strike, venc, tipo = float(o["strike"]), o["venc"], o["tipo"]
    iv, p0 = (o["vol"] or 0) / 100.0, float(o["entrada"])
    if iv <= 0 or p0 <= 0:
        return None
    if so_ganhadora and not ganhou:
        return (o["pct"], saida_acao, "saiu com a ação (perdedora)")

    janela = candles[(candles.index > ent_hora) & (candles.index.date <= venc)]
    if janela.empty:
        return None
    dias = pd.DatetimeIndex(sorted(set(candles.index.date)))
    ordem = pd.Series(range(len(dias)), index=dias)
    fim = ordem.get(pd.Timestamp(venc), ordem.iloc[-1] + 1)
    t = np.clip((fim - ordem.reindex(pd.DatetimeIndex(janela.index.date)).to_numpy()) / 252.0, 0, None)

    def preco(s):
        return np.array([app._black_scholes(float(x), strike, float(tt), iv, app.TAXA_PADRAO, tipo)[0]
                         for x, tt in zip(s, t)])

    alto, baixo = janela["high"].to_numpy(), janela["low"].to_numpy()
    p_bom = preco(alto if tipo == "CALL" else baixo)
    p_ruim = preco(baixo if tipo == "CALL" else alto)
    p_fec = preco(janela["close"].to_numpy())

    solta_em = saida_acao if so_ganhadora else ent_hora
    topo = p0
    for i, hora in enumerate(janela.index):
        if hora < solta_em:
            continue
        topo = max(topo, float(p_bom[i]))
        piso = topo * (1 - trail / 100.0)
        if p_ruim[i] <= piso:
            return ((piso / p0 - 1) * 100, hora, "stop móvel")
    ultimo = janela.index[-1]
    if ultimo.date() >= venc:                       # chegou ao vencimento sem tocar o stop
        return ((float(p_fec[-1]) / p0 - 1) * 100, ultimo, "vencimento")
    return None                                     # ainda viva


def atualizar(desde: str | None = None, trail: float = TRAIL) -> dict:
    import app

    try:
        dados = json.loads(ARQ.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {"inicio": desde or date.today().isoformat(), "trail_pct": trail, "ops": {}}
    if desde:
        dados["inicio"] = min(dados["inicio"], desde)
    dados["trail_pct"] = trail

    hoje = date.today()
    inicio = max(date.fromisoformat(dados["inicio"]), hoje - pd.Timedelta(days=JANELA).to_pytimedelta())
    d = app._operacoes_realizadas(inicio.isoformat(), hoje.isoformat())
    if d["falhas"]:
        print("falhas:", d["falhas"])
    candles = {}
    novas = abertas = 0

    for l in d["linhas"]:
        o = l["opcao"]
        if not o or not o.get("entrada") or not o.get("strike"):
            continue
        ativo, ent_hora, ent_preco = l["ativo"], l["entrada"][0], float(l["entrada"][1])
        chave = f"{ativo}|{l['sinal']:%Y%m%d%H%M}"
        saida_acao = l["saidas"][-1][1]
        if ativo not in candles:
            candles[ativo] = _candles(app, ativo)
        reg = dados["ops"].get(chave, {})
        reg.update({"ativo": ativo, "sinal": l["sinal"].isoformat(), "lado": int(l["lado"]),
                    "entrada": ent_hora.isoformat(), "saida_acao": saida_acao.isoformat(),
                    "acao_pct": round(l["acao"], 3), "hoje_pct": round(o["pct"], 2),
                    "premio": round(float(o["entrada"]), 2), "iv": o.get("vol"),
                    "venc": o["venc"].isoformat(), "fonte_vol": o.get("fonte_vol")})
        reg.setdefault("visto_em", datetime.now().isoformat(timespec="seconds"))
        for nome, so_ganhadora in (("ganhadora", True), ("solta", False)):
            r = _caminho(app, candles[ativo], o, ent_hora, ent_preco, saida_acao,
                         l["acao"] > 0, so_ganhadora, trail)
            if r is None:
                reg[nome] = {"estado": "aberta"}
                abertas += 1
            else:
                pct, quando, motivo = r
                reg[nome] = {"estado": "fechada", "pct": round(float(pct), 2),
                             "quando": pd.Timestamp(quando).isoformat(), "motivo": motivo}
        if chave not in dados["ops"]:
            novas += 1
        dados["ops"][chave] = reg

    dados["gerado"] = datetime.now().isoformat(timespec="seconds")
    ARQ.parent.mkdir(exist_ok=True)
    tmp = ARQ.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARQ)
    print(f"{novas} operações novas, {abertas} pernas ainda abertas; {len(dados['ops'])} no total")
    return dados


def placar(dados: dict | None = None) -> str:
    if dados is None:
        try:
            dados = json.loads(ARQ.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "sem registro ainda"
    ops = list(dados["ops"].values())
    if not ops:
        return f"registro começou em {dados['inicio']}; nenhuma operação encerrada ainda"
    linhas = [f"registro desde {dados['inicio']} | stop móvel de {dados['trail_pct']:.0f}% do topo"
              f" | {len(ops)} operações"]
    fechadas = [o for o in ops if o.get("ganhadora", {}).get("estado") == "fechada"
                and o.get("solta", {}).get("estado") == "fechada"]
    linhas.append(f"{len(fechadas)} com as três pernas resolvidas"
                  + (f" ({len(ops) - len(fechadas)} ainda correndo)" if len(ops) > len(fechadas) else ""))
    if not fechadas:
        return "\n".join(linhas)
    for nome, pega in (("como é hoje", lambda o: o["hoje_pct"]),
                       ("ganhadora corre", lambda o: o["ganhadora"]["pct"]),
                       ("opção solta", lambda o: o["solta"]["pct"])):
        v = np.array([pega(o) for o in fechadas], dtype=float)
        linhas.append(f"  {nome:16s} total {v.sum():8.1f}% | por operação {v.mean():6.2f}%"
                      f" | acerto {(v > 0).mean()*100:3.0f}% | melhor {v.max():7.1f}% | pior {v.min():7.1f}%")
    acao = np.array([o["acao_pct"] for o in fechadas], dtype=float)
    linhas.append(f"  {'na ação':16s} total {acao.sum():8.1f}% | por operação {acao.mean():6.3f}%")
    return "\n".join(linhas)


if __name__ == "__main__":
    desde = None
    if "--desde" in sys.argv:
        desde = sys.argv[sys.argv.index("--desde") + 1]
    d = atualizar(desde)
    print()
    print(placar(d))
