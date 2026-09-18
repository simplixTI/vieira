-- 005_celular.sql — adiciona coluna celular em voter_records
--
-- Usado pelo tenant do Leo Vieira Filho (marketing/match de base de
-- contatos com CPF do TSE). Priscila e outros tenants ignoram (fica NULL).
-- RLS existente ja isola por batch_id/user_id — nenhum cliente ve celular
-- de outro cliente. Coluna free-form pra armazenar formatos variados
-- (com/sem DDD, com/sem 9º dígito, com/sem formatação).

alter table public.voter_records add column if not exists celular text;
