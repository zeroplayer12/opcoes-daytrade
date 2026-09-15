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
  18:30) e atualiza a cada 5 s. Ele sobe
  junto com o painel (`iniciar-painel.cmd`), espera o Profit abrir, reconecta se o
  Profit fechar, roda uma instância só e registra tudo em `coletor.log`; o
  `parar-painel.cmd` encerra os dois.
- **Exige no Profit:** *Exportação em Tempo Real (RTD / DDE)* com o RTD ativado e os 6
  ativos na lista. Com o RTD desligado, o servidor quebra ao receber o pedido.
- **Sem o coletor** (ou no Streamlit Cloud), o pregão de hoje vem do Yahoo, com cerca de
  15 min de atraso; o histórico dos dias anteriores vem sempre do Yahoo. Se o coletor
  começar com o pregão andando, as barras anteriores a ele também ficam com o Yahoo.
- Máxima e mínima de cada barra saem das cotações anotadas, então um pico entre duas
  atualizações pode escapar.
- Resultado em R$ para o lote de cada estratégia (100 ações), sem custos.

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
