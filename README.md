# Smart Daily Planner

Daily task planner with accounts (email + password, email OTP on sign-up),
per-user tasks, and daily push notifications for tasks due today or overdue.

## Run it

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # then fill in your Gmail + App Password
.\.venv\Scripts\python app.py
```

Open http://127.0.0.1:5000

Without SMTP settings in `.env`, the app runs in **dev mode**: OTP codes are
printed in the terminal instead of being emailed.

## How it works

- **Sign up:** name + email + password -> 6-digit code emailed -> account created.
  Each user also gets a username like `adityaraj4821` and can log in with it or their email.
- **Log in:** email (or username) + password. Sessions last 30 days.
- **Tasks** are stored per user in `data/planner.db` (SQLite). Tasks saved by the
  old browser-only version are imported into the first account that logs in on that browser.
- **Notifications:** click "Enable notifications". Once a day, after `NOTIFY_HOUR`
  (default 8 AM in the user's timezone), each subscribed device gets one notification
  listing pending tasks that are due today or overdue.

## Deploy on Render (free)

Render's free plan blocks SMTP and wipes local files on every restart, so production uses
**Brevo** (email over HTTPS) and **Neon** (free Postgres).

1. **Neon** (neon.tech): create a project, copy the connection string (`postgresql://...`).
2. **Brevo** (brevo.com): verify your sender email (Senders, Domains & Dedicated IPs > Senders),
   then create an API key (SMTP & API > API keys).
3. Push this folder to a GitHub repo.
4. **Render**: New > Blueprint > choose the repo. It reads `render.yaml`. Fill in
   `DATABASE_URL`, `BREVO_API_KEY` and `MAIL_FROM` (e.g. `Smart Daily Planner <you@gmail.com>`).
5. Free services sleep after 15 minutes without traffic, which pauses the daily reminders.
   To keep it awake, add a free monitor (e.g. cron-job.org or UptimeRobot) that opens
   `https://<your-app>.onrender.com/healthz` every 10 minutes.

## Notifications on a phone

Push notifications need the site to be served over **HTTPS** (only `localhost` is exempt),
so to test on a phone you need to deploy the app or tunnel to it (for example with
`ngrok http 5000`), and set `COOKIE_SECURE=1`.

- **Android (Chrome):** open the HTTPS URL, log in, tap "Enable notifications".
- **iPhone (iOS 16.4+):** open in Safari -> Share -> *Add to Home Screen*, open the app
  from the home screen, then tap "Enable notifications".

`data/` holds the database, session secret and VAPID push keys. Back it up and don't commit it.
