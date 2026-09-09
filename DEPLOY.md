# Deploy

Três caminhos, em ordem de recomendação.

---

## 1. Streamlit Community Cloud — permanente, gratuito, tudo funciona

Roda 24/7 sem depender do seu PC, e **a rota "Buscar no site" funciona** (ao
contrário do build da Vercel — veja a seção 3). O repositório local já está
commitado e pronto.

Preciso de você em dois pontos: criar o repositório e autorizar o Streamlit no
GitHub. Não crio contas nem faço login por você.

### Passo 1 — criar o repositório vazio

Em <https://github.com/new>:

| Campo | Valor |
|---|---|
| Repository name | `opcoes-daytrade` |
| Visibilidade | Público ou privado — o Community Cloud aceita os dois |
| Add a README | **deixe desmarcado** |

### Passo 2 — publicar o código

Um comando só. Troque o dono se preferir sua conta pessoal em vez da org:

```bash
cd "C:\Users\Vitor\OneDrive\Área de Trabalho\Leilo\opcoes-daytrade" && git remote add origin https://github.com/Leiloai/opcoes-daytrade.git && git branch -M main && git push -u origin main
```

O `credential.helper` da máquina está como `manager`, então o Windows deve
reaproveitar a credencial do GitHub sem pedir nada. Se pedir, use um Personal
Access Token como senha (github.com/settings/tokens, escopo `repo`).

### Passo 3 — publicar o app

1. Entre em <https://share.streamlit.io> e faça login com o GitHub.
2. Autorize o acesso ao repositório quando ele pedir.
3. **Create app** → **Deploy a public app from GitHub** e preencha:

| Campo | Valor |
|---|---|
| Repository | `Leiloai/opcoes-daytrade` |
| Branch | `main` |
| Main file path | `app.py` |
| Python version | **3.12** |

4. **Deploy**. O primeiro build leva 2–4 minutos.

A URL final fica no formato `https://<algo>-opcoes-daytrade.streamlit.app`, é
fixa e pode ser favoritada.

### O que muda na nuvem

- **`requirements.txt` foi enxugado para o build.** Ficam só `streamlit`,
  `pandas`, `numpy`, `requests` e `openpyxl`. Selenium, bs4, lxml e html5lib
  estão comentados: são importados apenas dentro de funções (rotas lazy), o
  contêiner não tem Chrome, e mantê-los só alongava o build. O app sobe igual
  sem eles — a Rota A2 simplesmente não entra na cascata.

- **A Rota A1 (JSON) sustenta o painel, e é incerta na nuvem.** As requisições
  sairão de um IP de datacenter, possivelmente fora do Brasil, e o
  opcoes.net.br pode recusar. Não dá para saber sem testar. Se recusar, o
  **upload de CSV/Excel** continua funcionando e é o plano B.

- **O app fica público.** Qualquer pessoa com o link abre. Não há dado pessoal
  nem credencial no projeto. Para restringir, as configurações do app no
  Community Cloud permitem lista de e-mails autorizados.

- O app hiberna após alguns dias sem acesso e acorda no primeiro request (~30s).

---

## 2. Túnel Cloudflare — link na hora, temporário

Serve o app **do seu próprio PC** para a internet. Não precisa de conta.
Dois cliques em [`abrir-web.bat`](abrir-web.bat) sobem o Streamlit e o túnel
juntos, e a URL aparece na janela.

Manualmente, em dois terminais:

```bash
streamlit run app.py
```

```bash
cloudflared tunnel --url http://localhost:8501 --no-autoupdate
```

**Limitações, sem rodeios:**

- Morre quando você fechar o terminal, desligar o PC ou cair a internet.
- A URL muda a cada execução — não dá para favoritar.
- É **pública**: qualquer um com o link abre o painel. Não há login.
- Sem garantia de uptime (tier gratuito sem conta da Cloudflare).

**Se o link não abrir no seu PC mas abrir no celular:** é cache de DNS negativo
do provedor. O resolver da Vivo às vezes cacheia `NXDOMAIN` quando o subdomínio
é consultado no instante em que o túnel nasce. Espere um minuto e recarregue, ou
aponte o DNS da máquina para `1.1.1.1`.

---

## 3. Vercel — por que não

Vercel **não roda Streamlit**: funções serverless não têm WebSocket nem processo
persistente, e o Streamlit depende dos dois. Um `streamlit run` lá trava em
"Please wait" para sempre.

O caminho viável seria **stlite** (Streamlit compilado para WebAssembly, servido
como site estático) mais uma função serverless de proxy, já que o opcoes.net.br
não envia cabeçalho CORS. Isso está construído em `web/` e foi testado
localmente com um servidor que emula a plataforma:

- ✅ o painel roda inteiro no navegador — Demo e upload completos;
- ✅ a função de proxy responde 200 para os seis ativos e 400 para o resto,
  com allowlist dupla para não virar proxy aberto;
- ❌ **a rota "Buscar no site" não completa dentro do WASM.**

O stlite embrulha `st.cache_data` num wrapper assíncrono: a função decorada
devolve uma corrotina, e o próprio cache tenta serializá-la, produzindo um erro
de `pickle` sem relação aparente com a causa. `cache_dados()` desliga o cache no
navegador e elimina esse erro, mas a busca ainda não completa — consertar exige
tornar assíncrona toda a cadeia de extração.

Duas descobertas dali viraram correções permanentes no app: a detecção de
ambiente por `sys.platform == "emscripten"` (testar `"pyodide" in sys.modules`
dá `False` no worker do stlite) e o cache condicional.

Há ainda um bloqueio de permissão: a integração Vercel conectada tem acesso de
leitura, mas não pode criar projeto (`403 forbidden`). Liberar exige escopo
"All Projects" em Settings → Integrations.

Para testar o build localmente, sirva `web/` com uma função em `/api/opcoes`.
