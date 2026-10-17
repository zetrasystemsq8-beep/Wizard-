-- NaijaLearn social publisher: queue, attempts log, atomic claim function.
-- Run once in the Supabase SQL editor (or move into supabase/migrations/).

create table if not exists public.social_posts (
  id              uuid primary key default gen_random_uuid(),
  destination     text not null default 'facebook_page' check (destination in ('facebook_page')),
  kind            text not null default 'text' check (kind in ('text','link','photo')),
  message         text not null check (char_length(message) between 1 and 63000),
  link_url        text check (link_url  is null or link_url  ~ '^https://'),
  image_url       text check (image_url is null or image_url ~ '^https://'),
  content_hash    text not null,
  scheduled_at    timestamptz not null,
  status          text not null default 'draft'
                  check (status in ('draft','approved','publishing','published','failed','cancelled')),
  approved_at     timestamptz,
  attempts        int  not null default 0,
  max_attempts    int  not null default 5 check (max_attempts between 1 and 10),
  next_attempt_at timestamptz not null default now(),
  locked_at       timestamptz,
  published_at    timestamptz,
  external_id     text,
  permalink       text,
  last_error      text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  constraint social_posts_kind_fields check (
    (kind = 'text'  and link_url is null     and image_url is null) or
    (kind = 'link'  and link_url is not null and image_url is null) or
    (kind = 'photo' and image_url is not null and link_url is null))
);

create index if not exists social_posts_due_idx
  on public.social_posts (destination, scheduled_at) where status = 'approved';
create index if not exists social_posts_published_idx
  on public.social_posts (destination, published_at desc) where status = 'published';
create index if not exists social_posts_hash_idx on public.social_posts (destination, content_hash);

create table if not exists public.social_post_attempts (
  id             bigint generated always as identity primary key,
  post_id        uuid not null references public.social_posts(id) on delete cascade,
  attempt_no     int  not null,
  started_at     timestamptz not null,
  finished_at    timestamptz not null,
  outcome        text not null check (outcome in ('success','retryable_error','permanent_error')),
  http_status    int,
  error_code     int,
  error_subcode  int,
  error_message  text,
  trace_id       text,
  external_id    text
);
create index if not exists social_post_attempts_post_idx on public.social_post_attempts (post_id, id desc);

create or replace function public.social_touch_updated_at() returns trigger
language plpgsql as $$ begin new.updated_at = now(); return new; end $$;

drop trigger if exists social_posts_touch on public.social_posts;
create trigger social_posts_touch before update on public.social_posts
  for each row execute function public.social_touch_updated_at();

-- Atomically claims due, approved posts. Rows stuck in 'publishing' (worker died) are NOT retried
-- automatically: the post may already be live, so they become 'failed' for a human to verify.
create or replace function public.social_claim_due_posts(
  p_destination text, p_limit int, p_lock_timeout_minutes int default 15)
returns setof public.social_posts
language plpgsql as $$
begin
  update public.social_posts
     set status = 'failed', locked_at = null,
         last_error = 'Worker stopped while publishing. Check the Page for this post before requeueing to avoid a duplicate.'
   where destination = p_destination and status = 'publishing'
     and locked_at < now() - make_interval(mins => p_lock_timeout_minutes);

  return query
  with due as (
    select id from public.social_posts
     where destination = p_destination and status = 'approved'
       and scheduled_at <= now() and next_attempt_at <= now()
     order by scheduled_at asc
     limit greatest(p_limit, 0)
     for update skip locked)
  update public.social_posts p
     set status = 'publishing', locked_at = now(), attempts = p.attempts + 1
    from due where p.id = due.id
  returning p.*;
end $$;

-- Lock everything down: only the service role (used by the worker) may touch the queue.
alter table public.social_posts         enable row level security;
alter table public.social_post_attempts enable row level security;
revoke all on public.social_posts, public.social_post_attempts from anon, authenticated;
revoke all on function public.social_claim_due_posts(text, int, int) from public, anon, authenticated;
grant execute on function public.social_claim_due_posts(text, int, int) to service_role;
