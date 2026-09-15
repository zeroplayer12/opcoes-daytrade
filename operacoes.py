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
- as variáveis guardam o valor de um candle para o outro;
- a posição segue de um pregão para o outro até o alvo ou o stop: as
  estratégias não zeram no fim do dia.

Quando um candle toca stop e alvo ao mesmo tempo não dá para saber o que veio
primeiro; o motor assume o stop (conservador), a menos que a abertura já tenha
passado de um dos dois.
"""
from __future__ import annotations

import copy
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
    # ADX(14, 0): sem suavização, o ADX é o próprio DX (a conferir contra o Profit)
    return dx.fillna(0) if n_adx == 0 else _wilder(dx.fillna(0), n_adx)


def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    """RSI (IFR) de Wilder, o clássico; RSI(14, 0) no Profit (tipo 0 a conferir)."""
    d = c.diff()
    ganho = _wilder(d.clip(lower=0), n)
    perda = _wilder((-d).clip(lower=0), n)
    return (100 - 100 / (1 + ganho / perda.replace(0, np.nan))).fillna(100)


def rsi_simples(c: pd.Series, n: int = 14) -> pd.Series:
    """RSI com médias aritméticas de ganhos e perdas (variante de Cutler)."""
    d = c.diff()
    ganho = media(d.clip(lower=0), n)
    perda = media((-d).clip(lower=0), n)
    return (100 - 100 / (1 + ganho / perda.replace(0, np.nan))).fillna(100)


def adx_simples(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    """ADX com médias aritméticas: DI+ e DI- pela média de n do DM e do TR, ADX pela média de n do DX."""
    sobe, desce = h.diff(), -l.diff()
    dm_mais = pd.Series(np.where((sobe > desce) & (sobe > 0), sobe, 0.0), index=h.index)
    dm_menos = pd.Series(np.where((desce > sobe) & (desce > 0), desce, 0.0), index=h.index)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = media(tr, n)
    di_mais, di_menos = 100 * media(dm_mais, n) / atr, 100 * media(dm_menos, n) / atr
    dx = 100 * (di_mais - di_menos).abs() / (di_mais + di_menos).replace(0, np.nan)
    return media(dx.fillna(0), n)


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
    limite: float = NAN  # stop-limite: pior preço aceito depois do disparo


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
    """Preço de execução da ordem no candle (o, h, l), ou None se não executa.

    Como no backtest do Profit (conferido nas listas de operações):
    - limite: se o candle abre além do preço, a favor, sai na abertura (na BPAC11, alvos
      de 1,3% saíram com 2 a 3% depois de gap); senão, sai no próprio preço se o candle
      chegar nele;
    - stop-limite (VALE3 e PETR4 até 15/09/2026): sai no disparo quando o candle chega
      nele; num gap além do disparo, sai na abertura se ela estiver dentro do limite
      (PETR4, 24/06/2026: stop 37,85, abertura 37,83, saída 37,83); se abriu além do
      limite, só sai se o preço voltar — no próprio disparo, se o candle chegar nele
      (VALE3, 07/08/2026: stop 73,82 num candle que abriu a 74,08, saída 73,82), ou no limite.
    """
    if ordem.tipo == "mercado":
        return o
    p = ordem.preco
    if ordem.tipo == "limite":
        if (o >= p) if ordem.lado < 0 else (o <= p):
            return o
        return p if l <= p <= h else None
    if ordem.tipo == "stop":
        lim, venda = ordem.limite, ordem.lado < 0
        if o < p if venda else o > p:                      # abriu além do disparo
            if math.isnan(lim) or (o >= lim if venda else o <= lim):
                return o
            if l <= p <= h:
                return p
            return lim if l <= lim <= h else None
        return p if l <= p <= h else None
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
    lote_padrao = 100         # lote da B3: ordem que não é múltiplo dele não executa
    parcial_no_profit = True  # False: a parcial do código é de 50 ações e não executa
    stop_mercado = True       # VALE3/PETR4: False reproduz o stop-limite do código antigo
    folga_stop = 0.05         # stop-limite: distância do limite ao disparo (código antigo: 0,05)
    ultimo_candle = "16:50"   # abertura do último candle do pregão (horário de verão dos EUA)
    zera_no_fim_do_dia = False  # as estratégias dele carregam a posição de um dia para o outro
    breakeven = True

    def indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def reiniciar(self) -> None:
        """Zera as variáveis que o NTSL guarda de um candle para o outro (fora da posição)."""

    def no_fechamento(self, barra: pd.Series, op: Operacao | None) -> tuple[list[Ordem], str | None, Operacao | None]:
        """Devolve (ordens para o próximo candle, cor do candle, operação nova se houver sinal)."""
        raise NotImplementedError

    # ---- blocos que as estratégias dele repetem -------------------------------
    def _entrada(self, b: pd.Series, lado: int, stop: float, fator_parcial: float, rr_final: float):
        """Seção 6 do NTSL: pinta o candle, entra a mercado e fixa stop, parcial e alvo."""
        entrada = b["close"]
        risco = (entrada - stop) * lado
        op = Operacao(lado, b.name, entrada, stop, entrada + lado * risco * fator_parcial,
                      entrada + lado * risco * rr_final, stop=stop)
        return [Ordem("mercado", lado, self.lote, rotulo="entrada")], ("verde" if lado > 0 else "vermelho"), op

    def _gerenciar(self, b: pd.Series, op: Operacao, nivel_breakeven: float) -> list[Ordem]:
        """Seção 7 do NTSL: parcial de 50% na alvo 1 com stop no zero a zero, breakeven num
        múltiplo do risco e, a cada candle, o alvo final. O stop sai a mercado na abertura
        do candle seguinte ao toque, testado antes de a parcial ou o breakeven moverem o
        stop (código atualizado em 15/09/2026; com `stop_mercado=False`, o stop-limite de
        0,05 do código antigo, que não executava em gap maior que 0,05).

        A parcial é de metade do lote (50 ações), fora do lote padrão de 100 da B3: no
        Profit ela não executa, e o que sobra dela é o stop no zero a zero."""
        lado, entrada = op.lado, op.preco_sinal
        risco = (entrada - op.stop_inicial) * lado
        if self.stop_mercado and (b["low"] <= op.stop if lado > 0 else b["high"] >= op.stop):
            return [Ordem("mercado", -lado, None, rotulo="stop")]
        ordens = []
        if not op.parcial_feita and (b["close"] - op.alvo1) * lado >= 0:
            ordens.append(Ordem("limite", -lado, abs(op.qtd) / 2, op.alvo1, "parcial"))
            op.stop, op.stop_movido, op.parcial_feita = entrada + 0.01 * lado, True, True
        elif self.breakeven and not op.stop_movido and \
                (b["close"] - (entrada + lado * risco * nivel_breakeven)) * lado >= 0:
            op.stop, op.stop_movido = entrada + 0.01 * lado, True
        ordens.append(Ordem("limite", -lado, None, op.alvo2, "alvo"))
        if not self.stop_mercado:
            ordens.append(Ordem("stop", -lado, None, op.stop, "stop", limite=op.stop - self.folga_stop * lado))
        return ordens


class Vale3(Estrategia):
    """Estratégia de Execução VALE3 (Profit), 10 minutos.

    Médias 9 > 21 > 50 e preço acima da VWAP para compra (o inverso para venda),
    volume acima da média de 20, ADX(14,14) > 20, rompimento da máxima dos dois
    candles anteriores com candle de alta, e stop (mínima de 3 candles − 0,03)
    a no máximo 1,60% do preço. Parcial de 50% em 1R com stop no zero a zero,
    alvo final em 2,5R (RiscoRetorno: o código traz 1,85, mas a lista de operações do
    Profit só bate com 2,5), breakeven em 1,5R. No Profit a parcial não executa (50 ações
    fica fora do lote padrão de 100); o fechamento além de 1R só leva o stop ao zero a zero.
    """
    ativo, nome, minutos, lote = "VALE3", "Execução VALE3", 10, 100
    parcial_no_profit = False

    def __init__(self, risco_retorno: float = 2.50, breakeven: bool = True, max_stop_pct: float = 1.60):
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
        if op is None:
            if b["sinal_compra"] or b["sinal_venda"]:
                lado = 1 if b["sinal_compra"] else -1
                return self._entrada(b, lado, b["stop_compra"] if lado > 0 else b["stop_venda"], 1.0, self.rr)
            return [], None, None
        return self._gerenciar(b, op, 1.5), None, None


class Petr4(Estrategia):
    """Estratégia de Execução PETR4 (Profit), 20 minutos.

    A tendência da VALE3 (médias 9 > 21 > 50 e VWAP) mais IFR entre 55 e 80
    (compra) ou 20 e 45 (venda), MACD 12/26 acima ou abaixo do sinal de 9,
    média de 9 subindo ou descendo, ADX(14, 0) > 20, candle com corpo maior que
    5% da amplitude média de 14, volume acima da média de 20 e stop (extremo de
    4 candles ± 0,03) a no máximo 1,60%. Parcial de 50% em 1,5R, alvo final em
    2,9R, breakeven em 1,5R. Mesma gestão da VALE3: no Profit a parcial não executa.
    """
    ativo, nome, minutos, lote = "PETR4", "Execução PETR4", 20, 100
    parcial_no_profit = False
    ultimo_candle = "16:40"

    def __init__(self, rr_final: float = 2.90, fator_parcial: float = 1.50, filtro_amplitude: float = 0.05,
                 breakeven: bool = True, adx_min: float = 20.0, max_stop_pct: float = 1.60,
                 adx_modo: str = "dx", rsi_modo: str = "wilder"):
        self.rr, self.fator_parcial, self.amplitude = rr_final, fator_parcial, filtro_amplitude
        self.breakeven, self.adx_min, self.max_stop = breakeven, adx_min, max_stop_pct
        self.adx_modo, self.rsi_modo = adx_modo, rsi_modo

    def indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        c, h, l, o = df["close"], df["high"], df["low"], df["open"]
        x = pd.DataFrame(index=df.index)
        x["ema9"], x["ema21"], x["ema50"] = media_exp(c, 9), media_exp(c, 21), media_exp(c, 50)
        x["vwap"] = vwap_diario(df)
        x["vol_media"] = media(df["volume"], 20)
        x["rsi"] = rsi_simples(c, 14) if self.rsi_modo == "simples" else rsi(c, 14)
        if self.adx_modo == "simples":
            x["adx"] = adx_simples(h, l, c, 14)
        else:
            x["adx"] = adx(h, l, c, 14, 14 if self.adx_modo == "wilder" else 0)
        x["macd"] = media_exp(c, 12) - media_exp(c, 26)
        x["macd_sinal"] = media_exp(x["macd"], 9)
        alta = (x["ema9"] > x["ema21"]) & (x["ema21"] > x["ema50"]) & (c > x["vwap"])
        baixa = (x["ema9"] < x["ema21"]) & (x["ema21"] < x["ema50"]) & (c < x["vwap"])
        filtros = (df["volume"] > x["vol_media"]) & ((c - o).abs() > media(h - l, 14) * self.amplitude) \
            & (x["adx"] > self.adx_min)
        x["stop_compra"] = l.rolling(4).min() - 0.03
        x["stop_venda"] = h.rolling(4).max() + 0.03
        stop_ok_c = (c - x["stop_compra"]) / c <= self.max_stop / 100
        stop_ok_v = (x["stop_venda"] - c) / c <= self.max_stop / 100
        x["sinal_compra"] = (alta & filtros & stop_ok_c & (x["rsi"] > 55) & (x["rsi"] < 80)
                             & (x["macd"] > x["macd_sinal"]) & (x["ema9"] > x["ema9"].shift(1))
                             & (c > h.rolling(2).max().shift(1)) & (c > o))
        x["sinal_venda"] = (baixa & filtros & stop_ok_v & (x["rsi"] < 45) & (x["rsi"] > 20)
                            & (x["macd"] < x["macd_sinal"]) & (x["ema9"] < x["ema9"].shift(1))
                            & (c < l.rolling(2).min().shift(1)) & (c < o))
        return x

    def no_fechamento(self, b: pd.Series, op: Operacao | None):
        if op is None:
            if b["sinal_compra"] or b["sinal_venda"]:
                lado = 1 if b["sinal_compra"] else -1
                return self._entrada(b, lado, b["stop_compra"] if lado > 0 else b["stop_venda"],
                                     self.fator_parcial, self.rr)
            return [], None, None
        return self._gerenciar(b, op, self.fator_parcial), None, None


class Pullback(Estrategia):
    """Família "pullback na média de 9" (BPAC11, BBAS3).

    Tendência (fechamento acima da média de 50 e média de 9 acima da de 21, ou o
    inverso), pullback (a mínima deste candle ou do anterior tocou a média de 9;
    na venda, a máxima), volume acima da média de 20, candle a favor rompendo a
    máxima (mínima) do anterior e entrada só em candle até a hora limite. Alvo
    fixo em %, sem parcial nem breakeven; stop técnico no extremo dos últimos N
    candles, com folga (3 candles ± 0,03 na BPAC11 e na BBAS3; 2 ± 0,05 no BOVA11).
    Filtros opcionais: tamanho máximo do stop (%) e força da tendência
    (distância entre as médias de 9 e 21 maior que uma fração do preço).

    O stop não é ordem stop: se a mínima (na venda, a máxima) de um candle toca o
    nível, a posição sai a mercado na abertura do candle seguinte. A saída das
    17:40 do código compara Time (HHMM) com 174000 e nunca dispara, então a
    posição segue para o dia seguinte, como nas outras estratégias.
    """

    def __init__(self, alvo_pct: float, hora_limite: int, max_stop_pct: float | None = None,
                 filtro_forca: float | None = None, stop_candles: int = 3, stop_folga: float = 0.03):
        self.alvo_pct, self.hora_limite = alvo_pct, hora_limite
        self.max_stop, self.filtro_forca = max_stop_pct, filtro_forca
        self.stop_candles, self.stop_folga = stop_candles, stop_folga

    def indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        c, h, l, o = df["close"], df["high"], df["low"], df["open"]
        x = pd.DataFrame(index=df.index)
        x["ema9"], x["ema21"], x["ema50"] = media_exp(c, 9), media_exp(c, 21), media_exp(c, 50)
        x["vol_media"] = media(df["volume"], 20)
        alta = (c > x["ema50"]) & (x["ema9"] > x["ema21"])
        baixa = (c < x["ema50"]) & (x["ema9"] < x["ema21"])
        pullback_alta = (l <= x["ema9"]) | (l.shift(1) <= x["ema9"])
        pullback_baixa = (h >= x["ema9"]) | (h.shift(1) >= x["ema9"])
        comum = df["volume"] > x["vol_media"]
        if self.filtro_forca:
            comum &= (x["ema9"] - x["ema21"]).abs() > c * self.filtro_forca
        # Time do NTSL: HHMM do candle (abertura) — a conferir contra o Profit
        comum &= pd.Series(df.index.hour * 100 + df.index.minute, index=df.index) <= self.hora_limite
        x["stop_compra"] = l.rolling(self.stop_candles).min() - self.stop_folga
        x["stop_venda"] = h.rolling(self.stop_candles).max() + self.stop_folga
        ok_c = ok_v = True
        if self.max_stop:
            ok_c = (c - x["stop_compra"]) / c <= self.max_stop / 100
            ok_v = (x["stop_venda"] - c) / c <= self.max_stop / 100
        x["sinal_compra"] = alta & pullback_alta & comum & ok_c & (c > o) & (c > h.shift(1))
        x["sinal_venda"] = baixa & pullback_baixa & comum & ok_v & (c < o) & (c < l.shift(1))
        return x

    def no_fechamento(self, b: pd.Series, op: Operacao | None):
        if op is None:
            if b["sinal_compra"] or b["sinal_venda"]:
                lado = 1 if b["sinal_compra"] else -1
                stop = b["stop_compra"] if lado > 0 else b["stop_venda"]
                alvo = b["close"] * (1 + lado * self.alvo_pct / 100)
                nova = Operacao(lado, b.name, b["close"], stop, NAN, alvo, stop=stop)
                return ([Ordem("mercado", lado, self.lote, rotulo="entrada")],
                        "verde" if lado > 0 else "vermelho", nova)
            return [], None, None
        ordens = [Ordem("limite", -op.lado, None, op.alvo2, "alvo")]
        tocou = b["low"] <= op.stop if op.lado > 0 else b["high"] >= op.stop
        if tocou:
            ordens.append(Ordem("mercado", -op.lado, None, rotulo="stop"))
        return ordens, None, None


class Bpac11(Pullback):
    """Pullback BPAC11 (Profit), 15 min: alvo de 1,3%, stop limitado a 1,65%, entrada até 16:30."""
    ativo, nome, minutos, lote = "BPAC11", "Pullback BPAC11", 15, 100
    ultimo_candle = "16:45"

    def __init__(self, alvo_pct: float = 1.3, max_stop_pct: float = 1.65, hora_limite: int = 1630):
        super().__init__(alvo_pct, hora_limite, max_stop_pct=max_stop_pct)


class Bbas3(Pullback):
    """Pullback BBAS3 (Profit), 10 min: alvo de 1,5%, força da tendência acima de 0,35% do preço
    (FiltroForcaMult 0,0035 no Profit, confirmado; o código enviado traz 0,0025),
    entrada até 14:00 e sem filtro de tamanho do stop. O código do Profit não pinta o candle do
    sinal; aqui ele é pintado mesmo assim, para o gráfico mostrar onde a operação começou."""
    ativo, nome, minutos, lote = "BBAS3", "Pullback BBAS3", 10, 100

    def __init__(self, alvo_pct: float = 1.5, filtro_forca: float = 0.0035, hora_limite: int = 1400):
        super().__init__(alvo_pct, hora_limite, filtro_forca=filtro_forca)


class Bova11(Pullback):
    """Pullback BOVA11 (Profit), 60 min: alvo de 2,3%, stop na mínima (máxima) dos 2 últimos candles
    ± 0,05 limitado a 1,85%, força da tendência acima de 0,15% do preço e entrada até o candle das
    16:00 (o último do pregão, então a entrada pode ficar para a abertura do dia seguinte). O código
    do Profit não pinta o candle do sinal; aqui ele é pintado mesmo assim."""
    ativo, nome, minutos, lote = "BOVA11", "Pullback BOVA11", 60, 100
    ultimo_candle = "16:00"

    def __init__(self, alvo_pct: float = 2.30, max_stop_pct: float = 1.85, filtro_forca: float = 0.0015,
                 hora_limite: int = 1600):
        super().__init__(alvo_pct, hora_limite, max_stop_pct=max_stop_pct, filtro_forca=filtro_forca,
                         stop_candles=2, stop_folga=0.05)


class Itub4(Estrategia):
    """Estratégia ITUB4 15 min (Profit): só compra, no máximo uma operação por dia.

    Tendência (fechamento acima das médias de 200 e de 50, média de 9 acima da de
    21), pullback profundo (a mínima tocou a média de 9 sem perder 0,2% abaixo da
    de 21), IFR(14) entre 50 e 68, fechamento no terço superior do candle (65% da
    amplitude), volume acima da média de 20, true range acima de 105% da sua média
    de 20, candle de alta rompendo a máxima do anterior e entrada só até o candle
    das 13:00. Stop na mínima de 4 candles − 0,02, com risco de até 1,40%.

    Gestão: parcial de 50 ações em 2R; quando a máxima de um candle toca a 2R, o
    stop vai para o zero a zero e o alvo final (4,4R) entra. O stop sai a mercado
    no candle seguinte ao toque. A parcial é de 50 ações, fora do lote padrão de 100:
    no Profit ela nunca executa (nas 3 operações do backtest, as 100 ações saem no alvo
    final), e o toque na 2R só leva o stop ao zero a zero e liga o alvo final.

    O toque no stop é testado antes de a parcial mover o stop (código corrigido em
    15/09/2026). O código antigo movia primeiro: se o candle que chegava na 2R tinha
    passado pela entrada — o próprio candle de entrada, por exemplo —, zerava na
    abertura seguinte. `stop_antes_do_breakeven=False` reproduz o antigo.
    """
    ativo, nome, minutos, lote = "ITUB4", "Execução ITUB4", 15, 100
    parcial_no_profit = False
    stop_antes_do_breakeven = True
    ultimo_candle = "16:45"

    def __init__(self, fator_parcial: float = 2.00, fator_alvo: float = 4.40, max_stop_pct: float = 1.40,
                 min_stop_pct: float = 0.0, hora_limite: int = 1300, qtd_parcial: int = 50):
        self.fator_parcial, self.fator_alvo = fator_parcial, fator_alvo
        self.max_stop, self.min_stop, self.hora_limite, self.qtd_parcial = max_stop_pct, min_stop_pct, hora_limite, qtd_parcial
        self.reiniciar()

    def reiniciar(self) -> None:
        self._dia, self._ja_operou = None, False

    def indicadores(self, df: pd.DataFrame) -> pd.DataFrame:
        c, h, l, o = df["close"], df["high"], df["low"], df["open"]
        x = pd.DataFrame(index=df.index)
        x["ema9"], x["ema21"], x["ema50"] = media_exp(c, 9), media_exp(c, 21), media_exp(c, 50)
        x["ema200"] = media_exp(c, 200)
        x["rsi"] = rsi(c, 14)
        x["vol_media"] = media(df["volume"], 20)
        x["tr"] = pd.concat([h, c.shift()], axis=1).max(axis=1) - pd.concat([l, c.shift()], axis=1).min(axis=1)
        x["tr_media"] = media(x["tr"], 20)
        tendencia = (c > x["ema200"]) & (c > x["ema50"]) & (x["ema9"] > x["ema21"])
        pullback = (l <= x["ema9"]) & (l >= x["ema21"] * 0.998)
        amplitude = h - l
        fecha_alto = ((c - l) / amplitude.where(amplitude > 0)) >= 0.65
        x["stop_compra"] = l.rolling(4).min() - 0.02
        risco_pct = (c - x["stop_compra"]) / c * 100
        # Time do NTSL: HHMM do candle (abertura) — a conferir contra o Profit
        no_horario = pd.Series(df.index.hour * 100 + df.index.minute, index=df.index) <= self.hora_limite
        x["sinal_compra"] = (tendencia & pullback & (x["rsi"] >= 50) & (x["rsi"] <= 68) & fecha_alto.fillna(False)
                             & (df["volume"] > x["vol_media"]) & (x["tr"] > x["tr_media"] * 1.05)
                             & (risco_pct <= self.max_stop) & (risco_pct >= self.min_stop)
                             & (c > o) & (c > h.shift(1)) & no_horario)
        x["sinal_venda"] = False
        return x

    def no_fechamento(self, b: pd.Series, op: Operacao | None):
        if b.name.date() != self._dia:                       # controle de 1 operação por dia
            self._dia, self._ja_operou = b.name.date(), False
        if op is None:
            if b["sinal_compra"] and not self._ja_operou:
                entrada, stop = b["close"], b["stop_compra"]
                risco = entrada - stop
                nova = Operacao(1, b.name, entrada, stop, entrada + risco * self.fator_parcial,
                                entrada + risco * self.fator_alvo, stop=stop)
                self._ja_operou = True
                return [Ordem("mercado", 1, self.lote, rotulo="entrada")], "verde", nova
            return [], None, None
        if self.stop_antes_do_breakeven and b["low"] <= op.stop:
            return [Ordem("mercado", -1, None, rotulo="stop")], None, None
        ordens = []
        if not op.parcial_feita:
            ordens.append(Ordem("limite", -1, self.qtd_parcial, op.alvo1, "parcial"))
            if b["high"] >= op.alvo1:
                op.parcial_feita, op.stop, op.stop_movido = True, op.preco_sinal + 0.01, True
        if op.parcial_feita:
            ordens.append(Ordem("limite", -1, None, op.alvo2, "alvo"))
        if not self.stop_antes_do_breakeven and b["low"] <= op.stop:
            ordens.append(Ordem("mercado", -1, None, rotulo="stop"))
        return ordens, None, None


ESTRATEGIAS: dict[str, Estrategia] = {"BOVA11": Bova11(), "VALE3": Vale3(), "PETR4": Petr4(), "BBAS3": Bbas3(),
                                      "ITUB4": Itub4(), "BPAC11": Bpac11()}


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


def simular(est: Estrategia, df: pd.DataFrame, zerar_no_fim_do_dia: bool | None = None,
            candle_em_formacao: pd.Series | None = None) -> Resultado:
    """Roda a estratégia sobre candles fechados.

    `candle_em_formacao` (o, h, l, c até agora) só executa as ordens pendentes —
    o código da estratégia não roda nele, porque ele ainda não fechou.
    Com `zerar_no_fim_do_dia` (padrão: o da estratégia, desligado), a posição é
    zerada no fechamento do último candle de cada pregão e não se abre entrada
    nesse candle (day trade).
    """
    zerar = est.zera_no_fim_do_dia if zerar_no_fim_do_dia is None else zerar_no_fim_do_dia
    est = copy.copy(est)      # estado próprio: simulações simultâneas não se atrapalham
    est.reiniciar()
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
            if ordem.qtd is not None and ordem.qtd % est.lote_padrao:
                continue      # fora do lote padrão (parcial de 50 num lote de 100): não executa
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
        fim_do_dia = zerar and bool(ultimo_do_dia.iloc[i])
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


def barras_rtd(ativo: str, pasta, dia) -> pd.DataFrame:
    """Barras de 5 min do pregão `dia` a partir das cotações gravadas pelo coletor RTD.

    Máxima e mínima saem das cotações anotadas (uma por mudança), então um pico
    entre duas atualizações pode escapar. O volume é a diferença da quantidade
    acumulada do dia; a primeira linha, se o coletor começou com o pregão em
    andamento, não entra no volume (seria o acumulado até ali).
    """
    import pathlib
    arq = pathlib.Path(pasta) / f"{dia:%Y-%m-%d}.csv"
    if not arq.exists():
        return pd.DataFrame()
    df = pd.read_csv(arq)
    df = df[(df["ativo"] == ativo) & (df["ult"] > 0)]
    if df.empty:
        return pd.DataFrame()
    idx = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(BRT)
    s = pd.DataFrame({"ult": df["ult"].to_numpy(), "qtt": df["qtt"].to_numpy()}, index=idx.to_numpy())
    s.index = pd.DatetimeIndex(s.index).tz_convert(BRT)
    # pregão, leilão de fechamento e after-market (o Profit mostra negócios até ~18:25)
    s = s[(s.index.strftime("%H:%M") >= "10:00") & (s.index.strftime("%H:%M") <= "18:30")]
    if s.empty:
        return pd.DataFrame()
    vol = s["qtt"].diff().clip(lower=0).fillna(0)
    agrup = s.assign(vol=vol).resample("5min", label="left", closed="left", origin="start_day")
    barras = pd.DataFrame({"open": agrup["ult"].first(), "high": agrup["ult"].max(), "low": agrup["ult"].min(),
                           "close": agrup["ult"].last(), "volume": agrup["vol"].sum()}).dropna(subset=["open"])
    barras.attrs["inicio"] = s.index[0]
    barras.attrs["ultima"] = s.index[-1]
    return barras


def juntar_barras(yahoo: pd.DataFrame, rtd: pd.DataFrame) -> pd.DataFrame:
    """Troca as barras do Yahoo pelas de um pregão gravado pelo coletor RTD, no trecho que
    ele gravou: da primeira barra que pegou inteira até a última. Fora dele — antes de o
    coletor ligar, ou depois de ele parar — fica o Yahoo."""
    if rtd is None or rtd.empty:
        return yahoo
    inicio = rtd.attrs.get("inicio", rtd.index[0])
    primeira_inteira = rtd.index[0] if inicio <= rtd.index[0] else rtd.index[0] + pd.Timedelta(minutes=5)
    rtd = rtd[rtd.index >= primeira_inteira]
    if rtd.empty:
        return yahoo
    fim = rtd.index[-1] + pd.Timedelta(minutes=5)
    fora = (yahoo.index < primeira_inteira) | (yahoo.index >= fim)
    return pd.concat([yahoo[fora], rtd]).sort_index()


def dias_rtd(pasta) -> list:
    """Datas dos pregões que o coletor RTD gravou (arquivos AAAA-MM-DD.csv), em ordem."""
    import pathlib
    dias = []
    for arq in pathlib.Path(pasta).glob("????-??-??.csv"):
        try:
            dias.append(datetime.strptime(arq.stem, "%Y-%m-%d").date())
        except ValueError:
            pass
    return sorted(dias)


def barras_diarias(ativo: str, faixa: str = "6mo") -> pd.DataFrame:
    """Fechamento e volume diários do Yahoo, pela data. O fechamento é o do leilão."""
    import requests
    resp = requests.get(YAHOO_CHART.format(simbolo=f"{ativo}.SA"), params={"range": faixa, "interval": "1d"},
                        headers=_UA, timeout=20)
    resp.raise_for_status()
    r = resp.json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    datas = pd.to_datetime(r["timestamp"], unit="s", utc=True).tz_convert(BRT).date
    return pd.DataFrame({"close": q["close"], "volume": q["volume"]}, index=datas).dropna(subset=["close"])


def com_leilao_fechamento(barras: pd.DataFrame, diario: pd.DataFrame, hoje=None) -> pd.DataFrame:
    """Põe de volta o leilão de fechamento, que as barras de 5 min do Yahoo perdem nos
    pregões passados: uma barra das 16:55 fechando no preço oficial do dia, com o volume
    que falta para o volume do dia (10 a 15% na VALE3). `candles_de_5min` a junta ao
    último candle do pregão, como no Profit, onde ela entra nas médias, na VWAP e na
    média de volume. O pregão de hoje fica de fora — ainda pode estar em andamento, e o
    RTD traz o leilão de verdade."""
    if barras.empty or diario.empty:
        return barras
    hoje = hoje or datetime.now(BRT).date()
    b = barras.copy()
    novas = []
    for dia, grupo in barras.groupby(barras.index.date):
        if dia >= hoje or dia not in diario.index or grupo.index[-1].strftime("%H:%M") >= "17:00":
            continue
        fech = float(diario.at[dia, "close"])
        falta = max(0.0, float(diario.at[dia, "volume"] or 0) - float(grupo["volume"].sum()))
        t = pd.Timestamp(f"{dia} 16:55").tz_localize(BRT)
        if t in b.index:
            b.loc[t, ["high", "low", "close"]] = [max(b.at[t, "high"], fech), min(b.at[t, "low"], fech), fech]
            b.loc[t, "volume"] += falta
        else:
            novas.append((t, fech, falta))
    if not novas:
        return b
    precos = [p for _, p, _ in novas]
    extra = pd.DataFrame({"open": precos, "high": precos, "low": precos, "close": precos,
                          "volume": [v for *_, v in novas]}, index=pd.DatetimeIndex([t for t, *_ in novas]))
    return pd.concat([b, extra]).sort_index()


# Fatores medidos nas listas de operações do Profit (preço de entrada do Profit ÷
# abertura crua do mesmo candle, antes da data ex; variação < 0,03% entre operações).
# O provento do Yahoo não reproduz o ajuste do Profit — na VALE3, na PETR4 e na BPAC11
# o Profit ajusta menos, na BBAS3 mais —, então, quando há fator medido, vale ele.
FATORES_PROFIT: dict[tuple[str, str], float] = {
    ("VALE3", "2026-08-12"): 0.97587,
    ("PETR4", "2026-08-24"): 0.97250,
    ("BPAC11", "2026-08-11"): 0.98774,
    ("BBAS3", "2026-09-02"): 0.99440,
}


def proventos(ativo: str, faixa: str = "1y") -> list[tuple]:
    """Proventos em dinheiro do Yahoo: (data ex, valor por ação), em ordem."""
    import requests
    resp = requests.get(YAHOO_CHART.format(simbolo=f"{ativo}.SA"),
                        params={"range": faixa, "interval": "1d", "events": "div"}, headers=_UA, timeout=20)
    resp.raise_for_status()
    eventos = (resp.json()["chart"]["result"][0].get("events") or {}).get("dividends") or {}
    return sorted((datetime.fromtimestamp(d["date"], BRT).date(), float(d["amount"])) for d in eventos.values())


def ajustar_proventos(barras: pd.DataFrame, eventos: list[tuple], ativo: str = "") -> pd.DataFrame:
    """Histórico ajustado como o gráfico do Profit: preços anteriores a cada data ex
    multiplicados por 1 − provento ÷ último fechamento antes dela (volume intacto).

    Sem isso, a lista de operações do Profit fica toda deslocada antes de uma data
    ex (na VALE3, 2,4% abaixo do preço de verdade até 12/08/2026). O sinal quase não
    muda — as médias andam juntas —, mas preço de entrada, stop e alvo mudam.
    """
    if barras.empty or not eventos:
        return barras
    b = barras.copy()
    datas = pd.Index(b.index.date)
    for ex, valor in eventos:
        antes = datas < ex
        if not antes.any() or antes.all():
            continue
        fator = FATORES_PROFIT.get((ativo, f"{ex:%Y-%m-%d}"))
        if fator is None:
            fator = 1 - valor / float(b.loc[antes, "close"].iloc[-1])
        b.loc[antes, ["open", "high", "low", "close"]] *= fator
    return b


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
    em_pregao = inicio.date() == agora.date() and agora.strftime("%H:%M") <= "18:30"
    if em_pregao:
        formando = candles.iloc[-1]
        return simular(est, candles.iloc[:-1], candle_em_formacao=formando), formando
    return simular(est, candles), None


def candles_de_5min(barras_5m: pd.DataFrame, minutos: int) -> pd.DataFrame:
    """Agrupa barras de 5 min no tempo gráfico da estratégia como o gráfico do Profit.

    O pregão regular fica alinhado às 10:00 (10:00, 10:10…) e o leilão de fechamento
    (negócios das 17:00 às 17:29) entra no último candle dele — no BOVA11 de 60 min,
    sinal no candle das 16:00 entra às 17:30, então não existe candle das 17:00. O
    after-market recomeça o alinhamento às 17:30 (17:30, 17:50… em 20 min; 17:30 em 60 min).
    """
    if barras_5m.empty:
        return barras_5m
    hm = np.asarray(barras_5m.index.hour * 60 + barras_5m.index.minute)
    efetivo = np.where((hm >= 17 * 60) & (hm < 17 * 60 + 30), 16 * 60 + 55, hm)
    base = np.where(efetivo >= 17 * 60 + 30, 17 * 60 + 30, 0)
    inicio = base + (efetivo - base) // minutos * minutos
    chave = barras_5m.index.normalize() + pd.to_timedelta(inicio, unit="min")
    agrup = barras_5m.groupby(chave)
    df = pd.DataFrame({"open": agrup["open"].first(), "high": agrup["high"].max(),
                       "low": agrup["low"].min(), "close": agrup["close"].last(),
                       "volume": agrup["volume"].sum()})
    df.index.name = None
    return df.dropna(subset=["open", "close"])
