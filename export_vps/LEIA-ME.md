# LEIA-ME — TSE Clicker (cliques físicos via AutoHotkey)

Scraper do TSE que dirige o Chrome por **cliques físicos do mouse** (AutoHotkey v2).
O Python cuida do banco (Supabase), enriquecimento, parsing e fallback; o humano
só resolve os captchas. **Não usa Selenium.**

Fluxo do modo produção (um comando só):

```
enriquece (lote 500) → coleta títulos (lote 200) → grava → repete até zerar
```

---

## 1. Pré-requisitos

- **Windows com tela (GUI)** e você logado na sessão (local) ou conectado por **RDP** (VPS).
  - ⚠️ O AutoHotkey move o mouse de verdade — **VPS headless (sem GUI) NÃO funciona**.
  - ⚠️ Na VPS, **não desconecte o RDP** durante a execução (a sessão bloqueia e os cliques param). Minimizar tudo bem; **logoff mata**.
- **Google Chrome** instalado.
- **Python 3.10+** (instala no passo 2).
- **AutoHotkey v2** (instala no passo 2).
- Um **humano** disponível pra resolver captchas (eles nem sempre aparecem).

---

## 2. Instalação (uma vez por máquina)

Abra o **PowerShell** dentro da pasta do projeto.

### 2.1 AutoHotkey v2

**Se a máquina tiver `winget`** (Windows 10/11 normais):
```powershell
winget install AutoHotkey.AutoHotkey --accept-package-agreements --accept-source-agreements
```

**Se NÃO tiver winget** (comum em Windows Server / VPS — dá erro "winget is not recognized"):
```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest "https://www.autohotkey.com/download/ahk-v2.exe" -OutFile "$env:USERPROFILE\Downloads\ahk-v2-setup.exe"
# abre o instalador — clique em "Install" (você está no RDP, tem tela):
Start-Process "$env:USERPROFILE\Downloads\ahk-v2-setup.exe"
# (silencioso, sem clicar:)
# Start-Process "$env:USERPROFILE\Downloads\ahk-v2-setup.exe" -ArgumentList "/silent" -Wait
```

### 2.2 Python

Confira se já tem:
```powershell
python --version
```
**Se der erro "not recognized"**, instale:
```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile "$env:USERPROFILE\Downloads\python-setup.exe"
Start-Process "$env:USERPROFILE\Downloads\python-setup.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1" -Wait
```
Depois **feche e reabra o PowerShell** (pro PATH atualizar) e confirme `python --version`.

### 2.3 Ambiente Python + dependências
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **Na sua máquina local** já existe um venv em `d:\CLAUDE\AUTOMACAO TSE\.venv` — pode
> usar ele direto (não precisa recriar). **Na VPS**, crie o venv como acima.

### 2.4 Credenciais e tenants
```powershell
Copy-Item .env.example .env
notepad .env        # preencha SUPABASE_URL / SUPABASE_SERVICE_KEY
                    # e (p/ enriquecer) API_B_KEY / DATASINTESE_USER / DATASINTESE_PASS
                    # opcional: NOTIFY_WEBHOOK_URL (Discord/Slack) p/ aviso de captcha no celular
notepad settings.py # ajuste CLIENTE_ATIVO (nome do cliente) ou adicione novos em CLIENTES
                    # Alternativa sem editar: $env:CLIENTE_ATIVO="Alfredo do Belo"
```

---

## 3. Descobrir os caminhos (AHK e Python)

Os caminhos mudam conforme a máquina (instalação por usuário × por Administrator).
Rode isto **uma vez por sessão** do PowerShell pra deixar tudo em variáveis:

```powershell
# Caminho do AutoHotkey64.exe
$ahk = (Get-ChildItem "C:\Program Files\AutoHotkey","$env:LOCALAPPDATA\Programs\AutoHotkey" -Recurse -Filter "AutoHotkey64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
"AHK = $ahk"

# Caminho do Python do venv (local OU VPS)
$py = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "d:\CLAUDE\AUTOMACAO TSE\.venv\Scripts\python.exe" }
"PY  = $py"
```

> Guarde esses dois — os comandos abaixo usam `$ahk` e `$py`.

---

## 4. Abrir o Chrome na página certa

Deixe o Chrome aberto na página de **consultar número do título** do TSE,
com **zoom 100%**. Depois de calibrar, **não mexa no tamanho/posição da janela**.

---

## 5. Iniciar o worker AutoHotkey

```powershell
& $ahk .\tse_clicker.ahk
```

(ou duplo-clique no `tse_clicker.ahk`). Vai aparecer uma notificação dizendo que está pronto.

### Teclas do worker

| Tecla | Ação |
|---|---|
| `F8` | Liga/desliga o tooltip com a posição do mouse ao vivo (ajuda a mirar) |
| `F9` / `F10` | **Calibra SITUAÇÃO:** campo CPF / botão Entrar |
| `Ctrl+Shift+K` | **Calibra TÍTULO** (guiado, 6 pontos — ver abaixo) |
| `Ctrl+Shift+C` | Override manual da captura (normalmente não precisa) |
| `Ctrl+Shift+Q` | Encerra o worker |

> ⚠️ **Nunca use `F11`** com o Chrome em foco — é a tecla de fullscreen. Se o
> Chrome entrar/sair de tela cheia, TODAS as coordenadas mudam e você precisa
> recalibrar tudo.

---

## 6. Calibrar (uma vez por máquina/resolução)

> ⚠️ As coordenadas são **pixels absolutos da tela** — dependem de resolução, DPI,
> zoom e posição da janela. **Em cada máquina (local e VPS) você calibra de novo.**

### Situação (página de 1 campo)
Abra a página de situação no Chrome:
1. Mouse no **campo CPF** → `F9`
2. Mouse no **botão Entrar** → `F10`

### Autenticação (modal "Olá, seja bem-vindo" — só se aparecer)
O TSE passou a exibir um portão de **Autenticação** antes de liberar o formulário
(pede CPF, data de nascimento, filiação e nome da mãe). O worker **não** preenche
esse modal — resolva manualmente: feche o modal no `X` ou autentique com os dados
pedidos. Enquanto o modal estiver na tela, as consultas estouram o tempo e os
registros ficam pendentes.

> ⚠️ **Não calibre F9/F10 com o modal aberto** — você estará mirando nos campos
> do modal, não no formulário de consulta. Feche o modal antes de calibrar.

### Título (página de 4 campos — `Ctrl+Shift+K`)
Abra a página de número do título no Chrome, aperte `Ctrl+Shift+K` e percorra os
**6 pontos** (mouse em cima de cada, `Espaço` pra salvar; `Esc` cancela):

1. **CPF**
2. **Abrir o dropdown** de filiação (a caixa)
3. **Opção "Apenas nome de mãe"** — *abra o dropdown na mão (clique nele) e passe o mouse na opção*
4. **Campo Nome da mãe**
5. **Campo Data de nascimento**
6. **Botão Entrar**

As coordenadas ficam salvas em `coords.ini` (gerado automaticamente).

---

## 7. Rodar — modo produção 🚀

Com o worker rodando e calibrado:

```powershell
& $py poc_clicker_main.py --producao
```

### O que ele faz, em loop, até zerar

1. **Conta** quantos registros estão sem elegibilidade (nos tenants alvo).
2. **Fase A — Enriquecimento (lote 500):** para quem não tem mãe/data, consulta
   **API B (principal) → DataSintese (fallback)** e grava nome_mae/data. Rápido,
   em paralelo, **sem captcha**.
3. **Fase B — Coleta de títulos (lote 200):** para cada registro (com captcha):
   - Tem mãe+data → consulta **NÚMERO DO TÍTULO** (traz zona/seção/título/local).
   - Título não localizou → **fallback SITUAÇÃO** (só CPF).
   - Nem situação localizou → marca **`regularizar_tse`**.
   - **Anti-stale:** confere o CPF da página; se não bater, mantém pendente.
   - **Captcha não resolvido em 30s** → mantém pendente e vai pro próximo.
4. **Repete** as fases A+B até não sobrar nada sem elegibilidade
   (ou até uma rodada não fazer progresso — aí ele para sozinho).

### Durante a execução
- **Resolva os captchas** conforme aparecem. Os que **não** aparecerem, ele captura
  o resultado direto. Não precisa apertar nada — ele detecta o resultado e segue.
- **Aviso de captcha no celular:** se `NOTIFY_WEBHOOK_URL` estiver no `.env`
  (webhook de Discord/Slack), você recebe uma mensagem sempre que um captcha
  aparecer — não precisa ficar olhando o RDP.
- **Log em arquivo:** tudo que aparece no console também vai para
  `logs/producao_AAAAMMDD.log` (com hora em cada linha). Se der problema de
  madrugada, o rastro está lá.
- **Pausar:** `Ctrl+C` no terminal. O que já gravou fica salvo; o resto continua
  pendente e é retomado no próximo `--producao`.

---

## 8. Comandos úteis

```powershell
# Produção completa (enriquece 500 + coleta 200, até zerar):
& $py poc_clicker_main.py --producao

# Produção com limites custom:
& $py poc_clicker_main.py --producao --lote-enriq 500 --lote-coleta 200 --max-rodadas 3

# Testes pontuais (não-produção):
& $py poc_clicker_main.py                         # 1 pendente, dry-run (não grava)
& $py poc_clicker_main.py --loop 10 --gravar      # 10 pendentes, grava
& $py poc_clicker_main.py --cpf 15173248742 --mae "NOME DA MAE" --data 26/04/1966
```

| Flag | O que faz |
|---|---|
| `--producao` | loop contínuo enriquece→coleta→repete até zerar |
| `--lote-enriq N` | tamanho do lote de enriquecimento (default **500**) |
| `--lote-coleta N` | tamanho do lote de coleta de títulos (default **200**) |
| `--max-rodadas N` | limita o nº de rodadas (default 1000) |
| `--loop N` | (modo simples) processa N pendentes |
| `--enriquecer` | (modo simples) enriquece antes de cada consulta |
| `--gravar` | (modo simples) persiste no banco (produção já grava sempre) |
| `--cpf X --mae "..." --data DD/MM/AAAA` | testa 1 CPF avulso |

---

## 9. Local vs VPS — o que muda

| | Local (sua máquina) | VPS (Windows Server) |
|---|---|---|
| winget | normalmente tem | normalmente **não tem** → baixa o instalador (2.1) |
| Python | já instalado | provavelmente **instalar** (2.2) |
| venv | já existe em `d:\CLAUDE\AUTOMACAO TSE\.venv` | criar com `python -m venv .venv` |
| AutoHotkey | já instalado | instalar (2.1) |
| `.env` | já preenchido | copiar de `.env.example` e preencher |
| Calibração | já feita (coords.ini) | **refazer** (tela diferente) |
| Caminho do AHK | `...\AppData\Local\Programs\AutoHotkey\...` | normalmente `C:\Program Files\AutoHotkey\...` |
| Sessão | desktop normal | **RDP conectado e ativo** (não desconectar) |

> O `coords.ini` **não** é portável — calibre em cada máquina.
> Use a seção **3** pra resolver os caminhos de AHK/Python automaticamente.

---

## 10. Cobertura (quem é processado)

Processa as tabelas `eleitores` e `liderancas` dos tenants selecionados por
`CLIENTE_ATIVO` no [settings.py](settings.py) (ou pela env var `CLIENTE_ATIVO`).
A tabela `liderancas` inclui **todos os cargos** (colaborador, coordenador,
conselheiro, líder de grupo, staff, etc.) — **não há filtro por cargo**, então
todos entram. Se existir uma tabela separada para algum papel, adicione o nome
dela em `TABELAS_ALVO` (no `settings.py`).

---

## 11. Limitações honestas

- **Sequencial e com humano:** um só mouse/teclado físico + um humano pro captcha.
  Não dá pra rodar 2 fluxos em paralelo na mesma sessão. Pra paralelizar, use
  máquinas/sessões separadas, cada uma calibrada.
- **Coordenadas por máquina:** recalibre em cada tela/resolução.
- **Título com divergência:** quando a mãe/data não bate exatamente no TSE, o
  título volta "não localizado" e cai no fallback situação (2 captchas nesse caso).
- **Escala:** milhares de registros = milhares de captchas. É um trabalho longo.

---

## 12. Troubleshooting

| Sintoma | Causa / Solução |
|---|---|
| `winget is not recognized` | Windows Server sem winget → use o instalador direto (seção 2.1). |
| `python is not recognized` | Python não instalado/sem PATH → seção 2.2 e reabra o PowerShell. |
| `$ahk` vazio | AHK não instalou ou está em outro lugar → confira `C:\Program Files\AutoHotkey`. |
| Download falha / TLS | Rode `[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12` antes. |
| `coordenadas nao calibradas` | Calibre: `F9`/`F10` (situação) e `Ctrl+Shift+K` (título). |
| Toda consulta estoura os 30s de captcha | Confira `bridge/debug_ultima_captura.txt` — é o texto que o worker está vendo. Se for o modal "Olá, seja bem-vindo", feche/autentique manualmente e recalibre se necessário. |
| Clicks descalibrados de repente | Chrome entrou em fullscreen (`F11`) ou a janela mudou de tamanho/posição → recalibre tudo (situação, título e autenticação). |
| Clica no lugar errado | Janela do Chrome mudou de tamanho/posição/zoom → recalibre. |
| `Chrome nao encontrado` | Abra o Chrome antes de rodar. |
| Worker não responde | Confirme que o `tse_clicker.ahk` está rodando (ícone do AHK na bandeja). |
| Trava esperando resultado | Captcha não resolvido em 30s → vira pendente e segue (é o esperado). |
| Erro de credencial Supabase | Cheque `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` no `.env`. |
| Enriquecimento "nada pendente" | Todos já têm `enriquecimento_status` enriquecido/sem_dados — normal. |

---

## Arquivos

| Arquivo | Papel |
|---|---|
| `tse_clicker.ahk` | Worker AutoHotkey — cliques físicos + auto-captura |
| `poc_clicker_main.py` | Orquestrador — produção (enriquece→coleta→repete) |
| `enriquecimento_lite.py` | API B (principal) → DataSintese (fallback) |
| `bridge_helper.py` | Ponte por arquivos (pasta `bridge/`) |
| `tse_extracao_texto.py` | Parser do resultado a partir de texto puro |
| `supabase_repo.py` | Banco (ler pendentes, contar, gravar) |
| `config.py` / `settings.py` / `utils.py` | Config, tenants, helpers |
| `requirements.txt` / `.env.example` | Dependências e template de credenciais |
| `coords.ini` | Coordenadas calibradas (gerado por máquina) |
