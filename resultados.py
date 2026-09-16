# -*- coding: utf-8 -*-
"""
Resultado mês a mês das estratégias recomendadas, como se tivessem sido operadas.

Backtest com o motor do painel (operacoes.py) sobre os candles que o próprio Profit guarda
no disco — %APPDATA%\\Nelogica\\Profit_Profit-cm\\database, arquivos .min —, de 2022 até o
último candle que o Profit baixou, 200 ações por operação, sem custos. Com esses candles
o motor bate com o backtest do Profit em número de operações (±1–13%) e fica de 2% a 19%
abaixo no saldo (conferido em 15/09/2026).

Refeito uma vez por dia pelo vigia.py depois das 18:40 (ou à mão: python resultados.py) e
gravado em dados_rt/resultados.json; a aba Operações mostra a tabela.

O resultado é em AÇÕES. Quem opera a opção (call na compra, put na venda) ganha ou perde
outra coisa: delta de 0,50 a 0,70 e a perda de valor com o tempo.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

import operacoes as ope

RAIZ = pathlib.Path(__file__).resolve().parent
ARQ = RAIZ / "dados_rt" / "resultados.json"
DB = pathlib.Path(os.environ.get("APPDATA", "")) / "Nelogica" / "Profit_Profit-cm" / "database"
ATIVOS = ["VALE3", "PETR4", "BPAC11", "ITUB4", "BOVA11"]      # carteira recomendada (15/09/2026)
INICIO = pd.Timestamp("2022-01-01", tz=ope.BRT)
AQUECE = pd.Timestamp("2021-06-01", tz=ope.BRT)                  # indicadores prontos em janeiro/2022
REG = 128                                                        # bytes por candle no .min
DELPHI = pd.Timestamp("1899-12-30")


def le_min(caminho) -> pd.DataFrame:
    """Um arquivo .min do Profit: TDateTime em 0, O/H/L/C em 16–40, quantidade (int64) em 56.
    Preço bruto, sem ajuste por proventos."""
    b = pathlib.Path(caminho).read_bytes()
    n = len(b) // REG
    a = np.frombuffer(b[: n * REG], dtype=np.uint8).reshape(n, REG)

    def f64(off):
        return a[:, off:off + 8].copy().view("<f8").ravel()

    t = f64(0)
    ok = t > 30000
    idx = DELPHI + pd.to_timedelta(np.round(t[ok] * 86400), unit="s")
    df = pd.DataFrame({"open": f64(16)[ok], "high": f64(24)[ok], "low": f64(32)[ok], "close": f64(40)[ok],
                       "volume": a[:, 56:64].copy().view("<i8").ravel()[ok].astype(float)},
                      index=pd.DatetimeIndex(idx))
    return df[~df.index.duplicated(keep="last")].sort_index()


def candles(ativo: str, minutos: int) -> pd.DataFrame:
    partes = []
    for pasta, meio in ((DB / "temp", "_1_0_0_"), (DB / "assets" / f"{ativo}_B_0", "_1_1_0_")):
        for arq in sorted(pasta.glob(f"{ativo}_B_0_1_{minutos}{meio}20*.min")):
            partes.append(le_min(arq))
    if not partes:
        raise FileNotFoundError(f"sem candles de {minutos} min da {ativo} no cache do Profit ({DB})")
    d = pd.concat(partes)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d.index = d.index.tz_localize(ope.BRT)
    return d[d.index >= AQUECE]


def _eventos(ativo: str):
    r = requests.get(ope.YAHOO_CHART.format(simbolo=f"{ativo}.SA"),
                     params={"range": "10y", "interval": "1d", "events": "div,split"}, headers=ope._UA, timeout=30)
    ev = r.json()["chart"]["result"][0].get("events") or {}
    data = lambda v: pd.Timestamp(v["date"], unit="s", tz="UTC").tz_convert(ope.BRT).date()  # noqa: E731
    divs = sorted((data(v), float(v["amount"])) for v in (ev.get("dividends") or {}).values())
    splits = sorted((data(v), float(v["numerator"]) / float(v["denominator"]))
                    for v in (ev.get("splits") or {}).values())
    return divs, splits


def ajustado(ativo: str, d: pd.DataFrame) -> pd.DataFrame:
    """Desdobramentos e proventos, como o gráfico do Profit (o arquivo guarda o preço bruto)."""
    divs, splits = _eventos(ativo)
    d = d.copy()
    datas = pd.Index(d.index.date)
    for dia, razao in splits:
        antes = datas < dia
        if antes.any() and not antes.all():
            d.loc[antes, ["open", "high", "low", "close"]] /= razao
            d.loc[antes, "volume"] *= razao
    return ope.ajustar_proventos(d, divs, ativo)


def operacoes_de(ativo: str) -> tuple[pd.DataFrame, pd.Timestamp]:
    est = copy.copy(ope.ESTRATEGIAS[ativo])
    d = ajustado(ativo, candles(ativo, est.minutos))
    res = ope.simular(est, d[["open", "high", "low", "close", "volume"]])
    linhas = [{"ativo": ativo, "entrada": o.entrada.hora, "saida": o.execucoes[-1].hora, "lado": o.lado,
               "resultado": o.resultado(), "capital": abs(o.entrada.preco * o.entrada.qtd)}
              for o in res.operacoes if not o.aberta and o.entrada.hora >= INICIO]
    return pd.DataFrame(linhas), d.index[-1]


def calcular(ativos=ATIVOS) -> dict:
    partes, ultimos, falhas = [], {}, {}
    for a in ativos:
        try:
            ops, ultimo = operacoes_de(a)
            partes.append(ops)
            ultimos[a] = ultimo
        except Exception as exc:
            falhas[a] = str(exc)
    if not partes:
        raise RuntimeError("; ".join(f"{a}: {m}" for a, m in falhas.items()))
    ops = pd.concat(partes, ignore_index=True)
    mes = ops["saida"].dt.tz_localize(None).dt.to_period("M")
    # em % sobre o valor da entrada: é como ele acompanha, e não muda de escala com o lote
    ops["pct"] = ops["resultado"] / ops["capital"] * 100
    tabela = ops.pivot_table(index=mes, columns="ativo", values="resultado", aggfunc="sum")
    percentual = ops.pivot_table(index=mes, columns="ativo", values="pct", aggfunc="sum")
    contagem = ops.pivot_table(index=mes, columns="ativo", values="resultado", aggfunc="size")
    todos = pd.period_range(INICIO.tz_localize(None), max(ultimos.values()).tz_localize(None), freq="M")
    colunas = [a for a in ativos if a in ultimos]
    tabela = tabela.reindex(index=todos, columns=colunas).fillna(0.0)
    percentual = percentual.reindex(index=todos, columns=colunas).fillna(0.0)
    contagem = contagem.reindex(index=todos, columns=colunas).fillna(0).astype(int)
    meses = [{"mes": str(m), "por_ativo": {a: round(float(v), 2) for a, v in linha.items()},
              "total": round(float(linha.sum()), 2), "ops": int(contagem.loc[m].sum()),
              "por_ativo_pct": {a: round(float(v), 3) for a, v in percentual.loc[m].items()},
              "total_pct": round(float(percentual.loc[m].sum()), 3),
              "ops_por_ativo": {a: int(v) for a, v in contagem.loc[m].items()}}
             for m, linha in tabela.iterrows()]
    dados = {"gerado": datetime.now(ope.BRT).isoformat(timespec="seconds"),
             "ate": min(ultimos.values()).isoformat(), "ativos": list(tabela.columns), "lote": 200,
             "falhas": falhas, "meses": meses}
    ARQ.parent.mkdir(exist_ok=True)
    tmp = ARQ.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARQ)
    return dados


def precisa_atualizar(agora: datetime | None = None) -> bool:
    """Uma vez por dia, depois das 18:40 (fim do after-market)."""
    agora = agora or datetime.now(ope.BRT)
    corte = agora.replace(hour=18, minute=40, second=0, microsecond=0)
    if agora < corte:
        corte -= timedelta(days=1)
    try:
        gerado = datetime.fromisoformat(json.loads(ARQ.read_text(encoding="utf-8"))["gerado"])
    except (OSError, ValueError, KeyError):
        return True
    return gerado < corte


if __name__ == "__main__":
    d = calcular()
    t = pd.DataFrame([{"mês": m["mes"], **m["por_ativo_pct"], "total %": m["total_pct"],
                       "total R$": m["total"], "ops": m["ops"]} for m in d["meses"]])
    pd.set_option("display.width", 200)
    print(t.round(0).to_string(index=False))
    print(f"\ncandles até {d['ate']}; gravado em {ARQ}" + (f"; falhas: {d['falhas']}" if d["falhas"] else ""))
