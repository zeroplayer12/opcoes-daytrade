# -*- coding: utf-8 -*-
"""
Operações — as estratégias do Profit traduzidas para Python.

Cada estratégia recebe os candles do tempo gráfico dela e devolve, candle a
candle, o que o Profit faria: a cor do candle, as ordens e o estado da posição.
A simulação segue a semântica do backtest do Profit:

- o código roda no fechamento de cada candle;
- ordem a mercado é executada na abertura do candle seguinte;
- ordens de saída (limite e stop) enviadas num fechamento valem só para o
  candle seguinte; ordem não reenviada é cancelada. Por isso, no candle em que
  a entrada é executada ainda não há stop — igual ao Profit;
- as variáveis guardam o valor de um candle para o outro.

Quando um candle toca stop e alvo ao mesmo tempo não dá para saber o que veio
primeiro; o motor assume o stop (conservador), a menos que a abertura já tenha
passado de um dos dois.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

BRT = timezone(timedelta(hours=-3), "BRT")
NAN = float("nan")


# =============================================================================
# Indicadores, nas convenções do Profit
# =============================================================================

def media_exp(s: pd.Series, n: int) -> pd.Series:
    """MediaExp: exponencial clássica, fator 2/(n+1)."""
    return s.ewm(span=n, adjust=False).mean()


def media(s: pd.Series, n: int) -> pd.Series:
    """Media: aritmética simples de n candles."""
    return s.rolling(n, min_periods=n).mean()


def _wilder(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1.0 / n, adjust=False).mean()


def adx(h: pd.Series, l: pd.Series, c: pd.Series, n_di: int = 14, n_adx: int = 14) -> pd.Series:
    """ADX de Wilder: DI+ e DI- suavizados em n_di, ADX é a média de Wilder do DX em n_adx."""
    sobe, desce = h.diff(), -l.diff()
    dm_mais = pd.Series(np.where((sobe > desce) & (sobe > 0), sobe, 0.0), index=h.index)
    dm_menos = pd.Series(np.where((desce > sobe) & (desce > 0), desce, 0.0), index=h.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, n_di)
    di_mais = 100 * _wilder(dm_mais, n_di) / atr
    di_menos = 100 * _wilder(dm_menos, n_di) / atr
    dx = 100 * (di_mais - di_menos).abs() / (di_mais + di_menos).replace(0, np.nan)
    return _wilder(dx.fillna(0), n_adx)


def vwap_diario(df: pd.DataFrame) -> pd.Series:
    """VWAP que recomeça a cada pregão, pelo preço típico de cada candle."""
    tipico = (df["high"] + df["low"] + df["close"]) / 3
    dia = pd.Index(df.index.date)
    pv = (tipico * df["volume"]).groupby(dia).cumsum()
    vol = df["volume"].groupby(dia).cumsum()
    return pd.Series((pv / vol.replace(0, np.nan)).to_numpy(), index=df.index)


# =============================================================================
# Motor: ordens, execução e posição
# =============================================================================

@dataclass
class Ordem:
    tipo: str            # "mercado" | "limite" | "stop"
    lado: int            # +1 compra, -1 venda (sentido da ordem, não da posição)
    qtd: float | None    # None = zera a posição inteira
    preco: float = NAN   # limite, ou disparo do stop
    rotulo: str = ""     # "entrada", "parcial", "alvo", "stop", "zeragem"


@dataclass
class Execucao:
    hora: pd.Timestamp
    preco: float
    qtd: float           # com sinal: + compra, - venda
    rotulo: str


@dataclass
class Operacao:
    lado: int                            # +1 comprado, -1 vendido
    hora_sinal: pd.Timestamp
    preco_sinal: float                   # fechamento do candle do sinal (base dos níveis)
    stop_inicial: float
    alvo1: float
    alvo2: float
    execucoes: list[Execucao] = field(default_factory=list)
    stop: float = NAN                    # stop vigente
    parcial_feita: bool = False
    stop_movido: bool = False

    @property
    def qtd(self) -> float:
        return sum(e.qtd for e in self.execucoes)

    @property
    def entrada(self) -> Execucao | None:
        return next((e for e in self.execucoes if e.rotulo == "entrada"), None)

    @property
    def aberta(self) -> bool:
        return self.entrada is None or abs(self.qtd) > 1e-9

    def resultado(self, preco_atual: float | None = None) -> float:
        """R$ realizado mais o que está aberto marcado a `preco_atual`."""
        caixa = -sum(e.preco * e.qtd for e in self.execucoes)
        if abs(self.qtd) > 1e-9 and preco_atual is not None:
            caixa += self.qtd * preco_atual
        return caixa


def _preenche(ordem: Ordem, o: float, h: float, l: float) -> float | None:
    """Preço de execução da ordem no candle (o, h, l), ou None se não executa."""
    if ordem.tipo == "mercado":
        return o
    if ordem.tipo == "limite":
        if ordem.lado < 0 and h >= ordem.preco:
            return max(o, ordem.preco)
        if ordem.lado > 0 and l <= ordem.preco:
            return min(o, ordem.preco)
        return None
    if ordem.tipo == "stop":
        if ordem.lado < 0 and l <= ordem.preco:
            return min(o, ordem.preco)
        if ordem.lado > 0 and h >= ordem.preco:
            return max(o, ordem.preco)
        return None
    raise ValueError(ordem.tipo)


def _prioridade(ordem: Ordem, o: float) -> int:
    """Ordem de execução dentro do candle: o que já é executável na abertura vai
    primeiro; depois o stop (hipótese conservadora), depois a parcial e o alvo."""
    na_abertura = _preenche(ordem, o, o, o) is not None
    base = {"mercado": 0, "stop": 1, "parcial": 2, "alvo": 3}.get(ordem.rotulo, 2)
    return (0 if na_abertura else 10) + base


# =============================================================================
# Estratégias
# =============================================================================

class Estrategia:
    """Base: subclasses calculam os indicadores e decidem no fechamento."""
    ativo = ""
    nome = ""
    minutos = 10
    lote = 100
    ultimo_candle = "16:50"   # abertura do último candle do pregão (horário de verão dos EUA)

    def indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def no_fechamento(self, barra: pd.Series, op: Operacao | None) -> tuple[list[Ordem], str | None, Operacao | None]:
        """Devolve (ordens para o próximo candle, cor do candle, operação nova se houver sinal)."""
        raise NotImplementedError


class Vale3(Estrategia):
    """Estratégia de Execução VALE3 (Profit), 10 minutos.

    Médias 9 > 21 > 50 e preço acima da VWAP para compra (o inverso para venda),
    volume acima da média de 20, ADX(14,14) > 20, rompimento da máxima dos dois
    candles anteriores com candle de alta, e stop (mínima de 3 candles − 0,03)
    a no máximo 1,60% do preço. Parcial de 50% em 1R com stop no zero a zero,
    alvo final em 1,85R, breakeven em 1,5R.
    """
    ativo, nome, minutos, lote = "VALE3", "Execução VALE3", 10, 100

    def __init__(self, risco_retorno: float = 1.85, breakeven: bool = True, max_stop_pct: float = 1.60):
        self.rr, self.breakeven, self.max_stop = risco_retorno, breakeven, max_stop_pct

    def indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        c, h, l = df["close"], df["high"], df["low"]
        x = pd.DataFrame(index=df.index)
        x["ema9"], x["ema21"], x["ema50"] = media_exp(c, 9), media_exp(c, 21), media_exp(c, 50)
        x["vwap"] = vwap_diario(df)
        x["vol_media"] = media(df["volume"], 20)
        x["adx"] = adx(h, l, c, 14, 14)
        x["stop_compra"] = l.rolling(3).min() - 0.03
        x["stop_venda"] = h.rolling(3).max() + 0.03
        alta = (x["ema9"] > x["ema21"]) & (x["ema21"] > x["ema50"]) & (c > x["vwap"])
        baixa = (x["ema9"] < x["ema21"]) & (x["ema21"] < x["ema50"]) & (c < x["vwap"])
        filtros = (df["volume"] > x["vol_media"]) & (x["adx"] > 20)
        stop_ok_c = (c - x["stop_compra"]) / c <= self.max_stop / 100
        stop_ok_v = (x["stop_venda"] - c) / c <= self.max_stop / 100
        x["sinal_compra"] = alta & filtros & stop_ok_c & (c > h.rolling(2).max().shift(1)) & (c > df["open"])
        x["sinal_venda"] = baixa & filtros & stop_ok_v & (c < l.rolling(2).min().shift(1)) & (c < df["open"])
        return x

    def no_fechamento(self, b: pd.Series, op: Operacao | None):
        ordens: list[Ordem] = []
        cor, nova = None, None
        if op is None:
            # 6. entrada e coloração
            if b["sinal_compra"] or b["sinal_venda"]:
                lado = 1 if b["sinal_compra"] else -1
                cor = "verde" if lado > 0 else "vermelho"
                entrada = b["close"]
                stop = b["stop_compra"] if lado > 0 else b["stop_venda"]
                risco = (entrada - stop) * lado
                nova = Operacao(lado, b.name, entrada, stop, entrada + lado * risco,
                                entrada + lado * risco * self.rr, stop=stop)
                ordens.append(Ordem("mercado", lado, self.lote, rotulo="entrada"))
            return ordens, cor, nova

        # 7. gerenciamento: parcial de 50% em 1R, breakeven em 1,5R, alvo e stop
        lado, entrada = op.lado, op.preco_sinal
        risco = (entrada - op.stop_inicial) * lado
        if not op.parcial_feita and (b["close"] - op.alvo1) * lado >= 0:
            ordens.append(Ordem("limite", -lado, abs(op.qtd) / 2, op.alvo1, "parcial"))
            op.stop, op.stop_movido, op.parcial_feita = entrada + 0.01 * lado, True, True
        if self.breakeven and not op.stop_movido and (b["close"] - (entrada + lado * risco * 1.5)) * lado >= 0:
            op.stop, op.stop_movido = entrada + 0.01 * lado, True
        ordens.append(Ordem("limite", -lado, None, op.alvo2, "alvo"))
        ordens.append(Ordem("stop", -lado, None, op.stop, "stop"))
        return ordens, cor, nova


ESTRATEGIAS: dict[str, Estrategia] = {"VALE3": Vale3()}


# =============================================================================
# Simulação candle a candle
# =============================================================================

@dataclass
class Resultado:
    estrategia: Estrategia
    candles: pd.DataFrame                 # OHLCV + indicadores + cor
    operacoes: list[Operacao]             # fechadas e a aberta, em ordem
    aberta: Operacao | None               # posição em andamento no fim dos dados
    pendentes: list[Ordem]                # ordens enviadas no último fechamento


def simular(est: Estrategia, df: pd.DataFrame, zerar_no_fim_do_dia: bool = True,
            candle_em_formacao: pd.Series | None = None) -> Resultado:
    """Roda a estratégia sobre candles fechados.

    `candle_em_formacao` (o, h, l, c até agora) só executa as ordens pendentes —
    o código da estratégia não roda nele, porque ele ainda não fechou.
    Com `zerar_no_fim_do_dia`, a posição é zerada no fechamento do último candle
    de cada pregão e não se abre entrada nesse candle (day trade).
    """
    x = est.indicadores(df)
    dados = df.join(x)
    dados["cor"] = None
    dias = pd.Series(dados.index.date, index=dados.index)
    ultimo_do_dia = dias.ne(dias.shift(-1))
    # o último candle dos dados só fecha o pregão se já for o candle de encerramento;
    # num pregão em andamento, ele é só o mais recente
    if len(dados) and dados.index[-1].strftime("%H:%M") < est.ultimo_candle:
        ultimo_do_dia.iloc[-1] = False
    operacoes: list[Operacao] = []
    op: Operacao | None = None
    pendentes: list[Ordem] = []
    nova_pendente: Operacao | None = None

    def executa(ordens: list[Ordem], hora, o: float, h: float, l: float) -> None:
        nonlocal op, nova_pendente
        for ordem in sorted(ordens, key=lambda od: _prioridade(od, o)):
            if ordem.rotulo == "entrada":
                if nova_pendente is None:
                    continue
                preco = _preenche(ordem, o, h, l)
                op = nova_pendente
                op.execucoes.append(Execucao(hora, preco, ordem.lado * ordem.qtd, "entrada"))
                operacoes.append(op)
                nova_pendente = None
                continue
            if op is None or not op.aberta or abs(op.qtd) < 1e-9:
                continue
            preco = _preenche(ordem, o, h, l)
            if preco is None:
                continue
            qtd = abs(op.qtd) if ordem.qtd is None else min(ordem.qtd, abs(op.qtd))
            op.execucoes.append(Execucao(hora, preco, ordem.lado * qtd, ordem.rotulo))
            if abs(op.qtd) < 1e-9:
                op = None

    for i, (hora, b) in enumerate(dados.iterrows()):
        # 1) ordens do fechamento anterior valem neste candle
        executa(pendentes, hora, b["open"], b["high"], b["low"])
        pendentes = []
        # 2) o código da estratégia roda no fechamento
        fim_do_dia = zerar_no_fim_do_dia and bool(ultimo_do_dia.iloc[i])
        if fim_do_dia and op is not None:
            op.execucoes.append(Execucao(hora, b["close"], -op.qtd, "zeragem"))
            op = None
            continue
        ordens, cor, nova = est.no_fechamento(b, op)
        if nova is not None and fim_do_dia:
            continue          # day trade: não abre posição no último candle do pregão
        if cor:
            dados.at[hora, "cor"] = cor
        if nova is not None:
            nova_pendente = nova
        pendentes = ordens

    if candle_em_formacao is not None and pendentes:
        f = candle_em_formacao
        executa(pendentes, f.name, f["open"], f["high"], f["low"])
    return Resultado(est, dados, operacoes, op if (op is not None and op.aberta) else None, pendentes)


def resumo(res: Resultado) -> pd.DataFrame:
    """Uma linha por operação, para conferir contra a lista de operações do Profit."""
    linhas = []
    for op in res.operacoes:
        ent = op.entrada
        saidas = [e for e in op.execucoes if e.rotulo != "entrada"]
        linhas.append({
            "lado": "compra" if op.lado > 0 else "venda",
            "sinal": op.hora_sinal, "entrada": ent.hora if ent else None,
            "preço entrada": ent.preco if ent else NAN,
            "saídas": " · ".join(f"{e.rotulo} {e.hora:%H:%M} {e.preco:.2f}×{abs(e.qtd):g}" for e in saidas),
            "resultado R$": round(op.resultado(), 2) if not op.aberta else NAN,
            "aberta": op.aberta,
        })
    return pd.DataFrame(linhas)


# =============================================================================
# Candles
# =============================================================================

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/128.0 Safari/537.36", "Accept": "application/json"}


def barras_5min(ativo: str, faixa: str = "5d") -> pd.DataFrame:
    """Barras de 5 min do Yahoo (atraso de ~15 min), em horário de Brasília.

    Fonte provisória: quando a exportação RTD do Profit estiver ligada, os
    candles passam a vir dela, em tempo real.
    """
    import requests
    resp = requests.get(YAHOO_CHART.format(simbolo=f"{ativo}.SA"), params={"range": faixa, "interval": "5m"},
                        headers=_UA, timeout=20)
    resp.raise_for_status()
    r = resp.json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")},
                      index=pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert(BRT))
    return df.dropna(subset=["open", "high", "low", "close"])


def situacao(est: Estrategia, barras_5m: pd.DataFrame,
             agora: datetime | None = None) -> tuple[Resultado, pd.Series | None]:
    """Estado da estratégia agora: simula os candles fechados e passa o candle em
    formação só pelas ordens pendentes. Fora do pregão, todos os candles contam
    como fechados. Devolve (resultado, candle em formação ou None)."""
    agora = agora or datetime.now(BRT)
    candles = candles_de_5min(barras_5m, est.minutos)
    if len(candles) < 60:
        raise ValueError(f"só {len(candles)} candles de {est.minutos} min; os indicadores precisam de mais")
    inicio = candles.index[-1]
    em_pregao = inicio.date() == agora.date() and agora.strftime("%H:%M") < "17:10"
    if em_pregao:
        formando = candles.iloc[-1]
        return simular(est, candles.iloc[:-1], candle_em_formacao=formando), formando
    return simular(est, candles), None


def candles_de_5min(barras_5m: pd.DataFrame, minutos: int) -> pd.DataFrame:
    """Agrupa barras de 5 min no tempo gráfico da estratégia, alinhado ao pregão (10:00, 10:10…)."""
    if minutos == 5:
        return barras_5m
    regra = f"{minutos}min"
    agrup = barras_5m.resample(regra, label="left", closed="left", origin="start_day")
    df = pd.DataFrame({"open": agrup["open"].first(), "high": agrup["high"].max(),
                       "low": agrup["low"].min(), "close": agrup["close"].last(),
                       "volume": agrup["volume"].sum()})
    return df.dropna(subset=["open", "close"])
