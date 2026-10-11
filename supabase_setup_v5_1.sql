begin;
create table if not exists public.sgsb_outlets (code text primary key, active boolean not null default true, updated_at timestamptz not null default now());
create table if not exists public.sgsb_records (
 rid text primary key,
 data jsonb not null,
 outlet text generated always as (data->>'outlet') stored,
 seal text generated always as (upper(data->>'seal')) stored,
 bagging_date text generated always as (data->>'bagging_date') stored,
 status text generated always as (data->>'status') stored,
 owner_id text generated always as (data->>'owner_id') stored,
 updated_at timestamptz not null default now(),
 check (data->>'rid'=rid), check (data ? 'photo' and data ? 'video'),
 check (data->>'bagging_date' ~ '^\d{4}-\d{2}-\d{2}$')
);
create unique index if not exists sgsb_records_one_active_seal on public.sgsb_records(outlet,seal,bagging_date) where status <> 'CANCELLED';
create index if not exists sgsb_records_date_outlet on public.sgsb_records(bagging_date,outlet);
create index if not exists sgsb_records_owner_status on public.sgsb_records(owner_id,status);
create table if not exists public.sgsb_sessions (key text primary key, data jsonb not null, updated_at timestamptz not null default now());
create table if not exists public.sgsb_updates (update_id bigint primary key, done boolean not null default false, received_at timestamptz not null default now());
create table if not exists public.sgsb_processing_lock (id integer primary key check(id=1), token text, expires_at timestamptz);
insert into public.sgsb_processing_lock(id) values(1) on conflict do nothing;
create table if not exists public.sgsb_migration_conflicts (rid text primary key, data jsonb not null, reason text not null, imported_at timestamptz default now());
alter table public.sgsb_outlets enable row level security;
alter table public.sgsb_records enable row level security;
alter table public.sgsb_sessions enable row level security;
alter table public.sgsb_updates enable row level security;
alter table public.sgsb_processing_lock enable row level security;
alter table public.sgsb_migration_conflicts enable row level security;
revoke all on public.sgsb_outlets,public.sgsb_records,public.sgsb_sessions,public.sgsb_updates,public.sgsb_processing_lock,public.sgsb_migration_conflicts from anon,authenticated;
grant all on public.sgsb_outlets,public.sgsb_records,public.sgsb_sessions,public.sgsb_updates,public.sgsb_processing_lock,public.sgsb_migration_conflicts to service_role;
create or replace function public.sgsb_claim_update(p_id bigint,p_token text) returns text language plpgsql set search_path=public as $$
begin
 perform 1 from sgsb_processing_lock where id=1 for update;
 if exists(select 1 from sgsb_updates where update_id=p_id and done) then return 'done'; end if;
 if exists(select 1 from sgsb_processing_lock where id=1 and expires_at>now()) then return 'busy'; end if;
 insert into sgsb_updates(update_id) values(p_id) on conflict do nothing;
 update sgsb_processing_lock set token=p_token,expires_at=now()+interval '180 seconds' where id=1;
 return 'claimed';
end $$;
create or replace function public.sgsb_finish_update(p_id bigint,p_token text,p_done boolean) returns void language plpgsql set search_path=public as $$
begin
 perform 1 from sgsb_processing_lock where id=1 and token=p_token for update;
 if not found then raise exception 'lease lost'; end if;
 update sgsb_updates set done=p_done where update_id=p_id;
 update sgsb_processing_lock set token=null,expires_at=null where id=1;
end $$;
create or replace function public.sgsb_set_outlets(p_codes text[]) returns void language plpgsql set search_path=public as $$
begin
 update sgsb_outlets set active=false,updated_at=now() where not(code=any(p_codes));
 insert into sgsb_outlets(code,active) select distinct unnest(p_codes),true on conflict(code) do update set active=true,updated_at=now();
end $$;
revoke all on function public.sgsb_claim_update(bigint,text),public.sgsb_finish_update(bigint,text,boolean),public.sgsb_set_outlets(text[]) from public,anon,authenticated;
grant execute on function public.sgsb_claim_update(bigint,text),public.sgsb_finish_update(bigint,text,boolean),public.sgsb_set_outlets(text[]) to service_role;
commit;
