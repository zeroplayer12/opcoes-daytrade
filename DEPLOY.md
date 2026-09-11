# Deploy

Três caminhos, em ordem de recomendação.

---

## 1. Streamlit Community Cloud — permanente, gratuito, tudo funciona

Não depende do seu PC e **a rota "Buscar no site" funciona** (ao contrário do
build da Vercel — veja a seção 3). Mas **não é 24/7**: dorme por inatividade, veja
"O que muda na nuvem". Publicado, e privado, em
<https://zeroplayer12-opcoes-daytrade-app-2z3e1c.streamlit.app>.

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

Um comando só. O repositório real é `zeroplayer12/opcoes-daytrade`:

```bash
cd "C:\Users\Vitor\OneDrive\Área de Trabalho\Leilo\opcoes-daytrade" && git remote add origin https://zeroplayer12@github.com/zeroplayer12/opcoes-daytrade.git && git branch -M main && git push -u origin main
```

Cuidado com a credencial: o Windows guarda a da conta `Leiloai`, e o primeiro
push falhou com `Permission to zeroplayer12/opcoes-daytrade.git denied to Leiloai`.
O `zeroplayer12@` na URL força o Git a pedir a credencial da conta certa — o Git
Credential Manager abre uma janela de login uma vez e depois lembra.

### Passo 3 — publicar o app

1. Entre em <https://share.streamlit.io> e faça login com o GitHub.
2. Autorize o acesso ao repositório quando ele pedir.
3. **Create app** → **Deploy a public app from GitHub** e preencha:

| Campo | Valor |
|---|---|
| Repository | `zeroplayer12/opcoes-daytrade` |
| Branch | `main` |
| Main file path | `app.py` |
| Python version | **3.12** |

4. **Deploy**. O primeiro build leva 2–4 minutos.

URL deste deploy: <https://zeroplayer12-opcoes-daytrade-app-2z3e1c.streamlit.app>
— fixa, pode ser favoritada.

### O que muda na nuvem

- **`requirements.txt` foi enxugado para o build.** Ficam só `streamlit`,
  `pandas`, `numpy`, `requests` e `openpyxl`. Selenium, bs4, lxml e html5lib
  estão comentados: são importados apenas dentro de funções (rotas lazy), o
  contêiner não tem Chrome, e mantê-los só alongava o build. O app sobe igual
  sem eles — a Rota A2 simplesmente não entra na cascata.

- **A Rota A1 (JSON) funciona na nuvem.** Era a incerteza antes do deploy — as
  requisições saem de um IP de datacenter — mas o opcoes.net.br aceitou. Validado
  em 09/09/2026 com resultado idêntico ao da versão local. Se um dia recusar, o
  **upload de CSV/Excel** continua como plano B.

- **O app está privado** (escolha de 11/09/2026). Só abre logado na conta
  `zeroplayer12`, inclusive no celular. Tornar público é um clique em
  Share → Make this app public, mas aí qualquer pessoa com o link acessa.

- **O app dorme por inatividade — e rápido.** Publicado em 09/09/2026, já estava
  dormindo em 11/09, menos de 48h sem acesso. Ao abrir aparece *"This app has gone
  to sleep due to inactivity"* com o botão **Yes, get this app back up!**; o
  contêiner volta em segundos (16 s em 11/09), mas a tela *"Your app is in the
  oven"* pode ficar parada com o app já no ar — **recarregue a página (F5)**.
  **Não é falha** — código e
  fonte de dados continuam intactos. É a causa mais provável de "parou de
  funcionar".

- **App privado não pode ser mantido acordado por ping.** Acesso anônimo é barrado
  na borda do Streamlit (o nginx responde `303 → share.streamlit.io/-/auth`) e
  nunca chega ao contêiner, então um agendador que "pinga" o app não conta como
  atividade. Saídas: tornar o app público (aí o ping funciona), aceitar a espera
  ao acordar, ou usar a versão local (`streamlit run app.py` ou `abrir-web.bat`)
  quando estiver no PC.

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
