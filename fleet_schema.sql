-- Additive candidate for BOT project vunizdpnptgitfzgekvk only. Not applied.
-- Existing Bagging tables, grants, triggers and records are untouched.
begin;
create table public.sgsb_fleet_reports (
 id uuid primary key, owner_id bigint not null, version integer not null check(version>0),
 data jsonb not null check(jsonb_typeof(data)='object'),
 updated_at timestamptz not null default now()
);
create table public.sgsb_fleet_events (
 event_id text primary key, request_hash text not null, result jsonb not null,
 created_at timestamptz not null default now()
);
alter table public.sgsb_fleet_reports enable row level security;
alter table public.sgsb_fleet_events enable row level security;
revoke all on public.sgsb_fleet_reports, public.sgsb_fleet_events from public,anon,authenticated;
grant select,insert,update on public.sgsb_fleet_reports to service_role;
grant select,insert on public.sgsb_fleet_events to service_role;
create index sgsb_fleet_reports_outlet_date on public.sgsb_fleet_reports
 ((data->>'outletCode'),(data->>'reportDate'));
create function public.sgsb_fleet_cas(p_event text,p_id uuid,p_owner bigint,p_expected integer,p_data jsonb)
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare previous public.sgsb_fleet_reports; memo public.sgsb_fleet_events;
 fingerprint text; answer jsonb;
begin
 if length(p_event)>180 or length(p_event)<4 or p_expected<0 or
    jsonb_typeof(p_data)<>'object' or octet_length(p_data::text)>50000 then
  raise exception 'FLEET_INVALID_INPUT';
 end if;
 fingerprint:=md5(jsonb_build_array(p_id,p_owner,p_expected,p_data)::text);
 perform pg_advisory_xact_lock(hashtextextended('fleet-event:'||p_event,0));
 select * into memo from public.sgsb_fleet_events where event_id=p_event;
 if found then
  if memo.request_hash<>fingerprint then raise exception 'FLEET_EVENT_CONFLICT'; end if;
  return memo.result;
 end if;
 perform pg_advisory_xact_lock(hashtextextended('fleet-record:'||p_id::text,0));
 select * into previous from public.sgsb_fleet_reports where id=p_id for update;
 if found then
  if previous.owner_id<>p_owner then raise exception 'FLEET_OWNER_FORBIDDEN'; end if;
  if previous.version<>p_expected then raise exception 'FLEET_VERSION_CONFLICT'; end if;
  if previous.data->>'state'='SENT' then raise exception 'FLEET_IMMUTABLE'; end if;
  if previous.data->>'state' in ('SENDING','UNKNOWN') and p_data->>'state' not in ('SENT','UNKNOWN')
   then raise exception 'FLEET_DELIVERY_REVIEW_REQUIRED'; end if;
 else
  if p_expected<>0 or p_data->>'state'<>'DRAFT' then raise exception 'FLEET_VERSION_CONFLICT'; end if;
 end if;
 if p_data->>'id' is distinct from p_id::text or (p_data->>'ownerId')::bigint is distinct from p_owner or
    coalesce(p_data->>'state','') not in ('DRAFT','SENDING','SENT','UNKNOWN') then
  raise exception 'FLEET_INVALID_INPUT';
 end if;
 insert into public.sgsb_fleet_reports(id,owner_id,version,data)
 values(p_id,p_owner,p_expected+1,p_data)
 on conflict(id) do update set data=excluded.data,version=excluded.version,updated_at=now();
 answer:=jsonb_build_object('version',p_expected+1,'data',p_data);
 insert into public.sgsb_fleet_events(event_id,request_hash,result) values(p_event,fingerprint,answer);
 return answer;
end $$;
revoke all on function public.sgsb_fleet_cas(text,uuid,bigint,integer,jsonb) from public,anon,authenticated;
grant execute on function public.sgsb_fleet_cas(text,uuid,bigint,integer,jsonb) to service_role;
create function public.sgsb_fleet_read(p_from date,p_to date,p_codes text[])
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare result jsonb;
begin
 if p_from is null or p_to is null or p_from>p_to or p_to-p_from>365 or
    cardinality(p_codes) is null or cardinality(p_codes)>12 or
    exists(select 1 from unnest(p_codes) c where c!~'^SBH[0-9]+$') then raise exception 'FLEET_INVALID_INPUT'; end if;
 select coalesce(jsonb_agg(jsonb_build_object('data',r.data) order by r.id),'[]'::jsonb) into result
 from (select id,data from public.sgsb_fleet_reports where data->>'state'='SENT'
 and data->>'outletCode'=any(p_codes) and data->>'reportDate'>=p_from::text and data->>'reportDate'<=p_to::text
 order by id limit 2001) r;
 if jsonb_array_length(result)>2000 then raise exception 'FLEET_RANGE_TOO_LARGE'; end if;
 return result;
end $$;
revoke all on function public.sgsb_fleet_read(date,date,text[]) from public,anon,authenticated;
grant execute on function public.sgsb_fleet_read(date,date,text[]) to service_role;
commit;
