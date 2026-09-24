# -*- coding: utf-8 -*-
"""Prêmio de volatilidade das opções: quanto o mercado cobra acima da volatilidade realizada.

É o número que decide se comprar opção nessas estratégias vale a pena. O resultado da perna da
opção, por operação, é `alavancagem × movimento da ação − pedágio do tempo`. A alavancagem é
inversamente proporcional à volatilidade implícita — quanto mais cara a opção, menor ela é — e o
pedágio, em % do prêmio, não depende da implícita, só da fração da vida da opção que a operação
consome (`1 − √(1 − dias segurados / prazo)`). Ou seja: comprar opção só paga quando o movimento
capturado supera o que a implícita cobra pelo tempo de posição.

Medido em 24/09/2026, com os 6 pregões de book que o coletor tinha gravado (10 opções): spread de
1,1% do prêmio e implícita de 1,30× a realizada de 60 pregões na mediana — VALE3 entre 1,09 e 1,14,
PETR4 em 1,47 e BOVA11 em 1,50. Amostra pequena, de um regime só de volatilidade: serve para
levantar a suspeita, não para fechar questão. Por isso este módulo acumula a medida a cada pregão
e o painel usa o que estiver acumulado — a estimativa melhora sozinha com o tempo.

    python volatilidade.py            mede os pregões ainda não medidos e mostra o resumo
    python volatilidade.py --tudo     refaz a medição do zero

Para a série antiga, em que não há book gravado, o painel precifica a opção com a realizada da
época vezes este prêmio (`iv_da_epoca`). Antes ele usava a implícita de uma opção de HOJE para
2021–2026 inteiro, o que distorcia os dois extremos: a volatilidade da VALE3 foi de 40% em 2022 a
22% em 2024, e o prêmio vai junto.
"""
from __future__ import annotations

import json
import math
import pathlib
from datetime import date, datetime

import numpy as np
import pandas as pd

RAIZ = pathlib.Path(__file__).resolve().parent
PASTA_RT = RAIZ / "dados_rt"
PASTA_OPC = PASTA_RT / "opcoes"
DIARIO = PASTA_RT / "diario_opcoes.json"
ARQ = PASTA_RT / "premio_vol.json"

PADRAO = 1.30                 # mediana medida em 24/09/2026; vale enquanto não houver dados do ativo
MIN_OBS, MIN_DIAS = 12, 8     # a partir daqui o ativo usa o próprio prêmio, não o geral
MIN_OBS_GERAL = 20
FAIXA = (0.5, 3.0)            # fator fora disso é erro de medida (strike errado, cotação fantasma)
JANELA_RV = 60                # pregões da volatilidade realizada, como na literatura de prêmio de vol

PREFIXO = {"VALE": "VALE3", "PETR": "PETR4", "ITUB": "ITUB4", "BPAC": "BPAC11",
           "BOVA": "BOVA11", "BBAS": "BBAS3", "BBDC": "BBDC4"}


def _app():
    """O app tem o calendário da B3 e o Black-Scholes. Importado aqui dentro para o app poder
    importar este módulo sem ciclo."""
    import app
    return app


# ------------------------------------------------------------------ medição
def _meta_das_opcoes() -> dict:
    """Ticker da opção -> tipo, strike, vencimento e ativo, pelo diário que o painel escreve."""
    try:
        d = json.loads(DIARIO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    meta = {}
    for chave, v in d.items():
        if not v.get("ticker"):
            continue
        meta[v["ticker"]] = {"tipo": v["tipo"], "strike": float(v["strike"]), "venc": v["venc"],
                             "ativo": chave.split("|")[0]}
    return meta


def _fechamentos(ativo: str) -> pd.Series:
    """Fechamento diário ajustado do ativo, do cache do Profit."""
    import resultados as R
    import operacoes as ope
    est = ope.ESTRATEGIAS[ativo]
    c = R.ajustado(ativo, R.candles(ativo, est.minutos, desde=None))
    s = c["close"].groupby(c.index.date).last()
    s.index = pd.to_datetime(s.index)
    return s.astype(float)


def realizada(fech: pd.Series, ate, janela: int = JANELA_RV) -> float | None:
    """Volatilidade realizada anualizada até a véspera de `ate`."""
    corte = pd.Timestamp(ate)
    if corte.tzinfo is not None:                 # o índice diário é sem fuso; o candle do painel tem
        corte = corte.tz_localize(None)
    ant = fech[fech.index < corte]
    r = np.log(ant).diff().dropna().tail(janela)
    if len(r) < max(20, janela // 3):
        return None
    return float(r.std() * math.sqrt(252))


_cache: dict = {"quando": None, "dados": None}


def _le() -> dict:
    """O painel chama `premio()` uma vez por operação (são centenas): relê só quando o arquivo muda."""
    try:
        quando = ARQ.stat().st_mtime
    except OSError:
        return {"gerado": None, "medidas": []}
    if _cache["quando"] != quando:
        try:
            _cache["dados"] = json.loads(ARQ.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache["dados"] = {"gerado": None, "medidas": []}
        _cache["quando"] = quando
    return _cache["dados"]


def medir(tudo: bool = False) -> dict:
    """Mede spread e implícita de cada opção em cada pregão gravado pelo coletor e acumula."""
    app = _app()
    dados = {"gerado": None, "medidas": []} if tudo else _le()
    ja = {(m["dia"], m["opcao"]) for m in dados["medidas"]}
    meta = _meta_das_opcoes()
    if not meta:
        print("sem diário de opções ainda (dados_rt/diario_opcoes.json); nada a medir")
        return dados
    fech, novas, sem_strike = {}, 0, set()

    for arq in sorted(PASTA_OPC.glob("[0-9]*-[0-9]*-[0-9]*.csv")):
        dia = arq.stem
        try:
            livro = pd.read_csv(arq)
            acoes = pd.read_csv(PASTA_RT / arq.name)
        except (OSError, ValueError):
            continue
        for ticker, g in livro.groupby("opcao"):
            if (dia, ticker) in ja:
                continue
            m = meta.get(ticker)
            if not m:
                sem_strike.add(ticker)
                continue
            ativo = m["ativo"] or PREFIXO.get(ticker[:4])
            if not ativo:
                continue
            g = g[(g["compra"] > 0) & (g["venda"] >= g["compra"])].copy()
            if len(g) < 5:                       # book raso demais para medir nada
                continue
            g["mid"] = (g["compra"] + g["venda"]) / 2
            g["spread"] = (g["venda"] - g["compra"]) / g["mid"] * 100
            spot = acoes[acoes["ativo"] == ativo][["hora", "ult"]].rename(columns={"ult": "spot"})
            g = g.merge(spot, on="hora", how="left").dropna(subset=["spot"])
            if g.empty:
                continue
            d = date.fromisoformat(dia)
            venc = date.fromisoformat(m["venc"])
            du = max(app.dias_uteis(d, venc), 0)
            if du <= 1:                          # na véspera do vencimento a implícita não significa nada
                continue
            mid, s = float(g["mid"].median()), float(g["spot"].median())
            iv = app.volatilidade_implicita(mid, s, m["strike"], du / 252.0, app.TAXA_PADRAO, m["tipo"])
            if not iv:
                continue
            if ativo not in fech:
                try:
                    fech[ativo] = _fechamentos(ativo)
                except Exception as exc:
                    print(f"  sem candles de {ativo}: {exc}")
                    fech[ativo] = pd.Series(dtype=float)
            rv60 = realizada(fech[ativo], d, JANELA_RV) if len(fech[ativo]) else None
            rv20 = realizada(fech[ativo], d, 20) if len(fech[ativo]) else None
            dados["medidas"].append({
                "dia": dia, "opcao": ticker, "ativo": ativo, "tipo": m["tipo"], "du": du,
                "mid": round(mid, 2), "spread_pct": round(float(g["spread"].median()), 2),
                "iv": round(iv * 100, 1), "rv20": round(rv20 * 100, 1) if rv20 else None,
                "rv60": round(rv60 * 100, 1) if rv60 else None,
                "fator": round(iv / rv60, 3) if rv60 else None, "cotacoes": int(len(g))})
            novas += 1

    # Qual fator cada ativo passa a usar. Quando um deles sai do geral para o próprio, o painel
    # muda de resposta — e é a deixa para refazer a análise dos anos (analise_opcao.py).
    antes = dados.get("em_uso") or {}
    dados["em_uso"] = {a: round(premio(a, dados=dados), 3)
                       for a in sorted({m["ativo"] for m in dados["medidas"]})}
    # ativo que aparece pela primeira vez não é mudança: ele já vinha no fator geral
    dados["mudou"] = {a: [antes[a], v] for a, v in dados["em_uso"].items() if a in antes and antes[a] != v}
    dados["gerado"] = datetime.now().isoformat(timespec="seconds")
    dados["medidas"].sort(key=lambda m: (m["dia"], m["opcao"]))
    ARQ.parent.mkdir(exist_ok=True)
    tmp = ARQ.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARQ)
    if sem_strike:
        print(f"  {len(sem_strike)} opções sem strike no diário (não dá para tirar a implícita):"
              f" {', '.join(sorted(sem_strike)[:8])}")
    print(f"{novas} medições novas; {len(dados['medidas'])} no total, em {ARQ}")
    return dados


# ------------------------------------------------------------------ uso pelo painel
def _validas(dados: dict, ativo: str | None = None) -> list[dict]:
    return [m for m in dados["medidas"]
            if m.get("fator") and FAIXA[0] <= m["fator"] <= FAIXA[1] and (ativo is None or m["ativo"] == ativo)]


def premio(ativo: str | None = None, min_obs: int = MIN_OBS, min_dias: int = MIN_DIAS,
           dados: dict | None = None) -> float:
    """Quanto a implícita fica acima da realizada, na mediana. Usa o do próprio ativo quando há
    medida suficiente; senão o geral; senão o padrão medido em 24/09/2026.

    `min_obs`/`min_dias` a 1 tiram a trava de amostra: serve para a análise (`analise_opcao.py`)
    mostrar o que aconteceria com o fator medido de cada ativo antes de ele valer no painel."""
    dados = _le() if dados is None else dados
    if ativo:
        meu = _validas(dados, ativo)
        if meu and len(meu) >= min_obs and len({m["dia"] for m in meu}) >= min_dias:
            return float(np.median([m["fator"] for m in meu]))
    geral = _validas(dados)
    if len(geral) >= MIN_OBS_GERAL:
        return float(np.median([m["fator"] for m in geral]))
    return PADRAO


def spread(ativo: str | None = None) -> float | None:
    """Spread mediano do book, em % do prêmio. É o custo por operação — o que pesa é ele vezes a
    quantidade de operações."""
    dados = _le()
    meu = [m["spread_pct"] for m in dados["medidas"]
           if m.get("spread_pct") is not None and (ativo is None or m["ativo"] == ativo)]
    return float(np.median(meu)) if len(meu) >= 5 else None


MEDIDA = PASTA_RT / "premio_cotahist.csv"
_medida: dict = {"quando": None, "iv": None}


def _tabela_medida() -> dict:
    """Implícita de verdade, por pregão/ativo/tipo, tirada do COTAHIST (premio_historico.py).
    É melhor do que qualquer estimativa: é o preço que a opção tinha naquele dia."""
    try:
        quando = MEDIDA.stat().st_mtime
    except OSError:
        return {}
    if _medida["quando"] != quando:
        try:
            t = pd.read_csv(MEDIDA, usecols=["dia", "ativo", "tipo", "iv"])
            _medida["iv"] = {(a, tp, d): v / 100.0
                             for d, a, tp, v in zip(t["dia"], t["ativo"], t["tipo"], t["iv"])}
        except (OSError, ValueError, KeyError):
            _medida["iv"] = {}
        _medida["quando"] = quando
    return _medida["iv"] or {}


def iv_medida(ativo: str, tipo: str, dia) -> float | None:
    """Implícita do pregão exato; sem ela, a do pregão anterior mais próximo (até 5 dias)."""
    tab = _tabela_medida()
    if not tab:
        return None
    d = pd.Timestamp(dia)
    if d.tzinfo is not None:
        d = d.tz_localize(None)
    for atras in range(6):
        v = tab.get((ativo, tipo, (d - pd.Timedelta(days=atras)).date().isoformat()))
        if v:
            return v
    return None


def iv_da_epoca(ativo: str, candles: pd.DataFrame, quando, tipo: str | None = None) -> float | None:
    """Implícita para uma operação antiga. Primeiro a medida no COTAHIST daquele pregão; só quando
    não há é que entra a estimativa (realizada da época × prêmio medido)."""
    if tipo:
        v = iv_medida(ativo, tipo, quando)
        if v:
            return v
    if candles is None or candles.empty:
        return None
    fech = candles["close"][candles.index < pd.Timestamp(quando).normalize()].astype(float)
    if fech.empty:
        return None
    diario = fech.groupby(fech.index.date).last()
    diario.index = pd.to_datetime(diario.index)
    rv = realizada(diario, pd.Timestamp(quando).normalize() + pd.Timedelta(days=1), JANELA_RV)
    if rv is None:
        return None
    return float(np.clip(rv * premio(ativo), 0.10, 1.5))


def resumo() -> str:
    dados = _le()
    linhas = [f"{len(dados['medidas'])} medições até {dados.get('gerado') or '—'}"]
    tudo = _validas(dados)
    if tudo:
        linhas.append(f"geral: fator {np.median([m['fator'] for m in tudo]):.2f}x "
                      f"({len(tudo)} medições, {len({m['dia'] for m in tudo})} pregões)")
    por = {}
    for m in tudo:
        por.setdefault(m["ativo"], []).append(m)
    for a, ms in sorted(por.items()):
        usa = " (em uso)" if len(ms) >= MIN_OBS and len({m["dia"] for m in ms}) >= MIN_DIAS else ""
        sp = np.median([m["spread_pct"] for m in ms])
        linhas.append(f"  {a:7s} fator {np.median([m['fator'] for m in ms]):.2f}x | spread {sp:.1f}% "
                      f"| {len(ms)} medições em {len({m['dia'] for m in ms})} pregões{usa}")
    return "\n".join(linhas)


if __name__ == "__main__":
    import sys
    medir(tudo="--tudo" in sys.argv)
    print()
    print(resumo())
    print(f"\nprêmio em uso: geral {premio():.2f}x")
