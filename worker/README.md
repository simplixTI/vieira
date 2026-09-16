# Worker do Portal — fila TSE (batches / voter_records)

Processa em segundo plano a fila de CPFs enviada pelo portal web. Duas fases
por registro:

1. **Fase A — enriquecimento** (`status: pending → ready_tse`): preenche
   `nome_mae` + `data_nascimento` (+ `nome`, `titulo_eleitoral`) via cascata
   de APIs pagas (API B → Hashiro → DataSintese), reaproveitando o código de
   `export_vps/enriquecimento_lite.py`. Roda em paralelo (8 threads) e loga
   `enriquecimento: N chamadas` pra você acompanhar o gasto.
2. **Fase B — consulta TSE** (`status: ready_tse → done`): consulta o
   autoatendimento do TSE (consulta de **NÚMERO DO TÍTULO** se tem mãe+data;
   senão de **SITUAÇÃO**, só CPF), parseia o texto
   (`export_vps/tse_extracao_texto.py`) e grava elegibilidade, zona/seção,
   município, etc. Tem anti-stale: só confia no resultado se o CPF que veio
   na página (`cpf_pagina`) for o esperado.

## Setup

1. Crie **`.env.portal` na RAIZ do repo** (pasta pai de `worker/`) com as chaves
   do projeto **novo** do portal:

   ```env
   SUPABASE_URL=https://wipthjinvcyglbeuxxsb.supabase.co
   SUPABASE_SERVICE_KEY=<service_role key>
   ```

   As chaves das APIs de enriquecimento (`API_B_KEY`, `HASHIRO_TOKEN`,
   `DATASINTESE_USER/PASS`) e o `NOTIFY_WEBHOOK_URL` vêm do
   `export_vps/.env` — não precisa repetir.

2. Instale as dependências no venv que vai rodar o worker:

   ```bat
   ..\export_vps\.venv_new\Scripts\python.exe -m pip install -r requirements.txt
   ```

## Como rodar

```bat
cd worker

rem Dry-run SEM rede (TSE falsificado) — use primeiro:
..\export_vps\.venv_new\Scripts\python.exe main.py --stub --limite 5

rem 1 rodada contra o TSE real (só depois que o endpoint for descoberto):
..\export_vps\.venv_new\Scripts\python.exe main.py --limite 20

rem Produção (loop até zerar a fila):
run_worker.bat
```

Opções: `--producao` (loop), `--limite N`, `--fase a|b|ambas`, `--stub` (dry-run).

## Parada

- **Ctrl+C** — para entre registros; o que já foi gravado fica salvo.
- **Arquivo `worker/STOP`** — crie esse arquivo (vazio) que o worker para na
  primeira checagem entre registros e consome o arquivo.

Logs em `worker/logs/worker_YYYYMMDD.log`. Se `NOTIFY_WEBHOOK_URL` estiver
configurado, chegam avisos de lote concluído, erros e parada sem progresso.

## Teto diário de consultas TSE por cliente

Cada cliente do portal (`batches.user_id`) pode consumir no máximo
`LIMITE_DIARIO_POR_CLIENTE` consultas TSE por dia (padrão **6000**; ajuste via
variável de ambiente, `0` ou negativo = ilimitado). Vale só para a **fase B**
(a consulta TSE — a etapa cara); o enriquecimento da fase A não consome teto.

- Contagem do dia: um registro conta se virou `done` hoje (`checked_at`) ou se
  está `error` com `updated_at` de hoje (a falha também consumiu consulta).
  Meia-noite é a local da máquina do worker.
- Ao atingir o teto, os registros excedentes **não são tocados**: ficam
  `ready_tse` (sem incrementar `attempts`, sem virar `error`) e o worker
  volta neles sozinho no dia seguinte. O lote correspondente continua
  `processing` até zerar.

## Agendamento (mesmo padrão do export_vps/AUTOMACAO-CRMLITE.md)

O worker é idempotente: com a fila vazia ele encerra em segundos. Sugestão:
Agendador de Tarefas do Windows a cada **15 minutos**:

```powershell
# No PowerShell (roda e encerra sozinho quando a fila zera):
$py = "C:\Users\GalaxyBook3\Documents\Claude Code\VieireaTse\export_vps\.venv_new\Scripts\python.exe"
$acao = New-ScheduledTaskAction -Execute $py -Argument "main.py --producao" -WorkingDirectory "C:\Users\GalaxyBook3\Documents\Claude Code\VieireaTse\worker"
$gatilho = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
Register-ScheduledTask -TaskName "Portal-TSE-Worker" -Action $acao -Trigger $gatilho -Force
```

(O `schtasks /create` pode quebrar com o espaço em "Claude Code" no caminho —
por isso o registro via PowerShell, igual ao `registrar_tarefa.ps1` do
export_vps.)

## ✅ Estado: completo — situação (CPF-only) + onde votar (zona/seção/município)

`worker/tse_client.py:TseHttpClient` consulta o **cad-api.tse.jus.br** de
verdade, com dois fluxos (sempre 2 chamadas coladas por CPF — token + consulta;
o pacing 2.5–4s fica ENTRE CPFs):

- **CPF-only** (sem mãe/data): password-grant com só o CPF (token nível
  FERRO/ESTANHO) + POST `v5/eleitores/situacao-eleitoral` (headers
  `cpf`/`usuario`/`sistema`/`ip` — o `ip` é obrigatório).
- **Onde votar** (com mãe+data): o grant ganha `dataNascimento` (ISO) +
  `nomeMae` (maiúsculas) e sobe o token p/ nível ALUMINIO_CPF, dando acesso ao
  POST `v3/eleitores/onde-votar` — traz zona/seção/município/UF/local/endereço/
  bairro (prefere `ondeVotara`, fallback `domicilioEleitoral`) + nomeCivil e
  dataNascimento. Se o TSE **rejeitar mãe+data (401)**, o worker cai
  automaticamente no fluxo CPF-only (mesmo custo, registro não falha).

Token é sempre **1 por CPF, nunca reusado** (a API identifica pelo token e
ignora o header `cpf`). Retry com backoff em 429/5xx/rede (401 é definitivo,
sem retry), sessão `requests.Session` única (cookies). O resultado em JSON é
mapeado por `parse_resultado_json` pro mesmo shape do parser de texto — que
continua disponível como fallback (o `--stub` usa esse caminho). Nome/data que
vierem do próprio TSE preenchem as colunas `nome`/`data_nascimento` **só se
estiverem null** (best-effort, nunca sobrescreve o enriquecimento).
