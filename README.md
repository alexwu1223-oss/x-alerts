# x-alerts

Posts new tweets from chosen X accounts into a 企业微信 group, using the group bot (Tencent's official webhook). It runs free on GitHub Actions about every 5 minutes. Nothing runs on your computer.

## Settings

All settings are in **Settings → Secrets and variables → Actions**.

| Secret | What it is |
|---|---|
| `TW_AUTH_TOKEN`, `TW_CT0` | Cookies of the spare X account |
| `WATCH_ACCOUNTS` | Usernames to follow, comma-separated |
| `WECOM_WEBHOOK` | The 企业微信 group bot's webhook URL |

Optional **variables**:

| Variable | Default | What it does |
|---|---|---|
| `INCLUDE_RETWEETS` | `false` | Also push reposts |
| `INCLUDE_REPLIES` | `false` | Also push replies |
| `INCLUDE_QUOTES` | `true` | Push quote posts |
| `KEYWORDS` | (none) | Only push posts that contain one of these words |
| `EXCLUDE_KEYWORDS` | (none) | Skip posts that contain any of these words |
| `WECOM_MENTION_ALL` | `false` | @everyone on each post |
| `SEND_IMAGES` | `true` | Also send the post's images |
| `TIMEZONE` | `Asia/Shanghai` | Time zone for the times shown in messages |
| `HEARTBEAT_HOUR` | `-1` | Hour (0–23) for a daily "still running" message; `-1` turns it off |

## Common tasks

- **"cookie 已失效" alert in the group:**
  1. Log in to the spare X account in a private browser window.
  2. Press F12 → Application → Cookies → x.com.
  3. Copy `auth_token` and `ct0`.
  4. Click **Update** on the `TW_AUTH_TOKEN` and `TW_CT0` secrets and paste in the new values.
- **Follow different accounts:** update the `WATCH_ACCOUNTS` secret.
- **Test it now:** Actions → x-alerts → **Run workflow**.
- **Pause:** Actions → x-alerts → ⋯ → **Disable workflow**.

## How it behaves

- **Timing:** GitHub runs the 5-minute schedule on a best-effort basis, so posts usually arrive 5–15 minutes after they're published.
- **What gets pushed:**
  - The first run never pushes old posts.
  - Posts older than 6 hours are skipped.
  - At most 5 posts per account are pushed per run.
- **Alerts:** if the cookies expire, X stops returning data, or a username is wrong, a warning goes to the group. The same warning is sent at most once every 6 hours.
- **Privacy:** the repo is public so Actions is free without limits. Secrets stay hidden, and the logs hide usernames and links.
- **Staying active:** GitHub pauses schedules in repos with no activity for 60 days. The workflow commits a tiny `.keepalive` file about every 40 days to prevent that.
- **Reading X:** it uses [twscrape](https://github.com/vladkens/twscrape), with its usage statistics turned off. Use a spare X account only, because this method is against X's terms.
