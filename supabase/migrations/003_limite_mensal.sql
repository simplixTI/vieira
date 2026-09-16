-- 003_limite_mensal.sql — controle de aviso de limite mensal por cliente
-- Registra que o ADM ja foi avisado quando um cliente atinge a cota mensal,
-- garantindo uma unica notificacao por cliente por mes.

create table if not exists public.avisos_limite (
    user_id        uuid not null references auth.users(id) on delete cascade,
    mes            date not null,               -- primeiro dia do mes (ex.: 2026-09-01)
    limite         int  not null,               -- cota que estava configurada no aviso
    notificado_em  timestamptz not null default now(),
    primary key (user_id, mes)
);

-- Acesso restrito: so o worker (service_role) escreve/ler. Sem policy p/ authenticated.
alter table public.avisos_limite enable row level security;
