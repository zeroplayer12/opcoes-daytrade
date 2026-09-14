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
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import streamlit as st

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
                       du_limite: int = 40,
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
        alvos = [v for v in vencimentos if 0 < v["du"] <= du_limite][:max_vencimentos]
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
# Yahoo Finance, que cobre os mesmos futuros e índices. Minério de ferro
# (SGX/Dalian) não tem fonte gratuita aberta: BHP e Rio Tinto entram como
# termômetro do setor.

YAHOO_SPARK = "https://query1.finance.yahoo.com/v7/finance/spark"
_CABECALHOS_YAHOO = {"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}
_LOTE_YAHOO = 20                               # limite de símbolos por chamada
BRT = timezone(timedelta(hours=-3), "BRT")     # sem horário de verão desde 2019
JANELAS_CORR = [20, 60, 120]                   # pregões usados na correlação

# (chave, título, descrição, [(símbolo no Yahoo, nome, detalhe)])
GRUPOS_GLOBAIS: list[tuple[str, str, str, list[tuple[str, str, str]]]] = [
    ("eua", "EUA · índices futuros", "futuros da CME, negociados quase 24 h", [
        ("ES=F", "S&P 500", "futuro"), ("NQ=F", "Nasdaq 100", "futuro"),
        ("YM=F", "Dow Jones", "futuro"), ("RTY=F", "Russell 2000", "futuro"),
        ("^VIX", "VIX", "volatilidade do S&P")]),
    ("brny", "Brasil em Nova York", "ADRs e ETF · valem o pré e o pós-mercado", [
        ("EWZ", "EWZ", "ETF de Brasil"), ("PBR", "Petrobras", "ADR"), ("VALE", "Vale", "ADR"),
        ("ITUB", "Itaú", "ADR"), ("BBD", "Bradesco", "ADR")]),
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
    ("metais", "Metais e mineração", "COMEX · BHP e Rio Tinto no lugar do minério", [
        ("GC=F", "Ouro", "US$/onça"), ("SI=F", "Prata", "US$/onça"), ("HG=F", "Cobre", "US$/libra"),
        ("PL=F", "Platina", "US$/onça"), ("ALI=F", "Alumínio", "US$/tonelada"),
        ("BHP.AX", "BHP", "mineradora · Sydney"), ("RIO.L", "Rio Tinto", "mineradora · Londres")]),
    ("cambio", "Câmbio e juros", "moedas 24 h · Treasury em % a.a.", [
        ("USDBRL=X", "Dólar/Real", "USD/BRL"), ("DX-Y.NYB", "DXY", "dólar contra 6 moedas"),
        ("CNY=X", "Dólar/Yuan", "USD/CNY"), ("^TNX", "Treasury 10 anos", "taxa")]),
    ("agro", "Agrícolas e pecuária", "futuros CBOT, ICE e CME", [
        ("ZS=F", "Soja", "US¢/bushel"), ("ZC=F", "Milho", "US¢/bushel"), ("ZW=F", "Trigo", "US¢/bushel"),
        ("KC=F", "Café", "US¢/libra"), ("SB=F", "Açúcar", "US¢/libra"), ("CT=F", "Algodão", "US¢/libra"),
        ("LE=F", "Boi gordo", "US¢/libra")]),
]
PULSO: list[str] = ["ES=F", "NQ=F", "BZ=F", "^HSI", "USDBRL=X", "EWZ"]
# Colunas da matriz de correlação. ADRs ficam de fora: correlação perto de 1
# com o próprio papel não ensina nada.
MOTORES: list[tuple[str, str]] = [
    ("EWZ", "EWZ"), ("ES=F", "S&P 500"), ("^VIX", "VIX"), ("^HSI", "Hang Seng"), ("BZ=F", "Brent"),
    ("HG=F", "Cobre"), ("RIO.L", "Rio Tinto"), ("GC=F", "Ouro"), ("USDBRL=X", "Dólar"),
    ("DX-Y.NYB", "DXY"), ("^TNX", "Treasury"),
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


@cache_dados(ttl=60, show_spinner=False)
def cotacoes_globais(simbolos: tuple[str, ...]) -> tuple[dict[str, dict], datetime, list[str]]:
    """Cotação de cada símbolo, com a curva do dia em barras de 5 min (cache de 1 min)."""
    respostas, falhas = _spark(list(simbolos), "1d", "5m")
    agora = time.time()
    cot = {}
    for s in simbolos:
        c = ler_cotacao(respostas[s], agora) if s in respostas else None
        if c:
            cot[s] = c
    if not cot:
        raise FalhaExtracao("; ".join(falhas) or "o Yahoo Finance não devolveu cotações")
    return cot, datetime.now(BRT), falhas


@cache_dados(ttl=3600, show_spinner=False)
def historico_diario(simbolos: tuple[str, ...]) -> pd.DataFrame:
    """Fechamentos diários de um ano, indexados pela data local de cada bolsa.

    A data local importa: a barra diária de um futuro de Nova York e a do Hang
    Seng têm carimbos UTC diferentes, mas pertencem ao mesmo dia.
    """
    respostas, falhas = _spark(list(simbolos), "1y", "1d")
    series = {}
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


def matriz_correlacao(hist: pd.DataFrame, linhas: list[str], colunas: list[str],
                      janela: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pearson dos retornos diários, par a par, nos últimos `janela` pregões em comum.

    Cada série vira retorno sobre o próprio pregão anterior antes do cruzamento,
    para que o feriado de uma bolsa não apague o retorno do dia seguinte na
    outra. Par com menos da metade da janela em comum fica em branco.
    """
    retornos = {c: hist[c].dropna().pct_change().dropna() for c in hist.columns}
    corr = pd.DataFrame(np.nan, index=linhas, columns=colunas)
    nobs = pd.DataFrame(0, index=linhas, columns=colunas)
    minimo = max(10, janela // 2)
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
# Direção visual (skill ui-ux-pro-max): Swiss/minimal escuro e denso, Fira Sans
# na interface e Fira Code nos códigos de negociação. A cor fica reservada para
# os dados (skill dataviz): call e put são identidade, não bom/ruim, e usam o par
# categórico validado — azul #3987e5 e laranja #d95926, ΔE CVD 26,8 sobre a
# superfície dos cards. Verde e vermelho aparecem só em variação com sinal,
# sempre com seta. O cromo da interface é monocromático.

_FONTES = ("https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500;600"
           "&family=Fira+Sans:wght@400;500;600;700&display=swap")

_CSS = """
<style>
@import url('__FONTES__');
:root{
  --bg:#05070C; --s1:#0B0F1A; --s2:#10151F; --s3:#171D2C;
  --ln:#1C2333; --ln2:#29324A;
  --t1:#F1F5F9; --t2:#A3AEC2; --t3:#7C889E;
  --call:#3987e5; --put:#d95926;
  --up:#22C55E; --down:#F05252; --gray-mark:#222939; --seq:#64748B;
  --c-pos:#3987e5; --c-neg:#e66767; --c-mid:#222838;
  --sans:'Fira Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
  --mono:'Fira Code',ui-monospace,Consolas,monospace;
  --r:10px;
}
[data-testid="stAppViewContainer"],[data-testid="stMain"]{background:var(--bg);}
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
.pulse{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:6px 0 14px;}
@container (max-width:1300px){ .pulse{grid-template-columns:repeat(3,minmax(0,1fr));} }
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
    """Casas decimais do Yahoo (priceHint), entre 2 e 4."""
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
        rotulo = f'<div class="l">{_esc(nome)}<span class="sub">{_esc(det)}</span></div>'
        c = cot.get(s)
        if not c:
            tiles.append(f'<div class="kpi pz">{rotulo}<div class="v">—</div>'
                         f'<div class="d">sem cotação agora</div></div>')
            continue
        casas = _casas(c)
        hora, _ = _hora_cotacao(c["hora"], agora)
        fase = f'<span class="fase">{c["fase"]}</span>' if c["fase"] != "regular" else ""
        tiles.append(
            f'<div class="kpi pz">{rotulo}<div class="v">{_num(c["ultimo"], casas)}</div>'
            f'<div class="d">{_variacao(c["var_pct"])}<span class="q">{fase}'
            f'{_num(c["var"], casas, sinal=True)} · {hora}</span></div>'
            f'{_sparkline(c["pontos"], c["ref"])}</div>')
    return f'<div class="pulse">{"".join(tiles)}</div>'


def _quadro_grupo(chave: str, titulo: str, desc: str, itens: list, cot: dict,
                  escala: float, agora: datetime) -> str:
    """Tabela de um grupo de mercados: último, variação, curva da sessão e hora."""
    linhas = []
    for s, nome, det in itens:
        rotulo = (f'<td class="nm"><b>{_esc(nome)}</b><small>{_esc(det)} · '
                  f'<span class="mono">{_esc(s)}</span></small></td>')
        c = cot.get(s)
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
            f'<td>{_barra_div(c["var_pct"], escala)}{_variacao(c["var_pct"])}</td>'
            f'<td class="c-sp">{_sparkline(c["pontos"], c["ref"])}</td>'
            f'<td class="hr{" old" if parado else ""}">{fase}{hora}</td></tr>')
    th = '<th>Mercado</th><th>Último</th><th>Var. %</th><th class="c-sp">Sessão</th><th>Hora</th>'
    return (f'<div class="card mk"><div class="card-h"><div class="t">{_ic(_ICONE_GRUPO.get(chave, "globo"))}'
            f'{_esc(titulo)}</div><div class="d">{_esc(desc)}</div></div>'
            f'<div class="tbl-wrap"><table class="rk qt"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(linhas)}</tbody></table></div></div>')


def _mapa_correlacao(corr: pd.DataFrame, nobs: pd.DataFrame, cot: dict, janela: int) -> str:
    """Matriz ativo da B3 × mercado em cor divergente, com o número escrito em cada célula.

    Azul move junto, vermelho move ao contrário, e o cinza do meio é "sem
    relação"; a intensidade acompanha |ρ|, com um teto de mistura que mantém o
    texto claro legível. A última coluna traz os dois mercados mais
    correlacionados com o ativo e quanto cada um anda agora.
    """
    nomes = dict(MOTORES)
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
            dica = f"{ativo} × {nome}: ρ {_num(r, 2, sinal=True)} em {int(nobs.loc[s, sym])} pregões"
            cels.append(f'<td class="c{" fraco" if abs(r) < 0.2 else ""}" title="{_escape(dica)}" '
                        f'style="background:color-mix(in oklab,{base} {6 + 66 * min(1.0, abs(r)):.0f}%,'
                        f'var(--c-mid))">{_num(r, 2, sinal=True)}</td>')
        fortes = serie.dropna()
        fortes = fortes.reindex(fortes.abs().sort_values(ascending=False).index).head(2)
        mv = "".join(
            f'<span class="mv"><b>{_esc(nomes[sym])}</b><em>ρ {_num(r, 2, sinal=True)}</em>'
            f'{_variacao(cot[sym]["var_pct"] if sym in cot else None)}</span>'
            for sym, r in fortes.items())
        linhas.append(f'<tr><td class="ak">{ativo}</td>{"".join(cels)}<td class="lt">{mv or "—"}</td></tr>')
    cab = (f'<div class="card-h"><div class="t">{_ic("grade")}Correlação com seus ativos</div>'
           f'<div class="d">retornos diários · últimos {janela} pregões em comum</div></div>')
    legenda = ('<div class="hm-leg"><span>−1 · move ao contrário</span><span class="grad"></span>'
               "<span>+1 · move junto</span></div>")
    nota = (f'<div class="note">{_ic("info")}<span>Correlação de Pearson entre os retornos diários de cada '
            "ativo e de cada mercado. A Ásia fecha antes da B3 abrir, então ali o número mede o quanto o "
            "pregão asiático antecipa o nosso. Correlação passada não garante o movimento de hoje.</span></div>")
    return (f'<div class="card">{cab}<div class="hm-wrap"><table class="hm"><thead><tr><th></th>{th}'
            f'<th class="lt">Mais correlacionados · agora</th></tr></thead>'
            f'<tbody>{"".join(linhas)}</tbody></table></div>{legenda}{nota}</div>')


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
    "max_atraso": 1, "taxa_pct": 10.75, "spot_manual": 0.0, "usar_selenium": False,
    "headless": True, "janela_corr": 60, "auto_corr": True,
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
    rotulos = [":material/candlestick_chart: Opções", ":material/public: Correlações"]
    # ?aba=correlacoes abre direto na aba de mercados (dá para deixar nos favoritos)
    inicial = rotulos[1] if st.query_params.get("aba") == "correlacoes" else rotulos[0]
    aba_opcoes, aba_mercados = st.tabs(rotulos, default=inicial, key="aba", on_change="rerun")
    # Só a aba aberta roda: quem está nas opções não espera pelas cotações
    # globais, e vice-versa.
    if aba_mercados.open:
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
    elegiveis_v = no_ciclo or ((candidatos or vencimentos) if todos else [])
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
        st.caption("Minério de ferro (SGX e Dalian) não tem fonte gratuita aberta. BHP e Rio Tinto "
                   "entram como termômetro do setor.")
    ritmo = "Atualiza a cada <b>1 min</b>" if auto else "Atualização <b>manual</b>"
    _html(_moldura("O que os mercados lá fora fizeram enquanto a B3 estava fechada",
                   f'<span class="pill">{_ic("fonte")}<b>Yahoo Finance</b></span>'
                   f'<span class="pill">{_ic("relogio")}{ritmo}</span>'), cabecalho)
    st.fragment(_painel_mercados, run_every=60 if auto else None)()
    _rodape("Cotações do Yahoo Finance, com atraso que varia por bolsa — confirme no home broker "
            "antes de operar.")


def _painel_mercados() -> None:
    """Conteúdo da aba Correlações; roda como fragmento para se atualizar sozinho."""
    c_jan, c_sts, c_acao = st.columns([4, 5, 1.3], vertical_alignment="bottom")
    with c_jan:
        janela = st.segmented_control("Janela da correlação", JANELAS_CORR, key="janela_corr",
                                      required=True, format_func=lambda n: f"{n} pregões") or 60
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
        corr, nobs = matriz_correlacao(hist, linhas_b3, colunas, int(janela))
        mapa = _mapa_correlacao(corr, nobs, cot, int(janela))
    except Exception as exc:
        mapa = _cartao_aviso("grade", "Correlação com seus ativos",
                             f"Não consegui montar a matriz agora: {_esc(exc)}")

    agora = datetime.now(BRT)
    variacoes = [abs(c["var_pct"]) for c in cot.values() if c["var_pct"] is not None]
    escala = max(1.0, float(np.percentile(variacoes, 90))) if variacoes else 1.0
    quadros = "".join(_quadro_grupo(chave, titulo, desc, itens, cot, escala, agora)
                      for chave, titulo, desc, itens in GRUPOS_GLOBAIS)
    _html(f'<div class="dx">{_pulso(cot, agora)}{mapa}'
          f'<div class="sec"><div class="t">Cotações por mercado</div>'
          f'<div class="d">Sessão: curva do dia, com o fechamento anterior tracejado · a barra da '
          f'variação usa a mesma escala em todos os quadros</div></div>'
          f'<div class="mk-grid">{quadros}</div></div>')


if __name__ == "__main__":
    main()
