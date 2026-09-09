# -*- coding: utf-8 -*-
"""Função serverless (Vercel) que faz proxy do endpoint do opcoes.net.br.

Existe por um motivo só: o painel roda em WebAssembly dentro do navegador, e o
opcoes.net.br não envia cabeçalho CORS — o fetch direto do browser seria
bloqueado. Esta função, de mesma origem que a página, busca no servidor e
devolve o JSON.

Só stdlib: nada a instalar no runtime.
"""
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlparse

ALVO = "https://opcoes.net.br/listaopcoes/completa"
TIMEOUT = 25

# Allowlist dupla para o proxy não virar proxy aberto: só estes parâmetros
# passam, e só para estes ativos.
PARAMS_PERMITIDOS = {"idAcao", "listarVencimentos", "cotacoes", "vencimentos"}
ATIVOS_PERMITIDOS = {"BOVA11", "PETR4", "VALE3", "BBAS3", "ITUB4", "BPAC11"}

CABECALHOS_UPSTREAM = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": "https://opcoes.net.br/",
}


class handler(BaseHTTPRequestHandler):

    def do_GET(self):  # noqa: N802  (assinatura exigida pelo runtime)
        consulta = parse_qs(urlparse(self.path).query)
        params = {k: v[0] for k, v in consulta.items()
                  if k in PARAMS_PERMITIDOS and v and v[0]}

        ativo = params.get("idAcao", "")
        if ativo not in ATIVOS_PERMITIDOS:
            self._responder(400, {
                "success": False,
                "erro": f"ativo não permitido: {ativo!r}",
                "permitidos": sorted(ATIVOS_PERMITIDOS),
            })
            return

        requisicao = urllib.request.Request(f"{ALVO}?{urlencode(params)}",
                                            headers=CABECALHOS_UPSTREAM)
        try:
            with urllib.request.urlopen(requisicao, timeout=TIMEOUT) as resposta:
                corpo = resposta.read()
        except urllib.error.HTTPError as exc:
            self._responder(502, {"success": False,
                                  "erro": f"opcoes.net.br respondeu {exc.code}"})
            return
        except Exception as exc:
            self._responder(502, {"success": False,
                                  "erro": f"falha ao consultar a origem: {exc}"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        # Cotação com atraso: 60s de cache poupa a origem sem envelhecer o dado.
        self.send_header("Cache-Control", "public, max-age=60, s-maxage=60")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(corpo)

    def _responder(self, codigo: int, dados: dict) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):  # silencia o log padrão do BaseHTTPRequestHandler
        return
