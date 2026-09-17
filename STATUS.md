# STATUS DO PROJETO — Sistema de Verificação TSE em Lote

> **Pro contexto de IA/agente:** este arquivo é o ponto de partida pra retomar o projeto.
> Leia ele inteiro, depois `.env.portal` (chaves, NUNCA imprima valores) e os arquivos-chave listados abaixo.
> Última atualização: 2026-09-16 (piloto concluído, aguardando primeiro lote real do cliente).

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
| Portal (produção) | https://simplixti.github.io/vieira/ |
| Login do cliente | `anderson@vieira.com.br` / `Vieira@2026` |
| Projeto Supabase | `TSE_VIEIRA` — ref `wipthjinvcyglbeuxxsb` (Canadá Central) |
| Repositório GitHub | https://github.com/simplixTI/vieira (branch `main` = código; `gh-pages` = portal publicado) |
| Chaves do Supabase (anon/service/senha do banco) | arquivo `.env.portal` na raiz deste projeto |
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
supabase/      migrations/ (001_inicial, 002_retry_erros, 003_limite_mensal) + aplica_schema.py
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
- **Custo de enriquecimento:** 1 chamada API B por CPF (Hashiro só se API B falhar). Worker loga o total.
- Contagem de cota = registros `done` no período + `error` retentados no período.

## 6. Como operar

**Rodar o worker (janela manual):**
```
cd worker
..\export_vps\.venv_new\Scripts\python.exe main.py --producao --limite N
```
(`--fase a|b|ambas`, `--stub` p/ teste sem rede; arquivo `worker/STOP` para entre registros;
logs em `worker/logs/`.) O venv que funciona é `export_vps/.venv_new` (o `.venv` antigo tá quebrado).
**Produção contínua:** agendar `run_worker.bat` a cada 15 min via Task Scheduler (padrão CRMLite).

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
  ~15% dos CPFs aleatórios bateram com pessoas reais (conferido: estatística bate). Bugs de
  upload do portal e 3 do worker encontrados e corrigidos (histórico no git).
- **Banco ZERADO** (lotes de teste apagados; estrutura intacta; migrations 001–003 aplicadas).
- **Legenda "Entenda os resultados"** no rodapé do dashboard (apto/inapto/regularizar/erro).
- **PENDENTE:** primeiro upload real do cliente (planilha de CPFs verdadeiros).
- Elegibilidade: `apto` / `inapto_cancelado` / `inapto_suspenso` / `inapto_transferido` /
  `regularizar_tse` (CPF sem título). Mapa em `worker/repo.py:mapear_elegibilidade`.

## 8. Decisões e convenções pra não requebrar

- O portal **nunca** vê a service key (só anon key + RLS; updates só via worker).
- Upload: max 40.000 CPFs por lote (texto do portal cita o ritmo de 6k/dia).
- CSV exportado com `;` (Excel pt-BR) e BOM UTF-8.
- Git: `.env*`/venvs/logs/`bridge/*.txt`/artefatos de probe estão no `.gitignore`.
- Supabase CLI da máquina loga numa conta que NÃO tem acesso a este projeto — não usar CLI
  pra este projeto; usar `.env.portal` + `aplica_schema.py`.
- Ao alterar `portal/`, lembrar: commit → push main → `git subtree push --prefix portal origin gh-pages`.
