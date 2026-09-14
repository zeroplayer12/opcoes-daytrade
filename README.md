# 📈 Dashboard de Opções Day Trade — B3

Painel Streamlit que filtra a grade de opções e aponta, em segundos, a **melhor Call**
e a **melhor Put** do dia para `BOVA11`, `PETR4`, `VALE3`, `BBAS3`, `ITUB4` e `BPAC11`.

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

- **Privado de verdade:** escuta só em `127.0.0.1`, então nem outro aparelho da
  rede de casa acessa. Por isso não abre no celular.
- **Desativar o início automático:** apague `Painel de Opcoes.vbs` da pasta Inicializar.
- O endereço vai por linha de comando, não pelo `.streamlit/config.toml`: fixar
  `127.0.0.1` ali quebraria o deploy do Streamlit Cloud.
- O `abrir-web.bat` abre um túnel **público** — não use se quiser manter privado.
- Upload de `.xlsx` precisa do `openpyxl`, que não está instalado neste Python
  (`pip install openpyxl`). CSV funciona sem ele.

## Os filtros aplicados, na ordem

| # | Filtro | Regra |
|---|--------|-------|
| 1 | **Nomenclatura** | Só a série mensal convencional da B3. Aplicado **antes** de tudo, para o topo do ranking de liquidez ser sempre um contrato padrão. |
| 2 | **Vencimento** | **2 a 20 dias úteis**, apenas 3ª sexta. Seletor mostra `data · N DU · MENSAL/semanal`, com feriados da B3 calculados (Carnaval, Sexta Santa e Corpus Christi via algoritmo da Páscoa). |
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
- **A taxa livre de risco é um parâmetro**, em Ajustes avançados (padrão 10,75% a.a.).
  Em opções curtas o efeito sobre o Delta é pequeno.

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
SEÇÃO 1  Configuração ......... ativos, faixas, rótulos das colunas
SEÇÃO 2  Utilitários .......... parsing pt-BR, calendário e feriados da B3
SEÇÃO 3  Normalização ......... mapeamento de colunas → schema canônico
SEÇÃO 4  Extração ............. rotas A1 / A2 / B / Demo
SEÇÃO 5  Processamento ........ os três filtros + ordenação por liquidez
SEÇÃO 6  Formatação ........... números em pt-BR
SEÇÃO 7  Interface ............ sidebar, colunas Calls/Puts, diagnóstico
```

## Diagnóstico

O expander **🔎 Funil de filtragem** mostra quantas opções sobraram em cada etapa
(grade completa → vencimento → delta → liquidez). Quando o painel voltar vazio, é ali
que se vê qual filtro cortou tudo.

---

Ferramenta de apoio à decisão para uso próprio. Não é recomendação de investimento.
Dados do opcoes.net.br podem ter atraso — confirme preço e liquidez no home broker
antes de operar.
