# -*- coding: utf-8 -*-
"""
===============================================================================
 DASHBOARD DE OPÇÕES — DAY TRADE (B3)
===============================================================================
 Objetivo: em segundos, apontar a MELHOR CALL e a MELHOR PUT do dia para
           BOVA11, PETR4, VALE3, BBAS3, ITUB4 e BPAC11.

 Regras de negócio (o core operacional):
   1) VENCIMENTO ... série mensal da B3, 2 a 20 dias úteis (ajustável na UI)
   2) DELTA ........ |Delta| entre 0,50 e 0,70
                     (Calls: +0,50 a +0,70  |  Puts: -0,70 a -0,50)
   3) LIQUIDEZ ..... ordena por Volume Financeiro DESC e Núm. de Negócios DESC
                     -> o topo da lista é a opção escolhida para a operação

 Fontes de dados (cascata com fallback):
   Rota A1  requests  -> endpoint JSON interno do opcoes.net.br       (rápido)
   Rota A2  Selenium  -> renderiza a página e lê a tabela (headless)  (fallback)
   Rota B   Upload    -> CSV/XLSX exportado do próprio site           (à prova de bloqueio)
   Rota D   Demo      -> grade sintética (Black-Scholes) p/ testar a interface

 Aba Correlações: futuros de índices, commodities, câmbio e ADRs (Yahoo
 Finance), com a correlação de cada mercado com os seis ativos acima.

 Execução:  streamlit run app.py
===============================================================================
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html import escape as _escape
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import streamlit as st

import operacoes as ope

try:  # requests é opcional: sem ele a Rota A1 é simplesmente pulada
    import requests
except ImportError:  # pragma: no cover
    requests = None


# =============================================================================
# SEÇÃO 1 — CONFIGURAÇÃO
# =============================================================================

ATIVOS: list[str] = ["BOVA11", "PETR4", "VALE3", "BBAS3", "ITUB4", "BPAC11"]

DELTA_MIN_PADRAO, DELTA_MAX_PADRAO = 0.50, 0.70   # regra obrigatória
DU_MIN_PADRAO, DU_MAX_PADRAO = 2, 20              # janela de dias úteis
TOP_N = 3                                         # linhas na tabela de cada lado

URL_PAGINA = "https://opcoes.net.br/opcoes/bovespa/{ativo}"

# No navegador (stlite/Pyodide) não existem sockets: `requests` não funciona, e o
# opcoes.net.br não manda cabeçalho CORS, então o fetch direto seria bloqueado.
# Nesse ambiente as chamadas vão para uma função serverless de mesma origem que
# faz o proxy. Mesmo arquivo, dois ambientes.
def _detectar_navegador() -> bool:
    """Estamos rodando em WebAssembly (stlite/Pyodide)?

    Não dá para confiar em heurística de ambiente aqui: `"pyodide" in
    sys.modules` dá False no worker do stlite (o módulo existe mas ainda não foi
    importado) e `sys.platform` variou entre builds. Errar isso manda o app pela
    rota do `requests`, que o stlite remenda para async — a função chamadora
    vira corrotina e o erro que aparece é de pickle, sem relação aparente.
    A prova definitiva é conseguir importar o próprio módulo HTTP do Pyodide.
    """
    if sys.platform == "emscripten":
        return True
    try:
        import pyodide.http  # noqa: F401
        return True
    except Exception:
        return False


NO_NAVEGADOR = _detectar_navegador()


def cache_dados(**kwargs):
    """`st.cache_data`, exceto no WebAssembly.

    No stlite o wrapper de cache é assíncrono: a função decorada passa a
    devolver uma corrotina, e o próprio cache tenta serializá-la, falhando com
    um erro de pickle sem relação aparente com a causa. Como as funções de rede
    só rodam ao clicar em "Buscar no site" (o resultado fica em session_state),
    ficar sem cache no navegador não custa nada.
    """
    def decorador(fn):
        return fn if NO_NAVEGADOR else st.cache_data(**kwargs)(fn)
    return decorador
URL_JSON_DIRETO = "https://opcoes.net.br/listaopcoes/completa"
URL_JSON_PROXY = "/api/opcoes"
URL_JSON = URL_JSON_PROXY if NO_NAVEGADOR else URL_JSON_DIRETO

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://opcoes.net.br/",
}

# Colunas canônicas internas -> rótulo exibido na tabela
ROTULOS: dict[str, str] = {
    "ticker": "Ticker",
    "modelo": "Mod.",
    "strike": "Strike",
    "moneyness": "A/I/OTM",
    "ultimo": "Últ. (R$)",
    "variacao": "Var. (%)",
    "num_neg": "Núm. de Neg.",
    "vol_financeiro": "Vol. Financeiro",
    "vol_impl": "Vol. Impl. (%)",
    "delta": "Delta",
}
COLUNAS_EXIBICAO: list[str] = list(ROTULOS.keys())

# Compatibilidade de largura de widgets entre versões do Streamlit
_ST_VER = tuple(
    int(x) for x in (re.findall(r"\d+", getattr(st, "__version__", "1.0.0")) + ["0", "0", "0"])[:3]
)
_LARGURA = {"width": "stretch"} if _ST_VER >= (1, 49) else {"use_container_width": True}


class FalhaExtracao(Exception):
    """Qualquer erro nas rotas de extração automática."""


# =============================================================================
# SEÇÃO 2 — UTILITÁRIOS: parsing numérico pt-BR e calendário da B3
# =============================================================================

_MULTIPLICADORES = {"k": 1e3, "mil": 1e3, "m": 1e6, "mi": 1e6, "mm": 1e6,
                    "b": 1e9, "bi": 1e9, "bn": 1e9}
_NULOS = {"", "-", "--", "---", "n/a", "na", "nan", "none", "null", "s/n", "—"}


def _norm(texto: object) -> str:
    """Normaliza nome de coluna: sem acento, minúsculo, só alfanumérico.

    'Vol. Financeiro (R$)' -> 'volfinanceirors'
    """
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def texto_para_float(valor: object) -> float:
    """Converte texto em padrão brasileiro (ou americano) para float.

    Trata 'R$ 1.234,56' | '12,34%' | '1,2 mi' | '-0,62' | '1,234.56' | '' -> NaN
    """
    if valor is None:
        return np.nan
    if isinstance(valor, (int, float, np.number)):
        v = float(valor)
        return np.nan if math.isnan(v) else v

    t = str(valor).strip().lower().replace("\xa0", " ")
    if t in _NULOS:
        return np.nan
    t = t.replace("r$", "").replace("%", "").replace("−", "-").strip()

    # sufixos de escala (mil / mi / bi / k / m / b)
    mult = 1.0
    m = re.search(r"([a-z]+)\.?$", t)
    if m and m.group(1) in _MULTIPLICADORES:
        mult = _MULTIPLICADORES[m.group(1)]
        t = t[: m.start()].strip()

    # Quem for o ÚLTIMO separador é o decimal. Assim '1.234,56' (pt-BR) e
    # '1,234.56' (en-US) são lidos como 1234.56 sem ambiguidade.
    t = t.replace(" ", "")
    virgula, ponto = t.rfind(","), t.rfind(".")
    if virgula >= 0 and ponto >= 0:
        if virgula > ponto:                               # pt-BR: 1.234,56
            t = t.replace(".", "").replace(",", ".")
        else:                                             # en-US: 1,234.56
            t = t.replace(",", "")
    elif virgula >= 0:                                    # 1234,56 -> 1234.56
        t = t.replace(",", ".")
    elif re.fullmatch(r"[-+]?[1-9]\d{0,2}(\.\d{3})+", t):  # 1.234 -> 1234
        # O grupo inicial não pode ser 0: '0.312' é um decimal (delta, vol.
        # implícita em fração), nunca milhar.
        t = t.replace(".", "")

    try:
        return float(t) * mult
    except ValueError:
        return np.nan


def serie_para_float(serie: pd.Series) -> pd.Series:
    """Aplica texto_para_float em uma coluna inteira."""
    if pd.api.types.is_numeric_dtype(serie):
        return pd.to_numeric(serie, errors="coerce").astype(float)
    return serie.map(texto_para_float).astype(float)


def _pascoa(ano: int) -> date:
    """Domingo de Páscoa (algoritmo Anônimo Gregoriano)."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    mm = (a + 11 * h + 22 * ll) // 451
    mes = (h + ll - 7 * mm + 114) // 31
    dia = ((h + ll - 7 * mm + 114) % 31) + 1
    return date(ano, mes, dia)


def feriados_b3(ano_ini: int, ano_fim: int) -> list[date]:
    """Dias sem pregão na B3: nacionais + 09/07 (SP) + 24/12 e 31/12."""
    fixos = [(1, 1), (4, 21), (5, 1), (7, 9), (9, 7), (10, 12),
             (11, 2), (11, 15), (11, 20), (12, 24), (12, 25), (12, 31)]
    out: list[date] = []
    for ano in range(ano_ini, ano_fim + 1):
        out += [date(ano, m, d) for m, d in fixos]
        pa = _pascoa(ano)
        out += [pa - timedelta(days=48),   # segunda de carnaval
                pa - timedelta(days=47),   # terça de carnaval
                pa - timedelta(days=2),    # sexta-feira santa
                pa + timedelta(days=60)]   # corpus christi
    return sorted(set(out))


@st.cache_data(show_spinner=False)
def _feriados_np(ano_ini: int, ano_fim: int) -> np.ndarray:
    return np.array([np.datetime64(d) for d in feriados_b3(ano_ini, ano_fim)],
                    dtype="datetime64[D]")


def dias_uteis(inicio: date, fim: date) -> int:
    """Dias úteis entre duas datas (exclui fim de semana e feriados da B3)."""
    if inicio is None or fim is None:
        return -1
    hol = _feriados_np(min(inicio.year, fim.year) - 1, max(inicio.year, fim.year) + 1)
    return int(np.busday_count(np.datetime64(inicio, "D"),
                               np.datetime64(fim, "D"), holidays=hol))


def proximo_dia_util(d: date) -> date:
    hol = set(feriados_b3(d.year - 1, d.year + 1))
    while d.weekday() >= 5 or d in hol:
        d += timedelta(days=1)
    return d


def terceira_sexta(ano: int, mes: int) -> date:
    """Vencimento padrão de opções de ações na B3: 3ª sexta-feira do mês."""
    primeiro = date(ano, mes, 1)
    primeira_sexta = primeiro + timedelta(days=(4 - primeiro.weekday()) % 7)
    return proximo_dia_util(primeira_sexta + timedelta(days=14))


def eh_vencimento_mensal(v: date) -> bool:
    """True se a data é o vencimento mensal (3ª sexta) e não um semanal.

    Importa para day trade: os semanais existem no mesmo intervalo de dias úteis
    mas costumam ter uma fração da liquidez do mensal.
    """
    try:
        return v == terceira_sexta(v.year, v.month)
    except Exception:
        return False


def calendario_vencimentos(referencia: date, meses: int = 8) -> list[date]:
    """Próximos vencimentos teóricos da B3 a partir de uma data de referência."""
    out: list[date] = []
    ano, mes = referencia.year, referencia.month
    for _ in range(meses):
        v = terceira_sexta(ano, mes)
        if v >= referencia:
            out.append(v)
        mes += 1
        if mes > 12:
            mes, ano = 1, ano + 1
    return out


# Código de série B3: Calls A..L = jan..dez | Puts M..X = jan..dez
_SERIE_CALL = {chr(ord("A") + i): i + 1 for i in range(12)}
_SERIE_PUT = {chr(ord("M") + i): i + 1 for i in range(12)}
_RE_TICKER_STR = r"^([A-Z]{4})([A-Z])(\d+)"
_RE_TICKER_OPCAO = re.compile(_RE_TICKER_STR)

# Série mensal convencional da B3: 4 letras de raiz + 1 letra de série + dígitos,
# e NADA depois. Semanais e séries atípicas carregam sufixo (PETRI483W4, W1..W5).
# Ancorar no fim é o que separa os dois: um "contains W" quebraria raízes com W,
# como WEGE3 -> WEGEI50.
_RE_TICKER_PADRAO = r"^[A-Z]{4}[A-Z]\d+$"


def info_do_ticker(ticker: object) -> tuple[str | None, int | None]:
    """Deduz (tipo, mês de vencimento) pelo código. Ex.: PETRK50 -> ('CALL', 11)."""
    m = _RE_TICKER_OPCAO.match(str(ticker).strip().upper())
    if not m:
        return None, None
    letra = m.group(2)
    if letra in _SERIE_CALL:
        return "CALL", _SERIE_CALL[letra]
    if letra in _SERIE_PUT:
        return "PUT", _SERIE_PUT[letra]
    return None, None


# =============================================================================
# SEÇÃO 3 — NORMALIZAÇÃO DE COLUNAS
# =============================================================================
# A ordem do dicionário importa: campos mais específicos são resolvidos antes
# dos genéricos, para evitar que 'Tipo Exercício' seja capturado por 'tipo' ou
# que 'Vol. Impl.' seja capturado por 'volume'.

ALIASES: dict[str, list[str]] = {
    "modelo":         ["mod", "modelo", "estilo", "tipoexercicio", "tipodeexercicio"],
    "tipo":           ["tipo", "cp", "callput", "tipoopcao", "opcaotipo"],
    "ticker":         ["ticker", "codigo", "codigoopcao", "opcao", "papel", "simbolo", "ativo"],
    "vencimento":     ["vencimento", "datavencimento", "datadevencimento", "venc", "expiracao", "maturidade"],
    "data_hora":      ["datahora", "datahoraultneg", "dataultimonegocio", "horaultneg"],
    "dist_strike":    ["distdostrike", "distpctdostrike", "distanciastrike", "dist", "distancia"],
    "strike":         ["strike", "precoexercicio", "precodeexercicio", "preexercicio", "exercicio"],
    "moneyness":      ["aiotm", "atmitmotm", "itmotm", "moneyness", "situacao", "classificacao"],
    "ultimo":         ["ultr", "ultimors", "ultimo", "ult", "precoultimo", "fechamento", "fech"],
    "variacao":       ["varpct", "var", "variacao", "variacaopct", "varia"],
    "num_neg":        ["numdeneg", "numneg", "numerodenegocios", "qtdnegocios", "negocios", "neg"],
    "vol_impl":       ["volimpl", "volimplpct", "volimplicita", "volatilidadeimplicita", "volimp", "iv"],
    "vol_financeiro": ["volfinanceiro", "volumefinanceiro", "volfinanceirors", "volfinr", "volfin",
                       "financeiro", "volumers", "volume"],
    "delta":          ["delta"],
    "spot":           ["precoativo", "precodoativo", "cotacaoativo", "precoacao", "spot"],
}


def mapear_colunas(colunas) -> dict[str, str]:
    """Casa as colunas do arquivo/site com os nomes canônicos internos.

    Estratégia em dois passes para tolerar mudanças de nome no site:
      1) igualdade exata do nome normalizado;
      2) prefixo/substring — só para apelidos com 4+ caracteres, evitando
         falsos positivos com abreviações curtas ('iv', 'cp', 'var').
    """
    norm = {str(c): _norm(c) for c in colunas}
    usados: set[str] = set()
    mapa: dict[str, str] = {}

    for campo, apelidos in ALIASES.items():
        achou = None
        for apelido in apelidos:                                  # passe 1: exato
            for col, n in norm.items():
                if col not in usados and n == apelido:
                    achou = col
                    break
            if achou:
                break
        if not achou:                                             # passe 2: parcial
            for apelido in apelidos:
                if len(apelido) < 4:
                    continue
                for col, n in norm.items():
                    if col not in usados and (n.startswith(apelido) or apelido in n):
                        achou = col
                        break
                if achou:
                    break
        if achou:
            mapa[campo] = achou
            usados.add(achou)
    return mapa


def _achatar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """Achata MultiIndex (comum no read_html) e garante nomes únicos de coluna."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [" ".join(str(p) for p in tupla if "Unnamed" not in str(p)).strip()
                      for tupla in df.columns]
    else:
        df = df.rename(columns=lambda c: str(c).strip())

    # nomes duplicados quebrariam df[coluna] (devolveria DataFrame, não Series)
    vistos: dict[str, int] = {}
    unicos: list[str] = []
    for c in df.columns:
        nome = str(c)
        if nome in vistos:
            vistos[nome] += 1
            nome = f"{nome}.{vistos[nome]}"
        else:
            vistos[nome] = 0
        unicos.append(nome)
    df.columns = unicos
    return df


def _detectar_header(df: pd.DataFrame) -> pd.DataFrame:
    """Reposiciona o cabeçalho quando o arquivo exportado tem preâmbulo.

    Varre as primeiras linhas procurando a que casa com mais colunas canônicas.
    """
    melhor_i, melhor_n = None, len(mapear_colunas(list(df.columns)))
    for i in range(min(12, len(df))):
        n = len(mapear_colunas([str(v) for v in df.iloc[i].tolist()]))
        if n > melhor_n:
            melhor_i, melhor_n = i, n
    if melhor_i is None:
        return df
    novo = df.iloc[melhor_i + 1:].copy()
    novo.columns = [str(v) for v in df.iloc[melhor_i].tolist()]
    return novo.reset_index(drop=True)


def _moneyness(strike: float, spot: float | None, tipo: str) -> str:
    """ITM / ATM / OTM a partir do strike e do preço do ativo."""
    if not spot or pd.isna(strike):
        return "—"
    if abs(strike / spot - 1.0) <= 0.01:
        return "ATM"
    if tipo == "CALL":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


def preparar_dataframe(df_bruto: pd.DataFrame, spot: float | None = None,
                       hoje: date | None = None,
                       taxa: float = 0.1075) -> pd.DataFrame:
    """Converte a grade bruta (site ou arquivo) no formato canônico do painel.

    Cada etapa é isolada em try/except: se uma coluna sumir ou mudar de nome no
    site, o painel perde aquele campo — mas continua funcionando.
    """
    hoje = hoje or date.today()
    if df_bruto is None or df_bruto.empty:
        return pd.DataFrame(columns=COLUNAS_EXIBICAO + ["tipo", "vencimento"])

    bruto = _achatar_colunas(df_bruto.copy())
    bruto = bruto.dropna(axis=1, how="all").dropna(axis=0, how="all")
    bruto = _detectar_header(bruto)
    mapa = mapear_colunas(list(bruto.columns))

    df = pd.DataFrame(index=bruto.index)
    for campo, coluna in mapa.items():
        df[campo] = bruto[coluna]

    # --- ticker ---------------------------------------------------------
    if "ticker" not in df.columns:
        raise FalhaExtracao(
            "Não encontrei a coluna de Ticker/Código da opção. "
            f"Colunas recebidas: {list(bruto.columns)[:15]}"
        )
    df["ticker"] = (df["ticker"].astype(str).str.strip().str.upper()
                    .str.split("_").str[0].str.replace(r"\s+", "", regex=True))
    df = df[df["ticker"].str.match(_RE_TICKER_STR, na=False)]
    if df.empty:
        raise FalhaExtracao("Nenhuma linha com código de opção válido.")

    # --- numéricos ------------------------------------------------------
    for campo in ("strike", "ultimo", "variacao", "num_neg", "vol_financeiro",
                  "vol_impl", "delta", "dist_strike", "spot"):
        if campo in df.columns:
            try:
                df[campo] = serie_para_float(df[campo])
            except Exception:
                df[campo] = np.nan
        else:
            df[campo] = np.nan

    # Delta às vezes vem em pontos percentuais (62 em vez de 0,62)
    try:
        maior_delta = df["delta"].abs().max(skipna=True)
        if pd.notna(maior_delta) and maior_delta > 1.5:
            df["delta"] = df["delta"] / 100.0
    except Exception:
        pass
    # Vol. implícita: normaliza para pontos percentuais (0,32 -> 32)
    try:
        maior_vi = df["vol_impl"].max(skipna=True)
        if pd.notna(maior_vi) and 0 < maior_vi <= 3.0:
            df["vol_impl"] = df["vol_impl"] * 100.0
    except Exception:
        pass

    # --- tipo (CALL/PUT) ------------------------------------------------
    inferido = df["ticker"].map(lambda t: info_do_ticker(t)[0])
    if "tipo" in df.columns:
        t = df["tipo"].astype(str).str.upper().str.strip()
        df["tipo"] = np.where(t.str.startswith(("C", "1")), "CALL",
                              np.where(t.str.startswith(("P", "2", "V")), "PUT", None))
        df["tipo"] = df["tipo"].fillna(pd.Series(inferido, index=df.index))
    else:
        df["tipo"] = inferido
    # último recurso: sinal do delta
    df["tipo"] = df["tipo"].fillna(
        pd.Series(np.where(df["delta"] >= 0, "CALL", "PUT"), index=df.index)
    )

    # --- modelo (Americana / Europeia) ----------------------------------
    if "modelo" in df.columns:
        m = df["modelo"].astype(str).str.upper().str.strip()
        df["modelo"] = np.where(m.str.startswith("A"), "Americana",
                                np.where(m.str.startswith("E"), "Europeia", "—"))
    else:
        df["modelo"] = "—"

    # --- vencimento ------------------------------------------------------
    venc = None
    if "vencimento" in df.columns:
        try:
            venc = pd.to_datetime(df["vencimento"], dayfirst=True, errors="coerce")
            if venc.isna().all():
                venc = None
        except Exception:
            venc = None
    if venc is None:  # deduz pelo código de série (letra do ticker)
        def _venc_por_serie(tk: str):
            _, mes = info_do_ticker(tk)
            if not mes:
                return pd.NaT
            ano = hoje.year if mes >= hoje.month else hoje.year + 1
            return pd.Timestamp(terceira_sexta(ano, mes))
        venc = df["ticker"].map(_venc_por_serie)
    df["vencimento"] = pd.to_datetime(venc, errors="coerce").dt.date

    # --- data do último negócio -------------------------------------------
    # Decisiva: o Delta é invertido do preço negociado, então cotação velha
    # produz volatilidade implícita absurda e Delta sem significado.
    df["data_neg"] = pd.NaT
    if "data_hora" in df.columns:
        try:
            quando = pd.to_datetime(df["data_hora"], errors="coerce", format="ISO8601")
            faltou = quando.isna()
            if faltou.any():
                quando.loc[faltou] = pd.to_datetime(
                    df.loc[faltou, "data_hora"], errors="coerce", dayfirst=True
                )
            df["data_neg"] = quando.dt.date
        except Exception:
            pass

    # --- preço do ativo-objeto -------------------------------------------
    spot_final = spot
    if not spot_final and df["spot"].notna().any():
        spot_final = float(df["spot"].dropna().median())
    if not spot_final and df["dist_strike"].notna().any():
        spot_final = _spot_implicito(df["strike"], df["dist_strike"])

    # --- moneyness -------------------------------------------------------
    if "moneyness" in df.columns and df["moneyness"].notna().any():
        df["moneyness"] = (df["moneyness"].astype(str).str.upper().str.strip()
                           .replace({"NAN": "—", "": "—", "NONE": "—"}))
    else:
        df["moneyness"] = [
            _moneyness(s, spot_final, t) for s, t in zip(df["strike"], df["tipo"])
        ]

    # --- Delta ------------------------------------------------------------
    # Só agora se descartam as linhas sem Delta: no plano gratuito do site ele
    # vem censurado, e completar_delta o calcula a partir do preço negociado.
    df["delta_calculado"] = False
    df = completar_delta(df, spot_final, hoje, taxa=taxa)
    df = df.dropna(subset=["delta"])

    df = df.reset_index(drop=True)
    df.attrs["spot"] = spot_final
    return df


# =============================================================================
# SEÇÃO 4 — EXTRAÇÃO (Rotas A1, A2, B e Demo)
# =============================================================================

def _valida_grade(df: pd.DataFrame) -> bool:
    """Confere se o que veio parece mesmo uma grade de opções.

    NÃO exige Delta: no plano gratuito do opcoes.net.br os gregos vêm como uma
    imagem borrada (`volblur.png`), e o painel calcula o Delta por conta própria.
    """
    if df is None or df.empty:
        return False
    df = _achatar_colunas(df)
    mapa = mapear_colunas(list(df.columns))
    if "ticker" not in mapa or "strike" not in mapa:
        return False
    tickers_ok = df[mapa["ticker"]].astype(str).str.upper().str.split("_").str[0] \
                   .str.match(_RE_TICKER_STR, na=False).mean()
    strikes = serie_para_float(df[mapa["strike"]])
    return bool(tickers_ok >= 0.70 and strikes.notna().mean() >= 0.70
                and (strikes.dropna() > 0).all())


def _get_json(url: str, params: dict, cabecalhos: dict, timeout: int) -> dict:
    """GET que devolve JSON, funcionando em Python nativo e no navegador."""
    if NO_NAVEGADOR:
        # Pyodide: XHR síncrono, mesma origem. Cabeçalhos e timeout ficam a
        # cargo da função serverless que faz o proxy.
        from pyodide.http import open_url
        return json.loads(open_url(f"{url}?{urlencode(params)}").read())

    if requests is None:
        raise FalhaExtracao("biblioteca 'requests' não instalada")
    resp = requests.get(url, headers=cabecalhos, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _json_opcoes(ativo: str, vencimento: date | None = None,
                 listar_vencimentos: bool = True, timeout: int = 20) -> dict:
    """Chama o endpoint interno do opcoes.net.br e devolve o bloco `data`."""
    params = {
        "idAcao": ativo,
        "listarVencimentos": "true" if listar_vencimentos else "false",
        "cotacoes": "true",
    }
    if vencimento is not None:
        params["vencimentos"] = vencimento.isoformat()

    cabecalhos = dict(HEADERS)
    cabecalhos["Referer"] = URL_PAGINA.format(ativo=ativo)
    payload = _get_json(URL_JSON, params, cabecalhos, timeout)
    if not isinstance(payload, dict) or not payload.get("success"):
        raise FalhaExtracao("endpoint respondeu success=false")
    dados = payload.get("data")
    if not isinstance(dados, dict):
        raise FalhaExtracao("resposta sem o bloco 'data'")
    return dados


@cache_dados(ttl=600, show_spinner=False)
def vencimentos_do_site(ativo: str, timeout: int = 20) -> list[dict]:
    """Vencimentos oferecidos pelo site, com os dias úteis que ele mesmo calcula."""
    dados = _json_opcoes(ativo, listar_vencimentos=True, timeout=timeout)
    saida: list[dict] = []
    for item in dados.get("vencimentos") or []:
        try:
            attrs = item.get("dataAttributes") or {}
            saida.append({
                "data": datetime.strptime(item["value"], "%Y-%m-%d").date(),
                "du": int(attrs.get("du", -1)),
                "mensal": str(attrs.get("m", "0")) == "1",
            })
        except Exception:
            continue
    if not saida:
        raise FalhaExtracao("o site não devolveu a lista de vencimentos")
    return saida


def _rota_json(ativo: str, vencimento: date, timeout: int = 20) -> pd.DataFrame:
    """Rota A1 — cotações de UM vencimento, via endpoint JSON interno.

    O payload traz a própria definição das colunas em `data.columns`, então os
    nomes são lidos de lá em vez de adivinhados por posição: se o site inserir ou
    reordenar uma coluna, o mapeamento continua correto.
    """
    dados = _json_opcoes(ativo, vencimento, listar_vencimentos=False, timeout=timeout)
    linhas = dados.get("cotacoesOpcoes") or []
    if not linhas:
        raise FalhaExtracao(f"sem cotações para {vencimento:%d/%m/%Y}")

    titulos = [str(c.get("title") or c.get("name") or f"col{i}")
               for i, c in enumerate(dados.get("columns") or [])]
    largura = max(len(l) for l in linhas)
    titulos += [f"col{i}" for i in range(len(titulos), largura)]

    df = pd.DataFrame([list(l) + [None] * (largura - len(l)) for l in linhas],
                      columns=titulos[:largura])

    if not _valida_grade(df):
        raise FalhaExtracao("layout do JSON mudou — validação falhou")
    return df


def _tabelas_do_html(html: str) -> list[pd.DataFrame]:
    """Extrai tabelas do HTML: pandas.read_html e, se falhar, BeautifulSoup."""
    tabelas: list[pd.DataFrame] = []
    try:
        tabelas = pd.read_html(io.StringIO(html), decimal=",", thousands=".")
    except Exception:
        tabelas = []
    if tabelas:
        return tabelas

    try:  # fallback manual com BeautifulSoup
        from bs4 import BeautifulSoup
        sopa = BeautifulSoup(html, "html.parser")
        for tab in sopa.find_all("table"):
            linhas = []
            for tr in tab.find_all("tr"):
                celulas = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if celulas:
                    linhas.append(celulas)
            if len(linhas) > 2:
                largura = max(len(l) for l in linhas)
                linhas = [l + [""] * (largura - len(l)) for l in linhas]
                tabelas.append(pd.DataFrame(linhas[1:], columns=linhas[0]))
    except Exception:
        pass
    return tabelas


def _melhor_tabela(tabelas: list[pd.DataFrame]) -> pd.DataFrame:
    """Escolhe, entre as tabelas da página, a que é a grade de opções."""
    candidatas = []
    for t in tabelas:
        if t is None or t.empty or t.shape[1] < 5:
            continue
        t = _achatar_colunas(t)
        chaves = {_norm(c) for c in t.columns}
        pontos = sum(any(k in c for c in chaves)
                     for k in ("delta", "strike", "volimpl", "numdeneg", "aiotm"))
        if pontos >= 2:
            candidatas.append((pontos, len(t), t))
    if not candidatas:
        raise FalhaExtracao("nenhuma tabela da página parece a grade de opções")
    candidatas.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidatas[0][2]


def _rota_selenium(ativo: str, headless: bool = True, espera: int = 25) -> pd.DataFrame:
    """Rota A2 — abre a página no Chrome headless e lê a tabela renderizada."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as exc:
        raise FalhaExtracao(f"Selenium/webdriver-manager indisponível ({exc})") from exc

    opcoes = Options()
    if headless:
        opcoes.add_argument("--headless=new")
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--window-size=1920,1400", "--lang=pt-BR",
                "--blink-settings=imagesEnabled=false"):
        opcoes.add_argument(arg)
    opcoes.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    opcoes.add_experimental_option("excludeSwitches", ["enable-automation"])
    opcoes.add_experimental_option("useAutomationExtension", False)

    driver = None
    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                                  options=opcoes)
        driver.set_page_load_timeout(espera + 20)
        driver.get(URL_PAGINA.format(ativo=ativo))
        WebDriverWait(driver, espera).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
        # a grade é preenchida por JS depois do <table>: espera as linhas
        limite = time.time() + espera
        while time.time() < limite:
            if len(driver.find_elements(By.CSS_SELECTOR, "table tbody tr")) > 5:
                break
            time.sleep(0.5)
        html = driver.page_source
    except Exception as exc:
        raise FalhaExtracao(f"Selenium falhou: {exc}") from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    df = _melhor_tabela(_tabelas_do_html(html))
    if not _valida_grade(df):
        raise FalhaExtracao("tabela lida não passou na validação")
    return df


@cache_dados(ttl=180, show_spinner=False)
def extrair_automatico(ativo: str, usar_selenium: bool = True, headless: bool = True,
                       du_limite: int = 50,
                       max_vencimentos: int = 8) -> tuple[pd.DataFrame, str, list[str], datetime]:
    """Rotas automáticas em cascata. Devolve (df, rota, log, coletado_em).

    As linhas do endpoint não carregam o vencimento — semanais e mensais dividem a
    mesma letra de série. Por isso busca-se um vencimento por vez (o site filtra no
    servidor) e a coluna Vencimento é carimbada aqui, o que também deixa o seletor
    da barra lateral trocar de ciclo sem nova ida à rede.
    """
    log: list[str] = []

    try:
        inicio = time.time()
        vencimentos = vencimentos_do_site(ativo)
        # Os mensais vêm primeiro: com 50 DU cabem o vencimento curto e o seguinte,
        # e as semanais só completam o lote (servem a quem desliga o filtro de série).
        dentro = [v for v in vencimentos if 0 < v["du"] <= du_limite]
        mensais = [v for v in dentro if v["mensal"]][:3]
        semanais = [v for v in dentro if not v["mensal"]][:max(0, max_vencimentos - len(mensais))]
        alvos = sorted(mensais + semanais, key=lambda v: v["data"])
        if not alvos:
            alvos = vencimentos[:3]
        log.append(f"{len(vencimentos)} vencimentos no site; buscando {len(alvos)}")

        partes: list[pd.DataFrame] = []
        for venc in alvos:
            try:
                parte = _rota_json(ativo, venc["data"])
                parte["Vencimento"] = venc["data"].strftime("%d/%m/%Y")
                partes.append(parte)
                ciclo = "mensal" if venc["mensal"] else "semanal"
                log.append(f"ok · {venc['data']:%d/%m} ({venc['du']} DU, {ciclo}) — "
                           f"{len(parte)} opções")
            except Exception as exc:
                log.append(f"aviso · {venc['data']:%d/%m} — {exc}")

        if partes:
            df = pd.concat(partes, ignore_index=True)
            log.append(f"concluída · Rota A1 · JSON — {len(df)} linhas em "
                       f"{time.time() - inicio:.1f}s")
            return df, "Rota A1 · JSON", log, datetime.now()
        raise FalhaExtracao("nenhum vencimento retornou cotações")
    except Exception as exc:
        log.append(f"falhou · Rota A1 · JSON — {exc}")

    if usar_selenium:
        try:
            inicio = time.time()
            df = _rota_selenium(ativo, headless)
            log.append(f"concluída · Rota A2 · Selenium — {len(df)} linhas em "
                       f"{time.time() - inicio:.1f}s")
            return df, "Rota A2 · Selenium", log, datetime.now()
        except Exception as exc:
            log.append(f"falhou · Rota A2 · Selenium — {exc}")

    raise FalhaExtracao(
        "Todas as rotas automáticas falharam. Use o upload do CSV/Excel na barra lateral."
    )


def _csv_para_df(texto: str, sep: str) -> pd.DataFrame | None:
    """Lê o CSV linha a linha, tolerando preâmbulo e linhas de larguras diferentes.

    read_csv quebraria aqui: ele fixa a largura pela primeira linha, e arquivos
    exportados costumam trazer um cabeçalho de relatório antes da grade.
    """
    linhas = [linha for linha in csv.reader(io.StringIO(texto), delimiter=sep)
              if any(str(c).strip() for c in linha)]
    if len(linhas) < 2:
        return None
    largura = max(len(l) for l in linhas)
    if largura < 3:
        return None
    linhas = [l + [""] * (largura - len(l)) for l in linhas]
    return pd.DataFrame(linhas, columns=[str(i) for i in range(largura)])


@st.cache_data(show_spinner=False)
def ler_arquivo(nome: str, conteudo: bytes) -> pd.DataFrame:
    """Rota B — lê o CSV/Excel exportado do site, adivinhando separador e encoding.

    Sempre lê sem cabeçalho e deixa `_detectar_header` achar a linha de títulos,
    o que funciona igual com ou sem preâmbulo de relatório.
    """
    extensao = nome.lower().rsplit(".", 1)[-1]

    if extensao in ("xlsx", "xls", "xlsm"):
        try:
            bruto = pd.read_excel(io.BytesIO(conteudo), header=None, dtype=object)
        except Exception as exc:
            raise FalhaExtracao(f"não consegui ler o Excel: {exc}") from exc
        return _detectar_header(_achatar_colunas(bruto))

    melhor: pd.DataFrame | None = None
    melhor_pontos = -1
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            texto = conteudo.decode(encoding)
        except Exception:
            continue
        for sep in (";", ",", "\t", "|"):
            try:
                df = _csv_para_df(texto, sep)
            except Exception:
                continue
            if df is None:
                continue
            df = _detectar_header(df)
            pontos = len(mapear_colunas(list(df.columns)))
            if pontos > melhor_pontos:
                melhor, melhor_pontos = df, pontos
        if melhor_pontos >= 6:
            break

    if melhor is None:
        raise FalhaExtracao("não consegui identificar o separador do CSV")
    if melhor_pontos < 2:
        raise FalhaExtracao(
            "o arquivo foi lido, mas não reconheci as colunas da grade de opções"
        )
    return melhor


# ---- Rota D: grade sintética (só para validar a interface) -------------------

_SPOT_DEMO = {"BOVA11": 142.0, "PETR4": 38.5, "VALE3": 61.0,
              "BBAS3": 22.4, "ITUB4": 36.8, "BPAC11": 39.2}


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _black_scholes(s: float, k: float, t: float, sigma: float,
                   r: float, tipo: str) -> tuple[float, float]:
    """Devolve (preço, delta) — usado apenas na grade de demonstração."""
    if t <= 0 or sigma <= 0:
        intrinseco = max(s - k, 0.0) if tipo == "CALL" else max(k - s, 0.0)
        return intrinseco, (1.0 if intrinseco > 0 and tipo == "CALL" else 0.0)
    d1 = (math.log(s / k) + (r + sigma * sigma / 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if tipo == "CALL":
        return s * _phi(d1) - k * math.exp(-r * t) * _phi(d2), _phi(d1)
    return k * math.exp(-r * t) * _phi(-d2) - s * _phi(-d1), _phi(d1) - 1.0


def volatilidade_implicita(preco: float, s: float, k: float, t: float, r: float,
                           tipo: str, lo: float = 1e-4, hi: float = 5.0) -> float | None:
    """Inverte o preço de mercado para achar a volatilidade implícita (bisseção).

    Devolve None quando o preço não admite solução — típico de opção com última
    negociação velha, cujo preço já não é compatível com o spot de hoje.
    """
    if not preco or preco <= 0 or t <= 0 or s <= 0 or k <= 0:
        return None
    try:
        if _black_scholes(s, k, t, hi, r, tipo)[0] < preco:
            return None
        if _black_scholes(s, k, t, lo, r, tipo)[0] > preco:
            return None
        for _ in range(60):
            meio = (lo + hi) / 2
            if _black_scholes(s, k, t, meio, r, tipo)[0] < preco:
                lo = meio
            else:
                hi = meio
        return (lo + hi) / 2
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def _spot_implicito(strike: pd.Series, dist: pd.Series) -> float | None:
    """Deduz o preço do ativo-objeto a partir da distância percentual do strike.

    O site publica `Distância % do Strike`, e spot = strike / (1 + dist). Testa a
    escala (fração ou pontos percentuais) e só aceita se as linhas concordarem
    entre si — dispersão alta significa que a coluna não é o que se supunha.
    """
    melhor: tuple[float, float] | None = None
    for escala in (1.0, 100.0):
        razao = strike / (1 + dist / escala)
        validos = razao[np.isfinite(razao) & (razao > 0)]
        if len(validos) < 3:
            continue
        mediana = float(validos.median())
        if mediana <= 0:
            continue
        dispersao = float(validos.std()) / mediana
        if melhor is None or dispersao < melhor[0]:
            melhor = (dispersao, mediana)
    return melhor[1] if melhor and melhor[0] < 0.05 else None


def completar_delta(df: pd.DataFrame, spot: float | None, hoje: date,
                    taxa: float = 0.1075, iv_maxima: float = 1.50) -> pd.DataFrame:
    """Calcula Delta e Vol. Implícita nas linhas em que a fonte não os forneceu.

    No plano gratuito do opcoes.net.br os gregos vêm censurados (uma imagem
    borrada), o que zeraria o filtro de Delta — que é a regra central do painel.
    A saída: inverter a volatilidade implícita do preço negociado e derivar o
    Delta por Black-Scholes. Marca as linhas em `delta_calculado`.
    """
    df = df.copy()
    if "delta_calculado" not in df.columns:
        df["delta_calculado"] = False

    faltando = df["delta"].isna()
    if not faltando.any() or not spot or spot <= 0:
        return df

    deltas: list[float] = []
    vols: list[float] = []
    for idx in df.index[faltando]:
        linha = df.loc[idx]
        venc = linha.get("vencimento")
        du = dias_uteis(hoje, venc) if pd.notna(venc) else -1
        t = du / 252.0
        sigma = volatilidade_implicita(linha.get("ultimo"), spot, linha.get("strike"),
                                       t, taxa, linha.get("tipo"))
        if sigma is None or sigma > iv_maxima:
            deltas.append(np.nan)
            vols.append(np.nan)
            continue
        _, delta = _black_scholes(spot, linha["strike"], t, sigma, taxa, linha["tipo"])
        deltas.append(delta)
        vols.append(sigma * 100.0)

    df.loc[faltando, "delta"] = deltas
    vi_vazia = df["vol_impl"].isna() & faltando
    df.loc[faltando, "vol_impl"] = np.where(vi_vazia[faltando], vols,
                                            df.loc[faltando, "vol_impl"])
    df.loc[faltando, "delta_calculado"] = pd.notna(deltas)
    return df


@st.cache_data(show_spinner=False)
def grade_demo(ativo: str, hoje: date) -> pd.DataFrame:
    """Grade sintética coerente (Black-Scholes) para testar o painel offline."""
    rng = np.random.default_rng(abs(hash(ativo)) % (2 ** 32))
    spot = _SPOT_DEMO.get(ativo, 50.0)
    passo = 0.50 if spot < 50 else (1.0 if spot < 100 else 2.0)
    # âncora em múltiplo do passo, como na grade real de strikes da B3
    centro = round(spot / passo) * passo
    linhas: list[dict] = []

    for venc in calendario_vencimentos(hoje, meses=3):
        du = dias_uteis(hoje, venc)
        if du <= 0:
            continue
        t = du / 252.0
        mes = venc.month
        for i in range(-20, 21):
            strike = round(centro + i * passo, 2)
            if strike <= 0:
                continue
            for tipo in ("CALL", "PUT"):
                sigma = 0.28 + 0.10 * abs(strike / spot - 1) + rng.uniform(-0.02, 0.02)
                preco, delta = _black_scholes(spot, strike, t, sigma, 0.1075, tipo)
                if preco < 0.01:
                    continue
                letra = (chr(ord("A") + mes - 1) if tipo == "CALL"
                         else chr(ord("M") + mes - 1))
                # a B3 codifica strike fracionário multiplicado por 10 (37,50 -> 375)
                cod = (int(round(strike)) if abs(strike - round(strike)) < 0.01
                       else int(round(strike * 10)))
                proximidade = math.exp(-((strike / spot - 1) / 0.06) ** 2)
                negocios = int(max(0, rng.normal(900, 250) * proximidade))
                linhas.append({
                    "Ticker": f"{ativo[:4]}{letra}{cod}",
                    "Tipo": tipo,
                    "Mod.": "Americana" if tipo == "CALL" else "Europeia",
                    "Strike": strike,
                    "A/I/OTM": _moneyness(strike, spot, tipo),
                    "Últ. (R$)": round(preco, 2),
                    "Var. (%)": round(rng.normal(0, 6), 2),
                    "Núm. de Neg.": negocios,
                    "Vol. Financeiro": round(negocios * preco * rng.uniform(80, 400), 2),
                    "Vol. Impl. (%)": round(sigma * 100, 2),
                    "Delta": round(delta, 4),
                    "Vencimento": venc.strftime("%d/%m/%Y"),
                })
    return pd.DataFrame(linhas)


# =============================================================================
# SEÇÃO 5 — PROCESSAMENTO: os três filtros do operacional
# =============================================================================

def aplicar_filtros(df: pd.DataFrame, vencimento: date | None,
                    delta_min: float, delta_max: float,
                    min_negocios: int = 0,
                    exigir_negocio: bool = True,
                    max_atraso_du: int | None = 1,
                    somente_padrao: bool = True) -> tuple[pd.DataFrame, list[tuple[str, int]]]:
    """Nomenclatura -> vencimento -> frescor -> delta -> liquidez. Devolve (df, funil).

    A limpeza de nomenclatura vem primeiro, antes da ordenação por liquidez, para
    que o topo da lista seja necessariamente um contrato convencional.
    """
    funil: list[tuple[str, int]] = [("Grade completa", len(df))]
    out = df.copy()

    # 0) Nomenclatura: só a série mensal convencional da B3
    if somente_padrao:
        out = out[out["ticker"].str.match(_RE_TICKER_PADRAO, na=False)]
        funil.append(("Série mensal padrão", len(out)))

    # 1) Vencimento (ciclo escolhido no seletor)
    if vencimento is not None and out["vencimento"].notna().any():
        out = out[out["vencimento"] == vencimento]
    funil.append((f"Vencimento {vencimento:%d/%m}" if vencimento else "Vencimento", len(out)))

    # 2) Frescor da cotação. Opção parada há semanas mantém volume acumulado alto
    # e subiria no ranking de liquidez carregando um Delta calculado de preço
    # velho — o pregão de referência é o mais recente da própria grade.
    if max_atraso_du is not None and "data_neg" in df.columns and df["data_neg"].notna().any():
        referencia = max(d for d in df["data_neg"].dropna())
        atraso = out["data_neg"].map(
            lambda d: dias_uteis(d, referencia) if pd.notna(d) else 10_000
        )
        out = out[atraso <= max_atraso_du]
        funil.append((f"Negociada há ≤ {max_atraso_du} "
                      f"{'pregão' if max_atraso_du == 1 else 'pregões'}", len(out)))

    # 3) Delta absoluto na faixa obrigatória
    absd = out["delta"].abs()
    out = out[(absd >= delta_min) & (absd <= delta_max)]
    funil.append((f"|Δ| entre {_num(delta_min, 2)} e {_num(delta_max, 2)}", len(out)))

    # 4) Liquidez
    if exigir_negocio:
        out = out[(out["num_neg"].fillna(0) > 0) | (out["vol_financeiro"].fillna(0) > 0)]
    if min_negocios > 0:
        out = out[out["num_neg"].fillna(0) >= min_negocios]
    funil.append((f"Liquidez (≥ {min_negocios} neg.)" if min_negocios else "Liquidez > 0", len(out)))

    return ordenar_por_liquidez(out), funil


def ordenar_por_liquidez(df: pd.DataFrame) -> pd.DataFrame:
    """Volume Financeiro DESC, depois Núm. de Negócios DESC. O topo é a escolhida."""
    if df.empty:
        return df
    return df.sort_values(by=["vol_financeiro", "num_neg"],
                          ascending=[False, False], na_position="last").reset_index(drop=True)


def separar_calls_puts(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    calls = df[(df["tipo"] == "CALL") & (df["delta"] > 0)]
    puts = df[(df["tipo"] == "PUT")]
    return ordenar_por_liquidez(calls), ordenar_por_liquidez(puts)


# =============================================================================
# SEÇÃO 5B — MERCADOS GLOBAIS (aba Correlações)
# =============================================================================
# O que andou lá fora enquanto a B3 estava fechada. O investing.com proíbe
# reproduzir os dados dele sem autorização por escrito, então a fonte é o
# Yahoo Finance, que cobre os mesmos futuros e índices. O Yahoo não tem
# minério de ferro nem o Ibovespa futuro: o minério vem da API pública do SGX
# e o mini-índice, do site de cotações da B3.

YAHOO_SPARK = "https://query1.finance.yahoo.com/v7/finance/spark"
_CABECALHOS_YAHOO = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}
_LOTE_YAHOO = 20                               # limite de símbolos por chamada
BRT = timezone(timedelta(hours=-3), "BRT")     # sem horário de verão desde 2019
JANELAS_CORR: list = ["hoje", 20, 60, 120]     # "hoje" = barras de 5 min do pregão
SGX_FEF = "https://api.sgx.com/derivatives/v1.0/contract-code/FEF"
SGX_HISTORICO = "https://api.sgx.com/derivatives/v1.0/history/symbol/{simbolo}"
B3_DERIVATIVOS = "https://cotacao.b3.com.br/mds/api/v1/DerivativeQuotation/{ativo}"
B3_OSCILACAO = "https://cotacao.b3.com.br/mds/api/v1/DailyFluctuationHistory/{simbolo}"
SGT = timezone(timedelta(hours=8), "SGT")      # Singapura

# (chave, título, descrição, [(símbolo no Yahoo, nome, detalhe)])
GRUPOS_GLOBAIS: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
    ("eua", "EUA · índices futuros", "futuros da CME, negociados quase 24 h", [
        ("ES=F", "S&P 500", "futuro"), ("NQ=F", "Nasdaq 100", "futuro"),
        ("YM=F", "Dow Jones", "futuro"), ("RTY=F", "Russell 2000", "futuro"),
        ("^VIX", "VIX", "volatilidade do S&P")]),
    ("brny", "Brasil", "minicontratos na B3 · ADRs e ETF no pré e pós de Nova York", [
        ("B3:WIN", "Ibovespa futuro", "mini-índice · B3"), ("B3:WDO", "Dólar futuro", "mini-dólar · B3"),
        ("EWZ", "EWZ", "ETF de Brasil"),
        ("PBR", "Petrobras", "ADR"), ("VALE", "Vale", "ADR"), ("ITUB", "Itaú", "ADR"),
        ("BBD", "Bradesco", "ADR")]),
    ("europa", "Europa", "pregão à vista, aberto antes da B3", [
        ("^STOXX50E", "Euro Stoxx 50", "zona do euro"), ("^GDAXI", "DAX", "Alemanha"),
        ("^FTSE", "FTSE 100", "Reino Unido"), ("^FCHI", "CAC 40", "França"),
        ("FTSEMIB.MI", "FTSE MIB", "Itália"), ("^IBEX", "IBEX 35", "Espanha")]),
    ("asia", "Ásia e Pacífico", "fecham antes da B3 abrir · Nikkei em futuro", [
        ("NKD=F", "Nikkei 225", "futuro"), ("^HSI", "Hang Seng", "Hong Kong"),
        ("000001.SS", "Xangai", "China"), ("000300.SS", "CSI 300", "China"),
        ("^KS11", "KOSPI", "Coreia do Sul"), ("^AXJO", "ASX 200", "Austrália")]),
    ("energia", "Energia", "futuros NYMEX e ICE", [
        ("BZ=F", "Petróleo Brent", "US$/barril"), ("CL=F", "Petróleo WTI", "US$/barril"),
        ("NG=F", "Gás natural", "US$/MMBtu"), ("RB=F", "Gasolina RBOB", "US$/galão"),
        ("HO=F", "Óleo de aquecimento", "US$/galão")]),
    ("metais", "Metais e mineração", "COMEX e SGX · mineradoras de apoio", [
        ("SGX:FEF", "Minério de ferro", "62% Fe · US$/t · SGX"), ("GC=F", "Ouro", "US$/onça"), ("SI=F", "Prata", "US$/onça"), ("HG=F", "Cobre", "US$/libra"),
        ("PL=F", "Platina", "US$/onça"), ("ALI=F", "Alumínio", "US$/tonelada"),
        ("BHP.AX", "BHP", "mineradora · Sydney"), ("RIO.L", "Rio Tinto", "mineradora · Londres")]),
    ("cambio", "Câmbio e juros", "moedas 24 h · juros em % a.a., variação em pontos", [
        ("USDBRL=X", "Dólar/Real", "USD/BRL"), ("DX-Y.NYB", "DXY", "dólar contra 6 moedas"),
        ("CNY=X", "Dólar/Yuan", "USD/CNY"), ("B3:DI1C", "DI curto", "taxa % a.a. · B3"),
        ("B3:DI1M", "DI médio", "taxa % a.a. · B3"), ("B3:DI1L", "DI longo", "taxa % a.a. · B3"),
        ("^TNX", "Treasury 10 anos", "taxa % a.a.")]),
    ("agro", "Agrícolas e pecuária", "futuros CBOT, ICE e CME", [
        ("ZS=F", "Soja", "US¢/bushel"), ("ZC=F", "Milho", "US¢/bushel"), ("ZW=F", "Trigo", "US¢/bushel"),
        ("KC=F", "Café", "US¢/libra"), ("SB=F", "Açúcar", "US¢/libra"), ("CT=F", "Algodão", "US¢/libra"),
        ("LE=F", "Boi gordo", "US¢/libra")]),
]
PULSO: list[str] = ["B3:WIN", "B3:WDO", "B3:DI1M", "ES=F", "BZ=F", "SGX:FEF", "^HSI", "EWZ"]
# Colunas da matriz de correlação. ADRs ficam de fora: correlação perto de 1
# com o próprio papel não ensina nada.
MOTORES: list[tuple[str, str]] = [
    ("EWZ", "EWZ"), ("ES=F", "S&P 500"), ("^VIX", "VIX"), ("^HSI", "Hang Seng"), ("BZ=F", "Brent"),
    ("SGX:FEF", "Minério"), ("HG=F", "Cobre"), ("RIO.L", "Rio Tinto"), ("GC=F", "Ouro"),
    ("USDBRL=X", "Dólar"), ("DX-Y.NYB", "DXY"), ("^TNX", "Treasury"),
]
B3_YAHOO: dict[str, str] = {ativo: f"{ativo}.SA" for ativo in ATIVOS}
NOMES: dict[str, tuple[str, str]] = {s: (nome, det) for *_, itens in GRUPOS_GLOBAIS for s, nome, det in itens}


def simbolos_mercados() -> tuple[str, ...]:
    """Todos os símbolos da aba, sem repetição, na ordem dos grupos."""
    todos = [s for *_, itens in GRUPOS_GLOBAIS for s, _, _ in itens] + PULSO + [s for s, _ in MOTORES]
    return tuple(dict.fromkeys(todos))


def _spark(simbolos: list[str], faixa: str, intervalo: str) -> tuple[dict[str, dict], list[str]]:
    """Endpoint spark do Yahoo, em lotes de 20 disparados em paralelo.

    Devolve {símbolo: resposta} e os lotes que falharam: um lote fora do ar não
    derruba a aba inteira.
    """
    lotes = [simbolos[i:i + _LOTE_YAHOO] for i in range(0, len(simbolos), _LOTE_YAHOO)]
    base = {"range": faixa, "interval": intervalo, "includePrePost": "true"}

    def busca(lote: list[str]) -> tuple[list, str | None]:
        try:
            dados = _get_json(YAHOO_SPARK, {**base, "symbols": ",".join(lote)}, _CABECALHOS_YAHOO, 20)
            return ((dados or {}).get("spark") or {}).get("result") or [], None
        except Exception as exc:
            return [], f"{lote[0]} e mais {len(lote) - 1}: {exc}"

    if NO_NAVEGADOR or len(lotes) <= 1:
        blocos = [busca(lote) for lote in lotes]
    else:
        with ThreadPoolExecutor(max_workers=len(lotes)) as pool:
            blocos = list(pool.map(busca, lotes))
    respostas, falhas = {}, []
    for itens, falha in blocos:
        if falha:
            falhas.append(falha)
        for item in itens:
            resp = (item.get("response") or [None])[0]
            if item.get("symbol") and resp:
                respostas[item["symbol"]] = resp
    return respostas, falhas


def _fechamentos(resp: dict) -> list:
    return (((resp.get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []


def ler_cotacao(resp: dict, agora: float) -> dict | None:
    """Último preço, variação e curva do dia a partir de uma resposta do spark.

    A referência é o fechamento anterior (o ajuste, nos futuros). Em ADR e ETF
    de Nova York fora do pregão, o último negócio do pré ou do pós-mercado vira
    o preço e a referência passa a ser o fechamento regular.
    """
    meta = resp.get("meta") or {}
    ultimo = meta.get("regularMarketPrice")
    if ultimo is None:
        return None
    ref = meta.get("previousClose") or meta.get("chartPreviousClose")
    hora = int(meta.get("regularMarketTime") or 0)
    pontos = [(int(t), float(c)) for t, c in zip(resp.get("timestamp") or [], _fechamentos(resp))
              if c is not None]
    fase = "regular"
    if meta.get("hasPrePostMarketData") and pontos and pontos[-1][0] > hora + 120:
        abertura = int(((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("start") or 0)
        fase = "pré" if agora < abertura else "pós"
        pontos = [p for p in pontos if p[0] > hora]
        ref, ultimo, hora = ultimo, pontos[-1][1], pontos[-1][0]
    ultimo = float(ultimo)
    ref = float(ref) if ref else None
    return {"ultimo": ultimo, "ref": ref, "var": ultimo - ref if ref else None,
            "var_pct": (ultimo / ref - 1) * 100 if ref else None, "hora": hora, "fase": fase,
            "pontos": pontos, "casas": int(meta.get("priceHint") or 2)}


def _contrato_minerio() -> list[dict]:
    """Registros do SGX do contrato de minério mais negociado entre os três primeiros vencimentos.

    O primeiro vencimento é o mês corrente (média do índice no mês) e costuma
    negociar menos que o seguinte, que é a referência do mercado. Cada contrato
    tem um registro por sessão (diurna e T+1, à noite em Singapura); a lista sai
    em ordem de atualização, o mais recente por último.
    """
    dados = _get_json(SGX_FEF, {"order": "asc", "orderby": "delivery-month", "category": "futures",
                                "session": "-1", "t": int(time.time() * 1000), "showTAndTPlusOne": "false"},
                      _CABECALHOS_YAHOO, 20).get("data") or []
    com_preco = [d for d in dados if d.get("it") == "mffc" and d.get("last-traded-price-adj") is not None
                 and re.fullmatch(r"FEF[FGHJKMNQUVXZ]\d\d", str(d.get("symbol")))]
    if not com_preco:
        return []
    meses = sorted({d["symbol"]: str(d.get("delivery-month")) for d in com_preco}.items(), key=lambda kv: kv[1])
    volume = {sym: sum(float(d.get("volume-trade") or 0) for d in com_preco if d["symbol"] == sym)
              for sym, _ in meses[:3]}
    escolhido = max(volume, key=volume.get)
    return sorted((d for d in com_preco if d["symbol"] == escolhido),
                  key=lambda d: str(d.get("record-update-time") or ""))


def cotacao_minerio() -> dict | None:
    """Minério de ferro 62% Fe (SGX IODEX), em US$/t, no contrato mais negociado.

    O preço é o do registro mais recente; a referência é o ajuste anterior, que
    só a sessão diurna informa (último − variação). Na sessão T+1 a variação vem
    vazia, e sem esse cuidado o bloco ficaria sem variação a noite toda em
    Singapura, justamente a manhã daqui.
    """
    registros = _contrato_minerio()
    if not registros:
        return None
    d = registros[-1]
    ultimo = float(d["last-traded-price-adj"])
    ref = None
    for r in registros:
        if r.get("change-adj") is not None:
            ref = float(r["last-traded-price-adj"]) - float(r["change-adj"])
    try:
        hora = int(datetime.strptime(str(d.get("last-update-time"))[:19], "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=SGT).timestamp())
    except ValueError:
        hora = int(float(d.get("updated-time") or 0) / 1000)
    return {"ultimo": ultimo, "ref": ref, "var": ultimo - ref if ref else None,
            "var_pct": (ultimo / ref - 1) * 100 if ref else None, "hora": hora, "fase": "regular",
            "pontos": [], "casas": 2, "contrato": d["symbol"]}


def historico_minerio() -> pd.Series:
    """Ajustes diários do contrato de minério mais negociado, pela data do pregão em Singapura."""
    registros = _contrato_minerio()
    if not registros:
        return pd.Series(dtype=float)
    dados = _get_json(SGX_HISTORICO.format(simbolo=registros[-1]["symbol"]),
                      {"days": "1y", "category": "futures",
                       "params": "record-date,base-date,daily-settlement-price-abs"},
                      _CABECALHOS_YAHOO, 20).get("data") or []
    serie = {}
    for r in dados:
        try:
            # record-date é o dia do pregão; base-date já aponta para o dia seguinte
            dia = (datetime.strptime(r["record-date"], "%Y-%m-%d") if r.get("record-date")
                   else datetime.strptime(str(r["base-date"]), "%Y%m%d")).date()
            serie[dia] = float(r["daily-settlement-price-abs"])
        except (KeyError, TypeError, ValueError):
            continue
    return pd.Series(serie, dtype=float).sort_index()


def _curva_b3(simbolo: str, dia: date) -> list[tuple[int, float]]:
    """Minuto a minuto do pregão do mini-índice, com o mesmo atraso da cotação."""
    try:
        dados = _get_json(B3_OSCILACAO.format(simbolo=simbolo), {}, _CABECALHOS_YAHOO, 15)
    except Exception:
        return []
    pontos = []
    for q in (((dados.get("TradgFlr") or {}).get("scty") or {}).get("lstQtn")) or []:
        try:
            quando = datetime.combine(dia, datetime.strptime(str(q["dtTm"]), "%H:%M:%S").time(), tzinfo=BRT)
            pontos.append((int(quando.timestamp()), float(q["closPric"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pontos


def cotacao_futuro_b3(ativo: str, casas: int, escolher=None, unidade: str = "%") -> dict | None:
    """Minicontrato da B3 (WIN, WDO) no vencimento vigente, pelo site de cotações (15 min de atraso).

    Antes do primeiro negócio a B3 publica o preço teórico do leilão de abertura
    (compra igual à venda); sem ele, fica o ajuste anterior, marcado como tal. A
    lista mistura futuros e opções sobre o futuro, então só entra o mercado FUT.
    """
    dados = _get_json(B3_DERIVATIVOS.format(ativo=ativo), {}, _CABECALHOS_YAHOO, 20)
    try:
        consulta = datetime.strptime(str((dados.get("Msg") or {}).get("dtTm")),
                                     "%Y-%m-%d %H:%M:%S").replace(tzinfo=BRT)
    except ValueError:
        consulta = datetime.now(BRT)

    def vencimento(c: dict) -> str:
        return str(((c.get("asset") or {}).get("AsstSummry") or {}).get("mtrtyCode") or "")

    vigentes = sorted((c for c in dados.get("Scty") or []
                       if (c.get("mkt") or {}).get("cd", "FUT") == "FUT"
                       and vencimento(c) >= f"{consulta:%Y-%m-%d}"), key=vencimento)
    if not vigentes:
        return None
    contrato = escolher(vigentes, consulta.date()) if escolher else vigentes[0]
    if not contrato:
        return None
    q = contrato.get("SctyQtn") or {}
    ref = q.get("prvsDayAdjstmntPric")
    ultimo, fase = q.get("curPrc"), "regular"
    if ultimo is None:
        compra = (contrato.get("buyOffer") or {}).get("price")
        venda = (contrato.get("sellOffer") or {}).get("price")
        ultimo, fase = (compra, "leilão") if compra and compra == venda else (ref, "ajuste")
    if ultimo is None:
        return None
    pontos = _curva_b3(contrato["symb"], consulta.date()) if fase == "regular" else []
    if fase == "ajuste":
        hora = 0
    elif pontos:
        hora = pontos[-1][0]
    else:
        hora = int((consulta - timedelta(minutes=15)).timestamp())
    ultimo = float(ultimo)
    ref = float(ref) if ref else None
    return {"ultimo": ultimo, "ref": ref, "var": ultimo - ref if ref else None,
            "var_pct": (ultimo / ref - 1) * 100 if ref else None, "hora": hora, "fase": fase,
            "pontos": pontos, "casas_fixas": casas, "contrato": contrato["symb"], "unidade": unidade}


def cotacao_ibov_futuro() -> dict | None:
    """Mini-índice (WIN), em pontos."""
    return cotacao_futuro_b3("WIN", 0)


def cotacao_dolar_futuro() -> dict | None:
    """Mini-dólar (WDO), em reais por mil dólares."""
    return cotacao_futuro_b3("WDO", 1)


def _vertice_di(anos: int):
    """Escolhe o DI de janeiro `anos` depois do curto (o primeiro janeiro a mais de 90 dias).

    Assim os vértices rolam sozinhos: em 2026 são jan/27, jan/29 e jan/31, e o
    curto não vira um contrato a poucos dias do vencimento, que quase não oscila.
    """
    def escolher(vigentes: list[dict], dia: date) -> dict | None:
        janeiros = [c for c in vigentes if re.fullmatch(r"DI1F\d\d", str(c.get("symb")))]
        limite = f"{dia + timedelta(days=90):%Y-%m-%d}"
        curtos = [c for c in janeiros
                  if str(((c.get("asset") or {}).get("AsstSummry") or {}).get("mtrtyCode") or "") > limite]
        if not curtos:
            return None
        alvo = f"DI1F{(int(curtos[0]['symb'][-2:]) + anos) % 100:02d}"
        return next((c for c in janeiros if c["symb"] == alvo), None)
    return escolher


def cotacao_di(anos: int) -> dict | None:
    """DI futuro (taxa % a.a.) no vértice de janeiro pedido; variação em pontos percentuais."""
    return cotacao_futuro_b3("DI1", 3, escolher=_vertice_di(anos), unidade="pp")


# Mercados fora do Yahoo: símbolo interno -> função que devolve a cotação
_FONTES_EXTRAS = {"B3:WIN": cotacao_ibov_futuro, "B3:WDO": cotacao_dolar_futuro,
                  "B3:DI1C": lambda: cotacao_di(0), "B3:DI1M": lambda: cotacao_di(2),
                  "B3:DI1L": lambda: cotacao_di(4), "SGX:FEF": cotacao_minerio}
_EM_PONTOS = {"^TNX"}   # taxas do Yahoo: variação em pontos percentuais, não em %


@cache_dados(ttl=60, show_spinner=False)
def cotacoes_globais(simbolos: tuple[str, ...]) -> tuple[dict[str, dict], datetime, list[str]]:
    """Cotação de cada símbolo, com a curva do dia (cache de 1 min).

    Yahoo, SGX e B3 são consultados ao mesmo tempo; uma fonte fora do ar só deixa
    em branco os mercados dela.
    """
    yahoo = [s for s in simbolos if s not in _FONTES_EXTRAS]
    extras = [s for s in simbolos if s in _FONTES_EXTRAS]

    def extra(s: str) -> tuple[str, dict | None, str | None]:
        try:
            return s, _FONTES_EXTRAS[s](), None
        except Exception as exc:
            return s, None, f"{s}: {exc}"

    if NO_NAVEGADOR:
        (respostas, falhas), outros = _spark(yahoo, "1d", "5m"), [extra(s) for s in extras]
    else:
        with ThreadPoolExecutor(max_workers=1 + max(1, len(extras))) as pool:
            pedido = pool.submit(_spark, yahoo, "1d", "5m")
            outros = list(pool.map(extra, extras))
            respostas, falhas = pedido.result()
    agora = time.time()
    cot = {}
    for s in yahoo:
        c = ler_cotacao(respostas[s], agora) if s in respostas else None
        if c:
            cot[s] = c
    for s, c, falha in outros:
        if c:
            cot[s] = c
        if falha:
            falhas.append(falha)
    for s in _EM_PONTOS & set(cot):
        cot[s]["unidade"] = "pp"
    if not cot:
        raise FalhaExtracao("; ".join(falhas) or "o Yahoo Finance não devolveu cotações")
    return cot, datetime.now(BRT), falhas


@cache_dados(ttl=3600, show_spinner=False)
def historico_diario(simbolos: tuple[str, ...]) -> pd.DataFrame:
    """Fechamentos diários de um ano, indexados pela data local de cada bolsa.

    A data local importa: a barra diária de um futuro de Nova York e a do Hang
    Seng têm carimbos UTC diferentes, mas pertencem ao mesmo dia.
    """
    respostas, falhas = _spark([s for s in simbolos if s not in _FONTES_EXTRAS], "1y", "1d")
    series = {}
    if "SGX:FEF" in simbolos:
        try:
            minerio = historico_minerio()
            if not minerio.empty:
                series["SGX:FEF"] = minerio
        except Exception as exc:
            falhas.append(f"minério: {exc}")
    for s, resp in respostas.items():
        meta = resp.get("meta") or {}
        ts = pd.to_datetime(pd.Series(resp.get("timestamp") or [], dtype="int64"), unit="s", utc=True)
        try:
            datas = list(ts.dt.tz_convert(meta.get("exchangeTimezoneName") or "UTC").dt.date)
        except Exception:
            # Sem base de fusos no sistema: o deslocamento atual mais 2 h mantém a
            # data certa mesmo para barras de antes de uma troca de horário de verão.
            datas = list((ts + pd.Timedelta(seconds=int(meta.get("gmtoffset") or 0) + 7200)).dt.date)
        fech = _fechamentos(resp)[:len(datas)]
        serie = pd.Series(fech, index=datas[:len(fech)], dtype=float).dropna()
        if not serie.empty:
            series[s] = serie[~serie.index.duplicated(keep="last")]
    if not series:
        raise FalhaExtracao("; ".join(falhas) or "o Yahoo Finance não devolveu o histórico")
    return pd.DataFrame(series).sort_index()


def recorte_intradiario(respostas: dict[str, dict], b3: list[str]) -> tuple[pd.DataFrame, date | None]:
    """Barras de 5 min do último pregão da B3, com os mercados de fora nos mesmos horários.

    O carimbo de cada barra é arredondado para o múltiplo de 5 min (a barra em
    formação vem com a hora do último negócio), e o recorte vai da primeira à
    última barra dos ativos da B3 naquele dia — antes da abertura, o último
    pregão completo.
    """
    series = {}
    for sym, resp in respostas.items():
        dados = {}
        for t, c in zip(resp.get("timestamp") or [], _fechamentos(resp)):
            if c is not None:
                dados[int(t) - int(t) % 300] = float(c)
        if dados:
            series[sym] = pd.Series(dados, dtype=float)
    presentes = [sym for sym in b3 if sym in series]
    if not presentes:
        return pd.DataFrame(), None
    tabela = pd.DataFrame(series).sort_index()
    barras_b3 = tabela[presentes].dropna(how="all")
    dias = pd.Series([datetime.fromtimestamp(int(t), BRT).date() for t in barras_b3.index],
                     index=barras_b3.index)
    dia = dias.max()
    no_dia = barras_b3.index[dias == dia]
    return tabela.loc[no_dia.min():no_dia.max()], dia


@cache_dados(ttl=120, show_spinner=False)
def historico_intradiario(simbolos: tuple[str, ...]) -> tuple[pd.DataFrame, date | None]:
    """Barras de 5 min dos últimos dias (Yahoo), recortadas no último pregão da B3."""
    respostas, falhas = _spark([sym for sym in simbolos if sym not in _FONTES_EXTRAS], "5d", "5m")
    if not respostas:
        raise FalhaExtracao("; ".join(falhas) or "o Yahoo Finance não devolveu barras intradiárias")
    return recorte_intradiario(respostas, [sym for sym in simbolos if sym.endswith(".SA")])


def matriz_correlacao(hist: pd.DataFrame, linhas: list[str], colunas: list[str],
                      janela: int, minimo: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pearson dos retornos diários, par a par, nos últimos `janela` pregões em comum.

    Cada série vira retorno sobre o próprio pregão anterior antes do cruzamento,
    para que o feriado de uma bolsa não apague o retorno do dia seguinte na
    outra. Par com menos da metade da janela em comum fica em branco.
    """
    retornos = {c: hist[c].dropna().pct_change().dropna() for c in hist.columns}
    corr = pd.DataFrame(np.nan, index=linhas, columns=colunas)
    nobs = pd.DataFrame(0, index=linhas, columns=colunas)
    minimo = minimo or max(10, janela // 2)
    for a in linhas:
        for b in colunas:
            if a not in retornos or b not in retornos:
                continue
            par = pd.concat([retornos[a], retornos[b]], axis=1, join="inner").dropna().tail(janela)
            if len(par) >= minimo:
                corr.loc[a, b] = float(par.iloc[:, 0].corr(par.iloc[:, 1]))
                nobs.loc[a, b] = len(par)
    return corr, nobs


# =============================================================================
# SEÇÃO 6 — FORMATAÇÃO pt-BR
# =============================================================================

def _num(valor, casas: int = 2, prefixo: str = "", sufixo: str = "", sinal: bool = False) -> str:
    if valor is None or pd.isna(valor):
        return "—"
    texto = f"{float(valor):,.{casas}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if sinal and float(valor) > 0:
        texto = "+" + texto
    return f"{prefixo}{texto}{sufixo}"


def _moeda(valor) -> str:
    return _num(valor, 2, prefixo="R$ ")


def _moeda_compacta(valor) -> str:
    """R$ 1,2 mi / R$ 340,5 mil — para caber dentro do card."""
    if valor is None or pd.isna(valor):
        return "—"
    v = float(valor)
    for limite, divisor, sufixo in ((1e9, 1e9, " bi"), (1e6, 1e6, " mi"), (1e3, 1e3, " mil")):
        if abs(v) >= limite:
            return _num(v / divisor, 1, prefixo="R$ ", sufixo=sufixo)
    return _num(v, 0, prefixo="R$ ")


def tabela_exibicao(df: pd.DataFrame) -> pd.DataFrame:
    """Monta a tabela final já formatada, com as colunas visíveis pedidas."""
    if df.empty:
        return pd.DataFrame(columns=list(ROTULOS.values()))
    fmt = {
        "ticker": lambda v: str(v),
        "modelo": lambda v: str(v) if v and str(v) != "nan" else "—",
        "strike": _moeda,
        "moneyness": lambda v: str(v) if v and str(v) != "nan" else "—",
        "ultimo": _moeda,
        "variacao": lambda v: _num(v, 2, sufixo="%", sinal=True),
        "num_neg": lambda v: _num(v, 0),
        "vol_financeiro": _moeda_compacta,
        "vol_impl": lambda v: _num(v, 1, sufixo="%"),
        "delta": lambda v: _num(v, 3, sinal=True),
    }
    saida = pd.DataFrame(index=df.index)
    for campo in COLUNAS_EXIBICAO:
        serie = df[campo] if campo in df.columns else pd.Series([np.nan] * len(df), index=df.index)
        saida[ROTULOS[campo]] = serie.map(fmt[campo])
    return saida


# =============================================================================
# SEÇÃO 7 — INTERFACE
# =============================================================================
# Direção visual (skill ui-ux-pro-max): liquid glass sobre base escura e densa.
# O conteúdo fica em vidro fosco escuro, que mantém o contraste dos números; o
# vidro claro e brilhante fica para a navegação e os controles, como pede o
# material (abas, seletores, botões, selos). Inter na interface e Fira Code nos
# códigos de negociação. Sem transparência quando o sistema pede menos
# transparência ou o navegador não tem backdrop-filter. A cor fica reservada para
# os dados (skill dataviz): call e put são identidade, não bom/ruim, e usam o par
# categórico validado — azul #3987e5 e laranja #d95926, ΔE CVD 26,8 sobre a
# superfície dos cards. Verde e vermelho aparecem só em variação com sinal,
# sempre com seta. O cromo da interface é monocromático.

_FONTES = ("https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600"
           "&family=Inter:wght@400;500;600;700&display=swap")

_CSS = """
<style>
@import url('__FONTES__');
:root{
  --bg:#05070C; --s1:#0B0F1A; --s2:#10151F; --s3:#171D2C;
  --ln:#1C2333; --ln2:#29324A;
  --t1:#F1F5F9; --t2:#A9B4C8; --t3:#8B97AD;
  --call:#3987e5; --put:#d95926;
  --up:#22C55E; --down:#F05252; --gray-mark:#2B3552; --seq:#64748B;
  --c-pos:#3987e5; --c-neg:#e66767; --c-mid:#1E2740;
  --sans:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:'Fira Code',ui-monospace,Consolas,monospace;
  --r:18px;
  --solido:#111829;   /* vidro sem transparência: células fixas e fallback */
  --vidro:linear-gradient(180deg,rgba(26,33,57,.66) 0%,rgba(13,17,32,.58) 100%);
  --vidro-claro:linear-gradient(180deg,rgba(255,255,255,.13) 0%,rgba(255,255,255,.045) 100%);
}
[data-testid="stAppViewContainer"],[data-testid="stMain"]{background:transparent!important;}
[data-testid="stHeader"]{background:transparent;}
[data-testid="stDecoration"]{display:none;}
.block-container{padding:1rem 2rem 3rem!important;max-width:100%!important;}
[data-testid="stWidgetLabel"] p{font:600 11px/1.2 var(--sans)!important;letter-spacing:.08em;
  text-transform:uppercase;color:var(--t3)!important;}
[data-testid="stCheckbox"] [data-testid="stWidgetLabel"] p,[data-testid="stToggle"] [data-testid="stWidgetLabel"] p,
[data-testid="stCheckbox"] label p{font:400 13px/1.4 var(--sans)!important;letter-spacing:0;
  text-transform:none;color:var(--t2)!important;}
section[data-testid="stSidebar"] .sb{font:600 11px/1 var(--sans);letter-spacing:.1em;
  text-transform:uppercase;color:var(--t2);margin:20px 0 8px;padding-top:14px;border-top:1px solid var(--ln);}
section[data-testid="stSidebar"] .sb.first{border-top:none;padding-top:0;margin-top:4px;}

.dx,.dx *{box-sizing:border-box;}
.dx{font-family:var(--sans);color:var(--t1);-webkit-font-smoothing:antialiased;container-type:inline-size;}
.dx .mono{font-family:var(--mono);font-variant-ligatures:none;}
.dx svg.ic{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8;
  stroke-linecap:round;stroke-linejoin:round;flex:none;}

/* cabeçalho */
.hdr{display:flex;align-items:center;justify-content:space-between;gap:14px 24px;flex-wrap:wrap;
  padding:2px 0 8px;margin-bottom:0;}
.brand{display:flex;align-items:center;gap:12px;}
.brand .mark{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;color:var(--t1);
  background:linear-gradient(150deg,#1B2335 0%,#0C111D 100%);border:1px solid var(--ln2);}
.brand .mark svg.ic{width:18px;height:18px;}
.brand .t{font:600 17px/1.2 var(--sans);letter-spacing:-.01em;}
.brand .s{font:400 12.5px/1.3 var(--sans);color:var(--t3);margin-top:2px;}
.status{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.pill{display:inline-flex;align-items:center;gap:7px;padding:7px 11px;border:1px solid var(--ln);
  border-radius:999px;background:var(--s1);font:400 12px/1 var(--sans);color:var(--t2);white-space:nowrap;}
.pill b{color:var(--t1);font-weight:500;}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--up);box-shadow:0 0 0 3px rgba(34,197,94,.15);}
.pill .dot.off{background:var(--t3);box-shadow:none;}
.pill.warn{border-color:rgba(240,82,82,.35);}

/* KPIs */
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0 14px;}
.kpi{background:var(--s1);border:1px solid var(--ln);border-radius:var(--r);padding:14px 16px 13px;min-width:0;}
.kpi .l{display:flex;align-items:center;gap:7px;font:500 11px/1 var(--sans);letter-spacing:.08em;
  text-transform:uppercase;color:var(--t3);}
.kpi .v{font:600 23px/1.15 var(--sans);letter-spacing:-.015em;margin-top:10px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;}
.kpi .d{font:400 12.5px/1.35 var(--sans);color:var(--t2);margin-top:4px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;}

/* quadro principal */
.board{display:grid;gap:14px;grid-template-columns:repeat(2,minmax(0,1fr));
  grid-template-areas:"call put" "chart chart" "tabs tabs" "funil funil";}
.a-call{grid-area:call}.a-put{grid-area:put}.a-chart{grid-area:chart}.a-funil{grid-area:funil}
.a-tabs{grid-area:tabs;display:grid;gap:14px;grid-template-columns:minmax(0,1fr);}
.board>div>.card{height:100%;}
@container (min-width:1700px){
  .board{grid-template-columns:minmax(0,1fr) minmax(0,1.3fr) minmax(0,1fr);
    grid-template-areas:"call chart put" "tabs tabs tabs" "funil funil funil";}
  .a-tabs{grid-template-columns:repeat(2,minmax(0,1fr));}
}
@container (max-width:1050px){
  .kpis{grid-template-columns:repeat(3,minmax(0,1fr));}
  .kpis .kpi:last-child{grid-column:span 2;}
}
@container (max-width:860px){
  .board{grid-template-columns:minmax(0,1fr);
    grid-template-areas:"call" "put" "chart" "tabs" "funil";}
}
@container (max-width:560px){
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr));}
  .kpis .kpi:last-child{grid-column:span 2;}
  .kpi .v{font-size:18px;white-space:normal;}
  .kpi .d{white-space:normal;}
}
.card{background:var(--s1);border:1px solid var(--ln);border-radius:var(--r);min-width:0;overflow:hidden;}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 18px;
  border-bottom:1px solid var(--ln);}
.card-h .t{display:flex;align-items:center;gap:9px;font:600 13.5px/1.2 var(--sans);}
.card-h .d{font:400 12px/1.3 var(--sans);color:var(--t3);text-align:right;}

/* sinal */
.sig{position:relative;}
.sig::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;}
.sig.call::before{background:var(--call);} .sig.put::before{background:var(--put);}
.sig-top{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:16px 20px 0 22px;}
.side{display:inline-flex;align-items:center;gap:8px;font:600 11px/1 var(--sans);letter-spacing:.12em;
  text-transform:uppercase;color:var(--t2);}
.side i{width:9px;height:9px;border-radius:2px;display:inline-block;}
.call .side i{background:var(--call);} .put .side i{background:var(--put);}
.chips{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;}
.chip{font:500 11px/1 var(--sans);padding:5px 8px;border-radius:6px;border:1px solid var(--ln);
  background:var(--s2);color:var(--t2);white-space:nowrap;}
.chip.strong{color:var(--t1);border-color:var(--ln2);}
.sig-main{padding:12px 20px 0 22px;}
.sig-tick{font:600 27px/1.1 var(--mono);letter-spacing:.01em;font-variant-ligatures:none;}
.sig-rank{font:400 12.5px/1.4 var(--sans);color:var(--t3);margin-top:5px;}
.sig-price{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 12px;margin-top:16px;}
.sig-price .p{font:600 36px/1 var(--sans);letter-spacing:-.025em;}
.sig-price .p small{font-size:15px;font-weight:500;color:var(--t2);margin-right:5px;letter-spacing:0;}
.chg{display:inline-flex;align-items:center;gap:3px;font:600 13px/1 var(--sans);}
.chg svg.ic{width:14px;height:14px;stroke-width:2.2;}
.chg.up{color:var(--up);} .chg.down{color:var(--down);} .chg.na{color:var(--t3);font-weight:400;}
.sig-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin-top:18px;border-top:1px solid var(--ln);}
.sig-grid>div{padding:12px 18px 13px 22px;border-right:1px solid var(--ln);border-bottom:1px solid var(--ln);min-width:0;}
.sig-grid>div:nth-child(3n){border-right:none;}
.sig-grid>div:nth-last-child(-n+3){border-bottom:none;}
.sig-grid span{display:block;font:500 10.5px/1 var(--sans);letter-spacing:.08em;text-transform:uppercase;color:var(--t3);}
.sig-grid b{display:block;font:600 16px/1.2 var(--sans);margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.meter{padding:13px 20px 17px 22px;border-top:1px solid var(--ln);}
.meter .row{display:flex;justify-content:space-between;gap:10px;font:400 12px/1.2 var(--sans);color:var(--t3);}
.meter .row b{color:var(--t1);font-weight:600;}
.track{position:relative;height:6px;border-radius:999px;background:var(--s3);margin:11px 0 7px;}
.band{position:absolute;top:0;bottom:0;border-radius:999px;background:rgba(226,232,240,.18);}
.pin{position:absolute;top:50%;width:13px;height:13px;border-radius:50%;transform:translate(-50%,-50%);
  border:2px solid var(--s1);}
.call .pin{background:var(--call);} .put .pin{background:var(--put);}
.ticks{position:relative;height:12px;font:400 10.5px/1 var(--sans);color:var(--t3);}
.ticks span{position:absolute;transform:translateX(-50%);}
.sig.empty .sig-main{padding-bottom:26px;}
.sig.empty .msg{font:400 13.5px/1.55 var(--sans);color:var(--t2);margin-top:10px;max-width:46ch;}

/* gráfico */
.legend{display:flex;gap:6px 16px;flex-wrap:wrap;justify-content:flex-end;font:400 12px/1.2 var(--sans);color:var(--t2);}
.legend span{display:inline-flex;align-items:center;gap:6px;}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block;}
.legend i.rule{width:2px;height:12px;border-radius:0;background:var(--t2);}
.chart-wrap{overflow-x:auto;padding:10px 14px 8px 6px;}
.chart{display:flex;min-width:640px;}
.chart .yax{flex:0 0 64px;} .chart .plot{flex:1 1 auto;min-width:0;}
.chart svg{display:block;overflow:visible;}
.chart text{font-family:var(--sans);font-size:11px;fill:var(--t3);}
.chart text.side-l{font-size:10px;letter-spacing:.1em;text-transform:uppercase;fill:var(--t2);font-weight:600;}
.chart text.pk{font-family:var(--mono);font-size:11px;font-weight:600;fill:var(--t1);}
.chart text.sp{fill:var(--t2);font-size:11px;}
.chart .grid{stroke:var(--ln);stroke-width:1;}
.chart .base{stroke:var(--ln2);stroke-width:1;}
.chart .spot{stroke:var(--t2);stroke-width:1;}
.chart .bar{transition:opacity .15s ease;}
.chart svg.liq:hover .bar{opacity:.38;} .chart svg.liq .bar:hover{opacity:1;}
@container (max-width:980px){ .chart text.xl.alt{display:none;} }
.chart-foot{padding:0 18px 14px;font:400 12px/1.5 var(--sans);color:var(--t3);}

/* ranking */
.tbl-wrap{overflow-x:auto;}
.dx table.rk{width:100%;border-collapse:collapse;font:400 13px/1.2 var(--sans);}
.dx .rk th{font:500 10.5px/1.2 var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--t3);
  text-align:right;padding:11px 14px;border-bottom:1px solid var(--ln);white-space:nowrap;background:var(--s1);}
.dx .rk th:first-child,.dx .rk td:first-child{text-align:left;position:sticky;left:0;background:var(--s1);z-index:1;}
.dx .rk td{padding:12px 14px;text-align:right;border-bottom:1px solid var(--ln);white-space:nowrap;
  font-variant-numeric:tabular-nums;color:var(--t1);}
.dx .rk tr:last-child td{border-bottom:none;}
.dx .rk tbody tr:hover td{background:var(--s2);}
.dx .rk td.tk{font-family:var(--mono);font-weight:600;font-variant-ligatures:none;}
.dx .rk td.mut{color:var(--t2);}
.dx .rk .rank{display:inline-grid;place-items:center;width:19px;height:19px;border-radius:5px;margin-right:10px;
  background:var(--s3);color:var(--t2);font:600 10.5px/1 var(--sans);vertical-align:1px;}
.dx .call .rk tr.top .rank{background:var(--call);color:#05070C;} .dx .put .rk tr.top .rank{background:var(--put);color:#05070C;}
.vb{display:inline-flex;align-items:center;justify-content:flex-end;gap:9px;}
.vb i{display:block;height:4px;border-radius:2px;min-width:3px;}
.call .vb i{background:var(--call);} .put .vb i{background:var(--put);}
.dx .rk .chg{font-weight:500;}

/* funil */
.funil{display:flex;overflow-x:auto;padding:16px 8px 18px;}
.fs{flex:1 1 0;min-width:136px;padding:0 16px;border-left:1px solid var(--ln);}
.fs:first-child{border-left:none;}
.fs span{display:block;font:400 12px/1.35 var(--sans);color:var(--t3);min-height:2.7em;}
.fs b{display:block;font:600 19px/1 var(--sans);margin:7px 0 10px;}
.fs .fb{height:3px;border-radius:2px;background:var(--s3);}
.fs .fb i{display:block;height:100%;border-radius:2px;background:var(--seq);}
.note{display:flex;gap:10px;align-items:flex-start;font:400 12.5px/1.6 var(--sans);color:var(--t3);
  padding:13px 18px;border-top:1px solid var(--ln);}
.note svg.ic{margin-top:3px;color:var(--t2);}
.note b{color:var(--t2);font-weight:500;}

/* estados vazios e rodapé */
.empty-st{background:var(--s1);border:1px solid var(--ln);border-radius:var(--r);padding:28px 30px;margin-top:14px;}
.empty-st .t{display:flex;align-items:center;gap:10px;font:600 16px/1.3 var(--sans);}
.empty-st .t svg.ic{width:18px;height:18px;color:var(--t2);}
.dx .empty-st p{font:400 13.5px/1.6 var(--sans);color:var(--t2);margin:10px 0 0;max-width:72ch;}
.dx .empty-st ul{margin:12px 0 0;padding-left:18px;color:var(--t2);font:400 13px/1.7 var(--sans);}
.foot{display:flex;justify-content:space-between;gap:10px 24px;flex-wrap:wrap;margin-top:26px;padding-top:14px;
  border-top:1px solid var(--ln);font:400 12px/1.6 var(--sans);color:var(--t3);}
/* abas */
[data-testid="stTabs"] [role="tablist"]{gap:4px;}
[data-testid="stTabs"] [role="tab"]{height:auto;padding:9px 14px 11px;}
[data-testid="stTabs"] [role="tab"] p{font:600 13.5px/1 var(--sans)!important;color:var(--t3);transition:color .15s ease;}
[data-testid="stTabs"] [role="tab"]:hover p{color:var(--t2);}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] p{color:var(--t1);}
[data-testid="stTabs"] [data-baseweb="tab-highlight"]{background:var(--t1);height:2px;}
[data-testid="stTabs"] [data-baseweb="tab-border"]{background:var(--ln);}

/* tela estreita (aba Opções) */
.card-h{flex-wrap:wrap;}
.card-h .t{white-space:nowrap;}
@container (max-width:560px){
  .sig-grid{grid-template-columns:repeat(2,minmax(0,1fr));}
  .sig-grid>div{padding:11px 14px 12px 18px;}
  .sig-grid>div:nth-child(3n){border-right:1px solid var(--ln);}
  .sig-grid>div:nth-child(2n){border-right:none;}
  .sig-grid>div:nth-last-child(-n+3){border-bottom:1px solid var(--ln);}
  .sig-grid>div:nth-last-child(-n+2){border-bottom:none;}
  .legend{justify-content:flex-start;}
  .card-h .d{text-align:left;}
}

/* aba Correlações */
.stline{display:flex;align-items:center;gap:7px;min-height:40px;font:400 12.5px/1.3 var(--sans);color:var(--t3);}
.stline b{color:var(--t1);font-weight:500;}
.pulse{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:10px;margin:6px 0 14px;}
@container (max-width:1600px){ .pulse{grid-template-columns:repeat(4,minmax(0,1fr));} }
@container (max-width:560px){
  .pulse{grid-template-columns:repeat(2,minmax(0,1fr));}
  .pz .l .sub{display:none;}
  .pz .d{flex-wrap:wrap;gap:2px 8px;}
}
.pz{padding-bottom:8px;}
.pz .l{color:var(--t2);gap:5px;}
.pz .l .sub{font-weight:400;letter-spacing:0;text-transform:none;color:var(--t3);white-space:nowrap;}
.pz .l .sub::before{content:"· ";}
.pz .d{display:flex;align-items:center;gap:8px;}
.pz .d .q{color:var(--t3);font-size:12px;overflow:hidden;text-overflow:ellipsis;}
.spk{display:block;width:100%;height:34px;margin-top:10px;overflow:visible;}
.spk polyline{fill:none;stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke;}
.spk polyline.up{stroke:var(--up);} .spk polyline.down{stroke:var(--down);} .spk polyline.flat{stroke:var(--t3);}
.spk line{stroke:var(--t3);stroke-width:1;stroke-dasharray:2 3;opacity:.7;vector-effect:non-scaling-stroke;}
.spk-na{color:var(--t3);}
.spk-vazio{height:34px;margin-top:10px;display:flex;align-items:flex-end;font:400 11px/1.3 var(--sans);
  color:var(--t3);}
.fase{display:inline-block;font:600 9.5px/1 var(--sans);letter-spacing:.07em;text-transform:uppercase;
  padding:3px 5px;border-radius:4px;border:1px solid var(--ln2);color:var(--t2);margin-right:6px;vertical-align:1px;}
.sec{display:flex;justify-content:space-between;align-items:baseline;gap:6px 16px;flex-wrap:wrap;margin:24px 2px 10px;}
.sec .t{font:600 14px/1.2 var(--sans);color:var(--t1);}
.sec .d{font:400 12px/1.4 var(--sans);color:var(--t3);}
.mk-grid{display:grid;gap:14px;grid-template-columns:repeat(2,minmax(0,1fr));}
@container (min-width:1700px){ .mk-grid{grid-template-columns:repeat(3,minmax(0,1fr));} }
@container (max-width:860px){ .mk-grid{grid-template-columns:minmax(0,1fr);} }
.mk{container-type:inline-size;}
.dx table.qt td{padding:9px 14px;}
.dx .qt td.nm{min-width:150px;}
.qt .nm b{display:block;font:500 13px/1.25 var(--sans);color:var(--t1);}
.qt .nm small{display:block;font:400 11px/1.3 var(--sans);color:var(--t3);margin-top:2px;}
.qt .nm small .mono{font-size:10.5px;}
.dx .qt td.hr{color:var(--t2);font-size:12px;}
.dx .qt td.hr.old{color:var(--t3);}
.qt .spk{display:inline-block;width:92px;height:24px;margin:0;vertical-align:middle;}
.dv{position:relative;display:inline-block;width:44px;height:6px;margin-right:10px;vertical-align:middle;}
.dv::before{content:"";position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--ln2);}
.dv i{position:absolute;top:1px;height:4px;border-radius:2px;}
.dv i.up{background:var(--up);} .dv i.down{background:var(--down);}
@container (max-width:520px){
  .qt .c-sp{display:none;} .dv{display:none;}
  .dx table.qt th,.dx table.qt td{padding-left:9px;padding-right:9px;}
  .dx .qt td.nm{min-width:0;white-space:normal;}
}
.hm-wrap{overflow-x:auto;padding:8px 10px 0;}
.dx table.hm{width:100%;border-collapse:separate;border-spacing:3px;font:400 12.5px/1 var(--sans);}
.dx .hm th{font:500 10.5px/1.25 var(--sans);letter-spacing:.05em;text-transform:uppercase;color:var(--t3);
  padding:6px 4px 8px;text-align:center;white-space:nowrap;vertical-align:bottom;border:none;background:none;}
.dx .hm th.lt,.dx .hm td.lt{text-align:left;padding-left:16px;width:220px;}
.dx .hm td{border:none;}
.dx .hm td.ak{font:600 13px/1 var(--mono);font-variant-ligatures:none;color:var(--t1);padding:0 12px 0 6px;
  white-space:nowrap;position:sticky;left:0;background:var(--s1);z-index:1;}
.dx .hm td.c{min-width:60px;height:36px;padding:0 6px;text-align:center;border-radius:5px;color:var(--t1);
  font-weight:600;font-variant-numeric:tabular-nums;}
.dx .hm td.c.fraco{color:var(--t2);font-weight:500;}
.dx .hm td.c.na{color:var(--t3);background:var(--s2);font-weight:400;}
.dx .hm td.lt{white-space:nowrap;font-size:12.5px;color:var(--t2);}
.hm .mv{display:flex;align-items:center;gap:8px;line-height:1.5;}
.hm .mv b{color:var(--t1);font-weight:500;}
.hm .mv em{font-style:normal;color:var(--t3);font-variant-numeric:tabular-nums;}
.hm-leg{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;padding:12px 18px 14px;
  font:400 11.5px/1 var(--sans);color:var(--t3);}
.hm-leg .grad{width:160px;height:8px;border-radius:4px;
  background:linear-gradient(90deg,var(--c-neg),var(--c-mid) 50%,var(--c-pos));}
.ler{padding:12px 18px 16px;border-top:1px solid var(--ln);font:400 12.5px/1.6 var(--sans);color:var(--t3);}
.ler-t{display:flex;align-items:center;gap:8px;font:600 11px/1 var(--sans);letter-spacing:.08em;
  text-transform:uppercase;color:var(--t2);margin-bottom:8px;}
.dx .ler ul{margin:0 0 8px;padding-left:18px;}
.dx .ler li{margin:2px 0;}
.ler b{color:var(--t2);font-weight:500;}
.dx .ler p{margin:0;max-width:140ch;}
.lei{display:flex;align-items:center;gap:4px;margin-top:5px;font:600 11.5px/1.2 var(--sans);}
.lei svg.ic{width:13px;height:13px;stroke-width:2.2;}
.lei.up{color:var(--up);} .lei.down{color:var(--down);} .lei.na{color:var(--t3);font-weight:400;}
.guia{display:flex;gap:10px;align-items:flex-start;padding:11px 16px;margin:0 0 12px;border:1px solid var(--ln);
  border-radius:var(--r);background:var(--s1);font:400 12.5px/1.6 var(--sans);color:var(--t3);}
.guia svg.ic{margin-top:3px;color:var(--t2);}
.guia b{color:var(--t2);font-weight:500;}
.ativos{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:14px;}
@container (max-width:1100px){ .ativos{grid-template-columns:repeat(2,minmax(0,1fr));} }
@container (max-width:680px){ .ativos{grid-template-columns:minmax(0,1fr);} }
.at{padding:16px 18px 12px;}
.at-h{display:flex;align-items:center;justify-content:space-between;gap:10px;}
.at-tk{font:600 18px/1 var(--mono);font-variant-ligatures:none;color:var(--t1);}
.at-h .lei{margin-top:0;padding:6px 9px;border-radius:6px;background:var(--s2);border:1px solid var(--ln);}
.dx .at-s{margin:10px 0 12px;font:400 13px/1.5 var(--sans);color:var(--t2);min-height:3em;}
.at-s b{color:var(--t1);font-weight:500;}
.drv{border-top:1px solid var(--ln);}
.drv-r{display:grid;grid-template-columns:minmax(0,1fr) auto auto;grid-template-areas:"n v e" "b b b";
  gap:6px 14px;align-items:center;padding:10px 0;border-bottom:1px solid var(--ln);}
.drv-r:last-child{border-bottom:none;padding-bottom:4px;}
.drv-n{grid-area:n;min-width:0;}
.drv-n b{display:block;font:500 13px/1.3 var(--sans);color:var(--t1);}
.drv-n span{display:block;font:400 11.5px/1.35 var(--sans);color:var(--t3);}
.drv-n em{font-style:normal;font-variant-numeric:tabular-nums;margin-left:6px;opacity:.8;}
.drv-v{grid-area:v;text-align:right;white-space:nowrap;}
.drv-e{grid-area:e;text-align:right;min-width:112px;}
.drv-b{grid-area:b;height:3px;border-radius:2px;background:var(--s3);}
.drv-b i{display:block;height:100%;border-radius:2px;background:var(--seq);}
.efe{display:inline-flex;align-items:center;gap:3px;font:600 11.5px/1 var(--sans);white-space:nowrap;}
.efe svg.ic{width:13px;height:13px;stroke-width:2.2;}
.efe.up{color:var(--up);} .efe.down{color:var(--down);} .efe.na{color:var(--t3);font-weight:400;}

/* aba Operações */
.ops{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:6px 0 14px;}
@container (max-width:1100px){ .ops{grid-template-columns:repeat(2,minmax(0,1fr));} }
@container (max-width:680px){ .ops{grid-template-columns:minmax(0,1fr);} }
.opc{padding:16px 18px 12px;}
.opc-h{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;}
.opc-e{display:block;font:400 12px/1.3 var(--sans);color:var(--t3);margin-top:6px;}
.sts{display:inline-flex;align-items:center;gap:5px;padding:6px 10px;border-radius:999px;white-space:nowrap;
  font:600 12px/1 var(--sans);border:1px solid var(--ln2);background:rgba(255,255,255,.05);color:var(--t2);}
.sts svg.ic{width:13px;height:13px;stroke-width:2.2;}
.sts.compra{color:var(--up);border-color:rgba(34,197,94,.35);}
.sts.venda{color:var(--down);border-color:rgba(240,82,82,.35);}
.opc-res{margin:14px 0 6px;}
.opc-res .v{font:600 30px/1.05 var(--sans);letter-spacing:-.02em;color:var(--t1);}
.opc-res .v.up{color:var(--up);} .opc-res .v.down{color:var(--down);}
.opc-res .d{font:400 12.5px/1.4 var(--sans);color:var(--t2);margin-top:5px;}
.regua{position:relative;height:52px;margin:4px 6px 8px;}
.regua .trilho{position:absolute;left:0;right:0;top:22px;height:4px;border-radius:2px;
  background:linear-gradient(90deg,rgba(240,82,82,.55),rgba(255,255,255,.12) 45%,rgba(34,197,94,.55));}
.regua .m{position:absolute;top:17px;width:2px;height:14px;border-radius:1px;background:var(--t2);transform:translateX(-50%);}
.regua .m span{position:absolute;left:50%;transform:translateX(-50%);white-space:nowrap;
  font:400 10.5px/1 var(--sans);color:var(--t3);top:19px;}
.regua .m.cima span{top:-15px;}
.regua .ag{position:absolute;top:18px;width:12px;height:12px;border-radius:50%;transform:translateX(-50%);
  border:2px solid var(--solido);background:var(--t1);box-shadow:0 0 0 3px rgba(255,255,255,.14);}
.regua .ag.up{background:var(--up);} .regua .ag.down{background:var(--down);}
.opc-g{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid var(--ln);margin-top:4px;}
.opc-g>div{padding:10px 8px 10px 0;border-bottom:1px solid var(--ln);min-width:0;}
.opc-g>div:nth-last-child(-n+3){border-bottom:none;}
.opc-g span{display:block;font:500 10.5px/1 var(--sans);letter-spacing:.07em;text-transform:uppercase;color:var(--t3);}
.opc-g b{display:block;font:600 14.5px/1.2 var(--sans);margin-top:6px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;font-variant-numeric:tabular-nums;}
.opc-g em{display:block;font:400 11.5px/1.3 var(--sans);font-style:normal;color:var(--t3);margin-top:3px;}
.opc-g em.ok{color:var(--up);}
.dx .opc-vazio{margin:14px 0 12px;font:400 13px/1.55 var(--sans);color:var(--t2);}
.opc.pendente .at-tk{color:var(--t2);}
.opc-t{display:block;font:600 15px/1.2 var(--sans);color:var(--t1);}
.chips-l{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 4px;}
.dx .chips-l .chip{font:600 12.5px/1 var(--mono);font-variant-ligatures:none;padding:7px 10px;color:var(--t1);}
.st-key-grafico_op{background:var(--vidro);border:1px solid rgba(255,255,255,.075);border-radius:var(--r);
  padding:12px 10px 4px;box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 24px 48px -28px rgba(0,0,0,.85);
  -webkit-backdrop-filter:blur(24px) saturate(165%);backdrop-filter:blur(24px) saturate(165%);}
/* ================= liquid glass ================= */
/* fundo: luz difusa fixa atrás do conteúdo, com grão bem leve */
[data-testid="stApp"]{background-color:#060913!important;background-image:
  url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='g'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 .55 0'/></filter><rect width='100%' height='100%' filter='url(%23g)' opacity='.05'/></svg>"),
  radial-gradient(58vw 42vw at 8% -6%,rgba(59,110,230,.34),transparent 62%),
  radial-gradient(46vw 38vw at 96% 2%,rgba(128,90,245,.24),transparent 62%),
  radial-gradient(44vw 36vw at 52% 48%,rgba(67,97,238,.10),transparent 64%),
  radial-gradient(52vw 44vw at 72% 108%,rgba(20,184,166,.17),transparent 62%),
  radial-gradient(38vw 34vw at -4% 88%,rgba(56,189,248,.12),transparent 62%)!important;}

/* conteúdo: vidro fosco escuro */
.dx .card,.dx .kpi,.dx .empty-st,.dx .guia{position:relative;background:var(--vidro);
  border:1px solid rgba(255,255,255,.075);border-radius:var(--r);
  -webkit-backdrop-filter:blur(24px) saturate(165%);backdrop-filter:blur(24px) saturate(165%);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.09),0 24px 48px -28px rgba(0,0,0,.85);}
/* luz de borda: mais forte no canto de cima, como vidro curvo pegando luz */
.dx .card::after,.dx .kpi::after,.dx .empty-st::after,.dx .guia::after{content:"";position:absolute;inset:-1px;
  border-radius:inherit;padding:1px;pointer-events:none;opacity:.85;transition:opacity .25s ease;
  background:linear-gradient(140deg,rgba(255,255,255,.34),rgba(255,255,255,.07) 26%,rgba(255,255,255,0) 55%,
    rgba(255,255,255,.12) 100%);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;
  mask:linear-gradient(#000 0 0) content-box exclude,linear-gradient(#000 0 0);}
.dx .card:hover::after,.dx .kpi:hover::after{opacity:1;}
.dx .rk th{background:transparent;}
.dx .rk th:first-child,.dx .rk td:first-child,.dx .hm td.ak{background:var(--solido);}
.dx .rk tbody tr:hover td{background:rgba(255,255,255,.045);}
.dx .hm td.c{border-radius:8px;}
.card-h{border-bottom-color:rgba(255,255,255,.07);}

/* marca: gota de vidro */
.brand .mark{border-radius:12px;border:1px solid rgba(255,255,255,.18);color:#fff;
  background:radial-gradient(120% 120% at 28% 18%,rgba(255,255,255,.42),rgba(255,255,255,.07) 46%,
    rgba(59,110,230,.32) 100%);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.45),0 8px 22px -8px rgba(59,110,230,.65);}

/* navegação e controles: vidro claro, com brilho no topo */
.dx .pill{background:var(--vidro-claro);border:1px solid rgba(255,255,255,.12);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.24),0 6px 18px -10px rgba(0,0,0,.8);
  -webkit-backdrop-filter:blur(16px) saturate(170%);backdrop-filter:blur(16px) saturate(170%);}
.dx .chip{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.11);}
.dx .chip.strong{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.22);}
[data-testid="stTabs"] [role="tablist"]{width:fit-content;gap:4px;padding:4px;border-radius:999px;
  background:var(--vidro-claro);border:1px solid rgba(255,255,255,.11);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.2),0 12px 28px -16px rgba(0,0,0,.9);
  -webkit-backdrop-filter:blur(18px) saturate(180%);backdrop-filter:blur(18px) saturate(180%);}
[data-testid="stTabs"] [role="tab"]{border-radius:999px;padding:8px 18px;margin:0;}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{
  background:linear-gradient(180deg,rgba(255,255,255,.22),rgba(255,255,255,.08));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.48),inset 0 -1px 0 rgba(255,255,255,.06),0 4px 14px -6px rgba(0,0,0,.75);}
[data-testid="stTabs"] [data-baseweb="tab-highlight"],[data-testid="stTabs"] [data-baseweb="tab-border"]{display:none;}
[data-testid="stBaseButton-segmented_control"],[data-testid="stBaseButton-segmented_controlActive"]{
  border-radius:12px!important;margin-right:6px;transition:background .15s ease,color .15s ease;}
[data-testid="stBaseButton-segmented_control"]{background:rgba(255,255,255,.04)!important;
  border:1px solid rgba(255,255,255,.09)!important;color:var(--t2)!important;
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);}
[data-testid="stBaseButton-segmented_control"]:hover{background:rgba(255,255,255,.08)!important;color:var(--t1)!important;}
[data-testid="stBaseButton-segmented_controlActive"]{
  background:linear-gradient(180deg,rgba(255,255,255,.24),rgba(255,255,255,.09))!important;
  border:1px solid rgba(255,255,255,.24)!important;color:#fff!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.5),0 8px 18px -10px rgba(0,0,0,.85)!important;}
[data-testid="stBaseButton-secondary"]{border-radius:12px!important;background:var(--vidro-claro)!important;
  border:1px solid rgba(255,255,255,.14)!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.3),0 10px 22px -12px rgba(0,0,0,.85);
  -webkit-backdrop-filter:blur(16px) saturate(170%);backdrop-filter:blur(16px) saturate(170%);}
[data-testid="stBaseButton-secondary"]:hover{
  background:linear-gradient(180deg,rgba(255,255,255,.2),rgba(255,255,255,.07))!important;
  border-color:rgba(255,255,255,.22)!important;}
[data-testid="stExpander"] details{border-radius:var(--r)!important;border:1px solid rgba(255,255,255,.08)!important;
  background:var(--vidro);box-shadow:inset 0 1px 0 rgba(255,255,255,.08);
  -webkit-backdrop-filter:blur(22px) saturate(160%);backdrop-filter:blur(22px) saturate(160%);}
[data-testid="stExpander"] summary:hover{background:rgba(255,255,255,.035);}
[data-testid="stSidebar"]{background:linear-gradient(180deg,rgba(15,20,38,.74),rgba(8,11,22,.68))!important;
  border-right:1px solid rgba(255,255,255,.08)!important;
  -webkit-backdrop-filter:blur(28px) saturate(160%);backdrop-filter:blur(28px) saturate(160%);}
[data-testid="stSidebarContent"],[data-testid="stSidebarUserContent"]{background:transparent!important;}
[data-baseweb="input"],[data-testid="stNumberInputContainer"]{border-radius:12px!important;
  background:rgba(255,255,255,.045)!important;border-color:rgba(255,255,255,.1)!important;}
[data-baseweb="input"] input,[data-testid="stNumberInputContainer"] input{background:transparent!important;}

/* menos transparência pedida pelo sistema, ou navegador sem backdrop-filter: tudo opaco */
@media (prefers-reduced-transparency:reduce){
  .dx .card,.dx .kpi,.dx .empty-st,.dx .guia,.dx .pill,[data-testid="stSidebar"],
  [data-testid="stExpander"] details,[data-testid="stTabs"] [role="tablist"],
  [data-testid="stBaseButton-secondary"]{background:var(--solido)!important;
    -webkit-backdrop-filter:none!important;backdrop-filter:none!important;}
}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
  .dx .card,.dx .kpi,.dx .empty-st,.dx .guia,.dx .pill,[data-testid="stSidebar"],
  [data-testid="stExpander"] details,[data-testid="stTabs"] [role="tablist"]{background:var(--solido)!important;}
}
@media (prefers-reduced-motion:reduce){ .dx .card::after,.dx .kpi::after{transition:none;} }
@media (prefers-reduced-motion:reduce){ .chart .bar{transition:none;} }
</style>
""".replace("__FONTES__", _FONTES)

# Ícones em SVG, no traço do Lucide — nada de emoji como ícone.
_ICONES = {
    "marca": '<path d="M9 5v4"/><rect x="7" y="9" width="4" height="6" rx="1"/><path d="M9 15v4"/>'
             '<path d="M16 3v3"/><rect x="14" y="6" width="4" height="8" rx="1"/><path d="M16 14v5"/>',
    "ativo": '<polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/>',
    "calendario": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/>'
                  '<path d="M3 10h18"/>',
    "vol": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "camadas": '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/>'
               '<polyline points="2 12 12 17 22 12"/>',
    "relogio": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "filtro": '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
    "barras": '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    "fonte": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/>'
             '<path d="M3 12a9 3 0 0 0 18 0"/>',
    "alerta": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
              '<path d="M12 9v4"/><path d="M12 17h.01"/>',
    "lista": '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/>'
             '<path d="M3 12h.01"/><path d="M3 18h.01"/>',
    "balanca": '<path d="M12 3v18"/><path d="M7 21h10"/><path d="M3 7h2c2 0 5-1 7-2 2 1 5 2 7 2h2"/>'
               '<path d="m2 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/>'
               '<path d="m16 16 3-8 3 8c-.87.65-1.92 1-3 1s-2.13-.35-3-1Z"/>',
    "globo": '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>'
             '<path d="M2 12h20"/>',
    "predio": '<path d="M3 22h18"/><path d="M6 18v-7"/><path d="M10 18v-7"/><path d="M14 18v-7"/>'
              '<path d="M18 18v-7"/><path d="M12 2 20 7H4z"/>',
    "chama": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.07-2.14-.22-4.05 2-6 .5 2.5 2 4.9 4 '
             '6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.15.43-2.29 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    "gema": '<path d="M6 3h12l4 6-10 13L2 9Z"/><path d="M11 3 8 9l4 13 4-13-3-6"/><path d="M2 9h20"/>',
    "moeda": '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2"/>'
             '<path d="M6 12h.01"/><path d="M18 12h.01"/>',
    "folha": '<path d="M7 20h10"/><path d="M10 20c5.5-2.5.8-6.4 3-10"/><path d="M9.5 9.4c1.1.8 1.8 2.2 2.3 '
             '3.7-2 .4-3.5.4-4.8-.3-1.2-.6-2.3-1.9-3-4.2 2.8-.5 4.4 0 5.5.8z"/><path d="M14.1 6a7 7 0 0 '
             '0-1.1 4c1.9-.1 3.3-.6 4.3-1.4 1-1 1.6-2.3 1.7-4.6-2.7.1-4 1-4.9 2z"/>',
    "grade": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/>'
             '<path d="M9 3v18"/><path d="M15 3v18"/>',
    "sobe": '<path d="m18 15-6-6-6 6"/>',
    "desce": '<path d="m6 9 6 6 6-6"/>',
}


def _ic(nome: str) -> str:
    return f'<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">{_ICONES[nome]}</svg>'


def _esc(valor) -> str:
    if valor is None or (not isinstance(valor, str) and pd.isna(valor)):
        return "—"
    return _escape(str(valor))


def _html(markup: str, alvo=None) -> None:
    """Renderiza HTML e SVG pelo markdown do Streamlit.

    st.html foi descartado: a sanitização dele remove SVG, e ícones e gráfico
    sumiam. Sem linhas em branco nem indentação, o bloco inteiro é lido como
    HTML cru em vez de virar parágrafo ou bloco de código.
    """
    limpo = "\n".join(linha.strip() for linha in markup.splitlines() if linha.strip())
    (alvo or st).markdown(limpo, unsafe_allow_html=True)


def _variacao(v, rotulo_na: str | None = "—") -> str:
    """Variação com sinal e seta. Sem dado: '—', ou nada quando rotulo_na é None."""
    if v is None or pd.isna(v):
        return "" if rotulo_na is None else f'<span class="chg na">{rotulo_na}</span>'
    if v > 0:
        return f'<span class="chg up">{_ic("sobe")}{_num(v, 2, sufixo="%", sinal=True)}</span>'
    if v < 0:
        return f'<span class="chg down">{_ic("desce")}{_num(v, 2, sufixo="%")}</span>'
    return f'<span class="chg na">{_num(v, 2, sufixo="%")}</span>'


def _variacao_c(c: dict | None) -> str:
    """Variação no formato do mercado: % para preço, pontos percentuais para taxa de juros."""
    if not c or c.get("unidade") != "pp":
        return _variacao((c or {}).get("var_pct"))
    v = c.get("var")
    if v is None or pd.isna(v):
        return _variacao(None)
    txt = _num(v, 2, sufixo=" pp", sinal=True)
    if round(v, 2) > 0:
        return f'<span class="chg up">{_ic("sobe")}{txt}</span>'
    if round(v, 2) < 0:
        return f'<span class="chg down">{_ic("desce")}{txt}</span>'
    return f'<span class="chg na">{txt}</span>'


def _cabecalho(fonte: str, coletado_em: datetime | None, ref_pregao: date | None,
               hoje: date, demo: bool) -> str:
    if demo:
        cot = '<span class="pill warn"><span class="dot off"></span>Dados sintéticos · não são preços reais</span>'
    elif ref_pregao is None:
        cot = '<span class="pill"><span class="dot off"></span>Data das cotações indisponível</span>'
    elif ref_pregao >= hoje:
        cot = '<span class="pill"><span class="dot"></span>Cotações de <b>hoje</b></span>'
    else:
        cot = (f'<span class="pill"><span class="dot off"></span>Último pregão com negócio: '
               f'<b>{ref_pregao:%d/%m}</b></span>')
    hora = (f'<span class="pill">{_ic("relogio")}Atualizado às <b>{coletado_em:%H:%M}</b></span>'
            if coletado_em else "")
    return _moldura("A melhor call e a melhor put do dia · B3",
                    f'<span class="pill">{_ic("fonte")}<b>{_esc(fonte)}</b></span>{cot}{hora}')


def _moldura(subtitulo: str, pills: str) -> str:
    """Cabeçalho comum às abas: marca à esquerda, estado da fonte à direita."""
    return f"""
    <div class="dx"><div class="hdr">
      <div class="brand"><div class="mark">{_ic("marca")}</div>
        <div><div class="t">Painel Day Trade</div>
        <div class="s">{_esc(subtitulo)}</div></div></div>
      <div class="status">{pills}</div>
    </div></div>"""


def _kpis(ativo: str, spot, venc: date, du: int, iv_atm, n_calls: int, n_puts: int,
          total: int, vol_calls: float = 0.0, vol_puts: float = 0.0) -> str:
    tiles = [
        ("ativo", f"{ativo} · preço", _moeda(spot) if spot else "—",
         "deduzido da grade de opções" if spot else "sem distância do strike na fonte"),
        ("calendario", "Vencimento", f"{venc:%d/%m/%Y}",
         f"{du} dias úteis · {'mensal' if eh_vencimento_mensal(venc) else 'semanal'}"),
        ("vol", "Vol. implícita ATM", _num(iv_atm, 1, sufixo="%") if iv_atm else "—",
         "mediana perto do dinheiro"),
        ("camadas", "Candidatas", f"{n_calls} calls · {n_puts} puts",
         f"de {_num(total, 0)} opções na grade"),
        ("balanca", "Put/Call · volume", _num(vol_puts / vol_calls, 2) if vol_calls else "—",
         f"{_moeda_compacta(vol_puts)} ÷ {_moeda_compacta(vol_calls)}"),
    ]
    corpo = "".join(
        f'<div class="kpi"><div class="l">{_ic(ic)}{_esc(l)}</div>'
        f'<div class="v">{_esc(v)}</div><div class="d">{_esc(d)}</div></div>'
        for ic, l, v, d in tiles)
    return f'<div class="kpis">{corpo}</div>'


def _medidor_delta(delta, dmin: float, dmax: float) -> str:
    lo, hi = 0.30, 0.90
    def pos(x): return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))
    absd = abs(delta) if delta is not None and not pd.isna(delta) else None
    pin = f'<span class="pin" style="left:{pos(absd):.2f}%"></span>' if absd is not None else ""
    ticks = "".join(f'<span style="left:{pos(x):.2f}%">{_num(x, 2)}</span>'
                    for x in sorted({lo, dmin, dmax, hi}))
    return f"""
    <div class="meter">
      <div class="row"><span>|Δ| <b>{_num(absd, 3) if absd is not None else "—"}</b></span>
        <span>faixa {_num(dmin, 2)}–{_num(dmax, 2)}</span></div>
      <div class="track"><span class="band" style="left:{pos(dmin):.2f}%;width:{pos(dmax) - pos(dmin):.2f}%"></span>{pin}</div>
      <div class="ticks">{ticks}</div>
    </div>"""


def _cartao_sinal(melhor: pd.Series, lado: str, n_lado: int, dmin: float, dmax: float) -> str:
    nome = "calls" if lado == "call" else "puts"
    rank = (f"1º em volume financeiro entre {n_lado} {nome} elegíveis" if n_lado > 1
            else f"única {nome[:-1]} elegível neste vencimento")
    situacao = _esc(melhor.get("moneyness"))
    chips = (f'<span class="chip{" strong" if situacao == "ATM" else ""}">{situacao}</span>'
             f'<span class="chip">{_esc(melhor.get("modelo"))}</span>')
    neg = melhor.get("num_neg")
    data_neg = melhor.get("data_neg")
    data_txt = f"{data_neg:%d/%m}" if isinstance(data_neg, date) else "—"
    return f"""
    <div class="card sig {lado}">
      <div class="sig-top"><span class="side"><i></i>Melhor {lado}</span><div class="chips">{chips}</div></div>
      <div class="sig-main">
        <div class="sig-tick">{_esc(melhor["ticker"])}</div>
        <div class="sig-rank">{rank}</div>
        <div class="sig-price"><span class="p"><small>R$</small>{_num(melhor["ultimo"], 2)}</span>
          {_variacao(melhor.get("variacao"), None)}</div>
      </div>
      <div class="sig-grid">
        <div><span>Strike</span><b>{_moeda(melhor["strike"])}</b></div>
        <div><span>Delta</span><b>{_num(melhor["delta"], 3, sinal=True)}</b></div>
        <div><span>Vol. impl.</span><b>{_num(melhor.get("vol_impl"), 1, sufixo="%")}</b></div>
        <div><span>Negócios</span><b>{_num(neg, 0) if neg is not None and not pd.isna(neg) else "—"}</b></div>
        <div><span>Vol. financeiro</span><b>{_moeda_compacta(melhor["vol_financeiro"])}</b></div>
        <div><span>Último negócio</span><b>{data_txt}</b></div>
      </div>
      {_medidor_delta(melhor["delta"], dmin, dmax)}
    </div>"""


def _cartao_vazio(lado: str, mensagem: str) -> str:
    return f"""
    <div class="card sig {lado} empty">
      <div class="sig-top"><span class="side"><i></i>Melhor {lado}</span></div>
      <div class="sig-main"><div class="msg">{_esc(mensagem)}</div></div>
    </div>"""


def _grafico_liquidez(grade: pd.DataFrame, elegiveis: set, spot,
                      picks: dict, janela: float = 0.10) -> str:
    """Liquidez por strike, espelhada: calls para cima, puts para baixo, em SVG.

    Forma de ênfase (skill dataviz): só os strikes que passaram em todos os
    filtros ganham a cor do lado; o resto fica cinza. x em porcentagem para o
    gráfico acompanhar a largura sem escalar o texto.
    """
    base = grade[grade["vol_financeiro"].fillna(0) > 0].copy()
    if spot:
        base = base[(base["strike"] >= spot * (1 - janela)) & (base["strike"] <= spot * (1 + janela))]
    cabeca = f"""
    <div class="card-h"><div class="t">{_ic("barras")}Liquidez por strike</div>
      <div class="legend"><span><i style="background:var(--call)"></i>Calls elegíveis</span>
        <span><i style="background:var(--put)"></i>Puts elegíveis</span>
        <span><i style="background:var(--gray-mark)"></i>Fora dos filtros</span>
        <span><i class="rule"></i>Preço do ativo</span></div></div>"""
    if base.empty:
        return (f'<div class="card">{cabeca}<div class="chart-foot" style="padding-top:18px">'
                f"Sem volume negociado perto do preço do ativo neste vencimento.</div></div>")

    lados = {}
    for tipo in ("CALL", "PUT"):
        parte = base[base["tipo"] == tipo]
        agg = {}
        for _, r in parte.iterrows():
            k = round(float(r["strike"]), 2)
            item = agg.setdefault(k, {"vol": 0.0, "neg": 0, "top": None, "top_vol": -1.0, "eleg": False})
            vol = float(r["vol_financeiro"] or 0)
            item["vol"] += vol
            item["neg"] += 0 if pd.isna(r["num_neg"]) else int(r["num_neg"])
            item["eleg"] = item["eleg"] or r["ticker"] in elegiveis
            if vol > item["top_vol"]:
                item["top_vol"], item["top"] = vol, r
        lados[tipo] = agg

    strikes = sorted(set(lados["CALL"]) | set(lados["PUT"]))
    n = len(strikes)
    vmax = max([v["vol"] for d in lados.values() for v in d.values()] or [1.0]) or 1.0
    H, TOP, BOT = 300, 26, 34
    meio = TOP + (H - TOP - BOT) / 2
    meia = (H - TOP - BOT) / 2 - 6
    fatia = 100 / n
    frac = 0.62 if n >= 36 else (0.5 if n >= 20 else 0.36)
    larg = fatia * frac

    def barra(i, item, tipo):
        h = max(1.5, item["vol"] / vmax * meia)
        x = (i + 0.5) * fatia - larg / 2
        cor = ("var(--call)" if tipo == "CALL" else "var(--put)") if item["eleg"] else "var(--gray-mark)"
        r = item["top"]
        dica = (f'{r["ticker"]} · {tipo.lower()} · strike {_moeda(r["strike"])} · '
                f'{_moeda_compacta(item["vol"])} · {item["neg"]} negócios · Δ {_num(r["delta"], 3, sinal=True)}'
                + ("" if item["eleg"] else " · fora dos filtros"))
        raio = min(4.0, h)
        if tipo == "CALL":
            y = meio - h
            capa = f'<rect x="{x:.3f}%" y="{meio - raio:.2f}" width="{larg:.3f}%" height="{raio:.2f}" fill="{cor}"/>'
        else:
            y = meio
            capa = f'<rect x="{x:.3f}%" y="{meio:.2f}" width="{larg:.3f}%" height="{raio:.2f}" fill="{cor}"/>'
        return (f'<g class="bar"><title>{_escape(dica)}</title>'
                f'<rect x="{x:.3f}%" y="{y:.2f}" width="{larg:.3f}%" height="{h:.2f}" rx="{raio:.2f}" fill="{cor}"/>'
                f"{capa}</g>")

    barras, rotulos = [], []
    passo = max(1, -(-n // 16))
    for i, k in enumerate(strikes):
        for tipo in ("CALL", "PUT"):
            if k in lados[tipo]:
                barras.append(barra(i, lados[tipo][k], tipo))
        if i % passo == 0:
            alt = " alt" if (i // passo) % 2 else ""
            rotulos.append(f'<text class="xl{alt}" x="{(i + 0.5) * fatia:.3f}%" y="{H - 10}" '
                           f'text-anchor="middle">{_num(k, 2)}</text>')

    marcas = []
    for tipo, tk in picks.items():
        for i, k in enumerate(strikes):
            item = lados[tipo].get(k)
            if item and item["top"] is not None and item["top"]["ticker"] == tk:
                h = max(1.5, item["vol"] / vmax * meia)
                y = meio - h - 7 if tipo == "CALL" else meio + h + 15
                marcas.append(f'<text class="pk" x="{(i + 0.5) * fatia:.3f}%" y="{y:.2f}" '
                              f'text-anchor="middle">{_escape(tk)}</text>')

    spot_svg = ""
    if spot and n > 1 and strikes[0] <= spot <= strikes[-1]:
        j = max(i for i, k in enumerate(strikes) if k <= spot)
        frac_s = 0.0 if j == n - 1 else (spot - strikes[j]) / (strikes[j + 1] - strikes[j])
        xs = (j + 0.5 + frac_s) * fatia
        spot_svg = (f'<line class="spot" x1="{xs:.3f}%" x2="{xs:.3f}%" y1="{TOP - 8}" y2="{H - BOT}"/>'
                    f'<text class="sp" x="{xs:.3f}%" y="{TOP - 12}" text-anchor="middle">'
                    f"ativo {_num(spot, 2)}</text>")

    grades = "".join(f'<line class="grid" x1="0%" x2="100%" y1="{meio + s * meia * f:.2f}" '
                     f'y2="{meio + s * meia * f:.2f}"/>' for f in (0.5, 1.0) for s in (-1, 1))
    eixo_y = "".join(
        f'<text x="58" y="{meio + s * meia * f + 4:.2f}" text-anchor="end">{_moeda_compacta(vmax * f)}</text>'
        for f in (0.5, 1.0) for s in (-1, 1))
    eixo_y += (f'<text class="side-l" x="4" y="{meio - 7:.2f}">calls</text>'
               f'<text class="side-l" x="4" y="{meio + 16:.2f}">puts</text>')
    resumo = (f"Volume financeiro por strike, {n} strikes perto do preço do ativo; "
              f"calls acima da linha de base e puts abaixo.")
    return f"""
    <div class="card">{cabeca}
      <div class="chart-wrap"><div class="chart" role="img" aria-label="{_escape(resumo)}">
        <div class="yax"><svg width="64" height="{H}">{eixo_y}</svg></div>
        <div class="plot"><svg class="liq" width="100%" height="{H}">{grades}
          <line class="base" x1="0%" x2="100%" y1="{meio:.2f}" y2="{meio:.2f}"/>
          {"".join(barras)}{spot_svg}{"".join(marcas)}{"".join(rotulos)}</svg></div>
      </div></div>
      <div class="chart-foot">Janela de ±{int(janela * 100)}% em torno do preço do ativo. Passe o mouse
        numa barra para ver ticker, volume, negócios e delta.</div>
    </div>"""


def _tabela_ranking(df_lado: pd.DataFrame, lado: str, n_total: int) -> str:
    nome = "Calls" if lado == "call" else "Puts"
    topo = df_lado.head(TOP_N)
    cab = (f'<div class="card-h"><div class="t">{_ic("lista")}Ranking de {nome.lower()}</div>'
           f'<div class="d">top {len(topo)} de {n_total} elegíveis, por volume financeiro</div></div>')
    if topo.empty:
        return (f'<div class="card {lado}">{cab}<div class="chart-foot" style="padding-top:16px">'
                f"Nenhuma {nome.lower()[:-1]} passou nos filtros.</div></div>")
    vmax = float(topo["vol_financeiro"].max() or 1)
    linhas = []
    for i, (_, r) in enumerate(topo.iterrows()):
        largura = max(3, int(56 * float(r["vol_financeiro"] or 0) / vmax))
        neg = "—" if pd.isna(r["num_neg"]) else _num(r["num_neg"], 0)
        linhas.append(
            f'<tr class="{"top" if i == 0 else ""}">'
            f'<td class="tk"><span class="rank">{i + 1}</span>{_esc(r["ticker"])}</td>'
            f'<td class="mut">{_esc(r["modelo"])}</td>'
            f'<td>{_num(r["strike"], 2)}</td>'
            f'<td class="mut">{_esc(r["moneyness"])}</td>'
            f'<td>{_num(r["ultimo"], 2)}</td>'
            f'<td>{_variacao(r["variacao"], "—")}</td>'
            f"<td>{neg}</td>"
            f'<td><span class="vb"><i style="width:{largura}px"></i>{_moeda_compacta(r["vol_financeiro"])}</span></td>'
            f'<td>{_num(r["vol_impl"], 1, sufixo="%")}</td>'
            f'<td>{_num(r["delta"], 3, sinal=True)}</td></tr>')
    colunas = ("Ticker", "Mod.", "Strike", "A/I/OTM", "Últ. (R$)", "Var. (%)", "Núm. de Neg.",
               "Volume Financeiro", "Vol. Impl.", "Delta")
    th = "".join(f"<th>{c}</th>" for c in colunas)
    return (f'<div class="card {lado}">{cab}<div class="tbl-wrap"><table class="rk">'
            f'<thead><tr>{th}</tr></thead><tbody>{"".join(linhas)}</tbody></table></div></div>')


def _funil(funil: list[tuple[str, int]], nota: str) -> str:
    total = max(1, funil[0][1]) if funil else 1
    passos = "".join(
        f'<div class="fs"><span>{_esc(rotulo)}</span><b>{_num(n, 0)}</b>'
        f'<div class="fb"><i style="width:{max(1.5, n / total * 100):.1f}%"></i></div></div>'
        for rotulo, n in funil)
    return (f'<div class="card"><div class="card-h"><div class="t">{_ic("filtro")}Como a escolha foi feita</div>'
            f'<div class="d">cada etapa filtra a anterior; o topo da liquidez vence</div></div>'
            f'<div class="funil">{passos}</div><div class="note">{_ic("info")}<span>{nota}</span></div></div>')


def _estado_vazio(icone: str, titulo: str, texto: str, itens: list[str] | None = None) -> None:
    lista = ("<ul>" + "".join(f"<li>{i}</li>" for i in itens) + "</ul>") if itens else ""
    _html(f'<div class="dx"><div class="empty-st"><div class="t">{_ic(icone)}{_esc(titulo)}</div>'
          f"<p>{texto}</p>{lista}</div></div>")


def _rodape(fonte: str = "Dados do opcoes.net.br podem ter atraso — confirme preço e liquidez "
                         "no home broker antes de operar.") -> None:
    _html('<div class="dx"><div class="foot"><span>Ferramenta de apoio à decisão para uso próprio. '
          f"Não constitui recomendação de investimento.</span><span>{_esc(fonte)}</span></div></div>")


def _sb(titulo: str, primeiro: bool = False) -> None:
    st.markdown(f'<div class="sb{" first" if primeiro else ""}">{_escape(titulo)}</div>',
                unsafe_allow_html=True)


def _grade_numerica(grade: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Grade completa com números de verdade (ordenável), formatada pelo idioma do navegador."""
    g = pd.DataFrame({
        "Ticker": grade["ticker"], "Tipo": grade["tipo"], "Mod.": grade["modelo"],
        "Strike": grade["strike"], "A/I/OTM": grade["moneyness"], "Últ. (R$)": grade["ultimo"],
        "Var. (%)": grade["variacao"], "Núm. de Neg.": grade["num_neg"],
        "Volume Financeiro": grade["vol_financeiro"], "Vol. Impl. (%)": grade["vol_impl"],
        "Delta": grade["delta"],
        "Últ. negócio": grade["data_neg"] if "data_neg" in grade.columns else None,
    })
    num = st.column_config.NumberColumn
    cfg = {c: num(format="localized") for c in
           ("Strike", "Últ. (R$)", "Var. (%)", "Núm. de Neg.", "Volume Financeiro", "Vol. Impl. (%)", "Delta")}
    return g, cfg


# ---------------- Aba Correlações ---------------------------------------------

_ICONE_GRUPO = {"eua": "ativo", "brny": "predio", "europa": "globo", "asia": "globo",
                "energia": "chama", "metais": "gema", "cambio": "moeda", "agro": "folha"}


def _casas(c: dict) -> int:
    """Casas decimais: fixas quando a fonte pede (índice em pontos), senão o priceHint do Yahoo, de 2 a 4."""
    if c.get("casas_fixas") is not None:
        return int(c["casas_fixas"])
    return min(4, max(2, int(c.get("casas") or 2)))


def _hora_cotacao(ts: int, agora: datetime) -> tuple[str, bool]:
    """Hora do último preço em Brasília ('08:21', ou '11/09' se não foi hoje) e se está parado."""
    if not ts:
        return "—", True
    quando = datetime.fromtimestamp(ts, BRT)
    parado = (agora - quando).total_seconds() > 30 * 60
    return (f"{quando:%H:%M}" if quando.date() == agora.date() else f"{quando:%d/%m}"), parado


def _sparkline(pontos: list, ref) -> str:
    """Curva da sessão, com o fechamento anterior tracejado e a cor do sinal da variação.

    O domínio vertical inclui a referência: o salto entre ela e o começo da
    curva mostra o que andou antes da primeira barra do dia.
    """
    if len(pontos) < 2:
        return '<span class="spk-na">—</span>'
    amostra = pontos[::max(1, -(-len(pontos) // 120))]
    if amostra[-1] != pontos[-1]:
        amostra.append(pontos[-1])
    t0, t1 = amostra[0][0], amostra[-1][0]
    valores = [v for _, v in amostra] + ([ref] if ref else [])
    lo, hi = min(valores), max(valores)
    if hi - lo < 1e-12:
        lo, hi = lo - 1, hi + 1
    def y(v): return 2 + (hi - v) / (hi - lo) * 26
    def x(t): return (t - t0) / max(1, t1 - t0) * 100
    ult = amostra[-1][1]
    cor = "up" if ref and ult > ref else ("down" if ref and ult < ref else "flat")
    tracejado = f'<line x1="0" x2="100" y1="{y(ref):.2f}" y2="{y(ref):.2f}"/>' if ref else ""
    pts = " ".join(f"{x(t):.2f},{y(v):.2f}" for t, v in amostra)
    return (f'<svg class="spk" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">'
            f'{tracejado}<polyline class="{cor}" points="{pts}"/></svg>')


def _barra_div(v, escala: float) -> str:
    """Barra que sai do zero para a direita (alta) ou a esquerda (queda), na mesma escala para todos."""
    if v is None or pd.isna(v) or v == 0:
        return '<span class="dv"></span>'
    largura = max(4.0, min(50.0, abs(v) / escala * 50))
    esquerda = 50.0 if v > 0 else 50.0 - largura
    return (f'<span class="dv"><i class="{"up" if v > 0 else "down"}" '
            f'style="left:{esquerda:.1f}%;width:{largura:.1f}%"></i></span>')


def _pulso(cot: dict, agora: datetime) -> str:
    """Faixa de abertura: os seis mercados que mais pesam na B3, com a curva da sessão."""
    tiles = []
    for s in PULSO:
        nome, det = NOMES.get(s, (s, ""))
        c = cot.get(s)
        sub = (c or {}).get("contrato") or det
        rotulo = f'<div class="l">{_esc(nome)}<span class="sub">{_esc(sub)}</span></div>'
        if not c:
            tiles.append(f'<div class="kpi pz">{rotulo}<div class="v">—</div>'
                         f'<div class="d">sem cotação agora</div></div>')
            continue
        casas = _casas(c)
        hora, _ = _hora_cotacao(c["hora"], agora)
        curva = (_sparkline(c["pontos"], c["ref"]) if len(c["pontos"]) > 1
                 else '<div class="spk-vazio">sem curva da sessão</div>')
        absoluta = "" if c.get("unidade") == "pp" else f'{_num(c["var"], casas, sinal=True)} · '
        fase = f'<span class="fase">{c["fase"]}</span>' if c["fase"] != "regular" else ""
        tiles.append(
            f'<div class="kpi pz">{rotulo}<div class="v">{_num(c["ultimo"], casas)}</div>'
            f'<div class="d">{_variacao_c(c)}<span class="q">{fase}{absoluta}{hora}</span></div>{curva}</div>')
    return f'<div class="pulse">{"".join(tiles)}</div>'


def _quadro_grupo(chave: str, titulo: str, desc: str, itens: list, cot: dict,
                  escala: float, agora: datetime) -> str:
    """Tabela de um grupo de mercados: último, variação, curva da sessão e hora."""
    linhas = []
    for s, nome, det in itens:
        c = cot.get(s)
        codigo = (c or {}).get("contrato") or s
        rotulo = (f'<td class="nm"><b>{_esc(nome)}</b><small>{_esc(det)} · '
                  f'<span class="mono">{_esc(codigo)}</span></small></td>')
        if not c:
            linhas.append(f'<tr>{rotulo}<td colspan="4" class="mut">sem cotação agora</td></tr>')
            continue
        casas = _casas(c)
        hora, parado = _hora_cotacao(c["hora"], agora)
        fase = f'<span class="fase">{c["fase"]}</span>' if c["fase"] != "regular" else ""
        dica = (f"{nome}: {_num(c['var'], casas, sinal=True)} desde o fechamento anterior "
                f"({_num(c['ref'], casas)})")
        linhas.append(
            f'<tr title="{_escape(dica)}">{rotulo}<td>{_num(c["ultimo"], casas)}</td>'
            f'<td>{_barra_div(c["var_pct"], escala)}{_variacao_c(c)}</td>'
            f'<td class="c-sp">{_sparkline(c["pontos"], c["ref"])}</td>'
            f'<td class="hr{" old" if parado else ""}">{fase}{hora}</td></tr>')
    th = '<th>Mercado</th><th>Último</th><th>Var. %</th><th class="c-sp">Sessão</th><th>Hora</th>'
    return (f'<div class="card mk"><div class="card-h"><div class="t">{_ic(_ICONE_GRUPO.get(chave, "globo"))}'
            f'{_esc(titulo)}</div><div class="d">{_esc(desc)}</div></div>'
            f'<div class="tbl-wrap"><table class="rk qt"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(linhas)}</tbody></table></div></div>')


def _leitura(fortes: pd.Series, cot: dict, sigmas: dict) -> str:
    """Direção que os mercados mais correlacionados sugerem para o ativo agora.

    Soma ρ × (variação desde o fechamento anterior, em desvios-padrão diários do
    próprio mercado), só com |ρ| ≥ 0,3: abaixo disso a relação é fraca demais.
    Dividir pelo desvio-padrão impede que o VIX, que anda 10% num dia comum,
    pese mais que o S&P, que anda 1%.
    """
    soma, usados = 0.0, 0
    for sym, r in fortes.items():
        c, sigma = cot.get(sym), sigmas.get(sym)
        if abs(r) < 0.3 or not c or c.get("var_pct") is None or not sigma:
            continue
        soma += r * c["var_pct"] / sigma
        usados += 1
    if not usados:
        return '<div class="lei na">sem mercado com correlação forte</div>'
    if abs(soma) < 0.5:
        return '<div class="lei na">sem direção clara</div>'
    if soma > 0:
        return f'<div class="lei up">{_ic("sobe")}pressão de alta</div>'
    return f'<div class="lei down">{_ic("desce")}pressão de baixa</div>'


def _descricao_janela(janela, dia: date | None, barras: int = 0) -> str:
    if janela == "hoje":
        quando = "de hoje" if dia == datetime.now(BRT).date() else (f"de {dia:%d/%m}" if dia else "")
        return f"retornos de 5 min · pregão {quando} · até {barras} barras"
    return f"retornos diários · últimos {janela} pregões em comum"


def _mapa_correlacao(corr: pd.DataFrame, nobs: pd.DataFrame, janela, dia: date | None = None) -> str:
    """Matriz completa ativo × mercado, para consulta: cor divergente e o número em cada célula.

    Azul move junto, vermelho move ao contrário, e o cinza do meio é "sem
    relação"; a intensidade acompanha |ρ|, com um teto de mistura que mantém o
    texto claro legível. `janela` é o número de pregões ou "hoje" (5 min).
    """
    intradiario = janela == "hoje"
    unidade = "barras de 5 min" if intradiario else "pregões"
    th = "".join(f"<th>{_esc(n)}</th>" for _, n in MOTORES)
    linhas = []
    for ativo in ATIVOS:
        s = B3_YAHOO[ativo]
        serie = corr.loc[s] if s in corr.index else pd.Series(dtype=float)
        cels = []
        for sym, nome in MOTORES:
            r = serie.get(sym, np.nan)
            if pd.isna(r):
                cels.append('<td class="c na">—</td>')
                continue
            base = "var(--c-pos)" if r >= 0 else "var(--c-neg)"
            dica = f"{ativo} × {nome}: ρ {_num(r, 2, sinal=True)} em {int(nobs.loc[s, sym])} {unidade}"
            cels.append(f'<td class="c{" fraco" if abs(r) < 0.2 else ""}" title="{_escape(dica)}" '
                        f'style="background:color-mix(in oklab,{base} {6 + 66 * min(1.0, abs(r)):.0f}%,'
                        f'var(--c-mid))">{_num(r, 2, sinal=True)}</td>')
        linhas.append(f'<tr><td class="ak">{ativo}</td>{"".join(cels)}</tr>')
    barras = int(nobs.to_numpy().max()) if nobs.size else 0
    if intradiario:
        nota = ("Barras de 5 min desde a abertura da B3 (antes dela, o último pregão); cada par precisa de "
                "40 min em comum. Ásia e minério não negociam no horário da B3 e ficam em branco.")
    else:
        nota = ("Fechamentos diários. Ásia e minério fecham antes da B3 abrir, então ali o número mede o "
                "quanto o movimento de lá antecipa o nosso.")
    legenda = ('<div class="hm-leg"><span>−1 · move ao contrário</span><span class="grad"></span>'
               "<span>+1 · move junto</span></div>")
    return (f'<div class="card"><div class="card-h"><div class="t">{_ic("grade")}Correlação ativo × mercado</div>'
            f'<div class="d">{_descricao_janela(janela, dia, barras)}</div></div>'
            f'<div class="hm-wrap"><table class="hm"><thead><tr><th></th>{th}</tr></thead>'
            f'<tbody>{"".join(linhas)}</tbody></table></div>{legenda}'
            f'<div class="note">{_ic("info")}<span>{nota} Passe o mouse numa célula para ver quantos dados '
            f"entraram na conta.</span></div></div>")


_FORCAS = ((0.6, "forte"), (0.3, "moderada"), (0.0, "fraca"))


def _forca(r: float) -> str:
    return next(nome for limite, nome in _FORCAS if abs(r) >= limite)


def _movimento(c: dict) -> str:
    """'cai 2,6%' ou 'sobe 0,10 pp', no formato do mercado."""
    if abs(c.get("var_pct") or 0.0) < 0.05:
        return "está estável"
    if c.get("unidade") == "pp":
        v, txt = c.get("var") or 0.0, f"{_num(abs(c.get('var') or 0.0), 2)} pp"
    else:
        v, txt = c["var_pct"], f"{_num(abs(c['var_pct']), 1)}%"
    return f"{'sobe' if v > 0 else 'cai'} {txt}"


def _frase(ativo: str, mercados: pd.Series, cot: dict, intradiario: bool) -> str:
    """Uma frase com o mercado de relação mais forte e o que ele faz agora."""
    nomes = dict(MOTORES)
    for sym, r in mercados.items():
        c = cot.get(sym)
        if abs(r) < 0.3 or not c or c.get("var_pct") is None:
            continue
        if intradiario:
            relacao = "tem andado junto com" if r > 0 else "tem ido na direção contrária de"
            return f"<b>{_esc(nomes[sym])}</b> {_movimento(c)} e, no pregão, {relacao} {ativo}."
        relacao = "costuma andar junto com" if r > 0 else "costuma ir na direção contrária de"
        return f"<b>{_esc(nomes[sym])}</b> {_movimento(c)} e {relacao} {ativo}."
    return f"Nenhum mercado tem relação forte com {ativo} nesta janela: os sinais de fora ajudam pouco."


def _efeito(r: float, c: dict | None) -> str:
    """O que o movimento de agora faz com o ativo, dada a relação entre os dois."""
    if abs(r) < 0.3:
        return '<span class="efe na">relação fraca</span>'
    if not c or c.get("var_pct") is None or abs(c["var_pct"]) < 0.05:
        return '<span class="efe na">parado</span>'
    if r * c["var_pct"] > 0:
        return f'<span class="efe up">{_ic("sobe")}puxa para cima</span>'
    return f'<span class="efe down">{_ic("desce")}puxa para baixo</span>'


def _cartoes_ativos(corr: pd.DataFrame, cot: dict, sigmas: dict, janela, dia: date | None) -> str:
    """Um cartão por ativo: os mercados que mais andam com ele, o que fazem agora e a pressão.

    É a matriz traduzida: a relação em palavras (anda junto ou contra; fraca,
    moderada ou forte), o movimento de agora e o efeito que ele tem no ativo.
    Entram até três mercados de relação moderada ou forte; sem nenhum, os dois
    mais fortes, marcados como relação fraca.
    """
    intradiario = janela == "hoje"
    nomes = dict(MOTORES)
    cartoes = []
    for ativo in ATIVOS:
        s = B3_YAHOO[ativo]
        serie = corr.loc[s].dropna() if s in corr.index else pd.Series(dtype=float)
        ordem = serie.reindex(serie.abs().sort_values(ascending=False).index)
        relevantes = ordem[ordem.abs() >= 0.3].head(3)
        mostrados = relevantes if len(relevantes) else ordem.head(2)
        linhas = []
        for sym, r in mostrados.items():
            c = cot.get(sym)
            linhas.append(
                f'<div class="drv-r"><div class="drv-n"><b>{_esc(nomes[sym])}</b>'
                f'<span>{"anda junto" if r > 0 else "anda contra"} · {_forca(r)}'
                f'<em>{_num(r, 2, sinal=True)}</em></span></div>'
                f'<div class="drv-v">{_variacao_c(c)}</div><div class="drv-e">{_efeito(r, c)}</div>'
                f'<div class="drv-b"><i style="width:{min(1.0, abs(r)) * 100:.0f}%"></i></div></div>')
        if linhas:
            topo = _leitura(relevantes, cot, sigmas)
            frase = _frase(ativo, mostrados, cot, intradiario)
        else:
            topo, frase = "", "Sem dados suficientes nesta janela."
        cartoes.append(f'<div class="card at"><div class="at-h"><span class="at-tk">{ativo}</span>{topo}</div>'
                       f'<p class="at-s">{frase}</p><div class="drv">{"".join(linhas)}</div></div>')
    if intradiario:
        quando = ("no pregão de hoje" if dia == datetime.now(BRT).date()
                  else (f"no pregão de {dia:%d/%m}" if dia else "no último pregão")) + ", em barras de 5 min"
    else:
        quando = f"nos últimos {janela} pregões"
    guia = (f'<div class="guia">{_ic("info")}<span><b>Anda junto</b>: quando o mercado sobe, o ativo costuma '
            "subir; <b>anda contra</b>: o contrário. <b>Força</b>: fraca abaixo de 0,3, moderada até 0,6, "
            "forte acima disso. <b>Puxa para cima ou para baixo</b> junta essa relação com o que o mercado faz "
            "agora, e a <b>pressão</b> soma os mercados de relação moderada ou forte, pesando cada um pelo "
            "tamanho normal do movimento dele. É contexto para o pregão, não sinal de entrada.</span></div>")
    return (f'<div class="sec"><div class="t">Seus ativos hoje</div><div class="d">relação medida {quando} · '
            f'movimentos desde o fechamento anterior</div></div>{guia}<div class="ativos">{"".join(cartoes)}</div>')


def _cartao_aviso(icone: str, titulo: str, texto: str) -> str:
    return (f'<div class="card"><div class="card-h"><div class="t">{_ic(icone)}{_esc(titulo)}</div></div>'
            f'<div class="chart-foot" style="padding-top:16px">{texto}</div></div>')


def _carregar(fonte: str, ativo: str, arquivo, usar_selenium: bool, headless: bool,
              hoje: date) -> tuple[dict | None, str | None]:
    """Busca os dados conforme a fonte. Devolve (estado, erro)."""
    if fonte == "Demo":
        return {"df": grade_demo(ativo, hoje), "fonte": "Demonstração", "demo": True,
                "coletado": datetime.now(), "log": ["Grade sintética gerada por Black-Scholes."]}, None
    if fonte == "Arquivo":
        if arquivo is None:
            return None, None
        try:
            return {"df": ler_arquivo(arquivo.name, arquivo.getvalue()), "fonte": f"Arquivo · {arquivo.name}",
                    "demo": False, "coletado": datetime.now(), "log": [f"Arquivo {arquivo.name}"]}, None
        except Exception as exc:
            return None, f"Não consegui ler o arquivo: {exc}"
    try:
        with st.spinner(f"Buscando a grade de {ativo} no opcoes.net.br…"):
            df, rota, log, coletado = extrair_automatico(ativo, usar_selenium, headless)
        return {"df": df, "fonte": "opcoes.net.br", "demo": False, "coletado": coletado,
                "log": log, "rota": rota}, None
    except Exception as exc:
        return None, str(exc)


# Valores de partida dos controles. Moram no session_state, e não no `value=`
# dos widgets, para sobreviver à troca de aba (ver _manter_estado).
_ESTADO_PADRAO: dict[str, object] = {
    "fonte_sel": "Site", "ativo_sel": "PETR4", "somente_padrao": True,
    "du_janela": (DU_MIN_PADRAO, DU_MAX_PADRAO), "todos_venc": False,
    "delta_faixa": (DELTA_MIN_PADRAO, DELTA_MAX_PADRAO), "min_neg": 0, "exigir_neg": True,
    "max_atraso": 1, "incluir_seguinte": True, "taxa_pct": 10.75, "spot_manual": 0.0, "usar_selenium": False,
    "headless": True, "janela_corr": 60, "auto_corr": True, "auto_ops": True,
    "grafico_ativo": "VALE3",
}


def _manter_estado() -> None:
    """Segura o valor dos controles quando a aba deles não é desenhada.

    Com abas preguiçosas, o Streamlit apaga o estado de todo widget que não
    aparece numa execução. Regravar as chaves no começo de cada execução
    interrompe essa limpeza; por isso os widgets são criados sem `value` ou
    `default`, que disputariam com o estado.
    """
    for chave, padrao in _ESTADO_PADRAO.items():
        st.session_state[chave] = st.session_state.get(chave, padrao)
    for chave in [k for k in st.session_state if str(k).startswith("venc_")]:
        st.session_state[chave] = st.session_state[chave]


def main() -> None:
    st.set_page_config(page_title="Painel Day Trade · B3", page_icon="📈", layout="wide",
                       initial_sidebar_state="auto")
    st.markdown(_CSS, unsafe_allow_html=True)
    _manter_estado()
    cabecalho = st.empty()
    rotulos = [":material/candlestick_chart: Opções", ":material/public: Correlações",
               ":material/monitoring: Operações"]
    # ?aba=correlacoes ou ?aba=operacoes abre direto na aba (dá para deixar nos favoritos)
    inicial = {"correlacoes": rotulos[1], "operacoes": rotulos[2]}.get(st.query_params.get("aba"), rotulos[0])
    aba_opcoes, aba_mercados, aba_operacoes = st.tabs(rotulos, default=inicial, key="aba", on_change="rerun")
    # Só a aba aberta roda: uma aba não espera pelos dados das outras.
    if aba_operacoes.open:
        with aba_operacoes:
            pagina_operacoes(cabecalho)
    elif aba_mercados.open:
        with aba_mercados:
            pagina_correlacoes(cabecalho)
    else:
        with aba_opcoes:
            pagina_opcoes(cabecalho, date.today())


def pagina_opcoes(cabecalho, hoje: date) -> None:
    # ---------------- Barra lateral: fonte e parâmetros ---------------------
    with st.sidebar:
        _sb("Fonte de dados", primeiro=True)
        fonte = st.segmented_control("Fonte", ["Site", "Arquivo", "Demo"], required=True,
                                     key="fonte_sel", label_visibility="collapsed") or "Site"
        arquivo = None
        if fonte == "Arquivo":
            arquivo = st.file_uploader("Grade exportada do opcoes.net.br",
                                       type=["csv", "xlsx", "xls", "xlsm"])
        _sb("Regras")
        somente_padrao = st.toggle(
            "Somente séries mensais padrão", key="somente_padrao",
            help="Aceita só RAIZ+LETRA+DÍGITOS (PETRI494) e descarta semanais e séries atípicas "
                 "(PETRI483W4), antes da ordenação por liquidez.")
        du_min, du_max = st.slider("Dias úteis até o vencimento", 0, 60, key="du_janela")
        incluir_seguinte = st.toggle(
            "Incluir o vencimento seguinte", key="incluir_seguinte",
            help="Mostra também o mensal logo depois da janela de dias úteis, para comparar o "
                 "vencimento curto com o próximo. O mais curto continua sendo o padrão.")
        todos = st.toggle("Mostrar vencimentos fora da janela", key="todos_venc")
        delta_min, delta_max = st.slider("Faixa de |Delta|", 0.05, 0.95, step=0.05, key="delta_faixa")
        _sb("Liquidez")
        min_negocios = st.number_input("Mínimo de negócios", min_value=0, step=50, key="min_neg")
        exigir_negocio = st.toggle("Descartar opções sem negócio", key="exigir_neg")
        max_atraso = st.slider(
            "Máx. pregões desde o último negócio", 0, 10, key="max_atraso",
            help="Opção parada há semanas guarda volume acumulado alto e subiria no ranking, "
                 "mas o Delta sairia de um preço velho.")
        _sb("Modelo")
        taxa_pct = st.number_input("Taxa livre de risco (% a.a.)", min_value=0.0, max_value=30.0,
                                   step=0.25, key="taxa_pct",
                                   help="Entra no Black-Scholes que calcula o Delta.")
        spot_manual = st.number_input("Preço do ativo (0 = deduzir)", min_value=0.0, step=0.10,
                                      key="spot_manual")
        with st.expander("Avançado"):
            usar_selenium = st.toggle("Tentar Selenium se o JSON falhar", key="usar_selenium")
            headless = st.toggle("Chrome em modo headless", key="headless")
            st.caption(f"Ambiente: {'WebAssembly' if NO_NAVEGADOR else 'Python local'} · "
                       f"dados via {URL_JSON}")

    # ---------------- Controles da aba ---------------------------------------
    c_ativo, c_venc, c_acao = st.columns([5, 5, 1.3], vertical_alignment="bottom")
    with c_ativo:
        ativo = st.segmented_control("Ativo", ATIVOS, key="ativo_sel", required=True)
        ativo = ativo or st.session_state.get("ativo_ult", "PETR4")
        st.session_state["ativo_ult"] = ativo
    with c_acao:
        atualizar = st.button("Atualizar", icon=":material/refresh:", key="atualizar_op", **_LARGURA)
    if atualizar and hasattr(extrair_automatico, "clear"):
        extrair_automatico.clear()

    # ---------------- Carga ---------------------------------------------------
    estado, erro = _carregar(fonte, ativo, arquivo, usar_selenium, headless, hoje)
    if erro:
        _html(_cabecalho("falha na coleta", None, None, hoje, False), cabecalho)
        _estado_vazio("alerta", "Não consegui buscar os dados",
                      f"{_esc(erro)}<br>Enquanto isso, exporte a grade no opcoes.net.br e use "
                      f"<b>Arquivo</b> na barra lateral, ou <b>Demo</b> para ver a interface.")
        _rodape()
        return
    if estado is None:
        _html(_cabecalho("aguardando arquivo", None, None, hoje, False), cabecalho)
        _estado_vazio("fonte", "Envie a grade de opções",
                      "Exporte a grade no opcoes.net.br (CSV ou Excel) e envie pela barra lateral. "
                      "O painel reconhece as colunas mesmo que o site mude os nomes.")
        _rodape()
        return

    try:
        df = preparar_dataframe(estado["df"], spot=(spot_manual or None), hoje=hoje, taxa=taxa_pct / 100.0)
    except Exception as exc:
        _html(_cabecalho(estado["fonte"], estado.get("coletado"), None, hoje, estado["demo"]), cabecalho)
        _estado_vazio("alerta", "Não consegui interpretar a grade recebida", _esc(exc))
        _rodape()
        return

    spot = df.attrs.get("spot")
    ref_pregao = max(df["data_neg"].dropna()) if "data_neg" in df.columns and df["data_neg"].notna().any() else None
    _html(_cabecalho(estado["fonte"], estado.get("coletado"), ref_pregao, hoje, estado["demo"]), cabecalho)

    # ---------------- Vencimento ------------------------------------------------
    vencimentos = sorted({v for v in df["vencimento"].dropna().unique()}) or calendario_vencimentos(hoje)
    candidatos = [v for v in vencimentos if eh_vencimento_mensal(v)] if somente_padrao else vencimentos
    no_ciclo = [v for v in candidatos if du_min <= dias_uteis(hoje, v) <= du_max]
    # O vencimento seguinte ao último da janela (ou o primeiro acima do piso, se a
    # janela estiver vazia), para comparar o curto com o próximo.
    limite = no_ciclo[-1] if no_ciclo else None
    seguinte = next((v for v in candidatos if dias_uteis(hoje, v) >= du_min
                     and (limite is None or v > limite)), None) if incluir_seguinte else None
    if todos:
        elegiveis_v = candidatos or vencimentos
    else:
        elegiveis_v = no_ciclo + ([seguinte] if seguinte else [])
    with c_venc:
        if elegiveis_v:
            chave_venc = f"venc_{fonte}_{ativo}"
            if st.session_state.get(chave_venc) not in elegiveis_v:
                st.session_state.pop(chave_venc, None)
            venc = st.segmented_control(
                "Vencimento", elegiveis_v, key=chave_venc, required=True,
                default=None if chave_venc in st.session_state else elegiveis_v[0],
                format_func=lambda v: f"{v:%d/%m} · {dias_uteis(hoje, v)} DU")
            venc = venc or elegiveis_v[0]
        else:
            venc = None
    if venc is None:
        mensais = [v for v in vencimentos if eh_vencimento_mensal(v)][:4]
        _estado_vazio("calendario", f"Nenhum vencimento mensal entre {du_min} e {du_max} dias úteis",
                      "Os mensais da B3 (3ª sexta) ficam a ~21 dias úteis um do outro, então há dias do mês "
                      "em que nenhum cai na janela. Ajuste a janela na barra lateral, ligue "
                      "<b>Mostrar vencimentos fora da janela</b>, ou desligue <b>Somente séries mensais padrão</b>.",
                      [f"{v:%d/%m/%Y} — {dias_uteis(hoje, v)} dias úteis" for v in mensais])
        _rodape()
        return

    # ---------------- Filtros e ranking -----------------------------------------
    filtrado, funil = aplicar_filtros(df, venc, delta_min, delta_max, int(min_negocios),
                                      exigir_negocio, max_atraso_du=int(max_atraso),
                                      somente_padrao=somente_padrao)
    calls, puts = separar_calls_puts(filtrado)
    grade_venc = df[df["vencimento"] == venc]
    if somente_padrao:
        grade_venc = grade_venc[grade_venc["ticker"].str.match(_RE_TICKER_PADRAO, na=False)]
    iv_atm = None
    if spot:
        perto = grade_venc[(grade_venc["strike"] / spot - 1).abs() <= 0.02]["vol_impl"].dropna()
        iv_atm = float(perto.median()) if not perto.empty else None

    vol_calls = float(grade_venc.loc[grade_venc["tipo"] == "CALL", "vol_financeiro"].sum())
    vol_puts = float(grade_venc.loc[grade_venc["tipo"] == "PUT", "vol_financeiro"].sum())
    picks = {}
    if not calls.empty:
        picks["CALL"] = calls.iloc[0]["ticker"]
    if not puts.empty:
        picks["PUT"] = puts.iloc[0]["ticker"]
    faixa = f"|Δ| {_num(delta_min, 2)}–{_num(delta_max, 2)}"
    call_html = (_cartao_sinal(calls.iloc[0], "call", len(calls), delta_min, delta_max) if not calls.empty
                 else _cartao_vazio("call", f"Nenhuma call com {faixa} passou nos filtros neste vencimento."))
    put_html = (_cartao_sinal(puts.iloc[0], "put", len(puts), delta_min, delta_max) if not puts.empty
                else _cartao_vazio("put", f"Nenhuma put com {faixa} passou nos filtros neste vencimento."))

    calculados = int(df["delta_calculado"].sum()) if "delta_calculado" in df.columns else 0
    nota = ("<b>Delta calculado pelo painel</b> — o opcoes.net.br não entrega os gregos no plano "
            "gratuito. A volatilidade implícita é invertida do preço negociado e o Delta sai por "
            f"Black-Scholes, com o ativo a {_moeda(spot)} e taxa de {_num(taxa_pct, 2, sufixo='%')} a.a. "
            f"({calculados} opções).") if calculados else (
            "Delta lido da própria fonte de dados.")

    _html(f"""
    <div class="dx">
      {_kpis(ativo, spot, venc, dias_uteis(hoje, venc), iv_atm, len(calls), len(puts), len(df), vol_calls, vol_puts)}
      <div class="board">
        <div class="a-call">{call_html}</div>
        <div class="a-put">{put_html}</div>
        <div class="a-chart">{_grafico_liquidez(grade_venc, set(filtrado["ticker"]), spot, picks)}</div>
        <div class="a-tabs">{_tabela_ranking(calls, "call", len(calls))}{_tabela_ranking(puts, "put", len(puts))}</div>
        <div class="a-funil">{_funil(funil, nota)}</div>
      </div>
    </div>""")

    # ---------------- Detalhes ---------------------------------------------------
    st.write("")
    with st.expander("Grade completa do vencimento", icon=":material/table_view:"):
        grade_tab, cfg = _grade_numerica(ordenar_por_liquidez(grade_venc))
        st.dataframe(grade_tab, column_config=cfg, hide_index=True, height=420, **_LARGURA)
        st.download_button(
            "Baixar grade filtrada (CSV)", icon=":material/download:",
            data=tabela_exibicao(filtrado).to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name=f"opcoes_{ativo}_{venc:%Y%m%d}.csv", mime="text/csv")
    with st.expander("Registro da coleta", icon=":material/receipt_long:"):
        for linha in estado.get("log", []):
            st.caption(linha)

    _rodape()


def pagina_correlacoes(cabecalho) -> None:
    with st.sidebar:
        _sb("Correlações", primeiro=True)
        auto = st.toggle("Atualizar sozinho a cada 1 min", key="auto_corr",
                         help="Recarrega só esta aba; a grade de opções não é buscada de novo.")
        _sb("Sobre os dados")
        st.caption("Cotações do Yahoo Finance, em horário de Brasília. O atraso varia por bolsa (nos "
                   "futuros dos EUA, até cerca de 10 min); a coluna Hora mostra a do último preço.")
        st.caption("Minério de ferro: SGX, 62% Fe em US$/t, no contrato mais negociado. Ibovespa, dólar "
                   "e DI futuros: site da B3, com 15 min de atraso; o DI nos janeiros curto, médio e longo.")
    ritmo = "Atualiza a cada <b>1 min</b>" if auto else "Atualização <b>manual</b>"
    _html(_moldura("O que os mercados lá fora fizeram enquanto a B3 estava fechada",
                   f'<span class="pill">{_ic("fonte")}<b>Yahoo Finance</b></span>'
                   f'<span class="pill">{_ic("relogio")}{ritmo}</span>'), cabecalho)
    st.fragment(_painel_mercados, run_every=60 if auto else None)()
    _rodape("Cotações do Yahoo Finance, com atraso que varia por bolsa — confirme no home broker "
            "antes de operar.")


def _painel_mercados() -> None:
    """Conteúdo da aba Correlações; roda como fragmento para se atualizar sozinho."""
    c_jan, c_sts, c_acao = st.columns([6, 3, 1.3], vertical_alignment="bottom")
    with c_jan:
        janela = st.segmented_control(
            "Janela da correlação", JANELAS_CORR, key="janela_corr", required=True,
            format_func=lambda n: "Pregão de hoje" if n == "hoje" else f"{n} pregões") or 60
    with c_acao:
        atualizar = st.button("Atualizar", icon=":material/refresh:", key="atualizar_mk", **_LARGURA)
    if atualizar and hasattr(cotacoes_globais, "clear"):
        cotacoes_globais.clear()

    simbolos = simbolos_mercados()
    try:
        with st.spinner("Buscando cotações…"):
            cot, quando, _ = cotacoes_globais(simbolos)
    except Exception as exc:
        _estado_vazio("alerta", "Não consegui buscar as cotações",
                      f"{_esc(exc)}<br>O Yahoo Finance às vezes recusa conexões por alguns instantes. "
                      "Tente <b>Atualizar</b> daqui a pouco.")
        return
    faltam = len(simbolos) - len(cot)
    _html(f'<div class="dx"><div class="stline">{_ic("relogio")}<span>Cotações de <b>{quando:%H:%M:%S}</b>'
          f'{f" · {faltam} mercados sem resposta" if faltam else ""}</span></div></div>', c_sts)

    linhas_b3, colunas = [B3_YAHOO[a] for a in ATIVOS], [s for s, _ in MOTORES]
    try:
        with st.spinner("Calculando correlações…"):
            hist = historico_diario(tuple(linhas_b3 + colunas))
            # tamanho normal do movimento diário de cada mercado, para pesar a pressão
            sigmas = {c: float(hist[c].dropna().pct_change().dropna().tail(60).std() * 100)
                      for c in colunas if c in hist.columns}
            if janela == "hoje":
                barras, dia = historico_intradiario(tuple(linhas_b3 + colunas))
                corr, nobs = matriz_correlacao(barras, linhas_b3, colunas, max(1, len(barras)), minimo=8)
            else:
                dia = None
                corr, nobs = matriz_correlacao(hist, linhas_b3, colunas, int(janela))
        mapa = _mapa_correlacao(corr, nobs, janela, dia=dia)
        ativos_html = _cartoes_ativos(corr, cot, sigmas, janela, dia)
    except Exception as exc:
        mapa = ""
        ativos_html = _cartao_aviso("grade", "Seus ativos hoje",
                                    f"Não consegui calcular as correlações agora: {_esc(exc)}")

    agora = datetime.now(BRT)
    variacoes = [abs(c["var_pct"]) for c in cot.values() if c["var_pct"] is not None]
    escala = max(1.0, float(np.percentile(variacoes, 90))) if variacoes else 1.0
    quadros = "".join(_quadro_grupo(chave, titulo, desc, itens, cot, escala, agora)
                      for chave, titulo, desc, itens in GRUPOS_GLOBAIS)
    _html(f'<div class="dx">{_pulso(cot, agora)}{ativos_html}</div>')
    if mapa:
        # expander, e não <details>: o estado aberto sobrevive à atualização de cada minuto
        with st.expander(f"Ver a matriz completa · {len(ATIVOS)} ativos × {len(MOTORES)} mercados",
                         icon=":material/grid_on:"):
            _html(f'<div class="dx">{mapa}</div>')
    _html(f'<div class="dx"><div class="sec"><div class="t">Cotações por mercado</div>'
          f'<div class="d">Sessão: curva do dia, com o fechamento anterior tracejado · a barra da '
          f'variação usa a mesma escala em todos os quadros</div></div>'
          f'<div class="mk-grid">{quadros}</div></div>')


# ---------------- Aba Operações ---------------------------------------------

@cache_dados(ttl=3600, show_spinner=False)
def _barras_historico(ativo: str) -> pd.DataFrame:
    """60 dias de barras de 5 min (o máximo do Yahoo) com o candle do leilão de
    fechamento recriado, renovadas de hora em hora. Sem ajuste por proventos: ele vem
    no fim, depois de entrarem os pregões do RTD."""
    barras = ope.barras_5min(ativo, "60d")
    try:
        return ope.com_leilao_fechamento(barras, ope.barras_diarias(ativo))
    except Exception:
        return barras


@cache_dados(ttl=3600, show_spinner=False)
def _proventos(ativo: str) -> list:
    return ope.proventos(ativo)


@cache_dados(ttl=3600, show_spinner=False)
def _barras_rtd_passado(ativo: str, dia) -> pd.DataFrame:
    """Pregão já encerrado gravado pelo coletor (o arquivo não muda mais)."""
    return ope.barras_rtd(ativo, PASTA_RT, dia)


@cache_dados(ttl=30, show_spinner=False)
def _barras_recentes(ativo: str) -> pd.DataFrame:
    return ope.barras_5min(ativo, "1d")


PASTA_RT = Path(__file__).resolve().parent / "dados_rt"   # onde o coletor_rtd.py grava


def barras_estrategia(ativo: str) -> pd.DataFrame:
    """Histórico longo mais o pregão de agora, o mais parecido possível com o gráfico do
    Profit. As estratégias carregam posição de um dia para o outro, então a simulação
    precisa começar bem antes da operação aberta.

    Todo pregão que o coletor RTD gravou (o de hoje inclusive) vem dele — com os leilões e o
    after-market, como no Profit; o resto vem do Yahoo. O ajuste por proventos fica por
    último, porque o RTD grava o preço negociado, sem ajuste."""
    barras = pd.concat([_barras_historico(ativo), _barras_recentes(ativo)])
    barras = barras[~barras.index.duplicated(keep="last")].sort_index()
    hoje = datetime.now(BRT).date()
    if len(barras):
        for dia in ope.dias_rtd(PASTA_RT):
            if barras.index[0].date() <= dia < hoje:
                barras = ope.juntar_barras(barras, _barras_rtd_passado(ativo, dia))
    barras = ope.juntar_barras(barras, ope.barras_rtd(ativo, PASTA_RT, hoje))
    try:
        return ope.ajustar_proventos(barras, _proventos(ativo), ativo)
    except Exception:
        return barras


def _estado_rtd() -> tuple[str, str]:
    """('vivo' | 'encerrado' | 'fora', hora da última cotação) conforme o arquivo do coletor."""
    arq = PASTA_RT / f"{datetime.now(BRT):%Y-%m-%d}.csv"
    if not arq.exists():
        return "fora", ""
    quando = datetime.fromtimestamp(arq.stat().st_mtime, BRT)
    agora = datetime.now(BRT)
    if (agora - quando).total_seconds() < 180 and "10:00" <= agora.strftime("%H:%M") <= "18:30":
        return "vivo", f"{quando:%H:%M:%S}"
    return ("encerrado" if agora.strftime("%H:%M") > "18:30" else "fora"), f"{quando:%H:%M}"


def _quando(t, dia) -> str:
    """'10:40' se foi no pregão mostrado, '11/09 10:40' se veio de um pregão anterior."""
    return f"{t:%H:%M}" if t.date() == dia else f"{t:%d/%m %H:%M}"


def _reais(v: float) -> str:
    """+R$ 66,00 / −R$ 12,50 — resultado com sinal na frente do símbolo."""
    if v is None or pd.isna(v):
        return "—"
    sinal = "+" if v > 0.004 else ("−" if v < -0.004 else "")
    return f"{sinal}R$ {_num(abs(v), 2)}"


def _cartao_operacao(ativo: str, est, res, formando, erro: str | None) -> str:
    """Cartão de um ativo: posição em andamento (alvo, parcial, stop e resultado) ou a falta dela."""
    if est is None:
        return (f'<div class="card opc pendente"><div class="opc-h"><div><span class="at-tk">{ativo}</span>'
                f'<span class="opc-e">estratégia ainda não cadastrada</span></div>'
                f'<span class="sts">aguardando código</span></div>'
                f'<p class="opc-vazio">Mande o código da estratégia do Profit para este ativo, com o tempo '
                f"gráfico, e ele passa a ser acompanhado aqui.</p></div>")
    cab = f'<div><span class="at-tk">{ativo}</span><span class="opc-e">{_esc(est.nome)} · {est.minutos} min</span></div>'
    if erro or res is None:
        return (f'<div class="card opc"><div class="opc-h">{cab}<span class="sts">sem dados</span></div>'
                f'<p class="opc-vazio">Não consegui montar os candles agora: {_esc(erro)}</p></div>')
    ultimo = formando if formando is not None else res.candles.iloc[-1]
    preco, dia = float(ultimo["close"]), ultimo.name.date()
    do_dia = [o for o in res.operacoes if o.hora_sinal.date() == dia]
    fechadas = [o for o in do_dia if not o.aberta]
    no_dia = sum(o.resultado() for o in fechadas)
    op = res.aberta
    if op is None:
        if do_dia:
            u = do_dia[-1]
            saida = u.execucoes[-1]
            texto = (f"Sem operação aberta. A última foi {'compra' if u.lado > 0 else 'venda'} com sinal às "
                     f"{u.hora_sinal:%H:%M}, encerrada por {saida.rotulo} às {saida.hora:%H:%M} "
                     f"({_reais(u.resultado())}).")
        else:
            texto = "Sem operação aberta e nenhum sinal neste pregão."
        return (f'<div class="card opc"><div class="opc-h">{cab}<span class="sts">sem posição</span></div>'
                f'<p class="opc-vazio">{texto}</p><div class="opc-g">'
                f'<div><span>Preço</span><b>{_moeda(preco)}</b><em>candle das {ultimo.name:%H:%M}</em></div>'
                f'<div><span>Operações</span><b>{len(fechadas)}</b><em>fechadas no pregão</em></div>'
                f'<div><span>Resultado</span><b>{_reais(no_dia)}</b><em>do pregão, lote {est.lote}</em></div>'
                f"</div></div>")

    ent = op.entrada
    resultado = op.resultado(preco)
    pct = resultado / (ent.preco * est.lote) * 100
    lado_txt, classe = ("Comprado", "compra") if op.lado > 0 else ("Vendido", "venda")
    icone = _ic("sobe") if op.lado > 0 else _ic("desce")
    parcial = next((e for e in op.execucoes if e.rotulo == "parcial"), None)
    tem_parcial = not pd.isna(op.alvo1)
    tom = "up" if resultado > 0 else ("down" if resultado < 0 else "")

    def pos(v: float) -> float:
        span = op.alvo2 - op.stop
        return 50.0 if abs(span) < 1e-9 else max(0.0, min(100.0, (v - op.stop) / span * 100))

    regua = (f'<div class="regua"><div class="trilho"></div>'
             f'<div class="m" style="left:0%"><span>stop</span></div>'
             f'<div class="m cima" style="left:{pos(op.preco_sinal):.1f}%"><span>entrada</span></div>'
             + (f'<div class="m cima" style="left:{pos(op.alvo1):.1f}%"><span>parcial</span></div>'
                if tem_parcial else "")
             + f'<div class="m" style="left:100%"><span>alvo</span></div>'
             f'<div class="ag {tom}" style="left:{pos(preco):.1f}%" title="preço atual {_moeda(preco)}"></div></div>')
    falta = abs(op.alvo2 - preco) / preco * 100
    if tem_parcial:
        if parcial:
            parcial_txt = f'<em class="ok">executada {_quando(parcial.hora, dia)}</em>'
        elif not getattr(est, "parcial_no_profit", True):
            parcial_txt = ('<em title="A parcial do código é de 50 ações, e o lote padrão da B3 é de 100: '
                           'no Profit a ordem não executa">não executa (50 ações)</em>')
        else:
            parcial_txt = "<em>pendente</em>"
        meio = (f'<div><span>Parcial</span><b>{_moeda(op.alvo1)}</b>{parcial_txt}</div>'
                f'<div><span>Alvo final</span><b>{_moeda(op.alvo2)}</b><em>faltam {_num(falta, 2)}%</em></div>')
    else:
        ate_stop = abs(op.stop - preco) / preco * 100
        meio = (f'<div><span>Alvo</span><b>{_moeda(op.alvo2)}</b><em>faltam {_num(falta, 2)}%</em></div>'
                f'<div><span>Até o stop</span><b>{_num(ate_stop, 2)}%</b><em>sem parcial</em></div>')
    if op.stop_movido:
        stop_txt = "no zero a zero"
    elif not tem_parcial:
        stop_txt = "sai a mercado após tocar"
    else:
        stop_txt = f"risco de {_num(abs(op.stop - ent.preco) / ent.preco * 100, 2)}%"
    return (f'<div class="card opc"><div class="opc-h">{cab}<span class="sts {classe}">{icone}{lado_txt}</span></div>'
            f'<div class="opc-res"><div class="v {tom}">{_reais(resultado)}</div>'
            f'<div class="d">{_num(pct, 2, sufixo="%", sinal=True)} sobre a entrada · {_num(abs(op.qtd), 0)} de '
            f"{est.lote} ações abertas</div></div>{regua}"
            f'<div class="opc-g">'
            f'<div><span>Entrada</span><b>{_moeda(ent.preco)}</b><em>{_quando(ent.hora, dia)} · sinal {_quando(op.hora_sinal, dia)}</em></div>'
            f'<div><span>Preço atual</span><b>{_moeda(preco)}</b><em>candle das {ultimo.name:%H:%M}</em></div>'
            f'{meio}'
            f'<div><span>Stop</span><b>{_moeda(op.stop)}</b><em>{stop_txt}</em></div>'
            f'<div><span>No pregão</span><b>{_reais(no_dia)}</b><em>{len(fechadas)} fechada(s)</em></div>'
            f"</div></div>")


def _cartao_pendentes(ativos: list[str]) -> str:
    """Os ativos ainda sem estratégia cadastrada, num cartão só."""
    chips = "".join(f'<span class="chip">{a}</span>' for a in ativos)
    return (f'<div class="card opc pendente"><div class="opc-h"><div><span class="opc-t">Aguardando código</span>'
            f'<span class="opc-e">{len(ativos)} {"ativo" if len(ativos) == 1 else "ativos"} sem estratégia '
            f'cadastrada</span></div></div>'
            f'<div class="chips-l">{chips}</div>'
            f'<p class="opc-vazio">Mande o código de cada estratégia do Profit, com o tempo gráfico, e o ativo '
            f"ganha um cartão próprio aqui.</p></div>")


def _grafico_operacao(res, formando):
    """Candles do pregão como no Profit: pintados de verde ou vermelho no sinal, os demais em cinza;
    médias, VWAP, entradas e saídas, e as linhas de alvo, parcial e stop da operação."""
    import plotly.graph_objects as go

    d = res.candles
    if formando is not None:
        d = pd.concat([d, formando.to_frame().T])
    dia = d.index[-1].date()
    # com posição aberta vinda de pregões anteriores, o gráfico começa no dia do sinal (até 5 pregões)
    datas = sorted({t.date() for t in d.index})
    primeiro = dia
    if res.aberta is not None:
        primeiro = max(res.aberta.hora_sinal.date(), datas[max(0, len(datas) - 5)])
    d = d[[t.date() >= primeiro for t in d.index]].copy()
    varios_dias = primeiro != dia
    for col in ("open", "high", "low", "close"):
        d[col] = d[col].astype(float)
    fig = go.Figure()

    def velas(mascara, cor_alta, cor_baixa, borda_alta, borda_baixa):
        sub = d[mascara]
        if not sub.empty:
            fig.add_trace(go.Candlestick(
                x=sub.index, open=sub["open"], high=sub["high"], low=sub["low"], close=sub["close"],
                increasing=dict(fillcolor=cor_alta, line=dict(color=borda_alta, width=1)),
                decreasing=dict(fillcolor=cor_baixa, line=dict(color=borda_baixa, width=1)),
                showlegend=False, hoverinfo="skip", whiskerwidth=0.3))

    cor = d["cor"].fillna("")
    velas(cor == "", "rgba(139,151,173,.30)", "#4A5670", "#8B97AD", "#6B7790")
    velas(cor == "verde", "#22C55E", "#22C55E", "#22C55E", "#22C55E")
    velas(cor == "vermelho", "#F05252", "#F05252", "#F05252", "#F05252")
    for col, nome, cor_l, larg, traco in (("ema9", "Média 9", "#86b6ef", 1.2, "solid"),
                                          ("ema21", "Média 21", "#3987e5", 1.2, "solid"),
                                          ("ema50", "Média 50", "#1c5cab", 1.4, "solid"),
                                          ("vwap", "VWAP", "#c98500", 1.3, "dot")):
        if col in d:
            fig.add_trace(go.Scatter(x=d.index, y=d[col].astype(float), mode="lines", name=nome,
                                     line=dict(color=cor_l, width=larg, dash=traco), hoverinfo="skip"))

    fim = d.index[-1] + pd.Timedelta(minutes=res.estrategia.minutos)
    op = res.aberta
    if op is not None and op.entrada is not None:
        linhas = [(op.alvo2, "alvo", "#22C55E", "solid")]
        if not pd.isna(op.alvo1):
            linhas.append((op.alvo1, "parcial", "#22C55E", "dash"))
        if abs(op.stop - op.preco_sinal) >= 0.05:
            linhas.append((op.preco_sinal, "entrada", "#E2E8F0", "dot"))
        linhas.append((op.stop, "stop no zero a zero" if op.stop_movido else "stop", "#F05252", "solid"))
        for preco, rotulo, cor_l, traco in linhas:
            fig.add_shape(type="line", x0=op.entrada.hora, x1=fim, y0=preco, y1=preco,
                          line=dict(color=cor_l, width=1.2, dash=traco))
            fig.add_annotation(x=fim, y=preco, text=f"{rotulo} {_num(preco, 2)}", showarrow=False,
                               xanchor="left", font=dict(size=11, color=cor_l), bgcolor="rgba(11,15,26,.75)")
    for o in res.operacoes:
        for e in o.execucoes:
            if e.hora.date() < primeiro:
                continue
            compra = e.qtd > 0
            fig.add_trace(go.Scatter(
                x=[e.hora], y=[e.preco], mode="markers", showlegend=False,
                marker=dict(symbol="triangle-up" if compra else "triangle-down", size=11,
                            color="#22C55E" if compra else "#F05252", line=dict(color="#05070C", width=1)),
                hovertemplate=f"{e.rotulo} · {'compra' if compra else 'venda'} {abs(e.qtd):g} a R$ %{{y:.2f}}"
                              "<br>%{x|%H:%M}<extra></extra>"))
    fig.update_layout(
        height=470, margin=dict(l=8, r=150, t=34, b=24), separators=",.",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", size=12, color="#A9B4C8"),
        xaxis=dict(rangeslider=dict(visible=False), showgrid=False,
                   tickformat="%d/%m %H:%M" if varios_dias else "%H:%M",
                   rangebreaks=[dict(bounds=["sat", "mon"]),
                                dict(bounds=[18.5 if (d.index.hour * 60 + d.index.minute >= 17 * 60 + 30).any()
                                             else 17.5, 10], pattern="hour")],
                   linecolor="rgba(255,255,255,.12)", range=[d.index[0], fim + pd.Timedelta(minutes=30)]),
        yaxis=dict(side="right", gridcolor="rgba(255,255,255,.06)", zeroline=False, tickformat=".2f"),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, font=dict(size=11)),
        hoverlabel=dict(bgcolor="#111829", bordercolor="rgba(255,255,255,.15)", font=dict(color="#F1F5F9")),
        hovermode="closest")
    return fig


def pagina_operacoes(cabecalho) -> None:
    with st.sidebar:
        _sb("Operações", primeiro=True)
        auto = st.toggle("Atualizar sozinho", key="auto_ops",
                         help="A cada 5 s com o Profit ao vivo; a cada 30 s com os dados do Yahoo.")
        _sb("Sobre os dados")
        st.caption("As estratégias são traduções do código do Profit. Sinal no fechamento do candle e entrada "
                   "na abertura do seguinte, como no backtest do Profit; a posição segue de um pregão para o "
                   "outro até o alvo ou o stop.")
        st.caption("O pregão de hoje vem do Profit em tempo real, pelo RTD, enquanto o coletor_rtd.py estiver "
                   "gravando (ele sobe junto com o painel). Sem ele, os candles vêm do Yahoo, com cerca de 15 min "
                   "de atraso; o histórico dos dias anteriores vem sempre do Yahoo.")
    estado, hora = _estado_rtd()
    if estado == "vivo":
        fonte = f'<span class="pill"><span class="dot"></span><b>Profit</b> · tempo real</span>'
    elif estado == "encerrado":
        fonte = f'<span class="pill"><span class="dot off"></span><b>Profit</b> · pregão encerrado ({hora})</span>'
    else:
        fonte = f'<span class="pill">{_ic("fonte")}<b>Yahoo Finance</b> · atraso de ~15 min</span>'
    ritmo = 5 if estado == "vivo" else 30
    _html(_moldura("Suas estratégias do Profit: posição, alvo, parcial, stop e resultado",
                   fonte + f'<span class="pill">{_ic("relogio")}'
                   + (f"Atualiza a cada <b>{ritmo} s</b>" if auto else "Atualização <b>manual</b>") + "</span>"),
          cabecalho)
    st.fragment(_painel_operacoes, run_every=ritmo if auto else None)()
    _rodape("Sinais recalculados a partir do código das estratégias do Profit — confira no Profit antes de agir.")


def _painel_operacoes() -> None:
    """Cartões dos seis ativos e o gráfico do ativo escolhido; roda como fragmento."""
    resultados, erros = {}, {}
    for ativo, est in ope.ESTRATEGIAS.items():
        try:
            resultados[ativo] = ope.situacao(est, barras_estrategia(ativo))
        except Exception as exc:
            erros[ativo] = str(exc)
    cartoes = "".join(
        _cartao_operacao(a, ope.ESTRATEGIAS[a], *(resultados.get(a) or (None, None)), erros.get(a))
        for a in ATIVOS if a in ope.ESTRATEGIAS)
    faltam = [a for a in ATIVOS if a not in ope.ESTRATEGIAS]
    if faltam:
        cartoes += _cartao_pendentes(faltam)
    _html(f'<div class="dx"><div class="ops">{cartoes}</div></div>')
    if not resultados:
        return
    disponiveis = [a for a in ATIVOS if a in resultados]
    if st.session_state.get("grafico_ativo") not in disponiveis:
        st.session_state["grafico_ativo"] = disponiveis[0]
    ativo = st.segmented_control("Gráfico", disponiveis, key="grafico_ativo", required=True) or disponiveis[0]
    res, formando = resultados[ativo]
    with st.container(key="grafico_op"):
        st.plotly_chart(_grafico_operacao(res, formando), config={"displayModeBar": False}, **_LARGURA)


if __name__ == "__main__":
    main()
