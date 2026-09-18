-- 004_limites_por_cliente.sql — override de limites diário/mensal por usuário
--
-- Substitui os defaults globais (LIMITE_DIARIO_POR_CLIENTE, LIMITE_MENSAL_POR_CLIENTE)
-- para clientes específicos. Ex: cliente em trial com limite reduzido, cliente
-- premium sem limite (colunas nulas ou 0 = "sem override, usa o default global").
--
-- Escrita/leitura APENAS via service_role (worker). O portal não precisa dessa tabela.

create table if not exists public.limites_por_cliente (
    user_id       uuid primary key references auth.users(id) on delete cascade,
    limite_mensal int,
    limite_diario int,
    nota          text,
    updated_at    timestamptz not null default now()
);

alter table public.limites_por_cliente enable row level security;
-- sem policies = ninguém autenticado acessa; service_role bypassa RLS.
