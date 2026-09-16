-- 002_retry_erros.sql — suporte a reprocessamento automatico de registros em ERRO
-- Adiciona updated_at (atualizado por trigger em qualquer UPDATE) para permitir
-- cooldown entre tentativas de reprocessamento.

alter table public.voter_records
    add column if not exists updated_at timestamptz not null default now();

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists voter_records_touch on public.voter_records;
create trigger voter_records_touch
    before update on public.voter_records
    for each row execute function public.touch_updated_at();
