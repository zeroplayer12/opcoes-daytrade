# -*- coding: utf-8 -*-
"""Decomposição da perna da opção, ano a ano, com o prêmio de volatilidade medido.

Cada operação na opção é `alavancagem × movimento da ação − pedágio do tempo`. Este script separa
as duas parcelas, para toda a série que o cache do Profit alcança:

  direcional   só o movimento da ação (mesmo prazo da entrada)
  tempo        só a passagem do tempo (ação parada no preço de entrada)
  cruzado      o resto — a convexidade (gama × tempo)

A razão `direcional ÷ pedágio` resume o ano: acima de 1 fecha positivo, abaixo fecha negativo.

A volatilidade de cada operação é a realizada de 60 pregões da época vezes o prêmio medido no book
(`volatilidade.py`) — inclusive para as recentes, que no painel usam a implícita de verdade; aqui
vale a regra única, senão os anos não se comparam.

    python analise_opcao.py                 fator em uso no painel (com trava de amostra)
    python analise_opcao.py --por-ativo     fator medido de cada ativo, mesmo com amostra curta
    python analise_opcao.py --fator 1.15    força um fator para todos
    python analise_opcao.py --desde 2018-01-01
"""
from __future__ import annotations

import logging
import math
import sys
import warnings
from datetime import date

warnings.filterwarnings("ignore")
for _n in ("streamlit", "streamlit.runtime.caching.cache_data_api"):
    logging.getLogger(_n).setLevel(logging.ERROR)

import numpy as np
import pandas as pd

DE_PADRAO = "2021-01-01"


def _fatores(por_ativo: bool, forcado: float | None, ativos) -> dict:
    import volatilidade as vol
    if forcado:
        return {a: forcado for a in ativos}
    if por_ativo:
        return {a: vol.premio(a, min_obs=1, min_dias=1) for a in ativos}
    return {a: vol.premio(a) for a in ativos}


def coleta(desde: str, ate: str, fatores: dict) -> pd.DataFrame:
    import app
    import volatilidade as vol

    d = app._operacoes_realizadas(desde, ate)
    if d["falhas"]:
        print("falhas:", d["falhas"])
    candles, linhas = {}, []
    for l in d["linhas"]:
        o = l["opcao"]
        if not o or not o.get("venc"):
            continue
        ativo = l["ativo"]
        if ativo not in candles:
            barras = app.barras_estrategia(ativo)
            antigas = app._barras_longas(ativo)
            if len(antigas):
                barras = pd.concat([antigas, barras])
                barras = barras[~barras.index.duplicated(keep="last")].sort_index()
            fech = barras["close"].groupby(barras.index.date).last().astype(float)
            fech.index = pd.to_datetime(fech.index)
            candles[ativo] = fech
        ent_hora, ent_preco = l["entrada"][0], float(l["entrada"][1])
        rv = vol.realizada(candles[ativo], ent_hora.normalize().tz_localize(None), vol.JANELA_RV)
        if rv is None:
            continue
        iv = float(np.clip(rv * fatores[ativo], 0.10, 1.5))

        # mesma escolha de série do painel: vencimento mensal que cobre o tempo típico + folga
        dia, tipo = ent_hora.date(), o["tipo"]
        minimo = max(app.DIAS_TIPICOS.get(ativo, 2) + app.FOLGA_DU, app.DU_MIN_PADRAO)
        venc = next((v for v in app.calendario_vencimentos(dia) if app.dias_uteis(dia, v) >= minimo), None)
        if venc is None:
            continue
        t0 = app.dias_uteis(dia, venc) / 252.0
        d1 = app.D1_DELTA_55 if tipo == "CALL" else -app.D1_DELTA_55
        strike = ent_preco * math.exp(-d1 * iv * math.sqrt(t0) + (app.TAXA_PADRAO + iv ** 2 / 2) * t0)

        def preco(s, quando):
            if quando.date() > venc:
                s = float(candles[ativo][candles[ativo].index <= pd.Timestamp(venc)].iloc[-1])
                return max(s - strike, 0.0) if tipo == "CALL" else max(strike - s, 0.0)
            pz = max(app.dias_uteis(quando.date(), venc), 0) / 252.0
            return app._black_scholes(float(s), strike, pz, iv, app.TAXA_PADRAO, tipo)[0]

        p0 = preco(ent_preco, ent_hora)
        if p0 <= 0:
            continue
        fim = l["saidas"][-1][1]
        pct = sum(fr * (preco(pr, h) / p0 - 1) * 100 for _, h, pr, fr in l["saidas"])
        s_medio = sum(pr * fr for _, _, pr, fr in l["saidas"]) / sum(fr for _, _, _, fr in l["saidas"])
        direcional = (app._black_scholes(float(s_medio), strike, t0, iv, app.TAXA_PADRAO, tipo)[0] / p0 - 1) * 100
        tempo = (preco(ent_preco, fim) / p0 - 1) * 100
        _, delta = app._black_scholes(ent_preco, strike, t0, iv, app.TAXA_PADRAO, tipo)
        linhas.append({"ano": fim.year, "ativo": ativo, "acao": l["acao"], "opcao": pct,
                       "direcional": direcional, "tempo": tempo, "cruzado": pct - direcional - tempo,
                       "iv": iv * 100, "premio_pct": p0 / ent_preco * 100,
                       "alav": abs(delta) * ent_preco / p0,
                       "du_segurada": app.dias_uteis(dia, fim.date()), "du_prazo": t0 * 252})
    return pd.DataFrame(linhas)


def relatorio(t: pd.DataFrame, fatores: dict) -> None:
    import volatilidade as vol
    pd.set_option("display.width", 250)
    g = t.groupby("ano")
    res = pd.DataFrame({
        "ops": g.size(), "ação %": g["acao"].sum(), "opção %": g["opcao"].sum(),
        "direcional": g["direcional"].sum(), "pedágio": g["tempo"].sum(), "cruzado": g["cruzado"].sum(),
        "razão": g["direcional"].sum() / -g["tempo"].sum(),
        "ação %/op": g["acao"].mean(), "empate em": -g["tempo"].mean() / g["alav"].mean(),
        "IV": g["iv"].mean(), "alav": g["alav"].mean(), "du segurados": g["du_segurada"].mean(),
    })
    print("\n=== por ano (bruto, em % do prêmio) ===")
    print(res.round(2).to_string())
    print("\n'razão' = direcional ÷ pedágio. Acima de 1 o ano fecha positivo.")
    print("'empate em' = quanto a ação precisa andar a favor, por operação, para a opção empatar;"
          " compare com 'ação %/op'.")

    print("\n=== por ano e ativo (opção %, bruto) ===")
    print(t.pivot_table(index="ano", columns="ativo", values="opcao", aggfunc="sum").round(0).to_string())

    print("\n=== líquido do spread medido no book ===")
    sp = {a: (vol.spread(a) or vol.spread() or 1.0) for a in t["ativo"].unique()}
    t = t.assign(liq=t["opcao"] - t["ativo"].map(sp))
    ano = t.groupby("ano")["liq"].sum()
    print("spread por ativo: " + " | ".join(f"{a} {v:.1f}%" for a, v in sorted(sp.items())))
    print(ano.round(0).to_string())
    print(f"total {ano.sum():.0f}% em {len(t)} operações | anos negativos {(ano < 0).sum()}/{len(ano)}"
          f" | R$ {ano.sum() * 150 / len(ano):,.0f} por ano com mão de R$ 15 mil")

    print("\n=== por ativo (líquido) ===")
    porativo = t.pivot_table(index="ano", columns="ativo", values="liq", aggfunc="sum")
    print(porativo.round(0).to_string())
    print("total: " + " | ".join(f"{a} {v:+.0f}% ({int((porativo[a] < 0).sum())} anos negativos)"
                                 for a, v in porativo.sum().items()))


def main() -> int:
    argv = sys.argv[1:]
    desde = argv[argv.index("--desde") + 1] if "--desde" in argv else DE_PADRAO
    forcado = float(argv[argv.index("--fator") + 1]) if "--fator" in argv else None
    por_ativo = "--por-ativo" in argv
    import app
    import volatilidade as vol

    fatores = _fatores(por_ativo, forcado, app.ATIVOS_OPERADOS)
    print(vol.resumo())
    print("\nfator usado: " + " | ".join(f"{a} {f:.2f}x" for a, f in fatores.items())
          + ("   (sem trava de amostra)" if por_ativo else "")
          + ("   (forçado)" if forcado else ""))
    t = coleta(desde, date.today().isoformat(), fatores)
    if t.empty:
        print("nenhuma operação precificada")
        return 1
    relatorio(t, fatores)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
