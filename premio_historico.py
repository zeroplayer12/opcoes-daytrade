# -*- coding: utf-8 -*-
"""Prêmio de volatilidade medido no histórico da B3, em vez de esperar o coletor juntar amostra.

O COTAHIST traz, em cada linha de opção, o strike (PREEXE), o vencimento (DATVEN), a melhor oferta
de compra e de venda no fechamento (PREOFC/PREOFV), o último negócio e a quantidade de negócios.
Com isso dá para calcular a implícita de todo pregão do arquivo e compará-la com a realizada de 60
pregões — que é exatamente a medida que o `volatilidade.py` acumula um dia por vez.

A opção escolhida em cada pregão é a que o painel escolheria: vencimento mensal que cobre o tempo
típico da operação mais a folga, e o strike de |Δ| mais perto de 0,55 entre as que tiveram
liquidez. Preço = meio do book de fechamento; sem book dos dois lados, o último negócio.

    python premio_historico.py                       usa o cache do agente-correlacoes
    python premio_historico.py --pasta C:\\...        outra pasta com COTAHIST (.zip ou .txt)
    python premio_historico.py --gravar              soma as medições ao premio_vol.json do painel

Os arquivos anuais da B3 (COTAHIST_A2021.ZIP e seguintes) cobrem o histórico inteiro; os diários
cobrem um pregão cada. Qualquer um serve — o script lê os dois formatos.
"""
from __future__ import annotations

import io
import json
import logging
import math
import pathlib
import sys
import warnings
import zipfile
from datetime import date, datetime

warnings.filterwarnings("ignore")
for _n in ("streamlit", "streamlit.runtime.caching.cache_data_api"):
    logging.getLogger(_n).setLevel(logging.ERROR)

import numpy as np
import pandas as pd

RAIZ = pathlib.Path(__file__).resolve().parent
CACHE_PADRAO = RAIZ.parent / "agente-correlacoes" / "cache" / "cotahist_diario"
BASE_DIARIA = RAIZ.parent / "agente-correlacoes" / "motor" / "saida" / "base_diaria.parquet"
ATIVOS = ["VALE3", "PETR4", "ITUB4", "BOVA11", "BPAC11"]
MIN_NEGOCIOS = 30            # série com menos que isso no dia não tem preço confiável
SPREAD_MAX = 15.0            # acima disso o book de fechamento não representa preço de mercado
DELTA_ALVO = 0.55


def _linhas(arq: pathlib.Path):
    """Em fluxo: o arquivo anual passa de 1 GB descompactado, não cabe de uma vez na memória."""
    if arq.suffix.lower() == ".zip":
        with zipfile.ZipFile(arq) as z:
            for nome in z.namelist():
                with z.open(nome) as f:
                    for linha in io.TextIOWrapper(f, encoding="latin1", newline=""):
                        yield linha.rstrip("\r\n")
    else:
        with arq.open(encoding="latin1") as f:
            for linha in f:
                yield linha.rstrip("\r\n")


def le_cotahist(arq: pathlib.Path, ativos) -> pd.DataFrame:
    """Opções e ação à vista dos ativos pedidos. Layout tipo 01 do COTAHIST, preços em centavos."""
    prefixos = tuple(a[:4] for a in ativos)
    linhas = []
    for l in _linhas(arq):
        if len(l) < 245 or l[0:2] != "01":
            continue
        tp = l[24:27]
        if tp not in ("010", "070", "080"):
            continue
        cod = l[12:24].strip()
        if not cod.startswith(prefixos):
            continue
        if tp == "010" and cod not in ativos:
            continue
        linhas.append({
            "data": l[2:10], "cod": cod, "tipo": {"010": "ACAO", "070": "CALL", "080": "PUT"}[tp],
            "ult": int(l[108:121]) / 100, "ofc": int(l[121:134]) / 100, "ofv": int(l[134:147]) / 100,
            "negocios": int(l[147:152]), "strike": int(l[188:201]) / 100, "venc": l[202:210]})
    d = pd.DataFrame(linhas)
    if d.empty:
        return d
    d["data"] = pd.to_datetime(d["data"], format="%Y%m%d")
    return d


def _realizada(fech: pd.Series, ate: pd.Timestamp, janela: int) -> float | None:
    r = np.log(fech[fech.index < ate]).diff().dropna().tail(janela)
    if len(r) < max(20, janela // 3):
        return None
    return float(r.std() * math.sqrt(252))


def mede(pasta: pathlib.Path, ativos=ATIVOS) -> pd.DataFrame:
    import app
    import volatilidade as vol

    arqs = sorted(p for p in pasta.iterdir() if p.suffix.lower() in (".zip", ".txt"))
    if not arqs:
        raise FileNotFoundError(f"nenhum COTAHIST em {pasta}")
    print(f"{len(arqs)} arquivos em {pasta}")

    fech = {}
    if BASE_DIARIA.exists():                      # série ajustada, para a realizada não contar provento
        b = pd.read_parquet(BASE_DIARIA, columns=["DATA", "ticker", "preult_aj"])
        b = b[b["ticker"].isin(ativos)]
        for a, g in b.groupby("ticker"):
            s = g.set_index("DATA")["preult_aj"].sort_index().astype(float)
            fech[a] = s[s > 0]
    faltam = [a for a in ativos if a not in fech]
    if faltam:
        print(f"  sem série ajustada para {faltam}: a realizada deles sai do próprio COTAHIST")

    bruto, linhas = {}, []
    for arq in arqs:
        try:
            d = le_cotahist(arq, ativos)
        except Exception as exc:
            print(f"  {arq.name}: {exc}")
            continue
        if d.empty:
            continue
        for dia, dd in d.groupby("data"):
            spot = dd[dd["tipo"] == "ACAO"].set_index("cod")["ult"].to_dict()
            for ativo in ativos:
                s = spot.get(ativo)
                if not s or s <= 0:
                    continue
                bruto.setdefault(ativo, {})[dia] = s
                minimo = max(app.DIAS_TIPICOS.get(ativo, 2) + app.FOLGA_DU, app.DU_MIN_PADRAO)
                cand = dd[(dd["tipo"] != "ACAO") & (dd["cod"].str.startswith(ativo[:4]))
                          & (dd["negocios"] >= MIN_NEGOCIOS)].copy()
                if cand.empty:
                    continue
                cand["venc_d"] = pd.to_datetime(cand["venc"], format="%Y%m%d").dt.date
                cand = cand[cand["venc_d"].map(app.eh_vencimento_mensal)]
                cand["du"] = [app.dias_uteis(dia.date(), v) for v in cand["venc_d"]]
                elegiveis = sorted({v for v, du in zip(cand["venc_d"], cand["du"]) if du >= minimo})
                if not elegiveis:
                    continue
                venc = elegiveis[0]
                cand = cand[cand["venc_d"] == venc]
                # O book do FECHAMENTO só vale quando é apertado: nas séries líquidas ele fica em
                # 1–2% e a implícita bate com a do último negócio, mas fora delas aparece 43%, 127%
                # ou uma ponta zerada — formador de mercado recolhendo a oferta no fim do pregão.
                # Nesses casos o último negócio, com MIN_NEGOCIOS atrás dele, é o preço honesto.
                tem_book = (cand["ofc"] > 0) & (cand["ofv"] >= cand["ofc"])
                meio = (cand["ofc"] + cand["ofv"]) / 2
                sp = np.where(tem_book, (cand["ofv"] - cand["ofc"]) / meio.replace(0, np.nan) * 100, np.nan)
                usa_book = tem_book & (sp <= SPREAD_MAX)
                cand = cand.assign(mid=np.where(usa_book, meio, cand["ult"]),
                                   spread=np.where(usa_book, sp, np.nan),
                                   preco_de=np.where(usa_book, "book", "último negócio"))
                t = max(app.dias_uteis(dia.date(), venc), 0) / 252.0
                if t <= 0:
                    continue
                # Uma medição por tipo: ele compra CALL no sinal de compra e PUT no de venda, e as
                # duas pontas têm prêmio próprio (a assimetria do sorriso de volatilidade).
                for tipo in ("CALL", "PUT"):
                    melhor = None
                    for _, o in cand[cand["tipo"] == tipo].iterrows():
                        if o["mid"] <= 0 or o["strike"] <= 0:
                            continue
                        iv = app.volatilidade_implicita(float(o["mid"]), s, float(o["strike"]), t,
                                                        app.TAXA_PADRAO, tipo)
                        if not iv or not (0.05 < iv < 2.0):
                            continue
                        _, delta = app._black_scholes(s, float(o["strike"]), t, iv, app.TAXA_PADRAO, tipo)
                        erro = abs(abs(delta) - DELTA_ALVO)
                        if melhor is None or erro < melhor[0]:
                            melhor = (erro, o, iv, abs(delta))
                    if melhor is None:
                        continue
                    _, o, iv, delta = melhor
                    linhas.append({"dia": dia.date().isoformat(), "opcao": o["cod"], "ativo": ativo,
                                   "tipo": tipo, "du": int(round(t * 252)), "mid": round(float(o["mid"]), 2),
                                   "spread_pct": None if pd.isna(o["spread"]) else round(float(o["spread"]), 2),
                                   "iv": round(iv * 100, 1), "delta": round(delta, 3),
                                   "negocios": int(o["negocios"]), "preco_de": o["preco_de"],
                                   "fonte": "cotahist"})

    t = pd.DataFrame(linhas)
    if t.empty:
        return t
    for a, m in bruto.items():                    # sem série ajustada, usa o fechamento do COTAHIST
        if a not in fech:
            s = pd.Series(m).sort_index()
            fech[a] = s[s > 0]
    rv20, rv60 = [], []
    for _, r in t.iterrows():
        s = fech.get(r["ativo"])
        dia = pd.Timestamp(r["dia"])
        rv60.append(_realizada(s, dia, vol.JANELA_RV) if s is not None else None)
        rv20.append(_realizada(s, dia, 20) if s is not None else None)
    t["rv60"] = [round(v * 100, 1) if v else None for v in rv60]
    t["rv20"] = [round(v * 100, 1) if v else None for v in rv20]
    t["fator"] = [round(r / v, 3) if v else None for r, v in zip(t["iv"] / 100, rv60)]
    return t


def grava(t: pd.DataFrame) -> int:
    """Soma as medições do COTAHIST ao arquivo que o painel usa."""
    import volatilidade as vol
    try:
        dados = json.loads(vol.ARQ.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {"gerado": None, "medidas": []}
    ja = {(m["dia"], m["opcao"]) for m in dados["medidas"]}
    novas = [m for m in t.replace({np.nan: None}).to_dict("records") if (m["dia"], m["opcao"]) not in ja]
    dados["medidas"].extend(novas)
    dados["medidas"].sort(key=lambda m: (m["dia"], m["opcao"]))
    antes = dados.get("em_uso") or {}
    dados["em_uso"] = {a: round(vol.premio(a, dados=dados), 3)
                       for a in sorted({m["ativo"] for m in dados["medidas"]})}
    dados["mudou"] = {a: [antes[a], v] for a, v in dados["em_uso"].items() if a in antes and antes[a] != v}
    dados["gerado"] = datetime.now().isoformat(timespec="seconds")
    tmp = vol.ARQ.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(vol.ARQ)
    return len(novas)


def main() -> int:
    argv = sys.argv[1:]
    pasta = pathlib.Path(argv[argv.index("--pasta") + 1]) if "--pasta" in argv else CACHE_PADRAO
    t = mede(pasta)
    if t.empty:
        print("nenhuma medição")
        return 1
    pd.set_option("display.width", 250)
    ok = t[t["fator"].notna()]
    print(f"\n{len(t)} medições ({len(ok)} com realizada disponível), "
          f"de {t['dia'].min()} a {t['dia'].max()}\n")
    por = ok.groupby("ativo").agg(medicoes=("fator", "size"), pregoes=("dia", "nunique"),
                                  fator=("fator", "median"), iv=("iv", "median"),
                                  rv60=("rv60", "median"), spread=("spread_pct", "median"),
                                  delta=("delta", "median"), du=("du", "median"))
    print(por.round(2).to_string())
    print(f"\ngeral: fator {ok['fator'].median():.2f}x | spread {ok['spread_pct'].median():.1f}%")
    ok = ok.assign(ano=ok["dia"].str[:4], mes=ok["dia"].str[:7])
    destino = RAIZ / "dados_rt" / "premio_cotahist.csv"
    destino.parent.mkdir(exist_ok=True)
    ok.to_csv(destino, index=False, encoding="utf-8")
    print(f"\ndetalhe (uma linha por pregão, ativo e tipo) em {destino}")
    print("\nfator mediano por ativo e ANO:")
    print(ok.pivot_table(index="ano", columns="ativo", values="fator", aggfunc="median").round(2).to_string())
    print("\nrealizada (RV60) mediana por ativo e ano — o fator anda contra ela:")
    print(ok.pivot_table(index="ano", columns="ativo", values="rv60", aggfunc="median").round(1).to_string())
    print("\nfator mediano por ano e tipo:")
    print(ok.pivot_table(index="ano", columns="tipo", values="fator", aggfunc="median").round(2).to_string())
    print("\nmedições por ano:")
    print(ok.pivot_table(index="ano", columns="ativo", values="fator", aggfunc="size").to_string())
    if "--gravar" in argv:
        n = grava(t)
        print(f"\n{n} medições somadas ao premio_vol.json")
        import volatilidade as vol
        print(vol.resumo())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
