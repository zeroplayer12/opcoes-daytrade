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
from datetime import date, datetime, timedelta
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
                       max_vencimentos: int = 8) -> tuple[pd.DataFrame, str, list[str]]:
    """Rotas automáticas em cascata. Devolve (df, rota, log).

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
        log.append(f"📅 {len(vencimentos)} vencimentos no site, buscando {len(alvos)}")

        partes: list[pd.DataFrame] = []
        for venc in alvos:
            try:
                parte = _rota_json(ativo, venc["data"])
                parte["Vencimento"] = venc["data"].strftime("%d/%m/%Y")
                partes.append(parte)
                ciclo = "mensal" if venc["mensal"] else "semanal"
                log.append(f"  ✅ {venc['data']:%d/%m} ({venc['du']} DU, {ciclo}) — "
                           f"{len(parte)} opções")
            except Exception as exc:
                log.append(f"  ⚠️ {venc['data']:%d/%m} — {exc}")

        if partes:
            df = pd.concat(partes, ignore_index=True)
            log.append(f"✅ Rota A1 · JSON — {len(df)} linhas em "
                       f"{time.time() - inicio:.1f}s")
            return df, "Rota A1 · JSON", log
        raise FalhaExtracao("nenhum vencimento retornou cotações")
    except Exception as exc:
        log.append(f"❌ Rota A1 · JSON — {exc}")

    if usar_selenium:
        try:
            inicio = time.time()
            df = _rota_selenium(ativo, headless)
            log.append(f"✅ Rota A2 · Selenium — {len(df)} linhas em "
                       f"{time.time() - inicio:.1f}s")
            return df, "Rota A2 · Selenium", log
        except Exception as exc:
            log.append(f"❌ Rota A2 · Selenium — {exc}")

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
        funil.append(("Série padrão B3 (descarta W/atípicas)", len(out)))

    # 1) Vencimento (ciclo escolhido no seletor)
    if vencimento is not None and out["vencimento"].notna().any():
        out = out[out["vencimento"] == vencimento]
    funil.append((f"Vencimento {vencimento:%d/%m/%Y}" if vencimento else "Vencimento", len(out)))

    # 2) Frescor da cotação. Opção parada há semanas mantém volume acumulado alto
    # e subiria no ranking de liquidez carregando um Delta calculado de preço
    # velho — o pregão de referência é o mais recente da própria grade.
    if max_atraso_du is not None and "data_neg" in df.columns and df["data_neg"].notna().any():
        referencia = max(d for d in df["data_neg"].dropna())
        atraso = out["data_neg"].map(
            lambda d: dias_uteis(d, referencia) if pd.notna(d) else 10_000
        )
        out = out[atraso <= max_atraso_du]
        funil.append((f"Negociada há ≤ {max_atraso_du} pregão(ões)", len(out)))

    # 3) Delta absoluto na faixa obrigatória
    absd = out["delta"].abs()
    out = out[(absd >= delta_min) & (absd <= delta_max)]
    funil.append((f"|Delta| entre {delta_min:.2f} e {delta_max:.2f}", len(out)))

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

_CSS = """
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 3rem;}
  div[data-testid="stMetricValue"] {font-size: 1.6rem;}
  .rodape {opacity:.65; font-size:.8rem; line-height:1.5;}
</style>
"""


def _painel_lado(titulo: str, df_lado: pd.DataFrame, vazio: str) -> None:
    """Renderiza uma coluna (Calls ou Puts): card da sugestão + top 3."""
    st.markdown(f"### {titulo}")
    if df_lado.empty:
        st.warning(vazio, icon="⚠️")
        return

    melhor = df_lado.iloc[0]
    with st.container(border=True):
        st.metric(
            label=f"🏆 Sugestão do dia · {melhor['ticker']}",
            value=_moeda(melhor["ultimo"]),
            delta=_num(melhor["variacao"], 2, sufixo="%", sinal=True),
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("Strike", _moeda(melhor["strike"]))
        c2.metric("Delta", _num(melhor["delta"], 3, sinal=True))
        c3.metric("Vol. Financeiro", _moeda_compacta(melhor["vol_financeiro"]))

    negocios = 0 if pd.isna(melhor["num_neg"]) else int(melhor["num_neg"])
    situacao = melhor["moneyness"] if pd.notna(melhor["moneyness"]) else "—"
    # A data do último negócio importa: Delta vindo de preço velho não vale nada.
    quando = melhor.get("data_hora")
    quando = f" · últ. neg. {quando}" if quando and pd.notna(quando) else ""
    st.caption(
        f"Escolhida por liquidez: {_num(negocios, 0)} negócios · "
        f"{_moeda_compacta(melhor['vol_financeiro'])} · {situacao} · "
        f"Vol. Impl. {_num(melhor['vol_impl'], 1, sufixo='%')} · "
        f"{melhor['modelo']}{quando}"
    )
    st.dataframe(tabela_exibicao(df_lado.head(TOP_N)), hide_index=True, **_LARGURA)


def _tela_inicial() -> None:
    st.info(
        "**Como começar:** clique em **Buscar no site** na barra lateral. "
        "Se o site bloquear a automação, exporte a grade em CSV/Excel no "
        "opcoes.net.br e use o **upload de backup** — o painel processa igual. "
        "Para só conferir a interface, marque **Modo demonstração**.",
        icon="👈",
    )


def main() -> None:
    st.set_page_config(page_title="Opções Day Trade · B3", page_icon="📈",
                       layout="wide", initial_sidebar_state="expanded")
    st.markdown(_CSS, unsafe_allow_html=True)
    hoje = date.today()

    # ---------------- Sidebar: ativo e fonte de dados --------------------
    with st.sidebar:
        st.markdown("## ⚙️ Painel de controle")
        ativo = st.selectbox("Ativo-objeto", ATIVOS, index=ATIVOS.index("PETR4"))

        st.markdown("#### 1 · Fonte dos dados")
        col_a, col_b = st.columns([3, 2])
        buscar = col_a.button("🔄 Buscar no site", **_LARGURA)
        demo = col_b.checkbox("Demo", value=False,
                              help="Grade sintética (Black-Scholes) para testar a interface.")
        arquivo = st.file_uploader(
            "Upload de backup — CSV/Excel exportado do opcoes.net.br",
            type=["csv", "xlsx", "xls", "xlsm"],
            help="Use quando o scraping for bloqueado ou exigir login.",
        )
        with st.expander("Ajustes avançados"):
            taxa_pct = st.number_input(
                "Taxa livre de risco (% a.a.)", min_value=0.0, max_value=30.0,
                value=10.75, step=0.25,
                help="Entra no Black-Scholes que calcula o Delta. Em opções curtas "
                     "o efeito é pequeno; use a Selic/DI corrente.",
            )
            spot_manual = st.number_input(
                "Preço do ativo (0 = deduzir do site)", min_value=0.0,
                value=0.0, step=0.10,
                help="O painel deduz o spot da coluna 'Distância % do Strike'. "
                     "Preencha só para forçar outro valor.",
            )
            usar_selenium = st.checkbox(
                "Tentar Selenium se o JSON falhar", value=False,
                help="Exige Chrome e a biblioteca selenium instalados.",
            )
            headless = st.checkbox("Chrome em modo headless", value=True)
            st.caption(
                f"Ambiente: `{'WebAssembly' if NO_NAVEGADOR else 'Python local'}` · "
                f"platform `{sys.platform}` · dados via `{URL_JSON}`"
            )

    # ---------------- Carga de dados -------------------------------------
    estado = st.session_state.get("dados")

    if buscar:
        with st.spinner(f"Extraindo a grade de {ativo}…"):
            try:
                df_bruto, rota, log = extrair_automatico(ativo, usar_selenium, headless)
                st.session_state["dados"] = {"df": df_bruto, "fonte": rota, "ativo": ativo,
                                             "ts": datetime.now(), "log": log, "chave": None}
            except Exception as exc:
                st.sidebar.error(f"{exc}", icon="🚫")
    elif demo:
        if not estado or estado.get("fonte") != "Demo" or estado.get("ativo") != ativo:
            st.session_state["dados"] = {"df": grade_demo(ativo, hoje), "fonte": "Demo",
                                         "ativo": ativo, "ts": datetime.now(),
                                         "log": ["🧪 Dados sintéticos"], "chave": None}
    elif arquivo is not None:
        conteudo = arquivo.getvalue()
        chave = f"{arquivo.name}:{len(conteudo)}"
        if not estado or estado.get("chave") != chave:
            try:
                st.session_state["dados"] = {
                    "df": ler_arquivo(arquivo.name, conteudo), "fonte": f"Upload · {arquivo.name}",
                    "ativo": ativo, "ts": datetime.now(),
                    "log": [f"📄 {arquivo.name}"], "chave": chave,
                }
            except Exception as exc:
                st.sidebar.error(f"Falha ao ler o arquivo: {exc}", icon="🚫")

    estado = st.session_state.get("dados")

    # ---------------- Cabeçalho ------------------------------------------
    st.title("📈 Opções Day Trade · B3")
    st.caption(
        f"Filtro obrigatório: vencimento em {DU_MIN_PADRAO}–{DU_MAX_PADRAO} dias úteis · "
        "|Delta| entre 0,50 e 0,70 · ordenação por liquidez."
    )

    if not estado:
        _tela_inicial()
        _rodape()
        return

    # ---------------- Preparação -----------------------------------------
    try:
        df = preparar_dataframe(estado["df"], spot=(spot_manual or None),
                                hoje=hoje, taxa=taxa_pct / 100.0)
    except Exception as exc:
        st.error(f"Não consegui interpretar a grade recebida: {exc}", icon="🚫")
        with st.expander("Dados brutos recebidos"):
            st.dataframe(estado["df"].head(30), **_LARGURA)
        _rodape()
        return

    if estado["fonte"] == "Demo":
        st.warning("**Modo demonstração** — dados sintéticos, não são preços reais.", icon="🧪")

    calculados = int(df["delta_calculado"].sum()) if "delta_calculado" in df.columns else 0
    if calculados:
        st.info(
            f"**Delta calculado pelo painel** em {calculados} de {len(df)} opções — o "
            "opcoes.net.br censura os gregos no plano gratuito (vêm como imagem borrada). "
            "A volatilidade implícita é invertida do preço negociado e o Delta sai por "
            f"Black-Scholes, com ativo a **{_moeda(df.attrs.get('spot'))}** e taxa de "
            f"{_num(taxa_pct, 2, sufixo='%')} a.a.",
            icon="🧮",
        )

    # ---------------- Sidebar: filtros (dependem dos dados) ---------------
    with st.sidebar:
        st.markdown("#### 2 · Filtros")

        vencimentos = sorted({v for v in df["vencimento"].dropna().unique()})
        if not vencimentos:
            vencimentos = calendario_vencimentos(hoje)

        somente_padrao = st.checkbox(
            "Somente séries mensais padrão", value=True,
            help="Aceita apenas RAIZ+LETRA+DÍGITOS (PETRI494) e descarta semanais e "
                 "séries atípicas (PETRI483W4). Aplicado ANTES da ordenação por "
                 "liquidez, para o topo da lista ser sempre um contrato convencional.",
        )

        du_min, du_max = st.slider("Janela de dias úteis até o vencimento", 0, 60,
                                   (DU_MIN_PADRAO, DU_MAX_PADRAO))
        todos = st.checkbox("Mostrar vencimentos fora da janela", value=False)

        # Com a regra estrita ligada, só a 3ª sexta entra na lista.
        candidatos = ([v for v in vencimentos if eh_vencimento_mensal(v)]
                      if somente_padrao else vencimentos)
        no_ciclo = [v for v in candidatos if du_min <= dias_uteis(hoje, v) <= du_max]

        if no_ciclo:
            elegiveis = no_ciclo
        elif todos:
            elegiveis = candidatos or vencimentos
        else:
            elegiveis = []

        vencimento = st.selectbox(
            "Vencimento", elegiveis, index=0 if elegiveis else None,
            format_func=lambda v: (
                f"{v:%d/%m/%Y} · {dias_uteis(hoje, v)} DU · "
                f"{'MENSAL' if eh_vencimento_mensal(v) else 'semanal'}"
            ),
            placeholder="nenhum vencimento elegível",
            help="Semanais têm bem menos liquidez que o mensal, mesmo caindo dentro "
                 "da janela de dias úteis.",
        ) if elegiveis else None
        if vencimento is not None and not eh_vencimento_mensal(vencimento):
            st.caption("⚠️ Vencimento semanal — confira o volume antes de operar.")

        delta_min, delta_max = st.slider("Faixa de |Delta|", 0.05, 0.95,
                                         (DELTA_MIN_PADRAO, DELTA_MAX_PADRAO), step=0.05)
        min_negocios = st.number_input("Mínimo de negócios", min_value=0, value=0, step=50)
        exigir_negocio = st.checkbox("Descartar opções sem negócio", value=True)
        max_atraso = st.slider(
            "Máx. pregões desde o último negócio", 0, 10, 1,
            help="Opção parada há semanas guarda volume acumulado alto e subiria no "
                 "ranking, mas o Delta sairia de um preço velho. 1 = negociada no "
                 "pregão mais recente da grade ou no anterior.",
        )

    # Janela vazia é um resultado legítimo da regra estrita, não um erro: os
    # vencimentos mensais distam ~21 DU entre si, então na semana anterior a cada
    # um deles nenhum cai na faixa. Só depois de montar a barra lateral
    # inteira, para os controles continuarem disponíveis para sair da situação.
    if not elegiveis:
        mensais = [v for v in vencimentos if eh_vencimento_mensal(v)]
        st.warning(
            f"**Nenhum vencimento mensal entre {du_min} e {du_max} dias úteis.** "
            "Os mensais da B3 (3ª sexta) ficam a ~21 DU um do outro, então há alguns "
            "dias por mês em que nenhum cai na janela — é o caso de hoje.",
            icon="📭",
        )
        if mensais:
            st.markdown("Mensais mais próximos:")
            st.dataframe(
                pd.DataFrame([{"Vencimento": f"{v:%d/%m/%Y}",
                               "Dias úteis": dias_uteis(hoje, v)} for v in mensais[:4]]),
                hide_index=True, **_LARGURA,
            )
        st.caption(
            "Saídas: ajuste a janela no slider, marque **Mostrar vencimentos fora da "
            "janela** para escolher na mão, ou desmarque **Somente séries mensais "
            "padrão** para aceitar semanais."
        )
        _rodape()
        return

    # ---------------- Filtros e resultado ---------------------------------
    filtrado, funil = aplicar_filtros(df, vencimento, delta_min, delta_max,
                                      int(min_negocios), exigir_negocio,
                                      max_atraso_du=int(max_atraso),
                                      somente_padrao=somente_padrao)
    calls, puts = separar_calls_puts(filtrado)

    du_venc = dias_uteis(hoje, vencimento) if vencimento else "—"
    # Uma linha só, de propósito: st.columns empilha no celular, e quatro
    # métricas empurrariam as Calls/Puts para fora da primeira tela.
    data_venc = f"{vencimento:%d/%m/%Y} ({du_venc} DU)" if vencimento else "—"
    st.markdown(
        f"**{ativo}** &nbsp;·&nbsp; venc. **{data_venc}** &nbsp;·&nbsp; "
        f"**{len(calls)} calls · {len(puts)} puts** de {len(df)} na grade "
        f"&nbsp;·&nbsp; {estado['fonte']} às {estado['ts']:%H:%M:%S}"
    )
    st.divider()

    col_call, col_put = st.columns(2, gap="large")
    with col_call:
        _painel_lado("🟢 CALLS", calls,
                     "Nenhuma Call com Delta entre "
                     f"{delta_min:.2f} e {delta_max:.2f} neste vencimento.")
    with col_put:
        _painel_lado("🔴 PUTS", puts,
                     "Nenhuma Put com Delta entre "
                     f"-{delta_max:.2f} e -{delta_min:.2f} neste vencimento.")

    # ---------------- Diagnóstico e grade completa -------------------------
    st.divider()
    esq, dir_ = st.columns([1, 2])

    with esq.expander("🔎 Funil de filtragem", expanded=False):
        st.dataframe(pd.DataFrame(funil, columns=["Etapa", "Opções"]),
                     hide_index=True, **_LARGURA)
        for linha in estado.get("log", []):
            st.caption(linha)

    with dir_.expander("📋 Grade completa do vencimento", expanded=False):
        grade = ordenar_por_liquidez(
            df[df["vencimento"] == vencimento] if vencimento is not None else df
        )
        tabela_grade = tabela_exibicao(grade)
        if "data_hora" in grade.columns:
            tabela_grade["Últ. neg."] = grade["data_hora"].astype(str).values
        st.dataframe(tabela_grade, hide_index=True, height=380, **_LARGURA)
        st.download_button(
            "⬇️ Baixar grade filtrada (CSV)",
            data=tabela_exibicao(filtrado).to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name=f"opcoes_{ativo}_{vencimento:%Y%m%d}.csv" if vencimento else f"opcoes_{ativo}.csv",
            mime="text/csv",
        )

    _rodape()


def _rodape() -> None:
    st.markdown(
        '<p class="rodape">Ferramenta de apoio à decisão para uso próprio. '
        "Não constitui recomendação de investimento. Dados do opcoes.net.br "
        "podem ter atraso — confirme preço e liquidez no home broker antes de operar.</p>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
