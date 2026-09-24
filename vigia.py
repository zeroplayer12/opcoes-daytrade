# -*- coding: utf-8 -*-
"""
Vigia dos sinais — avisa no Windows e no Telegram quando uma estratégia dá sinal, faz a
parcial, sobe o stop para o zero a zero, toca o stop ou sai da operação.

Roda em segundo plano, iniciado junto com o painel (iniciar-painel.cmd), e funciona com o
navegador fechado: durante o pregão recalcula as estratégias a cada 15 s com os mesmos
dados e o mesmo código da aba Operações (RTD do Profit; Yahoo se o coletor não estiver
gravando). No sinal, escolhe a opção pelas regras da aba Opções, pede a cotação dela ao
coletor RTD e manda o preço estimado no alvo e no stop.

Só leitura: não envia ordem nenhuma. Um vigia só por vez (mutex). Log em vigia.log.
Ativos avisados e canais em avisos.json (ver avisos.py); o Telegram liga com
python configurar_telegram.py.

Uso:  python vigia.py            (roda para sempre)
      python vigia.py --uma      (um ciclo: mostra os avisos dos últimos dias sem enviar e sai)
      python vigia.py --teste    (manda um aviso de teste pelos canais ligados e sai)
"""
from __future__ import annotations

import ctypes
import json
import logging
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta

import avisos

# Sem janela (iniciado pelo painel), a saída padrão fica na codificação do Windows, que não tem o
# "−" dos resultados negativos: o print quebrava e derrubava o que vinha depois dele.
for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

RAIZ = avisos.RAIZ
LOG = RAIZ / "vigia.log"
CICLO = 15                           # segundos entre verificações no pregão
RECENTE = timedelta(minutes=15)      # ao ligar, avisa o que aconteceu nesse intervalo
TAXA = 0.1075                        # juro para o Black-Scholes, o mesmo padrão da aba Opções


def log(msg: str) -> None:
    linha = f"{datetime.now():%d/%m %H:%M:%S} {msg}"
    try:
        print(linha, flush=True)
    except Exception:
        pass                   # console ausente ou sem a codificação: o arquivo de log basta
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.replace(LOG.with_suffix(".old.log"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def instancia_unica():
    k32 = ctypes.windll.kernel32
    alca = k32.CreateMutexW(None, False, "Local\\PainelDayTradeVigia")
    if k32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        log("outro vigia já está rodando; saindo")
        sys.exit(0)
    return alca


def carrega_painel():
    """Importa o app sem abrir o Streamlit: dados, estratégias e escolha da opção são as
    mesmas funções da aba Operações (o cache do Streamlit vira cache em memória)."""
    warnings.filterwarnings("ignore")
    import streamlit  # noqa: F401
    for nome in ("streamlit", "streamlit.runtime.caching.cache_data_api"):
        logging.getLogger(nome).setLevel(logging.ERROR)
    import app
    import operacoes as ope
    return app, ope


def em_pregao(agora: datetime) -> bool:
    return agora.weekday() < 5 and "09:58" <= f"{agora:%H:%M}" <= "18:35"


class Vigia:
    def __init__(self, app, ope):
        self.app, self.ope = app, ope
        self.cfg = avisos.carregar()
        self.arq = app.PASTA_RT / "vigia_estado.json"
        self.estado = self._le()
        self.primeiro = True

    def _le(self) -> dict:
        try:
            e = json.loads(self.arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            e = {}
        e.setdefault("vistos", {})
        e.setdefault("opcoes", {})
        return e

    def _salva(self) -> None:
        limite = f"{datetime.now(self.app.BRT) - timedelta(days=10):%Y-%m-%d}"
        self.estado["vistos"] = {k: d for k, d in self.estado["vistos"].items() if d >= limite}
        self.estado["opcoes"] = {k: v for k, v in self.estado["opcoes"].items() if v.get("dia", "") >= limite}
        self.arq.parent.mkdir(exist_ok=True)
        tmp = self.arq.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.estado, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.arq)

    def _eventos(self, agora: datetime) -> list[tuple]:
        app, ope = self.app, self.ope
        saida = []
        for ativo in self.cfg["ativos"]:
            est = ope.ESTRATEGIAS.get(ativo)
            if est is None:
                continue
            try:
                res, formando = ope.situacao(est, app.barras_estrategia(ativo), agora)
            except Exception as exc:
                log(f"{ativo}: sem dados agora ({exc})")
                continue
            ultimo = formando if formando is not None else res.candles.iloc[-1]
            for ev in avisos.eventos(ativo, est, res, agora):
                saida.append((ev, est, res, float(ultimo["close"])))
        return saida

    def _opcao(self, ev: dict, est, res, preco: float, agora: datetime) -> str:
        """Linha da opção no aviso: a sugerida no sinal; a da operação nas saídas."""
        app, hoje = self.app, f"{agora:%Y-%m-%d}"
        op = res.aberta
        if ev["tipo"] == "sinal" and op is not None and op.hora_sinal == ev["op"].hora_sinal:
            try:
                info = app.opcao_para_sinal(ev["ativo"], op.lado, agora.date(), TAXA)
                if info.get("erro"):
                    return f"\nOpção: {info['erro']} — confira na aba Opções."
                p = app.precos_opcao(ev["ativo"], est, op, preco, info)
            except Exception as exc:
                return f"\nOpção sugerida indisponível agora ({exc}); confira na aba Opções."
            app.assinar_opcoes([p["ticker"]])
            app.anotar_opcao(ev["ativo"], op, info)
            self.estado["opcoes"][ev["base"]] = {"ticker": p["ticker"], "tipo": p["tipo"], "dia": hoje}
            return "\n" + p["resumo"]
        guardada = self.estado["opcoes"].get(ev["base"])
        if guardada:
            nome = f"{guardada['tipo'].lower()} {guardada['ticker']}"
            if ev["tipo"] == "saida":
                return f"\nA estratégia saiu: hora de zerar a {nome}."
            if ev["tipo"] == "stop_tocado":
                return f"\nPrepare a saída da {nome}."
            if ev["tipo"] == "parcial":
                return f"\nParcial: metade da posição na {nome}."
        return ""

    def _cotacao_do_profit(self, agora: datetime) -> None:
        """Avisa quando o Profit para de mandar cotação no pregão — o painel passa a usar o Yahoo e o
        cache, e os sinais podem divergir do gráfico — e avisa de novo quando ela volta."""
        if not ("10:05" <= f"{agora:%H:%M}" <= "17:05"):
            return
        ultima = self.app._ultima_cotacao_rtd()
        parado = self.estado.get("rtd_parado")
        if ultima is None:
            if f"{agora:%H:%M}" >= "10:15" and not parado:
                self.estado["rtd_parado"] = f"{agora:%Y-%m-%dT%H:%M}"
                self._salva()
                avisos.enviar("Profit sem cotação hoje",
                              "O coletor não gravou nenhum negócio neste pregão. Confira se o Profit está "
                              "aberto e conectado; o painel está usando o Yahoo, com 15 min de atraso.",
                              self.cfg)
                log("aviso: Profit sem cotação hoje")
            return
        minutos = (agora - ultima).total_seconds() / 60
        if minutos >= 10 and not parado:
            self.estado["rtd_parado"] = ultima.isoformat(timespec="minutes")
            self._salva()
            avisos.enviar(f"Profit sem cotação há {minutos:.0f} min",
                          f"Último negócio às {ultima:%H:%M}. O painel passou a usar o Yahoo (15 min de "
                          f"atraso) e os candles gravados do Profit; os sinais do pregão podem divergir "
                          f"do gráfico até a cotação voltar.", self.cfg)
            log(f"aviso: Profit sem cotação há {minutos:.0f} min (último negócio {ultima:%H:%M})")
        elif minutos < 3 and parado:
            self.estado.pop("rtd_parado", None)
            self._salva()
            avisos.enviar("Profit voltou a mandar cotação",
                          f"Ficou parado desde {parado[-5:]}; agora o painel está em tempo real de novo.",
                          self.cfg)
            log("aviso: Profit voltou a mandar cotação")

    def ciclo(self, enviar: bool = True) -> list[dict]:
        agora = datetime.now(self.app.BRT)
        hoje = f"{agora:%Y-%m-%d}"
        feitos = []
        if enviar:
            try:
                self._cotacao_do_profit(agora)
            except Exception as exc:
                log(f"aviso de cotação parada falhou: {type(exc).__name__}: {exc}")
        for ev, est, res, preco in self._eventos(agora):
            if enviar and ev["chave"] in self.estado["vistos"]:
                continue
            recente = ev["hora"] is not None and agora - ev["hora"] <= RECENTE
            if enviar and self.primeiro and not recente:
                self.estado["vistos"][ev["chave"]] = hoje      # já tinha acontecido ao ligar
                continue
            texto = ev["texto"] + self._opcao(ev, est, res, preco, agora)
            if enviar:
                # anota e grava ANTES de mandar: se algo falhar depois do envio, o próximo ciclo não
                # repete o aviso (em 17/09/2026 um erro no log fez a saída da BOVA11 sair a cada 20 s)
                self.estado["vistos"][ev["chave"]] = hoje
                self._salva()
                canais = avisos.enviar(ev["titulo"], texto, self.cfg)
                log(f"aviso: {ev['titulo']} — {texto.replace(chr(10), ' / ')} {canais}")
            feitos.append({"titulo": ev["titulo"], "texto": texto})
        if enviar:
            self.primeiro = False
            self._salva()
        return feitos


_calculo = {"rodando": False, "tentativa": 0.0}


def resultados_do_dia() -> None:
    """Refaz o resultado mês a mês (resultados.py) uma vez por dia, depois das 18:40, numa
    linha à parte para não atrasar os avisos (leva ~30 s). Se falhar, tenta de novo em 30 min."""
    import resultados
    if _calculo["rodando"] or time.time() - _calculo["tentativa"] < 1800 or not resultados.precisa_atualizar():
        return
    _calculo.update(rodando=True, tentativa=time.time())

    def roda():
        try:
            d = resultados.calcular()
            log(f"resultado mês a mês refeito (candles até {d['ate'][:16]}"
                + (f"; falhas: {d['falhas']}" if d["falhas"] else "") + ")")
        except Exception as exc:
            log(f"resultado mês a mês falhou: {type(exc).__name__}: {exc}")
        finally:
            _calculo["rodando"] = False

    threading.Thread(target=roda, daemon=True).start()


_medida = {"rodando": False, "dia": None, "tentativa": 0.0}


def medidas_do_dia(agora) -> None:
    """Depois do fechamento, acumula as duas medidas que só o tempo resolve: o prêmio de
    volatilidade do book (volatilidade.py) e a mesa de testes da saída da opção (mesa_opcao.py).
    Uma vez por pregão, em linha à parte. Falhou, tenta de novo em 30 min — o dia só é marcado
    como feito quando as duas passam, senão um erro de rede comeria a medição do pregão."""
    if (_medida["rodando"] or _medida["dia"] == agora.date()
            or time.time() - _medida["tentativa"] < 1800 or (agora.hour, agora.minute) < (18, 45)):
        return
    _medida.update(rodando=True, tentativa=time.time())

    def roda():
        ok = True
        try:
            import volatilidade
            d = volatilidade.medir()
            log("prêmio de volatilidade: " + volatilidade.resumo().replace("\n", " · "))
            if d.get("mudou"):
                # O painel passou a precificar a série antiga desse ativo com o fator dele, não com
                # o geral. Muda o resultado das abas: é hora de refazer o analise_opcao.py.
                texto = "\n".join(f"{a}: {de or 'geral'} → {para}×" for a, (de, para) in d["mudou"].items())
                log("prêmio de volatilidade mudou — " + texto.replace("\n", " / "))
                avisos.enviar("Prêmio de volatilidade atualizado", texto, avisos.carregar())
        except Exception as exc:
            ok = False
            log(f"prêmio de volatilidade falhou: {type(exc).__name__}: {exc}")
        try:
            import mesa_opcao
            log("mesa da opção: " + mesa_opcao.placar(mesa_opcao.atualizar()).replace("\n", " · "))
        except Exception as exc:
            ok = False
            log(f"mesa da opção falhou: {type(exc).__name__}: {exc}")
        finally:
            _medida.update(rodando=False, dia=agora.date() if ok else None)

    threading.Thread(target=roda, daemon=True).start()


def main() -> None:
    _mutex = instancia_unica()  # noqa: F841 — segura o mutex até o processo acabar
    log("vigia iniciado")
    app, ope = carrega_painel()
    v = Vigia(app, ope)
    log(f"ativos {', '.join(v.cfg['ativos'])} · Windows {'ligado' if v.cfg.get('windows', True) else 'desligado'} · "
        f"Telegram {'ligado' if avisos.telegram_ligado(v.cfg) else 'sem configuração (python configurar_telegram.py)'}")
    while True:
        agora = datetime.now(app.BRT)
        resultados_do_dia()
        medidas_do_dia(agora)
        if not em_pregao(agora):
            time.sleep(60)
            continue
        v.cfg = avisos.carregar()          # mudou o avisos.json? vale no próximo ciclo
        try:
            v.ciclo()
        except Exception as exc:
            log(f"falha no ciclo: {type(exc).__name__}: {exc}")
        time.sleep(CICLO)


if __name__ == "__main__":
    if "--teste" in sys.argv:
        cfg = avisos.carregar()
        print(avisos.enviar("Painel Day Trade · teste", "Se você está vendo isto, os avisos estão funcionando.", cfg))
    elif "--uma" in sys.argv:
        app, ope = carrega_painel()
        for f in Vigia(app, ope).ciclo(enviar=False):
            print(f"· {f['titulo']}\n  {f['texto'].replace(chr(10), chr(10) + '  ')}")
    else:
        main()
