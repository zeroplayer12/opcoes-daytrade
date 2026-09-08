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

## As três regras aplicadas

| # | Filtro | Regra |
|---|--------|-------|
| 1 | **Vencimento** | Ciclo atual/próximo — **8 a 20 dias úteis**. Seletor na sidebar mostra `data · N DU`, com feriados da B3 calculados (inclui Carnaval, Sexta Santa e Corpus Christi via algoritmo da Páscoa). |
| 2 | **Frescor** | Descarta o que não negocia há mais de N pregões. Necessário porque o Delta é calculado do preço — veja abaixo. |
| 3 | **Delta** | `\|Δ\|` entre **0,50 e 0,70** — Calls de +0,50 a +0,70, Puts de -0,50 a -0,70. |
| 4 | **Liquidez** | Ordena por **Volume Financeiro ↓** e **Núm. de Negócios ↓**. **O topo da lista é a opção escolhida.** |

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
