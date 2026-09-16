# Automação — Worker TSE → CRM Lite

Como rodar o scraper do TSE (`export_vps`) contra o projeto Supabase do
**CRM Lite** (tabela `public.people`), de forma agendada no Windows.

Arquivos novos (na pasta `export_vps`):

| Arquivo | Papel |
|---|---|
| `supabase_repo_crmlite.py` | Camada de tradução: mesmas funções do `supabase_repo.py`, mas lendo/gravando em `people` |
| `crmlite_main.py` | Wrapper: injeta a camada nova e roda o orquestrador original intacto |
| `.env.crmlite.example` | Template de configuração |
| `run_crmlite.bat` | Sobe AHK + Chrome se preciso e roda `--producao` |

---

## 1. Configurar (uma vez)

1. Copie o template de ambiente e preencha a service_role key do CRM Lite:

   ```powershell
   cd "C:\Users\GalaxyBook3\Documents\Claude Code\tse_atual_2307\tse_atual_2307\export_vps"
   copy .env.crmlite.example .env.crmlite
   notepad .env.crmlite
   ```

   - `SUPABASE_URL` já vem preenchida (`https://gprahiwcxvqorpuielju.supabase.co`).
   - `SUPABASE_SERVICE_KEY`: Supabase → **Project Settings → API → service_role**
     (⚠️ ignora RLS — nunca commite nem exponha).
   - `API_B_KEY` / `HASHIRO_TOKEN`: opcionais, alimentam a fase A
     (enriquecimento de nome da mãe / data de nascimento a partir do CPF).
   - `NOTIFY_WEBHOOK_URL`: opcional (Discord/Slack) — avisa de captcha
     pendente, fila zerada e produção travada.

2. Teste manual (dry-run de conexão/leitura, sem scraping):

   ```powershell
   env_bg\Scripts\python.exe crmlite_main.py --producao --max-rodadas 1
   ```

   Com a tabela vazia ele deve dizer "ZERADO" e sair.

## 2. Calibrar (uma vez por máquina/resolução)

A calibração dos cliques (F9/F10, Ctrl+Shift+K, zoom 100%, janela fixa) é a
mesma do worker original — siga o **LEIA-ME.md**, seções 4–6. Sem calibração
o AHK clica nos lugares errados.

## 3. Agendar a cada 15 minutos (Agendador de Tarefas)

A tarefa **já está registrada** como `CRMLite-TSE-Worker`, criada pelo script
`registrar_tarefa.ps1` (o `schtasks /create` quebra com o espaço em
"Claude Code" no caminho — por isso o registro é via PowerShell).

Ela foi criada **DESABILITADA** de propósito: calibre e teste manualmente
antes de ligar, senão o AHK clica nos lugares errados a cada 15 min.

```powershell
# Habilitar (depois de calibrar e testar run_crmlite.bat na mão):
Enable-ScheduledTask -TaskName 'CRMLite-TSE-Worker'

# Ver status / rodar na hora / desabilitar / remover:
Get-ScheduledTask -TaskName 'CRMLite-TSE-Worker'
Start-ScheduledTask -TaskName 'CRMLite-TSE-Worker'
Disable-ScheduledTask -TaskName 'CRMLite-TSE-Worker'
Unregister-ScheduledTask -TaskName 'CRMLite-TSE-Worker' -Confirm:$false
```

Para recriar do zero (ex.: mudou o caminho da pasta):

```powershell
powershell -ExecutionPolicy Bypass -File registrar_tarefa.ps1
```

Log das execuções agendadas: `logs/agendador.log`.

O `.bat` é idempotente: se o AHK e o Chrome já estiverem no ar, ele só roda
o Python; se a fila estiver zerada, o Python encerra em segundos.

## 4. Limitações (inalteradas do worker original)

- **Captcha continua manual**: cada consulta TSE exibe 1 captcha; o worker
  espera ~30s por resolução humana (com webhook configurado você recebe o
  aviso). Captcha não resolvido = registro fica pendente para a próxima rodada.
- **Sessão TSE precisa estar logada/sem modal**: se o portão de autenticação
  ("Olá, seja bem-vindo") aparecer, resolva na mão — o worker não preenche
  esse modal (detalhes no LEIA-ME.md).
- A máquina precisa estar **com sessão gráfica aberta** (o AHK move o mouse
  de verdade — não funciona em sessão bloqueada/headless).
- Nunca use `F11` com o Chrome em foco (fullscreen descalibra tudo).
