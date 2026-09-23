# 📈 Painel Day Trade — B3

Painel Streamlit em duas abas:

- **Opções** — filtra a grade de opções e aponta, em segundos, a **melhor Call** e a
  **melhor Put** do dia para `BOVA11`, `PETR4`, `VALE3`, `BBAS3`, `ITUB4` e `BPAC11`.
- **Correlações** — o que os índices futuros, commodities, câmbio e ADRs fizeram
  enquanto a B3 estava fechada, e o quanto cada mercado anda junto com os seis ativos.
  Abre direto em `http://localhost:8501/?aba=correlacoes`.

## Como rodar

```bash
cd "C:\Users\Vitor\OneDrive\Área de Trabalho\Leilo\opcoes-daytrade"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Abre sozinho em `http://localhost:8501`.

Se preferir rodar direto no Python global (3.14), o painel já funciona — `streamlit`,
`pandas`, `numpy`, `lxml` e `requests` estão instalados. Faltam só três, e as três são
puro Python (instalam sem compilar):

```bash
pip install selenium webdriver-manager beautifulsoup4 openpyxl
```

Sem elas o app sobe igual: `requests` e `bs4`/`selenium` são importados de forma
tolerante, e as rotas que dependem deles simplesmente não entram na cascata.

Primeiro teste sem depender do site: marque **Demo** na barra lateral. Ele gera uma
grade sintética por Black-Scholes e exercita todos os filtros e a UI.

## Sempre ligado no PC

Desde 14/09/2026 o painel sobe sozinho quando o Windows inicia e fica em
**http://localhost:8501** — só neste PC, sem depender do Streamlit Cloud, que dorme
após 12h sem visita.

| Peça | Onde | O que faz |
|---|---|---|
| `Painel de Opcoes.vbs` | pasta Inicializar (`Win+R` → `shell:startup`) | roda o `iniciar-painel.cmd` escondido ao entrar no Windows |
| `iniciar-painel.cmd` | aqui no projeto | sobe o Streamlit em `127.0.0.1:8501` e, se cair, tenta de novo a cada 30 s |
| `parar-painel.cmd` | aqui no projeto | encerra o painel e o laço de reinício |
| `painel.log` | aqui no projeto | log; acima de 5 MB vira `painel.old.log` |
| `painel.inicio.log` | aqui no projeto | anota quando uma segunda partida é recusada |

- **Privado de verdade:** escuta só em `127.0.0.1`, então nem outro aparelho da
  rede de casa acessa. Por isso não abre no celular.
- **Para abrir de fora:** `publicar-painel.cmd` publica este mesmo painel em
  `https://painel.quantunlab.com.br`, com senha, por um túnel da Cloudflare — o
  Streamlit continua ouvindo só no `127.0.0.1`. Passo a passo no
  [DEPLOY.md](DEPLOY.md), seção 1.
- **Desativar o início automático:** apague `Painel de Opcoes.vbs` da pasta Inicializar.
- **Uma instância só:** se o painel já estiver no ar, uma segunda execução do
  `iniciar-painel.cmd` sai sozinha em vez de ficar disputando a porta.
- O endereço vai por linha de comando, não pelo `.streamlit/config.toml`: fixar
  `127.0.0.1` ali quebraria o deploy do Streamlit Cloud.
- O `abrir-web.bat` abre um túnel **público** — não use se quiser manter privado.
- Upload de `.xlsx` precisa do `openpyxl`, que não está instalado neste Python
  (`pip install openpyxl`). CSV funciona sem ele.

## Os filtros aplicados, na ordem

| # | Filtro | Regra |
|---|--------|-------|
| 1 | **Nomenclatura** | Só a série mensal convencional da B3. Aplicado **antes** de tudo, para o topo do ranking de liquidez ser sempre um contrato padrão. |
| 2 | **Vencimento** | **2 a 20 dias úteis**, apenas 3ª sexta, **mais o mensal seguinte** para comparar o curto com o próximo (desligável em *Incluir o vencimento seguinte*; o padrão continua sendo o mais curto). Seletor mostra `data · N DU`, com feriados da B3 calculados (Carnaval, Sexta Santa e Corpus Christi via algoritmo da Páscoa). |
| 3 | **Frescor** | Descarta o que não negocia há mais de N pregões. Necessário porque o Delta é calculado do preço — veja abaixo. |
| 4 | **Delta** | `\|Δ\|` entre **0,50 e 0,70** — Calls de +0,50 a +0,70, Puts de -0,50 a -0,70. |
| 5 | **Liquidez** | Ordena por **Volume Financeiro ↓** e **Núm. de Negócios ↓**. **O topo da lista é a opção escolhida.** |

### Como a nomenclatura é filtrada

O regex é `^[A-Z]{4}[A-Z]\d+$` — raiz de 4 letras, letra de série, dígitos, e **nada
depois**. A âncora no fim é o que faz o trabalho: semanais e séries atípicas carregam
sufixo (`PETRI483W4`, `W1`..`W5`).

Um `ticker.str.contains("W")` seria errado por dois motivos:

- descartaria raízes que contêm W — `WEGE3` gera `WEGEI50`;
- descartaria **puts de novembro**, cuja letra de série é justamente `W` (`PETRW38`).

Em PETR4 o filtro corta 796 de 1391 linhas, e a separação é limpa: todo vencimento
semanal tem 0 tickers padrão, todo mensal tem 100%.

### Por que o piso é 2 e não 8 dias úteis

Vencimentos mensais distam ~21 dias úteis entre si. Com piso em 8 DU e teto em 20, a
janela ficava **vazia na semana anterior a cada vencimento** — o mensal mais próximo a
~7 DU e o seguinte a ~26, nenhum elegível. Acontecia todo mês, justamente na semana de
maior movimento. Por isso o piso é **2 DU**.

Se a janela ficar vazia mesmo assim, **não é erro**: o painel explica a situação, lista
os mensais mais próximos com seus dias úteis e aponta as três saídas (ajustar o slider,
marcar *Mostrar vencimentos fora da janela*, ou desmarcar *Somente séries mensais
padrão*). A regra nunca é alargada sozinha.

Perto do vencimento o Delta calculado fica mais sensível: com pouco valor no tempo, a
inversão da volatilidade implícita perde precisão e o Delta salta rápido entre 0 e 1 a
cada centavo do ativo. Vale conferir no home broker antes de operar a 2 ou 3 DU.

O ganho de liquidez justifica o filtro. Em 09/09/2026, mesma faixa de Delta, mesmo ativo:

| Vencimento | | Melhor put | Negócios | Volume |
|---|---|---|---|---|
| 18/09 | MENSAL (7 DU) | `PETRU19` | 349 | R$ 1.353.766 |
| 25/09 | semanal (12 DU) | `PETRU498W4` | 1 | R$ 1.690 |

## Fontes de dados (cascata)

| Rota | Como | Quando entra |
|------|------|--------------|
| **A1** | `requests` no endpoint JSON interno do opcoes.net.br | Primeira tentativa, ~5s para 8 vencimentos |
| **A2** | `Selenium` + `webdriver-manager`, Chrome headless, lê a tabela renderizada | Se A1 falhar |
| **B** | **Upload de CSV/Excel** exportado do próprio site | Se o site bloquear/exigir login |
| **Demo** | Grade sintética (Black-Scholes) | Para testar a interface offline |

O endpoint JSON é um contrato **não público**, mas ele devolve a própria definição das
colunas em `data.columns` — os nomes são lidos de lá, não adivinhados por posição. Se o
site inserir ou reordenar uma coluna, o mapeamento continua correto. O retorno ainda
passa por `_valida_grade()` antes de ser aceito.

As linhas não carregam o vencimento (semanais e mensais dividem a mesma letra de série),
então busca-se **um vencimento por vez** — o site filtra no servidor via `vencimentos=` —
e a coluna é carimbada no cliente. Por padrão traz todos os vencimentos até 40 dias
úteis, o que deixa o seletor trocar de ciclo sem nova ida à rede.

---

## O Delta é pago — e o painel calcula o dele

**Descoberta ao ligar a rota no site:** no plano gratuito do opcoes.net.br os gregos vêm
censurados. Delta, Gamma, Theta, Vega e Vol. Implícita chegam como
`<img src="/images/volblur.png">` — uma imagem borrada, em **374 de 374 linhas**. Preço,
strike, volume e número de negócios são gratuitos; Delta não é.

Como o Delta é o filtro central do painel, ele é calculado aqui:

1. **Spot** — deduzido de `strike / (1 + Distância % do Strike)`. Só é aceito se as
   linhas concordarem entre si (dispersão < 5%), o que também valida a suposição.
2. **Volatilidade implícita** — invertida do preço negociado por bisseção sobre
   Black-Scholes (`volatilidade_implicita`), 60 iterações.
3. **Delta** — sai do `d1` com essa volatilidade.

Na prática o resultado bate: para PETR4 a 8 dias úteis a IV fica em 32–40% perto do
dinheiro, com smile correto, e o Delta cai suave de 0,99 a 0,01 conforme o strike sobe.

### Frescor da cotação — não é opcional

O Delta sai do **preço negociado**, então cotação velha produz IV absurda e Delta sem
significado. E o volume não protege: `ITUBU515` tinha R$ 342 mil acumulados e ficaria em
**2º lugar** no ranking de liquidez, mas seu último negócio era de três semanas antes —
o preço parado implicava IV de 201% e um Delta de −0,64 numa put 21% dentro do dinheiro,
que deveria estar perto de −1.

Duas travas resolvem:

- **Teto de IV** em 150% (`completar_delta(iv_maxima=...)`): acima disso o preço não é
  compatível com o spot de hoje e a linha é descartada.
- **Filtro de frescor** (`Máx. pregões desde o último negócio`, padrão 1): compara a data
  do último negócio de cada opção com o pregão mais recente da própria grade. Aparece no
  funil de filtragem, e desliga sozinho quando a fonte não traz data.

### Limites honestos

- **`Var. (%)` fica vazia na rota do site.** O endpoint gratuito não publica variação
  diária — só distância do strike e prêmio como % da cotação. A coluna preenche quando os
  dados vêm de um CSV que a tenha.
- **Calls americanas usam fórmula europeia.** A B3 lista os dois estilos para o mesmo
  ativo (para PETR4: 84 calls americanas, 103 europeias, 187 puts europeias). O prêmio de
  exercício antecipado é ignorado; em opções curtas na faixa de Delta 0,50–0,70 o erro é
  pequeno, mas existe.
- **Dividendos não entram no modelo.** Se houver data-com antes do vencimento, o Delta
  das opções sobre a ação sai enviesado.
- **A taxa livre de risco é um parâmetro**, na seção *Modelo* da barra lateral (padrão
  10,75% a.a.). Em opções curtas o efeito sobre o Delta é pequeno.

## Aba Correlações

Para a checagem de antes da abertura: como foram a madrugada e a manhã lá fora.

| Bloco | O que mostra |
|---|---|
| **Pulso** | Ibovespa, dólar e DI futuros, S&P 500 futuro, Brent, minério de ferro, Hang Seng e EWZ: último, variação e a curva da sessão |
| **Seus ativos hoje** | um cartão por ativo com os mercados que mais andam com ele, em linguagem simples: *anda junto* ou *anda contra*, força (fraca, moderada, forte), o movimento de agora e se ele *puxa para cima* ou *para baixo*; no topo, a **pressão** somada. Janela **Pregão de hoje** (barras de 5 min) ou de 20, 60 e 120 pregões |
| **Matriz completa** | num expander: os 6 ativos × 12 mercados (EWZ, S&P 500, VIX, Hang Seng, Brent, Minério, Cobre, Rio Tinto, Ouro, Dólar, DXY, Treasury 10 anos), para consulta |
| **Cotações por mercado** | 51 mercados em 8 quadros — EUA, Brasil (Ibovespa e dólar futuros, ADRs e EWZ), Europa, Ásia, Energia, Metais e mineração, Câmbio e juros (com DI curto, médio e longo), Agrícolas |

A aba se atualiza sozinha a cada minuto (desligável na barra lateral) e só roda quando
está aberta: quem fica na aba Opções não espera pelas cotações globais.

### De onde vêm os dados

- **Yahoo Finance**, pelo endpoint `v7/finance/spark` (não oficial): até 20 símbolos por
  chamada, três chamadas em paralelo. Cache de 1 min para as cotações e de 1 h para o
  histórico diário. As três fontes são consultadas ao mesmo tempo (~2,5 s no total), e uma
  fora do ar só deixa em branco os mercados dela.
- **Por que não o investing.com:** o rodapé do site proíbe usar, armazenar ou reproduzir
  os dados dele sem autorização por escrito. Os mesmos futuros e índices existem no
  Yahoo, com o mesmo contrato de referência.
- **Minério de ferro:** API pública do SGX (IODEX 62% Fe, US$/t), no contrato mais
  negociado entre os três primeiros vencimentos — o mês corrente costuma negociar menos
  que o seguinte. O SGX não publica curva intradiária, então o bloco mostra só preço e
  variação; a correlação usa os ajustes diários pela data do pregão (`record-date`). O
  `TIO=F` do Yahoo está parado desde agosto e não serve.
- **Ibovespa futuro:** mini-índice (WIN) do vencimento vigente, pelo site de cotações da
  B3, com 15 min de atraso. Antes do primeiro negócio aparece o preço teórico do leilão
  de abertura (etiqueta `LEILÃO`); sem ele, o ajuste anterior (`AJUSTE`). A variação é
  sempre contra o ajuste do dia anterior. O dólar futuro (WDO) vem do mesmo lugar; a
  lista da B3 mistura futuros e opções, então só entra o mercado `FUT`.
- **DI futuro:** DI1 da B3 em três vértices que rolam sozinhos — curto (o primeiro janeiro
  a mais de 90 dias; hoje, jan/27), médio (+2 anos, jan/29) e longo (+4 anos, jan/31). É
  taxa, então a variação aparece em pontos percentuais (`+0,10 pp`), como no Treasury.
- **ADRs e EWZ fora do pregão:** no pré e no pós-mercado de Nova York, o preço é o do
  último negócio estendido e a variação é contra o fechamento regular (etiqueta `PRÉ`).
- **Atraso:** varia por bolsa (nos futuros dos EUA, até ~10 min). A coluna *Hora* mostra
  o horário do último preço, em Brasília; mercado parado há mais de 30 min fica em cinza.

### Como a correlação é calculada

Pearson dos retornos diários, com três cuidados:

1. **Data local de cada bolsa.** A barra diária do S&P futuro e a do Hang Seng têm
   carimbos UTC diferentes, mas são o mesmo dia; o cruzamento é pela data local.
2. **Retorno sobre o próprio pregão anterior**, antes de cruzar as séries. Assim um
   feriado só nos EUA não apaga o retorno do dia seguinte na B3.
3. **Últimos N pregões em comum** por par; par com menos da metade da janela fica em
   branco.

A Ásia fecha antes da B3 abrir: ali o número mede o quanto o pregão asiático antecipa o
nosso. Correlação passada não garante o movimento de hoje.

**Pregão de hoje.** Mesma conta, com retornos de 5 minutos desde a abertura da B3 (antes
dela, o último pregão). As barras vêm do Yahoo com o carimbo arredondado para o múltiplo
de 5 min, e cada par precisa de ao menos 8 barras em comum (40 min). Ásia e minério não
negociam no horário da B3 e ficam em branco.

**Pressão.** Para cada ativo, soma ρ × (variação do mercado desde o fechamento anterior ÷
o desvio-padrão diário dele nos últimos 60 pregões), com até três mercados de |ρ| ≥ 0,3 —
os mesmos que aparecem no cartão do ativo. Dividir pelo desvio-padrão impede que o VIX, que anda 10% num dia
comum, pese mais que o S&P, que anda 1%. Abaixo de 0,5 em módulo, fica "sem direção
clara". É um resumo do que a tabela já mostra, não um sinal de entrada.

### Ajustes a pedido dele (15/09/2026)

O cartão da VALE3 traz o **minério de ferro sempre na primeira linha** (`FIXOS_NO_CARTAO`),
mesmo quando a correlação é fraca: é o preço do produto dela, e ele quer ver o número todo dia.
Nos últimos 60 pregões a relação medida é de apenas +0,17 — a Rio Tinto (+0,60) anda bem mais
junto. Na janela "Pregão de hoje" o minério não aparece: o SGX não negocia no horário da B3.

Saíram da aba o quadro que explicava "anda junto / anda contra / pressão" e o rodapé com o
aviso de uso próprio e a nota do Yahoo — a leitura em palavras já está em cada cartão.

## Design

**Liquid glass** sobre uma base escura e densa (skill *ui-ux-pro-max*), com Inter na
interface e Fira Code nos tickers. O material segue a regra do próprio estilo: vidro claro
e brilhante na navegação e nos controles (abas em cápsula, seletores, botões, selos), e
vidro fosco **escuro** no conteúdo, para os números não perderem contraste. Atrás de tudo,
uma luz difusa fixa (azul, violeta e verde-água) que o vidro desfoca ao rolar a página;
cada cartão tem uma borda de luz mais forte no canto de cima, como vidro curvo.

- **Contraste medido na tela, não estimado:** o fundo real de cada cartão é recortado do
  screenshot e comparado com as cores de texto — pior caso 5,7:1 no texto secundário
  (mínimo 4,5:1) e 4,3:1 na cor de put (mínimo 3:1 para marcas de dados).
- **Sem transparência quando pedido:** com "reduzir transparência" ligado no sistema, ou num
  navegador sem `backdrop-filter`, os painéis ficam opacos.

A cor fica reservada aos dados:

- **Call e put são identidade, não bom/ruim:** azul `#3987e5` e laranja `#d95926`, par
  validado contra a superfície dos cards (`#0B0F1A`) — separação para daltonismo ΔE 26,8.
- **Verde e vermelho só em variação com sinal**, sempre com seta.
- **Matriz de correlação:** azul ↔ vermelho com meio cinza (ΔE 19,2 para daltonismo,
  29,0 em visão normal); a mistura tem teto de 72%, o que mantém o número de cada
  célula com contraste mínimo de 5,1:1.
- Na aba Opções: carga automática ao escolher ativo e vencimento, faixa de KPIs com
  Put/Call por volume, cartões da melhor call e da melhor put com medidor de Delta,
  gráfico espelhado de liquidez por strike (só os elegíveis ganham cor), rankings e o
  funil *Como a escolha foi feita*.
- O layout segue a largura do contêiner: em telas ultralargas, call, gráfico e put ficam
  lado a lado; no celular, tudo empilha.

## Aba Operações

Acompanha as estratégias que você roda no Profit: para cada ativo, se há operação em
andamento, com entrada, parcial, alvo final, stop e o resultado em R$ e em %, mais o
gráfico de candles do pregão com as linhas da operação. Abre direto em
`http://localhost:8501/?aba=operacoes`.

- **As estratégias são traduzidas** do código do Profit para `operacoes.py` (hoje: VALE3
  em 10 min, PETR4 em 20 min, BPAC11 em 15 min, BBAS3 em 10 min, BOVA11 em 60 min e ITUB4
  em 15 min; BPAC11, BBAS3 e BOVA11 são a mesma família de pullback, com parâmetros
  diferentes). A ITUB4 só compra e faz no máximo uma operação por dia; desde 15/09/2026
  o toque no stop é testado antes de a parcial levar o stop ao zero a zero (o código antigo
  movia antes e zerava no candle da 2R se ele tivesse passado pela entrada). Na PETR4,
  `ADX(14, 0)` foi lido como ADX sem suavização (o próprio DX) e `RSI(14, 0)` como o IFR
  clássico de Wilder — as leituras com médias aritméticas e com o ADX suavizado não batem
  melhor com a lista do Profit; na BPAC11 e na BBAS3, `Time`
  como o horário de abertura do candle (entrada até o candle das 16:30 e das 14:00).
  Nessas duas o stop não é ordem stop — o toque no nível fecha a posição a mercado no
  candle seguinte — e a saída das 17:40 do código nunca dispara (compara `Time`, em HHMM,
  com `174000`). O código da BBAS3 não pinta o candle do sinal; o painel pinta mesmo
  assim, para o gráfico mostrar onde a operação começou. A simulação segue o backtest do Profit: o código roda no fechamento do candle,
  a entrada é na abertura do candle seguinte e ordens de saída valem só para o próximo
  candle. A posição **não é zerada às 17h**: segue de um pregão para o outro até o alvo
  ou o stop, como nas estratégias do Profit — por isso a simulação parte de 60 dias de
  histórico. Quando um candle toca stop e alvo, o motor assume o stop.
- **Conferência com o Profit (listas de operações de 22/06 a 14/09/2026):** 97 de 142
  operações iguais ao backtest do Profit (mesma entrada, mesmo preço, mesmo resultado) —
  BBAS3 15/16, BPAC11 32/43, VALE3 29/44, PETR4 13/24, BOVA11 6/12, ITUB4 2/3. Para isso o
  motor reproduz o backtest do Profit em:
  - **parcial de 50 ações não executa:** fica fora do lote padrão de 100 da B3, e nas listas
    da VALE3, da PETR4 e da ITUB4 nenhuma operação tem parcial. O que sobra dela é o stop no
    zero a zero (e, na ITUB4, o alvo final ligado). O cartão mostra o nível, marcado como
    "não executa (50 ações)";
  - **execução no preço da ordem:** limite sai na abertura se o candle abre além dela (alvos
    de 1,3% da BPAC11 saíram com 2 a 3% depois de gap) e, senão, no próprio preço;
    stop-limite sai no disparo quando o candle chega nele e, em gap, na abertura se ela
    estiver dentro do limite;
  - **histórico ajustado por proventos** com os fatores que o Profit usa, medidos nas listas
    (`FATORES_PROFIT`) — o valor do provento no Yahoo não reproduz o ajuste dele;
  - **leilão de fechamento dentro do último candle do pregão** e **after-market alinhado às
    17:30** (no BOVA11 de 60 min, sinal no candle das 16:00 entra às 17:30); o Yahoo perde o
    leilão nos pregões passados, e o painel o recria com o fechamento e o volume diários;
  - **parâmetros configurados no Profit, diferentes do código enviado** (confirmados em 15/09/2026): VALE3 com alvo
    final em 2,5R (o código traz 1,85) e BBAS3 com filtro de força de 0,0035 (o código
    traz 0,0025).

  O que ainda difere vem quase todo dos dados: o Yahoo não tem o after-market (o Profit roda
  as estratégias nele, com entradas às 17:30 e saídas até 18:20) e às vezes perde candles, e
  uma operação diferente desloca as seguintes. Os pregões gravados pelo coletor RTD vêm do
  próprio Profit, com leilão e after-market, e substituem o Yahoo, então a simulação fica
  mais fiel a cada dia gravado.
- **Stop a mercado em todas (15/09/2026):** a VALE3 e a PETR4 usavam stop-limite de 0,05
  (`SellToCoverStop(stop, stop - 0.05)`), que não executa num gap maior que 0,05 e deixa a
  posição aberta sem proteção até o preço voltar — justo o caso de quem carrega posição
  para o dia seguinte. Agora, como as outras quatro, saem a mercado na abertura do candle
  seguinte ao toque; o teste do toque vem antes de a parcial ou o breakeven moverem o stop.
  O código novo das duas foi entregue para atualizar no Profit. As listas de operações
  antigas se conferem com `stop_mercado=False`.
- **BBAS3 fora das abas Opções e Operações (15/09/2026):** a estratégia dela saiu da carteira
  recomendada, então ela não vira mais cartão nem opção sugerida (`ATIVOS_OPERADOS`). Continua
  na aba Correlações, onde serve de contexto do pregão, e o código dela segue em `operacoes.py`.
- **A opção do sinal no cartão (15/09/2026):** ele opera comprando a call no sinal de compra e a put
  no de venda. Cada posição aberta mostra a opção que as regras da aba Opções escolheriam (|Δ| 0,50–0,70,
  série mensal padrão, 2 a 20 DU mais o seguinte, a mais líquida), o preço estimado dela agora, na
  parcial, no alvo e no stop (Black-Scholes com a vol. implícita de hoje) e quanto ela perde por dia
  parada. O vencimento pula o curto quando ele não cobre o tempo típico das operações vencedoras da
  estratégia mais 3 DU (`DIAS_TIPICOS`: VALE3 2, PETR4 3, BPAC11 2, BBAS3 2, ITUB4 6, BOVA11 5).
- **Lote de 200 ações (15/09/2026):** é o que ele opera. Com 200, a parcial da VALE3 e da
  PETR4 (metade da posição, 100 ações) executa; a da ITUB4 é fixa em 50 no código e
  continua sem executar. As listas de operações transcritas eram de 100 ações, e o
  `compara_profit.py` simula cada uma com o lote dela.
- **BPAC11 com alvo de 2% (15/09/2026):** no backtest do Profit de 01/01/2022 a 15/09/2026,
  com 200 ações, alvo de 2% e zero a zero ao fechar a 70% do caminho até o alvo deu R$ 8.256
  contra R$ 6.892 do original (1,3%): fator de lucro 1,24 × 1,20 e lucro por operação
  R$ 9,18 × 6,79, com a mesma maior perda e queda topo-fundo parecida — em troca de acerto
  menor (38% × 45%) e sequências de perda mais longas. O lucro por operação maior é o que
  mais pesa: os custos da B3 comem boa parte dos R$ 6,79 do original. O zero a zero quase
  não muda o resultado; ficou porque foi a combinação testada.
- **ITUB4 com zero a zero a 50% (15/09/2026):** no backtest de 01/01/2022 a 15/09/2026 (200
  ações, risco máximo de 1,35%), alvo final de 4,4R desde a entrada e stop no zero a zero
  quando um candle fecha a 50% do caminho até ele deu R$ 2.960 contra R$ 2.660 da gestão
  antiga (parcial fixa de 50 ações em 2R, que não executa, e zero a zero no toque da 2R):
  fator de lucro 3,33 × 3,21 e queda topo-fundo R$ 202 × 288. São só 57 operações, então a
  vantagem é pequena. `Itub4(gatilho_be=0)` volta à gestão antiga.
- **BOVA11 com zero a zero a 70% (15/09/2026):** no backtest de 01/01/2022 a 15/09/2026 (200
  cotas), stop no zero a zero quando um candle fecha a 70% do caminho até o alvo deu R$ 14.490
  contra R$ 13.838 da original, fator de lucro 1,49 × 1,43 e queda topo-fundo R$ 3.932 × 4.992,
  com a mesma maior perda (R$ 866, de gap). A 50% ficou abaixo da original no saldo, e a parcial
  de 50% rendeu 36% menos com queda maior. `Bova11(gatilho_be=0)` volta à anterior.
- **VALE3 sem parcial, com zero a zero a 50% (15/09/2026):** no backtest de 01/01/2022 a
  15/09/2026 (200 ações), tirar a parcial de 1R e levar o stop ao zero a zero só quando um
  candle fecha a 50% do caminho até o alvo de 2,5R deu R$ 16.416 contra R$ 14.342 da gestão
  anterior (parcial e zero a zero em 1R): fator de lucro 1,35 × 1,31, lucro por operação
  R$ 18,44 × 13,92 e queda topo-fundo R$ 1.776 × 1.911, com a mesma maior perda. Zero a zero a
  70% deu R$ 15.988 e só alvo e stop R$ 15.202. `Vale3(parcial_antiga=True)` volta à anterior.
- **Tempo real pelo Profit:** o `coletor_rtd.py` lê as cotações dos 6 ativos no servidor
  RTD do Profit (o mesmo do Excel: `=RTD("RTDTrading.RTDServer";; "VALE3_B_0"; "ULT")`,
  campos ULT, QTT, VOL, NEG e HOR) e grava cada mudança em `dados_rt/AAAA-MM-DD.csv`.
  A aba monta as barras de 5 min de cada pregão gravado (com leilão e after-market, até
  18:30) e atualiza a cada 1 minuto. Ele sobe
  junto com o painel (`iniciar-painel.cmd`), espera o Profit abrir, reconecta se o
  Profit fechar, roda uma instância só e registra tudo em `coletor.log`; o
  `parar-painel.cmd` encerra o painel, o coletor e o vigia.
- **O servidor RTD do Profit atende um cliente por vez:** outro programa que se conectar
  (um teste, um segundo coletor) toma a conexão, e o primeiro para de receber sem aviso nenhum
  (visto em 15/09/2026) — qualquer leitura nova do RTD tem de entrar no próprio coletor.
- **Quando o Profit para de mandar cotação (15/09/2026):** aconteceu duas vezes no mesmo
  pregão — das 13:04 às 15:05 e das 16:08 em diante — com o profitchart.exe de pé. **A causa
  é a licença do Profit: um login por vez.** Ele abriu o Profit no celular para acompanhar as
  operações fora do escritório, e o acesso no PC caiu junto com o RTD, sem fechar o programa.
  Quando ele volta a logar no PC, o Profit baixa o pregão inteiro de novo, então o cache de
  candles fica completo e o painel se acerta sozinho. O que o painel faz enquanto isso:
  - o coletor **não grava o preço parado**. A cada conexão ele registra o estado dos ativos;
    se o último negócio (HOR) é mais de 5 min mais velho que o relógio, a linha é só uma cópia
    do preço de antes e fica de fora. Sem isso, cada reconexão vira um candle achatado — na
    primeira versão do vigia de conexão foram 33 linhas falsas, das 16:14 às 18:07, e o gráfico
    do painel divergiu do Profit;
  - `barras_rtd` descarta essas linhas também na leitura, para os arquivos já gravados;
  - o coletor reconecta depois de **5 min** de silêncio no pregão e **dobra a espera** a cada
    tentativa vã, até 30 min (o silêncio costuma ser do lado do Profit, e reconectar não resolve);
  - o buraco deixado no meio do pregão volta a ser preenchido: `juntar_barras` deixa o Yahoo
    valer onde o RTD não tem barra nenhuma, e o que ainda faltar sai do **cache de candles do
    próprio Profit** (`completar_com_profit`), que tem leilão e after-market. Conferido no dia:
    23 candles de 20 min da PETR4, 45 de 10 min da VALE3 e 30 de 15 min da BPAC11, todos iguais
    aos do Profit, com diferença máxima de R$ 0,01 no fechamento;
  - a pílula do cabeçalho passa a dizer **"Profit · sem cotação desde HH:MM"** (antes ela olhava
    a data do arquivo, que continuava sendo escrito, e dizia "tempo real");
  - se o coletor ou o vigia morrerem (fechar o Profit derruba o coletor junto), o painel sobe os
    dois de novo em até 1 minuto — cada um tem mutex nomeado, então cópia repetida sai sozinha.
- **Opção ao vivo (15/09/2026):** o opcoes.net.br grátis mostra o último negócio, muitas vezes
  do pregão anterior. O painel e o vigia anotam em `dados_rt/opcoes/assinar.json` a opção
  sugerida de cada posição, e o coletor assina ela também no RTD — o Profit entrega opção sem
  cadastrar na lista, com último (`ULT`), melhor compra (`OCP`) e melhor venda (`OVD`) — e grava
  em `dados_rt/opcoes/` (um CSV por pregão e `agora.json` com a última de cada uma). O cartão
  mostra "ao vivo", usa o meio do book como preço de agora e tira dele a volatilidade
  implícita com que estima a parcial, o alvo e o stop. Sem o coletor (ou na nuvem), a
  cotação vem do site da B3 (`InstrumentQuotation`, ~15 min de atraso, com a ação do mesmo
  instante); sem as duas, do opcoes.net.br.
- **Carteira operada (18/09/2026): VALE3, PETR4 e ITUB4.** BPAC11 e BOVA11 foram pausadas como a
  BBAS3 (`PAUSADOS` no `app.py` e `avisos.PADRAO`): no backtest do Profit desde 2018 elas não se
  sustentaram depois do custo e perderam dinheiro na opção. Saem das abas Opções, Operações e
  Realizadas, dos avisos e do resultado mês a mês; continuam nas Correlações, como contexto. O
  código delas fica em `operacoes.py`, para voltar a operar é só tirá-las das duas listas.
- **Avisos (15/09/2026):** sinal, parcial, zero a zero, stop tocado e saída das estratégias
  operadas (hoje VALE3, PETR4 e ITUB4) avisam em três lugares:
  - **no navegador:** balão na aba Operações, três bipes e o título da aba piscando, com a
    atualização automática ligada (chega em até 1 min) e a faixa "Avisos de hoje" acima dos
    cartões;
  - **no Windows e no Telegram:** pelo `vigia.py`, que sobe junto com o painel e roda com o
    navegador fechado — recalcula as estratégias a cada 15 s no pregão com o mesmo código da
    aba e, no sinal, manda a opção sugerida com o preço agora, no alvo e no stop. Ao ligar, só
    avisa o que aconteceu nos últimos 15 min; o que já foi avisado fica em
    `dados_rt/vigia_estado.json`. Log em `vigia.log`; `python vigia.py --uma` mostra os avisos
    dos últimos dias sem enviar e `--teste` manda um de teste;
  - o botão **Testar avisos** na barra lateral testa os três de uma vez.

  O Telegram liga com `python configurar_telegram.py`: você cria o bot no @BotFather, cola o
  token (a digitação não aparece) e manda uma mensagem ao bot para o script descobrir o seu
  chat. A configuração (ativos avisados, Windows ligado, token) fica em
  `%LOCALAPPDATA%\PainelDayTrade\avisos.json`, fora do OneDrive e do Git. A notificação do
  Windows sai pelo PowerShell (aparece como "Windows PowerShell") e o *Assistente de foco* /
  *Não perturbe* do Windows pode segurá-la.
- **Resultado mês a mês (15/09/2026):** a seção no fim da aba mostra, mês a mês desde 2022, quanto
  as estratégias operadas teriam dado — calendário por ano, total da carteira ou de uma
  estratégia, e a tabela com todas elas num expander. Vem do `resultados.py`: o motor do painel
  rodando sobre os candles que o próprio Profit guarda no disco
  (`%APPDATA%\Nelogica\Profit_Profit-cm\database`, arquivos `.min` de 128 bytes por candle, preço
  bruto, ajustado aqui por desdobramentos e proventos), 200 ações, sem custos. O vigia refaz a
  conta todo dia depois das 18:40 (~30 s); à mão, `python resultados.py`. É resultado em ações:
  com a opção, o ganho acompanha o delta e a opção perde valor com o tempo.
- **Aba Realizadas (17/09/2026):** as operações encerradas das estratégias operadas em 7, 30 ou 90 dias
  (`?aba=realizadas`), para conferir contra a lista de operações do Profit: sinal, entrada, saída
  com o motivo (alvo, stop, zero a zero, parcial com a fração), o resultado **na ação** (% sobre a
  entrada) e o resultado **na opção** — compra no sinal (call na compra, put na venda) e venda nas
  saídas da estratégia, na mesma proporção. Mesma simulação e mesmos candles da aba Operações.
  - **Qual opção:** a anotada em `dados_rt/diario_opcoes.json` na primeira vez que a operação aparece
    no painel ou no vigia (é a sugerida no sinal, a que ele compra). Operações de antes do diário
    usam uma equivalente pelas regras: vencimento mensal que cobre o tempo típico + folga e strike
    de |Δ| 0,55 na entrada, marcada "(equivalente)".
  - **Qual volatilidade (corrigido em 17/09/2026):** a da **própria opção** — a implícita do preço que
    o coletor gravou em qualquer ponta, ou a da cotação de hoje dela, ou a de uma opção recente do
    mesmo ativo (`_iv_recente`). A histórica da ação só entra quando não há opção nenhuma para
    consultar: ela fica bem abaixo da implícita e, misturada com um preço real, dá resultado sem
    sentido. Foi o que aconteceu com a BOVAV37 de 17/09 — entrada estimada em R$ 4,97 com vol. de 19%
    contra R$ 7,0 do book, saída gravada de R$ 6,00, e a operação apareceu como +20,7% quando foi
    −14,1%. A célula da opção mostra "gravado", "parte gravado" ou "estimado", e a dica do mouse diz
    de onde veio a volatilidade.
  - **Qual preço:** o que o coletor gravou da opção no instante da execução — abertura do candle
    para entrada e stop; para alvo e parcial, o primeiro negócio gravado da ação que chegou no
    preço da ordem. Sem gravação, Black-Scholes com a ação no preço da execução e a volatilidade
    implícita da entrada gravada ou, sem ela, a histórica de 20 pregões com folga de 15%. A célula
    diz "gravado", "estimado" ou "parte gravado".
- **Candles do Profit na simulação (17/09/2026):** a venda do BOVA11 das 11:00 abriu no Profit e não
  apareceu no painel, por dois motivos em sequência — os dois vinham de o painel montar candles com
  dados que não são os do Profit:
  - nos **dias anteriores** faltavam os candles do after-market (17:30, 18:30) e o leilão de
    fechamento, que o Yahoo não tem e as estratégias usam. Agora os pregões passados (últimos 120
    dias) saem inteiros do cache de candles do próprio Profit (`usar_candles_do_profit`);
  - **hoje**, o candle das 10:00 tinha os mesmos preços, mas o volume vinha em parte do Yahoo (o RTD
    ficou mudo das 10:24 às 11:13), e o pullback exige volume acima da média de 20 candles. Agora
    todo candle de hoje que o Profit já fechou e gravou entra inteiro, com o volume dele; o RTD fica
    com o candle em formação.

  Conferido nos cinco ativos operados: candles fechados desde 11/09 idênticos aos do Profit e as
  posições abertas iguais às da simulação só com os candles dele. A entrada da PETR4 passou de
  48,03 (abertura de 17/09) para 48,69 (after-market das 17:30 de 16/09), como no Profit.
- **Sem o Yahoo o painel continua de pé (17/09/2026):** o `query1.finance.yahoo.com` passou a recusar
  e a estourar o tempo de resposta, e como `_barras_historico` não tratava o erro, o ativo inteiro
  ficava sem candles: cartões em "sem dados" e ativos sumindo da aba Realizadas (a VALE3 tinha 15
  operações fora da lista). Agora a falha do Yahoo volta um histórico vazio e o cache do Profit
  (120 dias) sustenta a simulação sozinho; a aba Realizadas avisa quais ativos ficaram de fora.
- **Aviso de Profit sem cotação (17/09/2026):** no pregão, se passar de 10 min sem negócio novo no
  RTD, o vigia avisa (Windows e Telegram) que o painel passou a usar o Yahoo e o cache, e avisa de
  novo quando a cotação volta. O estado fica em `dados_rt/vigia_estado.json` (`rtd_parado`), então
  reiniciar o vigia não repete o aviso. A checagem que mantém coletor e vigia de pé saiu da aba
  Operações e foi para o `main()`: qualquer aba aberta segura os dois.
- **Sinais durante o pregão (17/09/2026):** o painel mostrou compras da BPAC11 às 11:45 e às 12:15 que
  o Profit não deu. O que se descobriu:
  - o **cache de candles do Profit não é gravado durante o pregão**: ele escreve o arquivo `.min` quando
    um gráfico é aberto ou recarregado (ou um backtest roda) — e escreve junto o candle em formação.
    Então, no pregão, os candles de hoje saem do RTD;
  - nos candles montados pelo RTD o **preço bate** (1 a 2 centavos de diferença na máxima ou mínima, de
    vez em quando), mas o **volume sai ~12% maior** que o do gráfico do Profit, sempre no mesmo sentido
    (medido em 15/09 com o pregão inteiro: Profit ÷ RTD de 0,87 a 0,90 nos cinco ativos). As estratégias
    de pullback exigem volume acima da média de 20 candles, e o candle do RTD passava com folga falsa.

  Correções: o volume das barras do RTD é multiplicado pelo fator medido de cada ativo
  (`FATOR_VOLUME_RTD`); depois de um silêncio do RTD a quantidade acumulada no período não cai num
  candle só (a barra fica marcada como falha); do cache do Profit entra o candle que fechou antes de o
  arquivo ser gravado e, entre os já fechados no relógio, também o que o RTD não cobriu inteiro. E,
  como o RTD ainda erra um candle por alguns por cento, **sinal de hoje em candle montado pelo RTD
  que muda com um detalhe** (volume ±15%, máxima/mínima ±2 centavos, fechamento ±1 centavo —
  `sinal_no_limite`) aparece como **"a confirmar"** no cartão, com aviso em amarelo, e o aviso do
  vigia pede para conferir a coloração no Profit antes de entrar. Candle que veio inteiro do cache do
  Profit é o do gráfico dele e nunca ganha esse aviso.
- **Aba Opções sem vencimento (17/09/2026):** o opcoes.net.br devolve **429 (Too Many Requests)** depois
  de uns 5 pedidos seguidos. A coleta buscava 8 vencimentos em ordem de data, as semanais gastavam a
  cota e o 16/10 — o único mensal da janela, com o 18/09 a 1 DU — voltava recusado, deixando a aba
  com "Nenhum vencimento mensal". Agora os mensais vêm primeiro, as semanais nem são buscadas com o
  filtro de série mensal ligado (3 pedidos em vez de 8), há uma pausa curta entre pedidos e, no 429,
  o painel espera e tenta de novo.
- **Tudo em % (16/09/2026):** a pedido dele, a aba não mostra mais resultado em R$. Cada operação
  conta em **% sobre o valor da entrada** (preço de entrada × quantidade): é o número do cartão, o
  "no pregão" é a soma das fechadas do dia e o mês a mês é a soma das operações do mês (o R$ ficou
  na dica do mouse das células). Percentual não muda de escala quando o lote muda. O aviso de saída
  (Windows/Telegram) também vai em %.
- **A opção desde o sinal (16/09/2026):** o bloco da opção abre com **No sinal** — dia, hora e quanto
  ela valia quando a estratégia deu o sinal — e os outros valores mostram a variação a partir dali:
  agora, na parcial, no alvo e no stop. O preço no sinal sai do que o coletor gravou (ele assina a
  opção assim que o sinal aparece, então a primeira linha dela é de minutos depois); sem isso, é
  Black-Scholes com a ação no preço da entrada, o prazo daquele dia e a volatilidade de agora — e o
  cartão diz qual dos dois é.
- **Menos texto (16/09/2026):** saíram os rodapés das três abas, a nota do Delta calculado (aba
  Opções), a nota de quanto a opção perde por dia, a nota da fonte da cotação e as duas explicações
  do resultado mês a mês. Ele lê o painel todo dia e já sabe o que cada número é.
- **Cuidado com `st.cache_data`:** parâmetro que começa com `_` fica **fora da chave** do cache. O
  `_resultados_salvos(_mtime)` recebia a data do arquivo justamente para renovar a leitura e, com o
  underscore, devolvia para sempre o primeiro resultado — a seção do mês a mês sumiu da página até
  o parâmetro virar `mtime` (16/09/2026).
- **Exige no Profit:** *Exportação em Tempo Real (RTD / DDE)* com o RTD ativado e os 6
  ativos na lista. Com o RTD desligado, o servidor quebra ao receber o pedido.
- **Sem o coletor** (ou no Streamlit Cloud), o pregão de hoje vem do Yahoo, com cerca de
  15 min de atraso; o histórico dos dias anteriores vem sempre do Yahoo. Se o coletor
  começar com o pregão andando, as barras anteriores a ele também ficam com o Yahoo.
- Máxima e mínima de cada barra saem das cotações anotadas, então um pico entre duas
  atualizações pode escapar.
- Resultado em R$ para o lote de 200 ações, sem custos.

## Robustez a mudança de colunas

O site pode renomear colunas a qualquer momento. `mapear_colunas()` resolve isso com
apelidos normalizados (sem acento, minúsculo, só alfanumérico) em dois passes — igualdade
exata e depois substring (só para apelidos com 4+ caracteres, para não confundir `iv`,
`cp`, `var`). Ex.: `Vol. Financeiro (R$)`, `Volume Financeiro` e `Financeiro` caem todas
em `vol_financeiro`.

Também é tolerado:

- Números em pt-BR e en-US: `R$ 1.234,56`, `1,234.56`, `12,34%`, `1,2 mi`.
- Delta em pontos percentuais (`62` vira `0,62`) e vol. implícita em fração (`0,32` vira `32`).
- CSV com preâmbulo antes do cabeçalho (`_detectar_header` acha a linha certa).
- Colunas ausentes: `Tipo` e `Vencimento` são deduzidos da letra de série do ticker
  (Calls `A..L` = jan..dez, Puts `M..X` = jan..dez), e `A/I/OTM` é calculado do strike.

## Estrutura do `app.py`

```
SEÇÃO 1   Configuração ......... ativos, faixas, rótulos das colunas
SEÇÃO 2   Utilitários .......... parsing pt-BR, calendário e feriados da B3
SEÇÃO 3   Normalização ......... mapeamento de colunas → schema canônico
SEÇÃO 4   Extração ............. rotas A1 / A2 / B / Demo
SEÇÃO 5   Processamento ........ os filtros + ordenação por liquidez
SEÇÃO 5B  Mercados globais ..... cotações do Yahoo e matriz de correlação
SEÇÃO 6   Formatação ........... números em pt-BR
SEÇÃO 7   Interface ............ CSS, peças em HTML/SVG, as duas abas
```

## Diagnóstico

O cartão **Como a escolha foi feita** mostra quantas opções sobraram em cada etapa
(grade completa → série mensal → vencimento → frescor → delta → liquidez). Quando o
painel voltar vazio, é ali que se vê qual filtro cortou tudo. O expander **Registro da
coleta** lista o que veio de cada vencimento.

---

Ferramenta de apoio à decisão para uso próprio. Não é recomendação de investimento.
Dados do opcoes.net.br e do Yahoo Finance podem ter atraso — confirme preço e liquidez
no home broker antes de operar.
