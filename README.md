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
| 2 | **Delta** | `|Δ|` entre **0,50 e 0,70** — Calls de +0,50 a +0,70, Puts de -0,50 a -0,70. |
| 3 | **Liquidez** | Ordena por **Volume Financeiro ↓** e **Núm. de Negócios ↓**. **O topo da lista é a opção escolhida.** |

## Fontes de dados (cascata)

| Rota | Como | Quando entra |
|------|------|--------------|
| **A1** | `requests` no endpoint JSON interno do opcoes.net.br | Primeira tentativa, ~1s |
| **A2** | `Selenium` + `webdriver-manager`, Chrome headless, lê a tabela renderizada | Se A1 falhar |
| **B** | **Upload de CSV/Excel** exportado do próprio site | Se o site bloquear/exigir login — **é o caminho garantido** |
| **Demo** | Grade sintética (Black-Scholes) | Para testar a interface offline |

O endpoint JSON da Rota A1 é um contrato **não público**: pode mudar sem aviso. Por isso
o retorno passa por `_valida_grade()` (checa formato de ticker e faixa de delta) antes de
ser aceito — se não bater, cai automaticamente para o Selenium e depois para o upload.

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
