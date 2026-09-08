# Deploy

Duas formas de acessar o painel de fora do PC. A primeira é instantânea e temporária;
a segunda é permanente e exige uns 5 minutos seus.

---

## 1. Túnel Cloudflare — link na hora, temporário

Serve o app **do seu próprio PC** para a internet. Não precisa de conta.

```bash
cloudflared tunnel --url http://localhost:8501 --no-autoupdate
```

O `cloudflared.exe` foi baixado para a pasta temporária da sessão. Para ter em definitivo:

```bash
winget install --id Cloudflare.cloudflared -e
```

Fluxo completo, em dois terminais:

```bash
streamlit run app.py
```

```bash
cloudflared tunnel --url http://localhost:8501 --no-autoupdate
```

O segundo comando imprime a URL `https://<palavras-aleatorias>.trycloudflare.com`.

**Limitações, sem rodeios:**

- Morre quando você fechar o terminal, desligar o PC ou cair a internet.
- A URL muda a cada execução — não dá para favoritar.
- É **pública**: qualquer um com o link abre o painel. Não há login.
- Sem garantia de uptime (é o tier gratuito sem conta da Cloudflare).

**Se o link não abrir no seu PC mas abrir no celular:** é cache de DNS negativo do
provedor. O resolver da Vivo às vezes cacheia `NXDOMAIN` quando o subdomínio é
consultado no instante em que o túnel nasce. Espere um pouco e recarregue, ou aponte o
DNS da máquina para `1.1.1.1`. Foi exatamente o que aconteceu no primeiro túnel desta
sessão; o segundo resolveu normal.

---

## 2. Streamlit Community Cloud — permanente e gratuito

Roda 24/7 sem depender do seu PC. O repositório local já está pronto e commitado.

### Passo a passo

1. Crie um repositório vazio em <https://github.com/new>. Sugestão de nome:
   `opcoes-daytrade`. Pode ser público — não há segredo nenhum no código.
   **Não** marque "Add a README file".

2. Conecte e publique (troque `SEU-USUARIO`):

```bash
cd "C:\Users\Vitor\OneDrive\Área de Trabalho\Leilo\opcoes-daytrade" && git remote add origin https://github.com/SEU-USUARIO/opcoes-daytrade.git && git push -u origin main
```

3. Entre em <https://share.streamlit.io>, faça login com o GitHub e autorize o acesso
   ao repositório.

4. **New app** → **Deploy a public app from GitHub** e preencha:

   | Campo | Valor |
   |---|---|
   | Repository | `SEU-USUARIO/opcoes-daytrade` |
   | Branch | `main` |
   | Main file path | `app.py` |
   | Python version | 3.12 |

5. **Deploy**. O primeiro build leva 2–4 minutos. A URL final fica no formato
   `https://SEU-USUARIO-opcoes-daytrade.streamlit.app`, é fixa e pode ser favoritada.

### O que muda na nuvem

- **Rota A2 (Selenium) não funciona.** O contêiner do Streamlit Cloud não tem Chrome.
  Desmarque "Tentar Selenium se o JSON falhar" na sidebar para não perder tempo com a
  tentativa. Para insistir no Selenium seria preciso um `packages.txt` com `chromium` e
  `chromium-driver` — dá trabalho e costuma quebrar a cada atualização de imagem.

- **Rota A1 (JSON) é incerta.** As requisições sairiam de um IP de datacenter nos EUA,
  e o opcoes.net.br pode recusar. Não dá para saber sem testar.

- **Rota B (upload) funciona sempre.** É a que sustenta o painel na nuvem: exporte o
  CSV/Excel no site e suba pela sidebar.

- **O app fica público.** Qualquer pessoa com o link abre. Não há dado pessoal nem
  credencial no projeto, mas vale saber. Para restringir, o Streamlit Cloud permite
  app privado com lista de e-mails autorizados nas configurações do app.

- O app hiberna após alguns dias sem acesso e acorda no primeiro request (leva ~30s).
