import os
import time
import pandas as pd
import truststore
from playwright.sync_api import sync_playwright
from supabase import create_client, Client
from datetime import datetime, timedelta
from dotenv import load_dotenv

truststore.inject_into_ssl()

ENV_FILE = ".env"

if not os.path.exists(ENV_FILE):
    print("Primeira execução detectada. Por favor, configure as credenciais:")
    
    # Credenciais da Amazon (pessoais de quem está a usar)
    email_amazon = input("E-mail da Amazon Relay: ").strip()
    senha_amazon = input("Senha da Amazon Relay: ").strip()
    
    # Credenciais do Supabase (do projeto)
    supabase_url = input("URL do Supabase: ").strip()
    supabase_key = input("Chave (Key) do Supabase: ").strip()
    
    with open(ENV_FILE, "w") as f:
        f.write(f"EMAIL_AMAZON={email_amazon}\n")
        f.write(f"SENHA_AMAZON={senha_amazon}\n")
        f.write(f"SUPABASE_URL={supabase_url}\n")
        f.write(f"SUPABASE_KEY={supabase_key}\n")
        
    print(".env criado com sucesso!\n")

# Carrega as variáveis para o script usar
from dotenv import load_dotenv
load_dotenv()

EMAIL_AMAZON = os.getenv("EMAIL_AMAZON")
SENHA_AMAZON = os.getenv("SENHA_AMAZON")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

TABELA = "viagens_relay"
CHAVE_UPSERT = "id_da_carga"  # precisa ter constraint UNIQUE na tabela


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

    # 6. Envia em lotes com upsert pela chave id_da_carga
    tamanho_lote = 500
    total_linhas = len(dados_para_enviar)

    for i in range(0, total_linhas, tamanho_lote):
        lote = dados_para_enviar[i:i + tamanho_lote]
        supabase.table(TABELA).upsert(lote, on_conflict=CHAVE_UPSERT).execute()
        print(f"Lote {i} a {min(i + tamanho_lote, total_linhas)} enviado com sucesso!")

    print("Sincronização com o Supabase concluída com sucesso!")


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
        
        # Cria o contexto simulando um User-Agent real de um computador normal
        context = browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()

        print("1. Acessando a raiz do Relay...")
        page.goto("https://relay.amazon.com.br/")
        page.wait_for_load_state("domcontentloaded")

        if "signin" in page.url or "ap/signin" in page.url or "ap/cvf" in page.url:
            print("2. Detectada tela de login. Inserindo e-mail...")
            try:
                page.wait_for_selector('input#ap_email, input[name="email"], input[type="email"]', timeout=15000)
                page.fill('input#ap_email, input[name="email"], input[type="email"]', EMAIL_AMAZON)
                
                page.click('input#continue, button#continue, input[type="submit"], span#continue input')
                page.wait_for_load_state("domcontentloaded")

                print("3. Inserindo senha...")
                page.wait_for_selector('input#ap_password, input[name="password"], input[type="password"]', timeout=15000)
                page.fill('input#ap_password, input[name="password"], input[type="password"]', SENHA_AMAZON)
                
                page.click('input#signInSubmit, button#auth-signin-button, input[type="submit"]')
                page.wait_for_load_state("domcontentloaded")

                # Se ainda estiver numa página de login/verificação, o login não concluiu
                # (senha errada, CAPTCHA ou verificação em duas etapas)
                if any(x in page.url for x in ("signin", "ap/mfa", "ap/cvf", "ap/challenge")):
                    raise Exception("Login não concluído (CAPTCHA, 2FA ou credenciais inválidas).")

                print("Login efetuado com sucesso!")
            except Exception as e:
                page.screenshot(path="erro_login.png")
                print("Erro no login. Screenshot guardado como 'erro_login.png'.")
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

        time.sleep(3)

        print("6. Tentando localizar e exportar o CSV...")
        try:
            # Espera o botão aparecer (pode ser "Exportar", "Download", ou ter um ícone)
            page.wait_for_selector('button:has-text("Exportar"), button:has-text("Export"), [aria-label*="Exportar"]', timeout=15000)
            
            with page.expect_download() as download_info:
                page.click('button:has-text("Exportar"), button:has-text("Export"), [aria-label*="Exportar"]')

            download = download_info.value
            csv_path = "./viagens_relay_baixado.csv"
            download.save_as(csv_path)
            print(f"Sucesso total! CSV salvo em: {csv_path}")
            
        except Exception as e:
            page.screenshot(path="erro_exportar.png")
            print("Erro ao tentar exportar. Screenshot guardado como 'erro_exportar.png'.")
            raise e

        browser.close()

    # Envia para o Supabase com o browser já fechado
    enviar_para_supabase(csv_path)


if __name__ == "__main__":
    testar_fluxo_relay()
