import os, sys
import time
import socket
import subprocess
import pandas as pd
import truststore
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from supabase import create_client, Client
from datetime import datetime, timedelta
from dotenv import load_dotenv

#Injeção de SSL para confiança
truststore.inject_into_ssl()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

# Arquivos auxiliares sempre na pasta do script (não dependem do diretório atual do agendador)
CSV_PATH = os.path.join(BASE_DIR, "viagens_relay_baixado.csv")
SESSION_FILE = os.path.join(BASE_DIR, "estado_sessao.json")  # cookies da sessão (NÃO versionar no git)
SCREENSHOT_LOGIN = os.path.join(BASE_DIR, "erro_login.png")
SCREENSHOT_EXPORTAR = os.path.join(BASE_DIR, "erro_exportar.png")

LOG_FILE = os.path.join(BASE_DIR, "crawler.log")
PORTA_TRAVA = 49733  # trava de instância única (só uma cópia em segundo plano por máquina)

CAMPOS_ENV = ["EMAIL_AMAZON", "SENHA_AMAZON", "SUPABASE_URL", "SUPABASE_KEY"]

# O processo em segundo plano é iniciado pelo próprio script com esta variável definida.
EM_SEGUNDO_PLANO = os.environ.get("CRAWLER_BG") == "1"

if EM_SEGUNDO_PLANO:
    # Sem janela, tudo que seria exibido vai para o crawler.log (o arquivo é zerado se passar de ~2 MB)
    _modo_log = "w" if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2_000_000 else "a"
    sys.stdout = sys.stderr = open(LOG_FILE, _modo_log, buffering=1, encoding="utf-8")


def _tem_todas_as_variaveis():
    return all(os.getenv(c) for c in CAMPOS_ENV)


# Primeira execução = não tem .env nem variáveis de ambiente -> vai abrir o modo interativo (visível)
PRIMEIRA_EXECUCAO = not os.path.exists(ENV_FILE) and not _tem_todas_as_variaveis()


# Verifica se já possui .env (ou variáveis de ambiente)
# Caso não tenha nenhum dos dois, cria o .env na hora com as credenciais do usuário,
# mas SÓ se houver um terminal interativo (em modo silencioso/agendado o input() travaria).
if not os.path.exists(ENV_FILE) and not _tem_todas_as_variaveis():
    if sys.stdin is not None and sys.stdin.isatty():
        print("Primeira execução detectada. Por favor, configure as credenciais:")

        # Credenciais da Amazon
        email_amazon = input("E-mail da Amazon Relay: ").strip()
        senha_amazon = input("Senha da Amazon Relay: ").strip()

        # Credenciais do Supabase
        supabase_url = input("URL do Supabase: ").strip()
        supabase_key = input("Chave (Key) do Supabase: ").strip()

        with open(ENV_FILE, "w") as f:
            f.write(f"EMAIL_AMAZON={email_amazon}\n")
            f.write(f"SENHA_AMAZON={senha_amazon}\n")
            f.write(f"SUPABASE_URL={supabase_url}\n")
            f.write(f"SUPABASE_KEY={supabase_key}\n")

        print(".env criado com sucesso!\n")
    else:
        print("ERRO: .env não encontrado e não há terminal interativo para pedir as credenciais. "
              "Rode o script uma vez de forma visível (ou defina as variáveis de ambiente).")
        sys.exit(1)

# Carrega as variáveis para o script usar (caminho explícito; não sobrescreve variáveis já definidas)
load_dotenv(ENV_FILE)

EMAIL_AMAZON = os.getenv("EMAIL_AMAZON")
SENHA_AMAZON = os.getenv("SENHA_AMAZON")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

_faltando = [c for c in CAMPOS_ENV if not os.getenv(c)]
if _faltando:
    print(f"ERRO: variáveis ausentes no .env/ambiente: {', '.join(_faltando)}")
    sys.exit(1)

TABELA = "viagens_relay"
CHAVE_UPSERT = "id_da_carga"  # precisa ter constraint UNIQUE na tabela

SEL_EMAIL = 'input#ap_email, input[name="email"], input[type="email"]'
SEL_SENHA = 'input#ap_password, input[name="password"], input[type="password"]'
SEL_EXPORTAR = 'button:has-text("Exportar"), button:has-text("Export"), [aria-label*="Exportar"]'
URLS_LOGIN = ("signin", "ap/signin", "ap/cvf", "ap/mfa", "ap/challenge")


def montar_mapeamento():
    """Mapeia os nomes das colunas do CSV para as colunas da tabela do Supabase."""
    mapeamento = {
        "ID da viagem": "id_da_viagem",
        "Bloco/Viagem": "bloco_viagem",
        "Etapa da viagem": "etapa_da_viagem",
        "ID da carga": "id_da_carga",
        "Sequência da instalação": "sequencia_da_instalacao",
        "Status de execução da carga": "status_de_execucao_da_carga",
        "Tipo de operador de trânsito": "tipo_de_operador_de_transito",
        "Tipo de equipamento": "tipo_de_equipamento",
        "ID do reboque": "id_do_reboque",
        "ID do veículo trator": "id_do_veiculo_trator",
        "Distância estimada": "distancia_estimada",
        "Unidade": "unidade",
        "Tipo de tarifa": "tipo_de_tarifa",
        "Filtro de empilhadeira": "filtro_de_empilhadeira",
        "Id do operador": "id_do_operador",
        "Conta do expedidor": "conta_do_expedidor",
        "Subtransportadora": "subtransportadora",
        "CR_ID": "cr_id",
        "Trabalho no local": "trabalho_no_local",
        "Tipo de contrato": "tipo_de_contrato",
        "ID do contrato": "id_do_contrato",
    }

    # Paradas 1 a 5 (mesmo padrão de colunas para cada uma)
    for n in range(1, 6):
        mapeamento.update({
            f"Parada {n}": f"parada_{n}",
            f"Deslocamento UTC na parada {n}": f"deslocamento_utc_parada_{n}",
            f"Data de chegada planejada na parada {n}": f"data_chegada_planejada_parada_{n}",
            f"Horário de chegada planejado na parada {n}": f"horario_chegada_planejado_parada_{n}",
            f"Data de chegada real na parada {n}": f"data_chegada_real_parada_{n}",
            f"Horário de chegada real na parada {n}": f"horario_chegada_real_parada_{n}",
            f"Data de partida planejada da parada {n}": f"data_partida_planejada_parada_{n}",
            f"Horário de partida planejado da parada {n}": f"horario_partida_planejado_parada_{n}",
            f"Data de partida real da parada {n}": f"data_partida_real_parada_{n}",
            f"Horário de partida real da parada {n}": f"horario_partida_real_parada_{n}",
        })
    return mapeamento


def enviar_para_supabase(csv_path):
    print("9. Iniciando o envio dos dados do CSV para o Supabase...")

    # 1. Lê o CSV como texto (evita que inteiros virem 123.0 por causa de NaN)
    df = pd.read_csv(csv_path, dtype=str)

    # 2. Renomeia as colunas e mantém só as que existem na tabela
    mapeamento_colunas = montar_mapeamento()
    df = df.rename(columns=mapeamento_colunas)

    colunas_validas = list(mapeamento_colunas.values())
    colunas_presentes = [col for col in colunas_validas if col in df.columns]
    df = df[colunas_presentes]

    # 3. Limpeza: remove linhas sem id_da_carga e duplicadas no próprio CSV
    total_inicial = len(df)
    df = df.dropna(subset=[CHAVE_UPSERT])
    df = df.drop_duplicates(subset=[CHAVE_UPSERT], keep="last")
    print(f"   {total_inicial} linhas no CSV -> {len(df)} para enviar "
          f"(sem {CHAVE_UPSERT} ou duplicadas foram descartadas)")

    if df.empty:
        print("Nada para enviar.")
        return

    # 4. Converte NaN para None puro do Python
    dados_para_enviar = [
        {k: (None if pd.isna(v) else v) for k, v in record.items()}
        for record in df.to_dict(orient="records")
    ]

    # 5. Conecta no Supabase
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 6. Envia em lotes com upsert pela chave id_da_carga.
    #    Se um lote falhar, os próximos continuam sendo enviados; as falhas são reportadas no final.
    tamanho_lote = 500
    total_linhas = len(dados_para_enviar)
    falhas = []

    for i in range(0, total_linhas, tamanho_lote):
        fim = min(i + tamanho_lote, total_linhas)
        lote = dados_para_enviar[i:fim]
        try:
            supabase.table(TABELA).upsert(lote, on_conflict=CHAVE_UPSERT).execute()
            print(f"Lote {i} a {fim} enviado com sucesso!")
        except Exception as e:
            falhas.append(f"{i}-{fim}")
            print(f"ERRO no lote {i} a {fim}: {e}")

    if falhas:
        raise Exception(f"Sincronização incompleta. Lotes com falha: {', '.join(falhas)}")

    print("Sincronização com o Supabase concluída com sucesso!")


def precisa_login(page):
    #Detecta se a página atual é de login (por URL ou pela presença do campo de e-mail).
    if any(x in page.url for x in URLS_LOGIN):
        return True
    try:
        # O redirect pro login pode demorar um pouco; espera o campo aparecer por alguns segundos
        page.wait_for_selector("input#ap_email", timeout=5000)
        return True
    except PlaywrightTimeoutError:
        return False


def criar_contexto(browser):
    #Cria o contexto reaproveitando a sessão salva (se existir e estiver íntegra).
    opcoes = dict(
        accept_downloads=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
    )
    if os.path.exists(SESSION_FILE):
        try:
            return browser.new_context(storage_state=SESSION_FILE, **opcoes)
        except Exception:
            print("Sessão salva inválida/corrompida. Será feito login do zero.")
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass
    return browser.new_context(**opcoes)


def testar_fluxo_relay():
    with sync_playwright() as p:

        # Lança o navegador com argumentos anti-deteção para enganar os bloqueios da Amazon
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            slow_mo=500,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=1920,1080",
            ]
        )

        # try/finally garante que o Chrome fecha mesmo se algo der erro no meio do caminho
        try:
            # Cria o contexto simulando um User-Agent real de um computador normal
            context = criar_contexto(browser)
            page = context.new_page()

            print("1. Acessando a raiz do Relay...")
            page.goto("https://relay.amazon.com.br/")
            page.wait_for_load_state("domcontentloaded")

            if precisa_login(page):
                print("2. Detectada tela de login. Inserindo e-mail...")
                try:
                    page.wait_for_selector(SEL_EMAIL, timeout=15000)
                    page.fill(SEL_EMAIL, EMAIL_AMAZON)

                    page.click('input#continue, button#continue, input[type="submit"], span#continue input')
                    page.wait_for_load_state("domcontentloaded")

                    print("3. Inserindo senha...")
                    page.wait_for_selector(SEL_SENHA, timeout=15000)
                    page.fill(SEL_SENHA, SENHA_AMAZON)

                    page.click('input#signInSubmit, button#auth-signin-button, input[type="submit"]')

                    # Espera sair das páginas de login/verificação. Se não sair, o login não concluiu
                    # (senha errada, CAPTCHA ou verificação em duas etapas)
                    try:
                        page.wait_for_url(
                            lambda url: not any(x in url for x in URLS_LOGIN),
                            timeout=30000
                        )
                    except PlaywrightTimeoutError:
                        raise Exception("Login não concluído (CAPTCHA, 2FA ou credenciais inválidas).")

                    page.wait_for_load_state("domcontentloaded")
                    print("Login efetuado com sucesso!")

                    # Salva a sessão para as próximas execuções não precisarem logar de novo
                    context.storage_state(path=SESSION_FILE)
                except Exception as e:
                    page.screenshot(path=SCREENSHOT_LOGIN)
                    print(f"Erro no login. Screenshot guardado como '{SCREENSHOT_LOGIN}'.")
                    raise e

            print("4. Navegando para o Relay autenticado...")
            if "relay.amazon.com.br" not in page.url:
                page.goto("https://relay.amazon.com.br/")
                page.wait_for_load_state("domcontentloaded")

            # Pega a data e hora atuais para montar o link dinâmico
            hoje = datetime.now()
            data_inicio = hoje - timedelta(days=7)
            data_fim = hoje + timedelta(days=3)

            # Formata as datas mantendo a estrutura original intacta
            str_inicio = data_inicio.strftime("%Y-%m-%d") + "+03%3A00%3A00%3A%3AAmerica%2FSao_Paulo"
            str_fim = data_fim.strftime("%Y-%m-%d") + "+03%3A00%3A00%3A%3AAmerica%2FSao_Paulo"

            # Monta o URL completo de pesquisa avançada diretamente
            url = f"https://relay.amazon.com.br/tours/search/results?ref=owp_dash_search_bar&asstrtdt={str_inicio}&asenddt={str_fim}&aslctntyp=firstStop"

            print(f"5. Navegando diretamente para o link dinâmico: {url}")
            page.goto(url)
            page.wait_for_load_state("domcontentloaded")

            # Em vez de um sleep fixo, tenta esperar a rede acalmar (se nunca acalmar, segue em frente)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            print("6. Tentando localizar e exportar o CSV...")
            try:
                # Espera o botão aparecer (pode ser "Exportar", "Download", ou ter um ícone)
                page.wait_for_selector(SEL_EXPORTAR, timeout=30000)

                # Timeout maior: exportações grandes podem passar dos 30s padrão
                with page.expect_download(timeout=120000) as download_info:
                    page.click(SEL_EXPORTAR)

                download = download_info.value
                download.save_as(CSV_PATH)
                print(f"Sucesso total! CSV salvo em: {CSV_PATH}")

                # Atualiza a sessão salva (renova cookies)
                context.storage_state(path=SESSION_FILE)

            except Exception as e:
                page.screenshot(path=SCREENSHOT_EXPORTAR)
                print(f"Erro ao tentar exportar. Screenshot guardado como '{SCREENSHOT_EXPORTAR}'.")
                raise e
        finally:
            browser.close()

    # Envia para o Supabase com o browser já fechado
    enviar_para_supabase(CSV_PATH)


# Temporizador
def rodar_crawler():
    """Roda o fluxo uma vez. Retorna True se deu certo, False se deu erro."""
    try:
        print("Iniciando o crawler...")
        testar_fluxo_relay()
        print("Crawler executado com sucesso!")
        return True
    except Exception as e:
        print(f"Erro no crawler: {e}")
        return False


def iniciar_em_segundo_plano():
    """Abre uma cópia deste script sem nenhuma janela e devolve o controle."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        candidato = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(candidato):
            exe = candidato

    env = os.environ.copy()
    env["CRAWLER_BG"] = "1"

    opcoes = dict(
        cwd=BASE_DIR,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.name == "nt":
        # python.exe com console oculto (e não pythonw), para o Chrome/Playwright também não abrirem janelas
        opcoes["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        opcoes["start_new_session"] = True

    subprocess.Popen([exe, os.path.abspath(__file__)], **opcoes)


def garantir_instancia_unica():
    """Evita várias cópias escondidas rodando juntas (ex.: agendador disparando de hora em hora)."""
    trava = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        trava.bind(("127.0.0.1", PORTA_TRAVA))
    except OSError:
        print("Já existe uma instância em execução. Encerrando esta.")
        sys.exit(0)
    return trava  # precisa ficar referenciada enquanto o script roda


def loop_principal():
    while True:
        print(f"--- {datetime.now():%d/%m/%Y %H:%M:%S} ---")
        rodar_crawler()
        print("Aguardando 1 hora para a próxima execução...\n")
        # Espera 3600 segundos (1 hora) antes de rodar de novo
        time.sleep(3600)


if __name__ == "__main__":
    if EM_SEGUNDO_PLANO:
        # Cópia escondida: só mantém o loop de 1 hora (saída vai para o crawler.log)
        _trava = garantir_instancia_unica()
        loop_principal()

    elif PRIMEIRA_EXECUCAO:
        # Sem .env: o terminal interativo já pediu as credenciais lá em cima.
        # Roda uma vez COM a janela visível pra você ver se o login funcionou.
        if rodar_crawler():
            print("\nTudo certo! A partir de agora o crawler roda em segundo plano, sem janela.")
            iniciar_em_segundo_plano()
            time.sleep(3)
        else:
            print("\nA primeira execução falhou. Veja o erro acima. "
                  "Se as credenciais estiverem erradas, apague o arquivo .env e rode de novo.")
            input("Pressione Enter para fechar...")
            sys.exit(1)  # o instalador usa isso pra não criar o atalho de inicialização

    elif os.path.exists(ENV_FILE):
        # Já configurado: nada aparece na tela, vai direto pro segundo plano
        iniciar_em_segundo_plano()

    else:
        # Sem .env mas com variáveis de ambiente (ex.: GitHub Actions): roda no próprio processo
        loop_principal()
