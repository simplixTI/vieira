-- 001_inicial.sql — Sistema de verificação TSE em lote
-- Tabelas: batches (lotes de upload) + voter_records (um CPF por linha)
-- RLS: cada cliente (auth.users) enxerga apenas os próprios lotes/registros.
-- O worker grava com service_role (bypassa RLS); o portal nunca vê a service key.

create table if not exists public.batches (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users(id) on delete cascade,
    filename    text,
    total       int not null default 0,
    status      text not null default 'processing'
                check (status in ('processing', 'done', 'error')),
    created_at  timestamptz not null default now(),
    finished_at timestamptz
);

create table if not exists public.voter_records (
    id                 bigint generated always as identity primary key,
    batch_id           uuid not null references public.batches(id) on delete cascade,
    cpf                text not null,
    nome               text,
    nome_mae           text,
    data_nascimento    text,
    status             text not null default 'pending'
                       check (status in
                       ('pending', 'enriching', 'ready_tse', 'checking',
                        'done', 'error')),
    elegibilidade      text,
    titulo_eleitoral   text,
    zona_eleitoral     text,
    secao_eleitoral    text,
    municipio_votacao  text,
    uf                 text,
    local_votacao      text,
    endereco_votacao   text,
    bairro_votacao     text,
    biometria          text,
    obrigacao_eleitoral text,
    motivo_situacao    text,
    ano_situacao       text,
    checked_at         timestamptz,
    attempts           int not null default 0,
    raw_text           text
);

create index if not exists voter_records_batch_status_idx
    on public.voter_records (batch_id, status);
create index if not exists voter_records_batch_cpf_idx
    on public.voter_records (batch_id, cpf);

-- ---------- RLS ----------

alter table public.batches enable row level security;
alter table public.voter_records enable row level security;

-- Dono do lote: ler/criar os próprios lotes. Update/delete só via service key.
drop policy if exists "batches_select_own" on public.batches;
create policy "batches_select_own" on public.batches
    for select to authenticated
    using (auth.uid() = user_id);

drop policy if exists "batches_insert_own" on public.batches;
create policy "batches_insert_own" on public.batches
    for insert to authenticated
    with check (auth.uid() = user_id);

-- Registros: ler/inserir apenas se o lote for do usuário. Update via service key.
drop policy if exists "voter_records_select_own" on public.voter_records;
create policy "voter_records_select_own" on public.voter_records
    for select to authenticated
    using (exists (
        select 1 from public.batches b
        where b.id = voter_records.batch_id and b.user_id = auth.uid()
    ));

drop policy if exists "voter_records_insert_own" on public.voter_records;
create policy "voter_records_insert_own" on public.voter_records
    for insert to authenticated
    with check (exists (
        select 1 from public.batches b
        where b.id = voter_records.batch_id and b.user_id = auth.uid()
    ));
