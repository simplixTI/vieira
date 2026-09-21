# STATUS DO PROJETO — Sistema de Verificação TSE em Lote

> **Pro contexto de IA/agente:** este arquivo é o ponto de partida pra retomar o projeto.
> Leia ele inteiro, depois `.env.portal` (chaves, NUNCA imprima valores) e os arquivos-chave listados abaixo.
> Última atualização: 2026-09-20.
> **19/09:** **dedupe de CPF** (um CPF consultado nunca mais é consultado nem cobrado,
> migration 006); **conceito de CONTA** acima do usuário (migration 007 — a conta Vieira tem
> 4 logins dividindo uma cota e uma base); **segundo cliente** (Leo Vieira Filho) com portal
> próprio; coluna Celular por tenant; dashboard admin. Antes disso o projeto tinha 1 cliente,
> 1 login e nenhuma proteção contra CPF repetido.
> **20/09:** investigada a fundo a ausência de zona/seção — **§9**. Leia essa seção antes de
> tentar "consertar" isso: as três hipóteses óbvias já foram testadas e falharam.

---

## 1. O que é o sistema

Portal web onde o **cliente faz login, sobe uma lista de CPFs (CSV/Excel)** e acompanha em um
dashboard a verificação na base oficial do TSE: **situação eleitoral (apto/inapto + motivo),
título, zona, seção, município/UF e local de votação**. Resultado exportável em CSV.
Tudo sem navegador e sem captcha — consulta direta à API JSON do TSE.

**Fluxo:** upload (portal) → fila no Supabase → worker Python enriquece (nome da mãe +
nascimento via APIs pagas) → consulta TSE → grava resultado → dashboard atualiza sozinho.

## 2. Acessos e endereços

| Item | Valor |
|---|---|
| **Portal 1 — conta Vieira** | https://simplixti.github.io/vieira/ · **4 logins, senha `Vieira@2026` em todos**: `priscila@` (`e4685c8b...`, renomeado de anderson em 17/09, user_id preservado), `priscila01@`, `priscila02@`, `priscila03@` — todos `@vieira.com.br`. Conta `20b322ba...` |
| **Portal 2 — conta Leo Vieira Filho** | https://simplixti.github.io/leovieirafilho/ · login `glaucio@leovieirafilho.com.br` (`e17b74c1...`) · único com `FEATURES.celular = true` |
| Projeto Supabase | `TSE_VIEIRA` — ref `wipthjinvcyglbeuxxsb` (Canadá Central) — **um banco só; os dois clientes são isolados por RLS/`user_id`** |
| Repo do código + portal 1 | https://github.com/simplixTI/vieira (`main` = código; `gh-pages` = portal da Priscila, publicado de `portal/`) |
| Repo do portal 2 | https://github.com/simplixTI/leovieirafilho (**`main` = portal publicado**, arquivos na raiz, sem pasta `portal/`) |
| **VPS worker (produção)** | Hostinger — `root@179.198.117.127` — Ubuntu 24.04, Python 3.12; SSH via chave `~/.ssh/vps-db-179` (senha do root guardada com o Bruno) |
| Path do worker na VPS | `/opt/vieira-tse/` (git clone, venv em `.venv/`, .env.portal e export_vps/.env copiados por scp) |
| Chaves do Supabase (anon/service/senha do banco) | arquivo `.env.portal` na raiz deste projeto (e cópia em `/opt/vieira-tse/.env.portal` da VPS) |
| Chaves das APIs de enriquecimento (API B / Hashiro) | `export_vps/.env` (`API_B_KEY`, `HASHIRO_TOKEN`) |
| Webhook de alertas do worker (opcional) | `NOTIFY_WEBHOOK_URL` em `export_vps/.env` |

**Usuário de teste `teste@vieira.local` foi APAGADO.** Para criar usuários novos: API admin
GoTrue com a service key (`POST {SUPABASE_URL}/auth/v1/admin/users`).

## 3. Arquitetura

```
portal/        SPA estática (vanilla JS + supabase-js + SheetJS). Zero build.
               index.html / app.js / styles.css / config.js (anon key embutida)
worker/        Python. main.py (loop fases A+B), repo.py (Supabase), tse_client.py (HTTP TSE),
               fase_a.py (enriquecimento), config_worker.py, run_worker.bat, STOP (parada)
supabase/      migrations/ (001_inicial, 002_retry_erros, 003_limite_mensal,
               004_limites_por_cliente, 005_celular, 006_dedupe_cpf,
               007_contas) + aplica_schema.py
admin/         Dashboard interno (Flask, 127.0.0.1:8765 via SSH tunnel, usa SERVICE_KEY)
portal_tse/    Ferramentas de descoberta: probe_rede.py (captura XHR), tse_http_client.py,
               pega_js_tse.py, testa_upload_portal.py (Playwright), .venv próprio
export_vps/    Automação LEGADA (clicker AHK + captcha humano). NÃO faz parte do fluxo novo —
               foi só o "norte". Código reaproveitado: enriquecimento_lite.py (API B→Hashiro),
               tse_extracao_texto.py (parser texto, fallback), config.py, utils.py (notificar).
```

**Deploy do portal:** `git subtree push --prefix portal origin gh-pages` (depois de push na main).
GitHub Pages: Settings → Pages → branch gh-pages / root. **Cache da borda demora ~1 min**
(o cliente deve dar Ctrl+F5 pra ver mudanças de JS/HTML).

**Schema do banco:** `batches` (lotes, RLS por dono) + `voter_records` (1 CPF por linha:
cpf, nome, nome_mae, data_nascimento, status, elegibilidade, titulo_eleitoral, zona_eleitoral,
secao_eleitoral, municipio_votacao, uf, local/endereco/bairro_votacao, biometria,
obrigacao_eleitoral, motivo/ano_situacao, checked_at, attempts, updated_at, raw_text)
+ `avisos_limite` (dedup do aviso mensal ao ADM).
Status: `pending → enriching → ready_tse → checking → done | error`.

## 4. Contrato da API do TSE (descoberto por probe, VERIFICADO ao vivo)

Base: `https://cad-api.tse.jus.br`
Headers fixos em TODAS as chamadas: `api-authorization: c6f7e0616edfef74aee7cde0e796d1ae`,
`usuario: tn3-web`, `sistema: tn3-web`, **`ip: 1.1.1.1` (OBRIGATÓRIO — sem ele dá 400)**,
`referer: https://www.tse.jus.br/`, User-Agent Chrome. Constantes embutidas em `worker/tse_client.py`.

1. **Token (1 por CPF — NUNCA reusar entre CPFs; o token amarra ao CPF e o header cpf é ignorado):**
   `POST /eleitor-oauth/oauth/token`, Basic `YW5ndWxhcjpAbmd1bEByMA==`,
   body `grant_type=password&cpf=<CPF>` (+ opcional `dataNascimento=AAAA-MM-DD&nomeMae=<UPPERCASE urlencoded>`
   → sobe o nível p/ `ALUMINIO_CPF`, necessário p/ consulta rica). Token JWT ~30 min.
2. **Situação (CPF apenas):** `POST /eleitor-servico/services/eleitoral/v5/eleitores/situacao-eleitoral`, body `{}`.
   Retorna situacao (REGULAR/CANCELADO/SUSPENSO/TRANSFERIDO), inscrição, biometria, obrigação.
   **400 com corpo vazio/`[]` = CPF não cadastrado no TSE** (tratar como NAO_CADASTRADO, não erro).
3. **Completa (zona/seção/município/local):** `POST .../v3/eleitores/onde-votar` (mesmos headers,
   token ALUMINIO), body `{}`. Retorna `ondeVotara` (fallback `domicilioEleitoral`) com
   zona/secao/nome/endereco/bairro/municipio/uf + pleito corrente.
   **403 "Acesso negado"** em alguns CPFs (cancelados etc.) → fallback automático pra consulta 2.
4. A página www.tse.jus.br bloqueia HTTP direto (Akamai 403) — só via browser. A API não bloqueia.
5. **Risco monitorado:** existe a chave `PROPRIEDADE.ELEITOR-OAUTH.CAPTCHA-GOOGLE-HABILITADO=true`
   na config do app — hoje NÃO é aplicada no endpoint de token. Se um dia ligarem, as consultas
   passam a falhar → o worker marca ERRO e o webhook avisa. Plano B: 2captcha ou fluxo humano AHK.

## 5. Regras de negócio implementadas (worker)

- **Pacing:** 2,5–4s (jitter) entre CPFs; retry c/ backoff em 429/5xx; timeout; 1 sessão requests.
- **Anti-stale:** resultado validado contra o CPF pedido antes de gravar.
- **Retry de erro:** registro em `error` volta pra fila sozinho após 30 min (`RETRY_COOLDOWN_MIN`),
  até `MAX_TENTATIVAS=5` (uma rodada falha consome 2 attempts: checking+1, error+1).
- **Limite diário por CONTA:** `LIMITE_DIARIO_POR_CLIENTE` (default **6000**/dia — nome do env
  é herdado, o teto é por conta). Estourou → registros aguardam o dia seguinte, sozinhos.
- **Limite mensal por CONTA:** `LIMITE_MENSAL_POR_CLIENTE` (default **55000**/mês). Estourou →
  processamento da conta inteira pausa + **aviso único ao ADM no webhook** ("🚨 LIMITE MENSAL..."),
  dedup pela tabela `avisos_limite` (1x por conta por mês).
  **Para liberar após nova cobrança:** gravar override em `limites_por_conta` (preferível ao env,
  que é global) ou apagar a linha daquela conta em `avisos_limite` pro mês.
- **CONTA é a unidade de cobrança (migration 007).** Um cliente = uma conta = N logins.
  A conta Vieira tem 4 logins que dividem **uma** cota e **uma** base de CPFs; a conta
  Leo Vieira Filho tem 1. Tabelas `contas` + `contas_usuarios` (`user_id → conta_id`, com
  `rotulo` exibido no portal). **RLS não mudou**: cada login vê só os próprios lotes — a
  "parede" entre as pessoas do mesmo contrato foi pedido explícito do cliente.
  ⚠️ **Criar cliente novo agora tem 2 passos**: criar o usuário no GoTrue **e** vincular em
  `contas_usuarios`. Sem vínculo, o trigger recusa os inserts dele com mensagem explícita.
- **Dedupe de CPF por conta (migrations 006 + 007):** um CPF já consultado **nunca mais** é
  consultado para aquela conta — repetir custava 1 chamada de API paga + 1 consulta da cota
  e criava uma 2ª linha do mesmo CPF (dois históricos, duplicata no CSV).
  Garantia no banco: `voter_records.conta_id` + **índice único `(conta_id, cpf)`** +
  trigger `voter_records_dedupe_trg` (before insert). O trigger descarta o insert em silêncio
  quando o CPF já tem resultado ou está na fila; quando está em `error` terminal,
  **reaproveita a linha original** — movendo pro lote novo se for da mesma pessoa, ou
  reenfileirando no lugar se for de outro login (mover arrancaria linha do lote alheio).
  **Sem janela de reconsulta** — liberar é manual (`delete` da linha).
  ⚠️ O trigger só dispara em INSERT: worker e admin (que fazem UPDATE) não são afetados.
- **Pré-check do portal atravessa a parede sem vazar resultado:** a função
  `cpfs_ja_consultados(text[])` (`security definer`) responde "este CPF já existe na conta?"
  devolvendo só metadado — data e **qual login** consultou. Resultado eleitoral de outro
  login **nunca** chega ao portal; o próprio o portal lê direto da tabela, via RLS.
  Existe porque consultar a tabela direto só acharia os CPFs do próprio login, deixando
  passar a repetição entre colegas — exatamente o que a conta veio evitar.
- **Custo de enriquecimento:** 1 chamada API B por CPF (Hashiro só se API B falhar). Worker loga o total.
- Contagem de cota = registros `done` no período + `error` retentados no período.
- **Modo `--daemon`** (usado no systemd da VPS): em vez de sair quando a fila zera, dorme
  `DAEMON_IDLE_SEG` (default 5s) e re-checa. Latência de consulta avulsa ≈ 5s.
- **Consultas avulsas (1 CPF)**: lote com `filename='__avulsas__'` (1 por usuário). O worker
  **prioriza** essas em ambas as fases (`pegar_pendentes_enriquecimento` /
  `pegar_prontos_tse` buscam avulsas primeiro) e **nunca fecha** o lote em
  `atualizar_batches` (fica `processing` pra sempre; o portal segue com auto-refresh).

## 6. Como operar

**Produção (VPS Hostinger, systemd 24/7)** em `root@179.198.117.127`, path `/opt/vieira-tse/`.
São **DOIS serviços systemd no mesmo clone**, e é fácil esquecer o segundo:

| Serviço | O quê |
|---|---|
| `vieira-tse-worker` | o worker, modo `--daemon` (sleep 5s quando a fila zera). 512 MB, CPU 80%, auto-restart |
| `vieira-tse-admin` | o dashboard admin, Flask em `127.0.0.1:8765` (localhost-only) |

Task Scheduler do Windows local está **desabilitado** (era do sistema antigo AHK).

```bash
# ── monitorar ──
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 "systemctl status vieira-tse-worker"
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 "tail -f /opt/vieira-tse/worker/logs/worker_\$(date +%Y%m%d).log"

# ── reiniciar (mudou env ou apenas travou) ──
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 "systemctl restart vieira-tse-worker"

# ── deploy de código novo (depois de push no git main) ──
# ⚠️ git pull NÃO reinicia nada. Reinicie os DOIS — em 19/09 o admin ficou
#    rodando código velho depois de um pull e mostrava uma linha por LOGIN,
#    cada uma com a cota cheia, em vez de uma por conta.
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 \
  "cd /opt/vieira-tse && git pull && systemctl restart vieira-tse-worker vieira-tse-admin"
```

⚠️ **Nunca use `pkill -f '<string>'` dentro de um `ssh "..."`**: a própria linha de comando do
shell remoto contém a string e o `pkill` se mata, abortando em silêncio tudo que vinha depois
(foi assim que um `systemctl restart` não aconteceu e o serviço seguiu no PID antigo).

**Abrir o dashboard admin** (localhost-only, exige túnel SSH). Num terminal que fica **aberto**:

```bash
ssh -N -L 8765:localhost:8765 -i $HOME\.ssh\vps-db-179 -o ExitOnForwardFailure=yes root@179.198.117.127
```
Depois abra **http://localhost:8765/**. O `-N` faz só o túnel, sem abrir shell — a janela fica
**parada e sem escrever nada**, é o comportamento certo; fechar derruba o túnel.
`-o ExitOnForwardFailure=yes` faz falhar alto se a 8765 já estiver ocupada, em vez de conectar
e o navegador não abrir nada (era o sintoma de 19/09).
Abas: **Visão geral** (fila global + consumo por conta + lotes recentes), **Erros** (com retry),
**Sem zona/seção** (aptidão confirmada mas o TSE não devolveu o local, com "Reprocessar TSE").

**Env vars do serviço** (drop-in em `/etc/systemd/system/vieira-tse-worker.service.d/limits.conf`) —
conferido na VPS em 19/09, **sem trial em lugar nenhum**:
- `LIMITE_MENSAL_POR_CLIENTE=55000` — teto padrão, aplicado a **toda conta sem override**.
- `LIMITE_DIARIO_POR_CLIENTE=6000` — idem.

⚠️ Apesar do nome (`..._POR_CLIENTE`, herdado), desde a migration 007 o teto é **por CONTA**:
a conta Vieira tem 4 logins que **dividem** esses 55.000, não um teto cada.

**Teto diferente pra uma conta específica** — grave na tabela em vez de mexer no systemd
(o env é global e afeta todas as contas):
```sql
insert into limites_por_conta (conta_id, limite_mensal, limite_diario, nota)
values ('<conta_id>', 80000, 8000, 'contrato X')
on conflict (conta_id) do update
  set limite_mensal = excluded.limite_mensal, limite_diario = excluded.limite_diario;
```
Hoje a tabela está **vazia** — as duas contas rodam no default global. Consumo atual: ver o
quadro no fim do §7 (ou, mais confiável, o próprio dashboard admin — o número muda todo dia).

**Janela manual (só pra debug local, opcional):**
```
cd worker
..\export_vps\.venv_new\Scripts\python.exe main.py --producao --limite N
```
(`--fase a|b|ambas`, `--stub` p/ teste sem rede; arquivo `worker/STOP` para entre registros;
logs em `worker/logs/`.) O venv local que funciona é `export_vps/.venv_new`. **Não usar a
máquina local pra produção — a VPS já cobre isso.**

**Aplicar migrations no banco:**
```
export_vps/.venv_new/Scripts/python.exe supabase/aplica_schema.py
```
(usa `.env.portal`; precisa de `psycopg[binary]` instalado nesse venv.)

**Enriquecimento:** `export_vps/enriquecimento_lite.py` (importado pelo worker), chaves de
`export_vps/.env`. API B: `consulta.tabajarachecks.vip/api/v1/pessoa/{cpf}?key=...`.
Hashiro (fallback): `https://hashirosearch.squareweb.app/?token=...&cpf1={cpf}` (token NOVO desde 16/09).

**Ligar o portal local p/ teste:** qualquer servidor estático na pasta `portal/`.

## 7. Estado atual e histórico

- **Fases 0–3 concluídas:** descoberta do endpoint, schema, worker, portal, deploy, Git.
- **Piloto de 100 CPFs aleatórios:** completo, rodada final com **0 erros** em 86 registros.
- **17/09 — Feature nova: consulta avulsa de 1 CPF** ([commit b5bb5f0](https://github.com/simplixTI/vieira/commit/b5bb5f0)).
  Card "Consultar 1 CPF" no topo do dashboard; cria lote persistente `__avulsas__` por usuário
  e insere 1 CPF `pending`. Worker prioriza avulsas nas duas fases; portal faz polling a cada
  5s e mostra o resultado (aptidão, zona, seção, município). Reusa a mesma tabela + botão de
  exportar CSV.
- **17/09 — Worker migrado pra VPS Hostinger 24/7** ([commit 93f5cf9](https://github.com/simplixTI/vieira/commit/93f5cf9)):
  novo modo `--daemon` + serviço systemd `vieira-tse-worker` rodando em `/opt/vieira-tse/`.
  Task Scheduler do Windows local desabilitado.
- **17/09 — Priscila entra:** anderson@ renomeado pra `priscila@vieira.com.br` (mesmo user_id);
  histórico de teste (2 avulsas) apagado. Entrou com limite de trial (100/mês), **encerrado
  desde então** — em 19/09 a VPS já estava com o teto de produção 55000/6000.
- **18/09 — Dashboard admin interno** (`admin/`, Flask em 127.0.0.1:8765 via SSH tunnel):
  visão por cliente, aba de erros, aba "Sem zona/seção" com botão "Reprocessar TSE"
  (é um UPDATE na linha, não um insert — não passa pelo dedupe).
- **18/09 — Limites por cliente em tabela** (`limites_por_cliente`, migration 004): override
  dos defaults globais sem mexer no systemd.
- **18/09 — Segundo cliente: Leo Vieira Filho.** Portal próprio em
  `simplixTI/leovieirafilho` (mesmo backend, isolado por RLS), com a coluna **Celular**
  opcional por tenant (migration 005 + `FEATURES.celular` no `config.js`).
- **19/09 — Dedupe de CPF** (migration 006 + portal; escopo virou CONTA na 007 no mesmo dia):
  um CPF consultado nunca
  mais é consultado nem cobrado. Motivador medido no banco: CPFs repetidos **todos vindos
  da consulta avulsa** — havia caso de CPF consultado às 19:12 e de novo às 19:13.
  A migration apagou **10 linhas excedentes** (261 → 251 registros), mantendo a consulta
  mais recente de cada CPF. Desenho completo em
  `docs/superpowers/specs/2026-09-19-dedupe-cpf-design.md`.
- **19/09 — Conceito de CONTA** (migration 007): a conta Vieira ganhou 3 logins novos
  (`priscila01/02/03@vieira.com.br`) para que cada pessoa veja só os próprios lotes, sem
  multiplicar custo. Dedupe e cota passaram de `user_id` para `conta_id`; RLS intocado.
  Worker (`repo.mapa_lotes_donos` agora devolve conta), admin (painel por conta) e portal
  (pré-check via `cpfs_ja_consultados`) acompanharam.
- **19/09 — Incidente de cache no portal do Leo.** O cliente viu *"Cannot coerce the result to
  a single JSON object"* numa consulta avulsa. **Não era bug de dado**: o CPF já tinha sido
  consultado pela conta em 18/09 e o trigger barrou corretamente; o navegador é que rodava o
  `app.js` antigo (com `.single()`), que estoura ao receber zero linha. Corrigido com `?v=` nos
  assets + tradução do erro pra mensagem de negócio. **Lição: republicar o portal não basta —
  sem bump do `?v=`, o cliente continua com o arquivo em cache.**
- **19/09 — Mensagem de CPF repetido simplificada** a pedido do cliente: lidera com
  *"O CPF digitado já foi consultado!"* e nunca renderiza em vermelho (ok/warn/pending).
- **Migrations aplicadas: 001–007.**

**Estado em 19/09 (fim do dia), conferido no admin:**

| Conta | Logins | Hoje | Mês | Teto |
|---|---|---|---|---|
| Leo Vieira Filho | glaucio | 387 | 412 | 55.000 |
| Vieira | priscila + priscila01/02/03 | 80 | 267 | 55.000 |

Fila global: **679 `done`, 0 `error`, 0 em aberto.** Worker e admin ativos na VPS, ambos no
código mais recente. Portais publicados em `?v=20260919c`.
- **Trial encerrado — nada pendente de cobrança.** Vieira e Leo Vieira Filho são contratos
  normais, ambos no teto padrão 55000/mês, sem override na `limites_por_conta`.
- Elegibilidade: `apto` / `inapto_cancelado` / `inapto_suspenso` / `inapto_transferido` /
  `regularizar_tse` (CPF sem título). Mapa em `worker/repo.py:mapear_elegibilidade`.
- **~34% dos registros saem sem zona/seção** — medido, explicado e sem solução com as fontes
  atuais. **Ver §9** antes de mexer nisso: reprocessar no TSE, reenriquecer e mandar só a data
  de nascimento já foram testados e os três falharam.

## 8. Decisões e convenções pra não requebrar

- O portal **nunca** vê a service key (só anon key + RLS; updates só via worker).
- Upload: max 40.000 CPFs por lote (texto do portal cita o ritmo de 6k/dia).
- CSV exportado com `;` (Excel pt-BR) e BOM UTF-8.
- Git: `.env*`/venvs/logs/`bridge/*.txt`/artefatos de probe estão no `.gitignore`.
- Supabase CLI da máquina loga numa conta que NÃO tem acesso a este projeto — não usar CLI
  pra este projeto; usar `.env.portal` + `aplica_schema.py`.
- **Ao alterar `portal/`, são DOIS destinos.** `app.js` e `styles.css` são idênticos nos dois
  portais; só `config.js` (flag `celular`) e `index.html` (nome na marca) diferem. Esquecer o
  segundo deixa um cliente sem a mudança:
  ```
  # 1) portal da Priscila
  git push origin main && git subtree push --prefix portal origin gh-pages
  # 2) portal do Leo — copia app.js/styles.css e mostra o diff de config.js/index.html
  bash scripts/sincroniza_portal_leo.sh [/caminho/do/clone]   # não faz push sozinho
  ```
  O script **não** copia `config.js`/`index.html` (são do tenant) — ele mostra o diff pra
  você aplicar à mão o que for estrutural. Clone: `git clone https://github.com/simplixTI/leovieirafilho`.
- **BUMP OBRIGATÓRIO do `?v=` no `index.html`** (`app.js?v=AAAAMMDD<letra>`) a cada deploy que
  mexa em `app.js`/`styles.css`, **nos dois portais**. Sem isso o navegador do cliente continua
  rodando o JS antigo contra o banco novo: em 19/09 o portal do Leo mostrou *"Cannot coerce the
  result to a single JSON object"* exatamente por isso (JS pré-dedupe recebendo zero linha do
  insert descartado pelo trigger). O `sincroniza_portal_leo.sh` avisa se as versões divergirem.
- Ao alterar `worker/`, `admin/` ou `export_vps/`, lembrar: push main → SSH na VPS →
  `cd /opt/vieira-tse && git pull && systemctl restart vieira-tse-worker vieira-tse-admin`.
  **São dois serviços no mesmo clone e o `git pull` não reinicia nenhum** (§6).
- **Criar cliente/login novo tem DOIS passos**: criar o usuário no GoTrue **e** vincular em
  `contas_usuarios` (a uma conta existente, se for mais um login do mesmo contrato; a uma conta
  nova, se for cliente novo). Sem o vínculo o trigger recusa os inserts dele com mensagem
  explícita. Cadastro público no portal continua desativado.
- **Teto de uma conta específica**: gravar em `limites_por_conta`, **não** mexer no env do
  systemd — o env é global e alteraria todas as contas de uma vez (§6).
- **Chave SSH** da VPS: `~/.ssh/vps-db-179` (ED25519, autorizada em `authorized_keys` do root).
  Se cair de novo (Hostinger reinstala VPS ou senha muda), pedir pro Bruno rodar
  `type $HOME\.ssh\vps-db-179.pub | ssh root@179.198.117.127 "cat >> ~/.ssh/authorized_keys"`
  digitando a senha nova do root uma vez.
- **Deploy da VPS ficou lembrado no comando** — não usar Docker aqui (a VPS tem docker rodando
  outros projetos: agente-sap/maturix/pontotel/postgres, mas o worker é 1 serviço systemd puro,
  1 venv, ~40 MB de RAM — mais simples que container).

---

## 9. Limitação medida: zona/seção ausente (investigado em 20/09/2026)

**Resumo:** ~34% dos registros `done` não têm zona/seção. **Isso não é bug e não tem conserto
com as fontes atuais** — está medido, não suposto. Antes de tentar "arrumar" de novo, leia isto:
as três hipóteses óbvias já foram testadas e as três falharam.

### Por que falta (355 registros sem zona, medidos em 20/09)

| Causa | Qtd | Tem solução? |
|---|---|---|
| Apto, **com** mãe+data — TSE respondeu 403 no `onde-votar` | 160 | ❌ recusa definitiva do TSE |
| Apto, **sem nome da mãe** (116 só mãe + 46 mãe+data) | 162 | ❌ não com as fontes atuais |
| Sem título (`regularizar_tse`) ou inapto | 33 | ✅ correto — não tem zona mesmo |

### O que já foi testado e NÃO funciona

1. **Reprocessar no TSE** (botão "Reprocessar TSE" do admin) nos que têm mãe+data:
   **0 de 15 recuperaram.** Nenhum virou erro, todos seguiram `apto` — a recusa do TSE é
   **permanente, não transitória**. Reprocessar os 160 gastaria 160 consultas pra recuperar
   ~zero. **Não faça.**
2. **Reenriquecer com API B / Hashiro** nos que não têm mãe: **0 de 15 trouxeram a filiação.**
   E o ponto importante: **11 dos 15 voltaram com status `enriquecido`** — as fontes *têm* a
   pessoa (devolvem nome, data, título), elas só **não carregam o nome da mãe** dela. Não foi
   falha pontual nem instabilidade; é lacuna de cobertura do dado.
3. **Pedir o token do TSE só com `dataNascimento`, sem `nomeMae`** (hipótese: talvez bastasse
   a data pra subir de nível). Testado direto na API: o token sai com
   `['BRONZE_NEG','NIQUEL_NEG','FERRO']` — **nunca `ALUMINIO_CPF`** — e o `onde-votar` devolve
   403 nos 5 testados. **O nome da mãe é obrigatório.** A lógica atual de
   `tse_client.py` (só tenta a consulta rica com mãe **E** data) está correta — não mexa nela
   achando que há ganho fácil ali.

### A descoberta que explica tudo: é problema de IDADE

| Faixa | Com nome da mãe | Sem | % sem |
|---|---|---|---|
| 16–21 | 7 | 46 | **87%** |
| 22–29 | 129 | 71 | 36% |
| 30–44 | 307 | 2 | 1% |
| 45–59 | 271 | 5 | 2% |
| 60+ | 130 | 0 | 0% |

**A lacuna é quase inteiramente de eleitores jovens.** Para 30+ as fontes cobrem 98–100%.
Faz sentido: API B e Hashiro são bureaus de crédito/consumo, e jovem de 17 anos não tem
rastro nesses cadastros ainda.

**Consequência prática:** o "34% sem zona" não é qualidade ruim do sistema — é concentrado
numa faixa etária. Campanha mirando 30+ recebe quase 100% completo; campanha de primeiro voto
perde um terço. **Antes de gastar com fonte nova, meça quantos jovens tem a base do cliente.**

### DataSintese: não está quebrada, está sem créditos

`DATASINTESE_ENABLED = False` em `export_vps/settings.py`, mas as credenciais existem no
`export_vps/.env`. Testada isoladamente em 20/09: autenticação OK, endpoint responde, e o
retorno é
`HTTP 400 {"consumo":{"mensagem":"Você excedeu seus créditos..."}}`.
Ou seja: **decisão comercial, não problema técnico.** Se recarregar os créditos, basta virar a
flag pra `True` — a cascata (`enriquecer_cpf`) já cai pra ela corretamente quando as anteriores
respondem incompletas. Não foi validado se a DataSintese tem filiação de jovem — teste antes
de contratar.

### Em aberto

- **Bruno está procurando fonte de dados com filiação de eleitor jovem** (20/09). Bureaus de
  crédito provavelmente não resolvem — a informação não existe nesses cadastros. O caminho
  seria fonte de origem diferente (cartório/Receita), não mais um bureau de consumo.
