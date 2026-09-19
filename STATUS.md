# STATUS DO PROJETO — Sistema de Verificação TSE em Lote

> **Pro contexto de IA/agente:** este arquivo é o ponto de partida pra retomar o projeto.
> Leia ele inteiro, depois `.env.portal` (chaves, NUNCA imprima valores) e os arquivos-chave listados abaixo.
> Última atualização: 2026-09-19 (dedupe de CPF por cliente — um CPF consultado nunca mais é
> consultado nem cobrado; **segundo cliente (Leo Vieira Filho) com portal próprio**; coluna
> Celular opcional por tenant; dashboard admin interno).

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
| **Portal 1 — Priscila** | https://simplixti.github.io/vieira/ · login `priscila@vieira.com.br` / `Vieira@2026` (renomeado de anderson em 17/09 via GoTrue admin; **user_id preservado** `e4685c8b...`) |
| **Portal 2 — Leo Vieira Filho** | https://simplixti.github.io/leovieirafilho/ · user_id `e17b74c1...` · único com `FEATURES.celular = true` |
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
               004_limites_por_cliente, 005_celular, 006_dedupe_cpf) + aplica_schema.py
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
- **Limite diário por cliente:** `LIMITE_DIARIO_POR_CLIENTE` (default **6000**/dia). Estourou →
  registros aguardam o dia seguinte, sozinho.
- **Limite mensal por cliente:** `LIMITE_MENSAL_POR_CLIENTE` (default **50000**/mês). Estourou →
  processamento do cliente pausa + **aviso único ao ADM no webhook** ("🚨 LIMITE MENSAL..."),
  dedup pela tabela `avisos_limite` (1x por cliente por mês).
  **Para liberar após nova cobrança:** subir o env ou apagar a linha do cliente em `avisos_limite` pro mês.
- **Dedupe de CPF por cliente (migration 006):** um CPF já consultado **nunca mais** é
  consultado para aquele cliente — repetir custava 1 chamada de API paga + 1 consulta da cota
  e criava uma 2ª linha do mesmo CPF (dois históricos, duplicata no CSV).
  Garantia no banco: coluna `voter_records.user_id` + **índice único `(user_id, cpf)`** +
  trigger `voter_records_dedupe_trg` (before insert). O trigger descarta o insert em silêncio
  quando o CPF já tem resultado ou está na fila; quando o CPF está em `error` terminal,
  **reaproveita a linha original** (move pro lote novo, volta a `pending`, zera `attempts`)
  em vez de duplicar. O portal avisa o cliente antes, com data e lote da consulta anterior.
  Escopo por cliente, não global. **Sem janela de reconsulta** — liberar é manual.
  ⚠️ O trigger só dispara em INSERT: worker e admin (que fazem UPDATE) não são afetados.
- **Custo de enriquecimento:** 1 chamada API B por CPF (Hashiro só se API B falhar). Worker loga o total.
- Contagem de cota = registros `done` no período + `error` retentados no período.
- **Modo `--daemon`** (usado no systemd da VPS): em vez de sair quando a fila zera, dorme
  `DAEMON_IDLE_SEG` (default 5s) e re-checa. Latência de consulta avulsa ≈ 5s.
- **Consultas avulsas (1 CPF)**: lote com `filename='__avulsas__'` (1 por usuário). O worker
  **prioriza** essas em ambas as fases (`pegar_pendentes_enriquecimento` /
  `pegar_prontos_tse` buscam avulsas primeiro) e **nunca fecha** o lote em
  `atualizar_batches` (fica `processing` pra sempre; o portal segue com auto-refresh).

## 6. Como operar

**Produção (VPS Hostinger, systemd 24/7):** o worker roda como serviço `vieira-tse-worker`
em `root@179.198.117.127`, em modo `--daemon` (loop com sleep 5s quando fila zera). Auto-restart
via systemd, memória limitada a 512 MB, CPU até 80%. Task Scheduler do Windows local está
**desabilitado** (não usar mais — era do sistema antigo AHK).

```bash
# ── monitorar ──
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 "systemctl status vieira-tse-worker"
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 "tail -f /opt/vieira-tse/worker/logs/worker_\$(date +%Y%m%d).log"

# ── reiniciar (mudou env ou apenas travou) ──
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 "systemctl restart vieira-tse-worker"

# ── deploy de código novo (depois de push no git main) ──
ssh -i ~/.ssh/vps-db-179 root@179.198.117.127 \
  "cd /opt/vieira-tse && git pull && systemctl restart vieira-tse-worker"
```

**Env vars do serviço** (drop-in em `/etc/systemd/system/vieira-tse-worker.service.d/limits.conf`):
- `LIMITE_MENSAL_POR_CLIENTE=100` — **trial da Priscila** (default de produção é 50000).
- `LIMITE_DIARIO_POR_CLIENTE=100` — trial da Priscila (default 6000).
- Pra liberar após Priscila pagar: `sed -i 's/=100/=50000/;...' limits.conf`, `daemon-reload`, `restart`
  (comando completo no card final da sessão de 17/09).

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
- **17/09 — Priscila em trial:** anderson@ renomeado pra `priscila@vieira.com.br` (mesmo
  user_id); histórico de teste (2 avulsas) apagado; limite de 100 consultas/mês configurado
  via env vars do systemd (`LIMITE_MENSAL_POR_CLIENTE=100` + `LIMITE_DIARIO_POR_CLIENTE=100`).
  Ao estourar, o worker pausa a fila dela e o webhook alerta o ADM.
- **18/09 — Dashboard admin interno** (`admin/`, Flask em 127.0.0.1:8765 via SSH tunnel):
  visão por cliente, aba de erros, aba "Sem zona/seção" com botão "Reprocessar TSE"
  (é um UPDATE na linha, não um insert — não passa pelo dedupe).
- **18/09 — Limites por cliente em tabela** (`limites_por_cliente`, migration 004): override
  dos defaults globais sem mexer no systemd.
- **18/09 — Segundo cliente: Leo Vieira Filho.** Portal próprio em
  `simplixTI/leovieirafilho` (mesmo backend, isolado por RLS), com a coluna **Celular**
  opcional por tenant (migration 005 + `FEATURES.celular` no `config.js`).
- **19/09 — Dedupe de CPF por cliente** (migration 006 + portal): um CPF consultado nunca
  mais é consultado nem cobrado. Motivador medido no banco: CPFs repetidos **todos vindos
  da consulta avulsa** — havia caso de CPF consultado às 19:12 e de novo às 19:13.
  A migration apagou **10 linhas excedentes** (261 → 251 registros), mantendo a consulta
  mais recente de cada CPF. Desenho completo em
  `docs/superpowers/specs/2026-09-19-dedupe-cpf-design.md`.
- **Migrations aplicadas: 001–006.**
- **PENDENTE 18/09+:** Priscila usar as 100 do trial e (a) pagar → subir limite pra 50000 (comando
  no §6); ou (b) não pagar → banir o usuário via painel Supabase Auth (`Users → priscila → Ban`).
- Elegibilidade: `apto` / `inapto_cancelado` / `inapto_suspenso` / `inapto_transferido` /
  `regularizar_tse` (CPF sem título). Mapa em `worker/repo.py:mapear_elegibilidade`.

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
- Ao alterar `worker/` ou `export_vps/`, lembrar: push main → SSH na VPS →
  `cd /opt/vieira-tse && git pull && systemctl restart vieira-tse-worker`.
- **Chave SSH** da VPS: `~/.ssh/vps-db-179` (ED25519, autorizada em `authorized_keys` do root).
  Se cair de novo (Hostinger reinstala VPS ou senha muda), pedir pro Bruno rodar
  `type $HOME\.ssh\vps-db-179.pub | ssh root@179.198.117.127 "cat >> ~/.ssh/authorized_keys"`
  digitando a senha nova do root uma vez.
- **Deploy da VPS ficou lembrado no comando** — não usar Docker aqui (a VPS tem docker rodando
  outros projetos: agente-sap/maturix/pontotel/postgres, mas o worker é 1 serviço systemd puro,
  1 venv, ~40 MB de RAM — mais simples que container).
