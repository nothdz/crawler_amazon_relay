# Crawler para Amazon Relay

Automação em Python que entra no **Amazon Relay**, exporta o CSV de viagens e envia os dados para uma tabela do **Supabase** (upsert em lotes).

## Como funciona

1. Abre o Relay com o Playwright (Chromium).
2. Se for redirecionado para o login, entra com e-mail e senha.
3. Abre direto a URL de resultados da pesquisa avançada, com o intervalo de datas montado dinamicamente: **7 dias atrás até 3 dias à frente** (fuso `America/Sao_Paulo`).
4. Clica em **Exportar** e salva o CSV em `viagens_relay_baixado.csv`.
5. Lê o CSV com pandas, renomeia as colunas para o padrão da tabela e limpa os dados.
6. Envia para a tabela `viagens_relay` do Supabase com `upsert` em lotes de 500, usando `id_da_carga` como chave.

### Tratamento dos dados

- O CSV é lido como texto (`dtype=str`) para evitar que inteiros virem `123.0` por causa de valores vazios.
- Só as colunas mapeadas em `montar_mapeamento()` são enviadas (dados da viagem e paradas 1 a 5).
- Linhas sem `id_da_carga` e duplicadas no próprio CSV são descartadas, o que evita o erro `ON CONFLICT DO UPDATE command cannot affect row a second time`.
- Valores `NaN` viram `None` antes do envio.

## Requisitos

- Python 3
- Uma tabela `viagens_relay` no Supabase com constraint **UNIQUE** na coluna `id_da_carga`
- Uma conta com acesso ao Amazon Relay

```bash
pip install playwright pandas supabase
playwright install chromium
```

## Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `EMAIL_AMAZON` | E-mail de login do Amazon Relay |
| `SENHA_AMAZON` | Senha do Amazon Relay |
| `SUPABASE_URL` | URL do projeto no Supabase |
| `SUPABASE_KEY` | Chave de API do Supabase |
| `CI` | Opcional. Com valor `true`, roda em modo headless (veja abaixo) |

> **Nunca coloque credenciais no código nem suba um arquivo `.env` para o repositório.**

Linux/macOS:

```bash
export EMAIL_AMAZON="seu-email"
export SENHA_AMAZON="sua-senha"
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_KEY="sua-chave"
```

Windows (PowerShell):

```powershell
$env:EMAIL_AMAZON = "seu-email"
$env:SENHA_AMAZON = "sua-senha"
$env:SUPABASE_URL = "https://xxxx.supabase.co"
$env:SUPABASE_KEY = "sua-chave"
```

## Como rodar

```bash
python crawler_relay.py
```

### Modo visível x headless

O modo é escolhido pela variável de ambiente `CI`:

- **`CI` não definida (padrão):** o navegador abre na tela, com `slow_mo` de 500 ms para acompanhar os cliques. Bom para desenvolver e depurar.
- **`CI=true`:** roda em modo headless, sem janela e sem atraso. Use em servidores sem interface gráfica.

```bash
CI=true python crawler_relay.py
```

## Solução de problemas

| Sintoma | O que verificar |
|---|---|
| `KeyError: 'EMAIL_AMAZON'` (ou outra variável) | A variável de ambiente não está definida no terminal |
| Erro ao abrir o navegador em servidor sem tela | Defina `CI=true` para rodar em headless |
| `Login não concluído` e arquivo `erro_login.png` | Credenciais inválidas, CAPTCHA ou verificação em duas etapas; abra o screenshot para ver a tela |
| `erro_exportar.png` | O botão **Exportar** não apareceu a tempo ou o layout do Relay mudou; confira os seletores |
| Erro `42501` no upsert | Política de RLS do Supabase bloqueando a escrita; ajuste as policies ou use uma chave com permissão |
| `ON CONFLICT ...` no upsert | A coluna `id_da_carga` não tem constraint UNIQUE na tabela |

