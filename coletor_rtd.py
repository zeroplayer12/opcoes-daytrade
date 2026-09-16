# -*- coding: utf-8 -*-
"""
Coletor RTD — cotações dos 6 ativos direto do Profit, em tempo real.

Liga os tópicos no servidor RTD do Profit (o mesmo que o Excel usa), anota cada
mudança num CSV por pregão (dados_rt/AAAA-MM-DD.csv) e a aba Operações monta
os candles a partir dele. Só leitura: não envia nada ao Profit além dos
pedidos de cotação.

Roda em segundo plano, iniciado junto com o painel (iniciar-painel.cmd). Com o
Profit fechado, espera; se ele fechar no meio do caminho, reconecta quando ele
voltar. Um coletor só por vez (mutex nomeado). Log em coletor.log.

Exige no Profit: Exportação em Tempo Real (RTD / DDE) com RTD ativado e os
ativos cadastrados na lista.

Opções: o painel e o vigia anotam em dados_rt/opcoes/assinar.json as opções sugeridas
nos sinais do dia; o coletor assina essas também (último, melhor compra e melhor venda —
o Profit entrega mesmo sem a opção estar na lista) e grava em dados_rt/opcoes/: um CSV
por pregão e agora.json com a cotação mais recente de cada uma.

O servidor RTD do Profit atende UM cliente por vez: outro programa que se conectar
(outro coletor, um teste) toma a conexão, e este para de receber sem aviso. Por isso, no
pregão, se nenhuma cotação chegar em 3 minutos o coletor reconecta sozinho.

Uso:  python coletor_rtd.py              (roda para sempre)
      python coletor_rtd.py --teste 8    (conecta, grava, mostra o formato do RefreshData, sai em 8 s)
"""
from __future__ import annotations

import csv
import ctypes
import json
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import comtypes
import comtypes.client
from comtypes import COMObject

RAIZ = pathlib.Path(__file__).resolve().parent
PASTA = RAIZ / "dados_rt"
LOG = RAIZ / "coletor.log"
ATIVOS = ["BOVA11", "PETR4", "VALE3", "BBAS3", "ITUB4", "BPAC11"]
CAMPOS = ["ULT", "QTT", "VOL", "NEG", "HOR"]
COLUNAS = ["ts", "hora", "ativo", "ult", "qtt", "vol", "neg", "hor"]
TYPELIB = ("{EFCFBDCA-78A5-450B-8228-346C4F44D5B8}", 1, 0)   # RTDTrading, dentro do profitchart.exe
BRT = timezone(timedelta(hours=-3), "BRT")
PASTA_OPC = PASTA / "opcoes"
ASSINAR = PASTA_OPC / "assinar.json"         # {"PETRJ500": "2026-09-15", ...} — escrito pelo painel/vigia
AGORA = PASTA_OPC / "agora.json"             # última cotação de cada opção assinada
CAMPOS_OPC = ["ULT", "OCP", "OVD", "QTT", "NEG", "HOR", "DAT"]   # OCP/OVD = melhor compra/venda
COLUNAS_OPC = ["ts", "hora", "opcao", "ult", "compra", "venda", "qtt", "neg", "hor"]
SILENCIO_MAX = 300                           # s sem cotação nova no pregão → tenta reconectar
SILENCIO_TETO = 1800                         # ... dobrando a espera até aqui, se não voltar


def log(msg: str) -> None:
    linha = f"{datetime.now(BRT):%d/%m %H:%M:%S} {msg}"
    print(linha, flush=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 2_000_000:
            LOG.replace(LOG.with_suffix(".old.log"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def instancia_unica():
    """Mutex nomeado: se já houver um coletor, este sai sem fazer nada."""
    k32 = ctypes.windll.kernel32
    alca = k32.CreateMutexW(None, False, "Local\\PainelDayTradeColetorRTD")
    if k32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        log("outro coletor já está rodando; saindo")
        sys.exit(0)
    return alca


def profit_aberto() -> bool:
    saida = subprocess.run(["tasklist", "/FI", "IMAGENAME eq profitchart.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return "profitchart.exe" in saida.lower()


def em_pregao() -> bool:
    agora = datetime.now(BRT)
    return agora.weekday() < 5 and "10:05" <= f"{agora:%H:%M}" <= "18:20"


def opcoes_pedidas() -> list[str]:
    """Opções que o painel ou o vigia pediram hoje."""
    try:
        pedidos = json.loads(ASSINAR.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hoje = f"{datetime.now(BRT):%Y-%m-%d}"
    return sorted(t for t, dia in pedidos.items() if dia == hoje and re.fullmatch(r"[A-Z0-9]{5,12}", str(t)))[:40]


def _parado(hor, agora, limite: int = 300) -> bool:
    """O último negócio (HOR) é bem mais velho que o relógio? Então gravar essa linha só
    repetiria um preço parado — é o que sai do snapshot de cada conexão quando o Profit
    deixou de mandar cotação, e no painel viraria candle achatado."""
    try:
        h, m, s = (int(x) for x in str(hor).split(":"))
        negocio = agora.replace(hour=h, minute=m, second=s, microsecond=0)
    except (ValueError, AttributeError, TypeError):
        return False
    atraso = (agora - negocio).total_seconds()
    return atraso > limite or atraso < -60        # -60 s: HOR de outro pregão


def _numero(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    texto = str(v).strip()
    try:
        return float(texto.replace(".", "").replace(",", ".")) if "," in texto else float(texto)
    except ValueError:
        return None


class Gravador:
    """Anexa uma linha por mudança de cotação no CSV do dia."""

    def __init__(self):
        PASTA.mkdir(exist_ok=True)
        self.dia, self.arq, self.escritor = None, None, None
        self.opcoes: dict[str, dict] = {}
        self._sujo, self._salvo = False, 0.0

    def _abre(self, agora: datetime) -> None:
        if self.dia == agora.date():
            return
        if self.arq:
            self.arq.close()
        caminho = PASTA / f"{agora:%Y-%m-%d}.csv"
        novo = not caminho.exists()
        self.arq = open(caminho, "a", newline="", encoding="utf-8")
        self.escritor = csv.writer(self.arq)
        if novo:
            self.escritor.writerow(COLUNAS)
        self.dia = agora.date()

    def grava(self, estado: dict, ativos) -> int:
        agora = datetime.now(BRT)
        self._abre(agora)
        n = 0
        for a in ativos:
            e = estado[a]
            ult = _numero(e.get("ULT"))
            if ult is None or ult <= 0 or _parado(e.get("HOR"), agora):
                continue
            self.escritor.writerow([f"{agora.timestamp():.3f}", f"{agora:%H:%M:%S}", a, round(ult, 6),
                                    _numero(e.get("QTT")), _numero(e.get("VOL")), _numero(e.get("NEG")),
                                    e.get("HOR")])
            n += 1
        self.arq.flush()
        return n

    def grava_opcoes(self, estado: dict, opcoes) -> None:
        """Uma linha por mudança em dados_rt/opcoes/AAAA-MM-DD.csv; a última de cada opção
        vai para agora.json (em salva_agora, no máximo uma vez por segundo)."""
        agora = datetime.now(BRT)
        PASTA_OPC.mkdir(parents=True, exist_ok=True)
        caminho = PASTA_OPC / f"{agora:%Y-%m-%d}.csv"
        novo = not caminho.exists()
        with open(caminho, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if novo:
                w.writerow(COLUNAS_OPC)
            for t in sorted(opcoes):
                e = estado[t]
                if t in self.opcoes and _parado(e.get("HOR"), agora):
                    continue          # opção sem negócio novo: não repete a linha
                linha = {"ult": _numero(e.get("ULT")), "compra": _numero(e.get("OCP")),
                         "venda": _numero(e.get("OVD")), "qtt": _numero(e.get("QTT")),
                         "neg": _numero(e.get("NEG")), "hor": e.get("HOR"), "dat": e.get("DAT")}
                w.writerow([f"{agora.timestamp():.3f}", f"{agora:%H:%M:%S}", t, linha["ult"], linha["compra"],
                            linha["venda"], linha["qtt"], linha["neg"], linha["hor"]])
                self.opcoes[t] = {**linha, "ts": round(agora.timestamp(), 3)}
        self._sujo = True

    def salva_agora(self) -> None:
        if not self._sujo or time.time() - self._salvo < 1:
            return
        tmp = AGORA.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.opcoes, ensure_ascii=False), encoding="utf-8")
            tmp.replace(AGORA)
            self._sujo, self._salvo = False, time.time()
        except OSError:
            pass            # o painel pode estar lendo; tenta de novo no próximo giro


def _pares(resposta):
    """(id do tópico, valor) do RefreshData, qualquer que seja a forma que o comtypes devolver.

    O servidor devolve uma matriz 2 × N (linha 0 = ids, linha 1 = valores) e o
    TopicCount de saída; o comtypes pode entregar isso como lista, tupla ou
    tuplas aninhadas em qualquer das duas orientações.
    """
    matriz = resposta
    if isinstance(resposta, (list, tuple)) and len(resposta) == 2:
        a, b = resposta
        if isinstance(a, int) and isinstance(b, (list, tuple)):
            matriz = b
        elif isinstance(b, int) and isinstance(a, (list, tuple)):
            matriz = a
    if not isinstance(matriz, (list, tuple)) or not matriz:
        return []
    if len(matriz) == 2 and all(isinstance(x, (list, tuple)) for x in matriz) and len(matriz[0]) == len(matriz[1]):
        return list(zip(matriz[0], matriz[1]))
    return [(linha[0], linha[1]) for linha in matriz if isinstance(linha, (list, tuple)) and len(linha) >= 2]


_ESPERA = [SILENCIO_MAX]     # silêncio aceito antes de reconectar (dobra a cada tentativa vã)


def coleta(gravador: Gravador, duracao: float | None = None) -> None:
    mod = comtypes.client.GetModule(TYPELIB)

    class Aviso(COMObject):
        """Callback que o Profit chama quando há cotação nova."""
        _com_interfaces_ = [mod.IRTDUpdateEvent]

        def __init__(self):
            super().__init__()
            self.chegou = 0
            self._hb = -1

        def UpdateNotify(self, *args):
            self.chegou += 1
            return 0

        def Disconnect(self, *args):
            self.chegou = -1
            return 0

        def _get_HeartbeatInterval(self, *args):
            return self._hb

        def _set_HeartbeatInterval(self, valor, *args):
            self._hb = valor

        IRTDUpdateEvent__get_HeartbeatInterval = _get_HeartbeatInterval
        IRTDUpdateEvent__set_HeartbeatInterval = _set_HeartbeatInterval

    srv = comtypes.client.CreateObject("RTDTrading.RtdServer", interface=mod.IRtdServer)
    aviso = Aviso()
    if srv.ServerStart(aviso) != 1:
        raise RuntimeError("o Profit recusou a conexão RTD — confira se o RTD está ativado")
    topicos, estado = {}, {a: {} for a in ATIVOS}
    opcoes: set[str] = set()
    fim = time.time() + duracao if duracao else None
    proximo = [0]

    def assina(nome: str, campos) -> None:
        estado.setdefault(nome, {})
        for campo in campos:
            proximo[0] += 1
            topicos[proximo[0]] = (nome, campo)
            r = srv.ConnectData(proximo[0], [f"{nome}_B_0", campo], True)
            estado[nome][campo] = r[-1] if isinstance(r, (list, tuple)) else r

    def assina_opcoes() -> None:
        novas = [t for t in opcoes_pedidas() if t not in estado]
        for t in novas:
            assina(t, CAMPOS_OPC)
            opcoes.add(t)
        if novas:
            gravador.grava_opcoes(estado, novas)
            gravador.salva_agora()
            log("opções assinadas: " + ", ".join(f"{t} {estado[t].get('ULT')}" for t in novas))

    try:
        for a in ATIVOS:
            assina(a, CAMPOS)
        n = gravador.grava(estado, ATIVOS)
        log(f"conectado; {n} ativos gravados: " + ", ".join(f"{a} {estado[a].get('ULT')}" for a in ATIVOS))
        if duracao:
            log(f"RefreshData direto (formato): {repr(srv.RefreshData(0))[:400]}")
        assina_opcoes()
        cru_logado = False
        checagem = pedidos = ultimo_aviso = time.time()
        silencio = _ESPERA[0]
        while fim is None or time.time() < fim:
            comtypes.client.PumpEvents(0.5)
            if aviso.chegou < 0:
                raise ConnectionError("o Profit encerrou a conexão RTD")
            if aviso.chegou:
                aviso.chegou = 0
                ultimo_aviso = time.time()
                silencio, _ESPERA[0] = SILENCIO_MAX, SILENCIO_MAX   # veio cotação: espera normal
                resposta = srv.RefreshData(0)
                pares = _pares(resposta)
                if not cru_logado:
                    log(f"primeira atualização: {len(pares)} tópicos · formato {repr(resposta)[:300]}")
                    cru_logado = True
                mudou = set()
                for t, v in pares:
                    chave = topicos.get(int(t))
                    if chave and estado[chave[0]].get(chave[1]) != v:
                        estado[chave[0]][chave[1]] = v
                        mudou.add(chave[0])
                if mudou - opcoes:
                    gravador.grava(estado, mudou - opcoes)
                if mudou & opcoes:
                    gravador.grava_opcoes(estado, mudou & opcoes)
            gravador.salva_agora()
            if time.time() - pedidos > 10:
                pedidos = time.time()
                assina_opcoes()
            if time.time() - checagem > 30:
                checagem = time.time()
                if not profit_aberto():
                    raise ConnectionError("o Profit foi fechado")
                if em_pregao() and time.time() - ultimo_aviso > silencio:
                    _ESPERA[0] = min(silencio * 2, SILENCIO_TETO)   # não voltou: tentar menos vezes
                    ultimo = max((e.get("HOR") for e in estado.values() if e.get("HOR")), default="?")
                    raise ConnectionError(f"nenhuma cotação nova há {silencio // 60} min (último negócio às "
                                          f"{ultimo}; o Profit pode ter perdido a conexão, ou outro programa "
                                          f"tomou o RTD); reconectando, e o próximo aviso só depois de "
                                          f"{_ESPERA[0] // 60} min de silêncio")
    finally:
        for t in topicos:
            try:
                srv.DisconnectData(t)
            except Exception:
                pass
        try:
            srv.ServerTerminate()
        except Exception:
            pass


def main() -> None:
    _mutex = instancia_unica()  # noqa: F841 — segura o mutex até o processo acabar
    gravador = Gravador()
    log(f"coletor iniciado; gravando em {PASTA}")
    avisou_espera = False
    while True:
        if not profit_aberto():
            if not avisou_espera:
                log("Profit fechado; esperando ele abrir")
                avisou_espera = True
            time.sleep(20)
            continue
        avisou_espera = False
        try:
            coleta(gravador)
        except Exception as exc:
            log(f"conexão caiu: {exc}; nova tentativa em 30 s")
            time.sleep(30)


if __name__ == "__main__":
    if "--teste" in sys.argv:
        segundos = float(sys.argv[sys.argv.index("--teste") + 1]) if len(sys.argv) > sys.argv.index("--teste") + 1 else 8
        coleta(Gravador(), duracao=segundos)
        log("teste encerrado")
    else:
        main()
