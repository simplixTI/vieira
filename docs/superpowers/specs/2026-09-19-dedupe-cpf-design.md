# Dedupe de CPF por cliente — design

Data: 2026-09-19 · Status: aprovado

## Problema

Um CPF que o cliente já consultou pode ser enviado de novo. Quando isso acontece:

1. **Custa dinheiro duas vezes** — a Fase A queima 1 chamada de API paga (API B/Hashiro)
   e a Fase B queima 1 consulta TSE, que conta na cota diária/mensal do cliente.
2. **Quebra a rastreabilidade** — o mesmo CPF passa a ter duas linhas com históricos
   separados, e sai duplicado no CSV exportado.

O portal só dedupa **dentro de um arquivo** (`portal/app.js`, motivo
`'CPF duplicado no arquivo'`). Entre lotes, e na caixa de "Consultar 1 CPF", não há nada.

### Evidência (banco em 19/09/2026)

253 registros, todos `done`. **9 pares (cliente, CPF) duplicados** — 8 da Priscila
(`e4685c8b`), 1 do Leo (`e17b74c1`). **Todos os 9 vieram da consulta avulsa**, não de
upload de arquivo. Dois deles foram consultados com 1 minuto de diferença
(19:12 e 19:13), o que indica cliente clicando duas vezes, não intenção de reconsultar.

## Regra de negócio

Um CPF consultado por um cliente **nunca mais** é consultado para aquele cliente.

- **Escopo: por cliente** (`user_id`), não global. Se fosse global, a Priscila seria
  bloqueada por um CPF que o Leo consultou e ficaria sem resultado nenhum.
- **Validade: permanente.** Sem janela de reconsulta automática. Se o cliente precisar
  reconsultar (o eleitor regularizou o título), a liberação é manual via admin.
- **Exceção: `error` terminal.** CPF que esgotou as 5 tentativas não entregou nada ao
  cliente, então volta a ser consultável.

| Situação do CPF no cliente | Decisão |
|---|---|
| Não existe | Consulta normalmente |
| `done` (inclusive `regularizar_tse`, que é resposta válida do TSE) | Bloqueia |
| Em processamento (`pending`/`enriching`/`ready_tse`/`checking`) | Bloqueia |
| `error` com `attempts >= 5` | Libera — reaproveita a linha existente |

## Arquitetura

Duas camadas: o banco **garante**, o portal **explica**.

### Camada 1 — Banco (migration 006)

**a) Coluna `voter_records.user_id`**, denormalizada de `batches.user_id`. Hoje só dá
pra saber o dono de uma linha passando pelo `batch_id`, o que impede qualquer constraint
de unicidade por cliente. Preenchida por trigger no insert + backfill dos 253 existentes.

**b) Índice único `(user_id, cpf)`** — a garantia física. Vale para o portal, o admin,
scripts e duas abas simultâneas. É o que dá a rastreabilidade: **1 CPF = 1 linha = 1 histórico**.

**c) Trigger `before insert`** que decide o destino do insert:

- CPF inédito → insere (preenchendo `user_id`).
- CPF já com resultado ou na fila → `return null`: descarta o insert em silêncio, custo zero.
- CPF em `error` → **reaproveita a linha original**: move pro lote novo, volta a `pending`,
  zera `attempts`, e descarta o insert. Assim o cliente vê o CPF no lote novo dele sem
  que exista uma segunda linha daquele CPF.

O trigger é `security definer` (precisa do UPDATE, que RLS não concede a `authenticated`),
com guarda: se `auth.uid()` não for nulo e não for o dono do lote, levanta exceção. Isso
impede que um cliente use um `batch_id` de outro para mexer nas linhas alheias.

### Camada 2 — Portal (`portal/app.js`)

O banco descarta em silêncio; o cliente precisa saber por quê.

**Upload de arquivo:** depois do parse, consulta os CPFs já existentes do cliente em
blocos de 500 (limite de tamanho de URL do PostgREST) com progresso na tela. Os repetidos
entram na lista de rejeitados que já existe, com motivo `Já consultado em DD/MM (lote X)`.

**Consulta avulsa** (onde estão os 9 casos reais): checa antes de enviar. Se o CPF já foi
consultado, mostra o resultado que já existe em vez de gastar uma consulta nova.

### Camada 3 — Worker e admin

Nada a fazer. O worker só insere em `avisos_limite`; o admin não insere nada (o botão
"Reprocessar TSE" é um `update` na linha existente). O trigger só dispara pelo portal.

## Limpeza dos duplicados existentes

O índice único não nasce com duplicata no banco. A migration apaga as 9 linhas excedentes
**mantendo a de `checked_at` mais recente** de cada par (dado eleitoral mais fresco).
Nenhum CPF desaparece — só a cópia.

## Deploy

**Ordem obrigatória: portal primeiro, migration depois.** Se a migration subir antes, o
trigger passa a descartar inserts enquanto o `app.js` antigo ainda espera uma linha de
volta na consulta avulsa (`.select('id').single()`), e o cliente veria "erro inesperado".
Com o portal primeiro, não existe janela ruim: o pré-check já protege, e a migration só
acrescenta a garantia dura.

`app.js` é **idêntico** nos dois repos — só `config.js` (flag `celular`) e `index.html`
(marca) diferem. Mesmo arquivo nos dois destinos:

| Cliente | Repo | Branch publicada |
|---|---|---|
| Priscila | `simplixTI/vieira` | `gh-pages` (subtree de `portal/`) |
| Leo Vieira Filho | `simplixTI/leovieirafilho` | `main` (arquivos na raiz) |

A migration é única e serve os dois — mesmo banco, isolamento por `user_id`.

## Como verificar

1. Subir o mesmo CPF duas vezes num arquivo de duas linhas → segunda rejeitada no resumo.
2. Consultar avulso um CPF já consultado → mostra o resultado existente, sem nova consulta.
3. Conferir no log do worker que nenhuma chamada de API foi feita nos dois casos.
4. `insert` direto no banco com CPF repetido → índice único barra.
