-- 007_contas.sql — conceito de CONTA acima do usuario
--
-- PROBLEMA: o cliente Vieira passa a ter 4 logins (priscila@, priscila01/02/03@)
-- para que cada pessoa veja so os proprios lotes. Mas dedupe de CPF e cota sao
-- por user_id (migration 006), entao 4 logins = 4 bases e 4 cotas: o mesmo CPF
-- seria consultado e cobrado 4x, exatamente o que a 006 veio impedir.
--
-- REGRA: a unidade de cobranca e de base de CPFs passa a ser a CONTA.
--   - dedupe: unico por (conta_id, cpf)  — era (user_id, cpf)
--   - cota diaria/mensal: somada por conta, nao por login
--   - RLS: INTOCADO. Cada login continua vendo so os proprios lotes (a "parede"
--     entre as pessoas do mesmo contrato e o que o cliente pediu).
--
-- A parede cria um problema: o pre-check do portal precisa saber "este CPF ja
-- existe na conta?" sem poder ler os registros dos outros logins. Resolvido pela
-- funcao cpfs_ja_consultados() do passo 4: devolve METADADO (data, quem
-- consultou) e nunca o resultado eleitoral de registro alheio.
--
-- ORDEM DE DEPLOY: esta migration ANTES do portal (inverso da 006). O portal
-- novo chama cpfs_ja_consultados(), que so existe depois daqui. O portal ANTIGO
-- continua funcionando sem ela: o pre-check dele so enxerga os proprios CPFs
-- (menos protecao, nenhum erro) e o insert descartado pelo trigger ja cai no
-- tratamento de "0 linhas" que a 006 deixou pronto.

-- ── 1. contas e seus membros ──────────────────────────────────────────────

create table if not exists public.contas (
    id         uuid primary key default gen_random_uuid(),
    nome       text not null unique,
    created_at timestamptz not null default now()
);

create table if not exists public.contas_usuarios (
    user_id    uuid primary key references auth.users(id) on delete cascade,
    conta_id   uuid not null references public.contas(id) on delete restrict,
    rotulo     text,          -- nome curto exibido no portal ("priscila01")
    created_at timestamptz not null default now()
);
create index if not exists contas_usuarios_conta_idx
    on public.contas_usuarios (conta_id);

alter table public.contas          enable row level security;
alter table public.contas_usuarios enable row level security;
-- Sem policy de escrita: so service_role administra. Leitura do proprio vinculo
-- e util pro portal saber em que conta esta.
drop policy if exists "contas_usuarios_select_own" on public.contas_usuarios;
create policy "contas_usuarios_select_own" on public.contas_usuarios
    for select to authenticated using (user_id = auth.uid());

-- Contas existentes. Cada cliente de hoje vira uma conta com 1 membro.
insert into public.contas (nome) values ('Vieira'), ('Leo Vieira Filho')
on conflict (nome) do nothing;

insert into public.contas_usuarios (user_id, conta_id, rotulo)
select u.id, c.id, 'priscila'
  from auth.users u, public.contas c
 where u.email = 'priscila@vieira.com.br' and c.nome = 'Vieira'
on conflict (user_id) do nothing;

insert into public.contas_usuarios (user_id, conta_id, rotulo)
select u.id, c.id, 'glaucio'
  from auth.users u, public.contas c
 where u.email = 'glaucio@leovieirafilho.com.br' and c.nome = 'Leo Vieira Filho'
on conflict (user_id) do nothing;

-- Qualquer usuario sem conta vira uma conta propria (comportamento identico ao
-- de antes desta migration). Evita que um usuario esquecido pare de funcionar.
insert into public.contas (nome)
select 'auto: ' || u.email
  from auth.users u
 where not exists (select 1 from public.contas_usuarios cu where cu.user_id = u.id)
on conflict (nome) do nothing;

insert into public.contas_usuarios (user_id, conta_id, rotulo)
select u.id, c.id, split_part(u.email, '@', 1)
  from auth.users u
  join public.contas c on c.nome = 'auto: ' || u.email
 where not exists (select 1 from public.contas_usuarios cu where cu.user_id = u.id)
on conflict (user_id) do nothing;

-- ── 2. voter_records passa a ser chaveado por conta ───────────────────────

alter table public.voter_records
    add column if not exists conta_id uuid references public.contas(id);

update public.voter_records vr
   set conta_id = cu.conta_id
  from public.contas_usuarios cu
 where cu.user_id = vr.user_id
   and vr.conta_id is null;

-- Duplicatas que so aparecem ao agrupar por conta (dois logins da mesma conta
-- com o mesmo CPF). Hoje nao ha, mas a ordem importa: o indice unico nao nasce
-- com duplicata. Mantem a consulta mais recente, como fez a 006.
with ranqueadas as (
    select id,
           row_number() over (
               partition by conta_id, cpf
               order by checked_at desc nulls last, id desc
           ) as posicao
      from public.voter_records
     where conta_id is not null
)
delete from public.voter_records
 where id in (select id from ranqueadas where posicao > 1);

drop index if exists public.voter_records_user_cpf_uniq;
create unique index if not exists voter_records_conta_cpf_uniq
    on public.voter_records (conta_id, cpf);

alter table public.voter_records alter column conta_id set not null;

-- ── 3. trigger de dedupe, agora por conta ─────────────────────────────────

create or replace function public.voter_records_dedupe()
returns trigger
language plpgsql
security definer
set search_path = public
as $fn$
declare
    v_dono      uuid;
    v_conta     uuid;
    v_existente record;
begin
    select user_id into v_dono from public.batches where id = new.batch_id;
    if v_dono is null then
        raise exception 'lote % nao existe ou nao tem dono', new.batch_id;
    end if;

    -- O trigger roda ANTES do WITH CHECK do RLS e tem efeito colateral (o update
    -- do passo 'error'). Sem esta guarda, um cliente que adivinhasse o batch_id
    -- de outro mexeria em linha alheia mesmo com o insert barrado.
    -- auth.uid() nulo = service_role (worker/admin/psql), confiavel.
    if auth.uid() is not null and v_dono <> auth.uid() then
        raise exception 'lote % pertence a outro usuario', new.batch_id;
    end if;

    select conta_id into v_conta from public.contas_usuarios where user_id = v_dono;
    if v_conta is null then
        raise exception 'usuario % nao esta vinculado a nenhuma conta (ver contas_usuarios)', v_dono;
    end if;

    new.user_id  := v_dono;
    new.conta_id := v_conta;

    select id, status, user_id into v_existente
      from public.voter_records
     where conta_id = v_conta and cpf = new.cpf
     limit 1;

    if not found then
        return new;                      -- CPF inedito na conta: insere normal
    end if;

    if v_existente.status = 'error' then
        if v_existente.user_id = v_dono then
            -- Mesma pessoa reenviando o que falhou: reaproveita a linha e traz
            -- pro lote novo. Continua 1 linha por CPF (historico preservado).
            update public.voter_records
               set batch_id        = new.batch_id,
                   status          = 'pending',
                   attempts        = 0,
                   nome            = coalesce(nullif(new.nome, ''), nome),
                   celular         = coalesce(nullif(new.celular, ''), celular),
                   nome_mae        = coalesce(nullif(new.nome_mae, ''), nome_mae),
                   data_nascimento = coalesce(nullif(new.data_nascimento, ''), data_nascimento)
             where id = v_existente.id;
        else
            -- A linha com erro e de OUTRO login da conta. Reenfileira no lugar,
            -- sem mover: mover arrancaria uma linha do lote de outra pessoa e
            -- mudaria o total do lote dela pelas costas. A "parede" entre os
            -- logins vale tambem pra nao surpreender ninguem.
            update public.voter_records
               set status = 'pending', attempts = 0
             where id = v_existente.id;
        end if;
    end if;

    return null;   -- CPF ja conhecido na conta: nunca cria linha nova
end;
$fn$;

drop trigger if exists voter_records_dedupe_trg on public.voter_records;
create trigger voter_records_dedupe_trg
    before insert on public.voter_records
    for each row execute function public.voter_records_dedupe();

-- ── 4. pre-check do portal, atravessando a parede sem vazar resultado ─────
-- Devolve, para os CPFs perguntados, o que existe NA CONTA do usuario logado.
-- Para registro de outro login devolve so metadado: nunca elegibilidade, zona,
-- secao, municipio ou qualquer dado eleitoral. O resultado proprio o portal le
-- direto da tabela, onde o RLS ja o autoriza.

create or replace function public.cpfs_ja_consultados(p_cpfs text[])
returns table (
    cpf        text,
    status     text,
    checked_at timestamptz,
    e_meu      boolean,
    quem       text,
    batch_id   uuid
)
language plpgsql
security definer
set search_path = public
as $fn$
declare
    v_conta uuid;
begin
    if auth.uid() is null then
        raise exception 'sem sessao';
    end if;
    select cu.conta_id into v_conta
      from public.contas_usuarios cu where cu.user_id = auth.uid();
    if v_conta is null then
        return;                         -- usuario sem conta: nada a informar
    end if;

    return query
        select v.cpf,
               v.status,
               v.checked_at,
               (v.user_id = auth.uid())                              as e_meu,
               coalesce(cu.rotulo, 'outro login')                    as quem,
               case when v.user_id = auth.uid() then v.batch_id end  as batch_id
          from public.voter_records v
          left join public.contas_usuarios cu on cu.user_id = v.user_id
         where v.conta_id = v_conta
           and v.cpf = any(p_cpfs);
end;
$fn$;

revoke all on function public.cpfs_ja_consultados(text[]) from public;
grant execute on function public.cpfs_ja_consultados(text[]) to authenticated;

-- ── 5. cota e avisos passam a ser por conta ───────────────────────────────

create table if not exists public.limites_por_conta (
    conta_id      uuid primary key references public.contas(id) on delete cascade,
    limite_mensal int,
    limite_diario int,
    nota          text,
    updated_at    timestamptz not null default now()
);
alter table public.limites_por_conta enable row level security;

-- Migra overrides que existiam por usuario (a tabela antiga fica como historico).
insert into public.limites_por_conta (conta_id, limite_mensal, limite_diario, nota)
select cu.conta_id, l.limite_mensal, l.limite_diario,
       coalesce(l.nota, '') || ' (migrado de limites_por_cliente)'
  from public.limites_por_cliente l
  join public.contas_usuarios cu on cu.user_id = l.user_id
on conflict (conta_id) do nothing;

-- avisos_limite: dedup do aviso mensal ao ADM passa a ser por conta.
alter table public.avisos_limite
    add column if not exists conta_id uuid references public.contas(id);

update public.avisos_limite a
   set conta_id = cu.conta_id
  from public.contas_usuarios cu
 where cu.user_id = a.user_id
   and a.conta_id is null;

-- Aviso cujo usuario nao existe mais nao tem conta resolvivel: nao serve mais.
delete from public.avisos_limite where conta_id is null;

-- Se dois logins da mesma conta ja foram avisados no mesmo mes, sobra um.
delete from public.avisos_limite a
 using public.avisos_limite b
 where a.conta_id = b.conta_id and a.mes = b.mes and a.user_id > b.user_id;

alter table public.avisos_limite drop constraint if exists avisos_limite_pkey;
alter table public.avisos_limite alter column conta_id set not null;
alter table public.avisos_limite add primary key (conta_id, mes);
