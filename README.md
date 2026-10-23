# Crawler para Amazon Relay
# Relay Crawler

Automação que exporta o relatório de viagens do **Amazon Relay** e sincroniza os dados com uma tabela no **Supabase** (PostgreSQL), mantendo a base atualizada de hora em hora.

O script usa a conta de transportadora do próprio operador para exportar, via interface web, o CSV de viagens disponível no portal. Nenhuma API privada ou endpoint não documentado é utilizado.

## Visão geral

```
Amazon Relay (web)  ──►  CSV de viagens  ──►  tratamento (pandas)  ──►  Supabase (upsert)
     Playwright              local                 mapeamento              tabela viagens_relay
```

1. Abre o Chrome (headless) e reaproveita a sessão salva, se houver.
2. Autentica no Relay apenas quando a sessão expirou.
3. Abre a busca de viagens com uma janela de datas dinâmica e exporta o CSV.
4. Normaliza o CSV e renomeia as colunas para o schema da tabela.
5. Faz `upsert` em lotes pela chave `id_da_carga`.
6. Aguarda 1 hora e repete.

## Requisitos

- Python 3.10+
- Google Chrome instalado (o Playwright usa `channel="chrome"`)
- Projeto Supabase com a tabela de destino criada
- Dependências: `playwright`, `pandas`, `supabase`, `python-dotenv`, `truststore`

O `truststore` injeta o repositório de certificados do sistema operacional no SSL do Python, o que evita erros de certificado em redes corporativas com inspeção de tráfego.

## Configuração

As credenciais são lidas de um arquivo `.env` na mesma pasta do script, ou diretamente de variáveis de ambiente.

| Variável        | Descrição                                      |
|-----------------|------------------------------------------------|
| `EMAIL_AMAZON`  | E-mail de acesso ao Amazon Relay               |
| `SENHA_AMAZON`  | Senha de acesso ao Amazon Relay                |
| `SUPABASE_URL`  | URL do projeto Supabase                        |
| `SUPABASE_KEY`  | Chave de API do Supabase (service role recomendada, pois o script escreve na tabela) |

Se nenhum `.env` nem as variáveis existirem e houver um terminal interativo, o script pergunta os valores na primeira execução e gera o arquivo automaticamente.

### Tabela no Supabase

A tabela `viagens_relay` precisa de uma constraint **UNIQUE** na coluna `id_da_carga`, que é a chave usada no `upsert`. Sem ela, o `on_conflict` falha.

As colunas seguem o mapeamento definido em `montar_mapeamento()`:

- Campos gerais da viagem: `id_da_viagem`, `bloco_viagem`, `etapa_da_viagem`, `id_da_carga`, `status_de_execucao_da_carga`, `tipo_de_equipamento`, `id_do_reboque`, `id_do_veiculo_trator`, `distancia_estimada`, `tipo_de_tarifa`, `id_do_operador`, `subtransportadora`, `tipo_de_contrato`, `id_do_contrato`, entre outros.
- Paradas 1 a 5, cada uma com: `parada_N`, `deslocamento_utc_parada_N`, data e horário de chegada (planejado e real), data e horário de partida (planejado e real).

Todas as colunas são gravadas como texto, sem conversão de tipos no cliente. Colunas do CSV que não constam no mapeamento são ignoradas.

## Execução

```bash
python crawler.py
```

O comportamento depende do estado do ambiente:

| Situação | Comportamento |
|----------|---------------|
| Sem `.env` e sem variáveis | **Primeira execução.** Pede as credenciais, roda uma vez em primeiro plano para validar o login e, se der certo, passa para segundo plano. |
| `.env` presente | Inicia uma cópia em segundo plano, sem janela, e devolve o terminal. |
| Sem `.env`, com variáveis de ambiente completas | Roda o loop no próprio processo (útil em CI/containers). |

Se a primeira execução falhar, o script encerra com código `1`. Em caso de credenciais incorretas, remova o `.env` e execute novamente.

### Segundo plano

O processo filho é iniciado com `CRAWLER_BG=1`, sem console visível no Windows (`CREATE_NO_WINDOW`) e em sessão separada em sistemas Unix. Toda a saída vai para `crawler.log`, que é truncado automaticamente ao passar de ~2 MB.

Uma trava via socket em `127.0.0.1:49733` garante **uma única instância** por máquina. Se um agendador (Task Scheduler, cron) disparar o script repetidamente, as execuções excedentes encerram sozinhas.

## Detalhes de funcionamento

**Janela de datas.** A busca usa de `hoje - 7 dias` até `hoje + 3 dias`, filtrando pela data da primeira parada, com fuso `America/Sao_Paulo`. O intervalo é recalculado a cada ciclo.

**Sessão.** Os cookies são salvos em `estado_sessao.json` após o login e renovados após cada exportação bem-sucedida. Se o arquivo estiver corrompido, é descartado e um novo login é feito.

**Envio.** Registros sem `id_da_carga` e duplicados dentro do próprio CSV são descartados (mantém a última ocorrência). O envio é feito em lotes de 500. Se um lote falhar, os seguintes continuam e a execução termina com erro listando os lotes afetados. Como o `upsert` é idempotente, o próximo ciclo reenvia os dados normalmente.

**Intervalo.** Fixo em 3600 s entre o fim de uma execução e o início da seguinte, independentemente de sucesso ou falha.

## Arquivos gerados

Todos na pasta do script, independentemente do diretório de trabalho do agendador.

| Arquivo | Função |
|---------|--------|
| `.env` | Credenciais |
| `estado_sessao.json` | Sessão autenticada (cookies) |
| `viagens_relay_baixado.csv` | Último CSV exportado |
| `crawler.log` | Log do modo segundo plano |
| `erro_login.png` | Captura da tela em falha de login |
| `erro_exportar.png` | Captura da tela em falha de exportação |

## Segurança

- `.env` e `estado_sessao.json` contêm credenciais e sessão ativa. **Não versione** esses arquivos; inclua-os no `.gitignore`, assim como os CSVs e screenshots.
- A senha fica em texto simples no `.env`. Restrinja a permissão do arquivo ao usuário que executa o script.
- Prefira uma conta dedicada à automação, com o menor nível de acesso necessário no Relay.
- Use no Supabase uma chave com escopo restrito à tabela de destino sempre que possível.

## Limitações conhecidas

- Login com **CAPTCHA** ou **verificação em duas etapas** não é tratado. Nesses casos o login expira por timeout (30 s) e o erro é registrado com screenshot. A sessão salva reduz a frequência desse cenário, mas não o elimina.
- Os seletores da interface (login e botão de exportação) dependem do HTML atual do portal e podem exigir ajuste se o layout mudar. Eles estão centralizados nas constantes `SEL_*` no topo do arquivo.
- O mapeamento de colunas é fixo. Novas colunas no CSV só são sincronizadas depois de adicionadas em `montar_mapeamento()` e na tabela.
- Suporta até 5 paradas por viagem.
