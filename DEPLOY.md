# Deploying DropHound (free, permanent)

This repo is ready to deploy to **Render**'s free tier — a permanent
`https://<name>.onrender.com` site with HTTPS and no credit card required.

Everything is pre-configured in [`render.yaml`](render.yaml): it installs the
deps, starts the web app on Render's port, health-checks `/api/health`, and ships
the full catalog in [`deploy/drophound.db`](deploy/drophound.db) so the live site
is populated from the first request.

## One-time steps (the parts only you can do — ~5 minutes)

1. **Put the code on GitHub** (Render deploys from a Git repo). Either:
   - `gh auth login` then tell me, and I'll create the repo and push for you, **or**
   - create an empty repo at github.com and run the `git push` it shows you.

2. **Create a Render account** at [render.com](https://render.com) — click
   *“Sign in with GitHub”* (free, no card).

3. **Deploy the blueprint:** in Render, click **New +** → **Blueprint** →
   select your DropHound repo → **Apply**. Render reads `render.yaml` and builds it.

4. Wait ~2–3 minutes. Render gives you a live URL like
   `https://drophound.onrender.com`. Done — it stays up permanently.

## Making the live site send real alerts (optional)

By default the deployed site runs in **dry-run** (no real Telegram/Discord/email),
which is the safe default for a public site. To make it actually send, add your
keys as **Environment Variables** in the Render dashboard (Settings → Environment):

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram channel alerts |
| `DISCORD_WEBHOOK_URL` | Discord alerts |
| `RESEND_API_KEY`, `DROPHOUND_EMAIL_FROM` | Email (verify a domain at resend.com first) |
| `DROPHOUND_HOOK_SECRET` | Protect the `/hook/restock` webhook |

Never commit these — set them in the dashboard. (`.env` is git-ignored.)

## Honest caveats of the free tier

- **Sleeps when idle:** after ~15 minutes with no traffic the free instance
  spins down; the next visit takes ~30–60s to wake. (Upgrade to remove this.)
- **Ephemeral storage:** the filesystem resets on each redeploy/restart, so
  sign-ups and watchlist picks made on the live site are not permanent. For
  durable user data, attach a paid disk or a Postgres database later.
- The catalog itself always ships with the build, so it's always there.

## Alternatives

- **Hugging Face Spaces** (free, Docker) — good no-GitHub option; ask and I'll
  prepare a `Dockerfile` + Space config instead.
- **Fly.io / Railway** — also work from a local CLI, but now ask for a card.
