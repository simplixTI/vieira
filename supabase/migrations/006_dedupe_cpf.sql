-- 006_dedupe_cpf.sql — um CPF por cliente, para sempre
--
-- PROBLEMA: um CPF reenviado custa 2x (1 chamada de API paga na fase A + 1 consulta
-- TSE da cota na fase B) e cria uma segunda linha, quebrando a rastreabilidade —
-- o mesmo CPF passa a ter dois historicos e sai duplicado no CSV.
--
-- REGRA: CPF ja consultado por um cliente nunca mais e consultado para ele.
-- Escopo por user_id (global bloquearia um cliente por consulta de outro).
-- Excecao: 'error' terminal (esgotou as 5 tentativas) — o cliente nao recebeu nada,
-- entao volta a ser consultavel REAPROVEITANDO a linha existente, nunca criando outra.
--
-- ⚠️ ORDEM DE DEPLOY: portal ANTES desta migration. O trigger passa a descartar
-- inserts em silencio; o app.js antigo espera uma linha de volta na consulta avulsa
-- (.select('id').single()) e mostraria "erro inesperado" ate ser atualizado.

-- ── 1. user_id denormalizado ──────────────────────────────────────────────
-- Sem isso nao ha como ter constraint de unicidade por cliente: hoje o dono de
-- uma linha so e conhecido passando por batches.batch_id.

alter table public.voter_records
    add column if not exists user_id uuid references auth.users(id) on delete cascade;

update public.voter_records vr
   set user_id = b.user_id
  from public.batches b
 where b.id = vr.batch_id
   and vr.user_id is null;

-- ── 2. limpeza dos duplicados que ja existem ──────────────────────────────
-- O indice unico nao nasce com duplicata na tabela. Mantem a consulta MAIS
-- RECENTE de cada par (user_id, cpf) — dado eleitoral mais fresco. Nenhum CPF
-- desaparece, so a copia. Em 19/09/2026 eram 9 linhas, todas da consulta avulsa.

with ranqueadas as (
    select id,
           row_number() over (
               partition by user_id, cpf
               order by checked_at desc nulls last, id desc
           ) as posicao
      from public.voter_records
     where user_id is not null
)
delete from public.voter_records
 where id in (select id from ranqueadas where posicao > 1);

-- ── 3. a garantia fisica ──────────────────────────────────────────────────
-- Vale para o portal, o admin, scripts e duas abas simultaneas.
-- 1 CPF = 1 linha = 1 historico.

create unique index if not exists voter_records_user_cpf_uniq
    on public.voter_records (user_id, cpf);

-- ── 4. trigger de dedupe ──────────────────────────────────────────────────
-- security definer: precisa do UPDATE da linha em 'error', que RLS nao concede
-- a 'authenticated' (o portal so tem policy de select/insert).

create or replace function public.voter_records_dedupe()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    v_dono      uuid;
    v_existente record;
begin
    select user_id into v_dono from public.batches where id = new.batch_id;
    if v_dono is null then
        raise exception 'lote % nao existe ou nao tem dono', new.batch_id;
    end if;

    -- Guarda: o trigger roda ANTES do WITH CHECK do RLS e tem efeito colateral
    -- (o update do passo 'error'). Sem isto, um cliente que adivinhasse o
    -- batch_id de outro mexeria nas linhas alheias mesmo com o insert barrado.
    -- auth.uid() nulo = service_role (worker/admin/psql), que e confiavel.
    if auth.uid() is not null and v_dono <> auth.uid() then
        raise exception 'lote % pertence a outro usuario', new.batch_id;
    end if;

    new.user_id := v_dono;

    select id, status into v_existente
      from public.voter_records
     where user_id = v_dono and cpf = new.cpf
     limit 1;

    if not found then
        return new;                      -- CPF inedito: segue o insert normal
    end if;

    if v_existente.status = 'error' then
        -- Falhou e o cliente nao recebeu nada. Reaproveita a linha ORIGINAL:
        -- move pro lote novo e devolve pra fila. Continua existindo uma unica
        -- linha daquele CPF — o cliente o ve no lote novo, sem duplicar.
        update public.voter_records
           set batch_id        = new.batch_id,
               status          = 'pending',
               attempts        = 0,
               nome            = coalesce(nullif(new.nome, ''), nome),
               celular         = coalesce(nullif(new.celular, ''), celular),
               nome_mae        = coalesce(nullif(new.nome_mae, ''), nome_mae),
               data_nascimento = coalesce(nullif(new.data_nascimento, ''), data_nascimento)
         where id = v_existente.id;
    end if;

    -- CPF ja conhecido (com resultado, na fila, ou acabou de ser reenfileirado):
    -- descarta o insert. O portal avisa o cliente; aqui o custo e zero.
    return null;
end;
$$;

drop trigger if exists voter_records_dedupe_trg on public.voter_records;
create trigger voter_records_dedupe_trg
    before insert on public.voter_records
    for each row execute function public.voter_records_dedupe();

-- ── 5. fecha a garantia ───────────────────────────────────────────────────
-- Todo registro tem dono: o backfill cobriu os existentes e o trigger preenche
-- os novos. Se algum NULL sobrar aqui a migration inteira falha — bom, porque
-- NULL escaparia do indice unico (NULLs sao distintos entre si no Postgres).
-- O indice unico do passo 3 tambem ja atende a busca do pre-check do portal
-- ("estes CPFs ja existem para este cliente?"), entao nao ha indice extra.

alter table public.voter_records alter column user_id set not null;
