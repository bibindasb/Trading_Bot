# Moving the bot to a test server (paper trading)

This guide moves the bot from your laptop to an always-on Linux server and starts the 4-week paper trial there.
Paper trading uses pretend money on real Binance prices: nothing real is bought or sold, and no Binance keys
are needed.

In the commands, replace `you@your-server` with your server's login, for example `ubuntu@10.0.4.21`.
Run each command in a terminal and wait for it to finish before the next one.

---

## What you need

- **A Linux server that stays on**, ideally Ubuntu 22.04 or 24.04, with at least 2 GB RAM and 10 GB free disk.
- **SSH access** to it from your laptop.
- **Internet access from the server** to:
  - Binance (`api.binance.com`). Binance blocks some countries and cloud regions, the US for example.
    Pick a server region where Binance works.
  - Your AI provider (`api.fastrouter.ai` and/or `openrouter.ai`)
  - The news feeds (coindesk.com, cointelegraph.com, decrypt.co)
  - Telegram (`api.telegram.org`), if you use alerts
  - Docker Hub and PyPI (or your company's package mirror), to build the bot

---

## Step 1. Get the code and your `.env` onto the server

On the **server**, clone the project from GitHub:

```bash
git clone https://github.com/bibindasb/Trading_Bot.git
```

Your `.env` holds your keys, so it is not on GitHub, on purpose. First, on your **laptop**, open `.env` and
empty `EXCHANGE_API_KEY` and `EXCHANGE_API_SECRET`. Paper trading never uses them, and this way your real
Binance key is never copied to the server. Then copy it over, also from your **laptop**:

```bash
scp ~/Documents/TR-automation/.env you@your-server:~/Trading_Bot/.env
```

The server starts with its own fresh paper account at $100. The laptop's paper account stays on the laptop.

---

## Step 2. Install Docker on the server

Log in to the server:

```bash
ssh you@your-server
```

Then, **on the server**:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
```

```bash
sudo systemctl enable --now docker
```

Check the Compose version. It must be **2.24 or newer**:

```bash
docker compose version
```

If it is older, install Docker from Docker's own instructions instead:
https://docs.docker.com/engine/install/ubuntu/

Docker now starts by itself when the server reboots, and so does the bot.

---

## Step 3. Check the `.env` file on the server

Still on the server:

```bash
cd ~/Trading_Bot && chmod 600 .env && nano .env
```

Make sure that:

| Line | For paper trading |
|---|---|
| `FASTROUTER_API_KEY`, `FASTROUTER_FAST_MODEL`, `FASTROUTER_REASONING_MODEL` | Filled in, the same as on your laptop |
| `OPENROUTER_...` | Optional. A second provider, used only when the first fails |
| `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET` | **Empty.** Paper trading never uses them, so do not keep a real key on the server |
| `LIVE_TRADING` | `false` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | **Recommended.** On a server nobody watches, Telegram is how you hear about trades and problems |
| `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_TRUSTED_HOST` | See below |

**The `PIP_` lines** point the build at your company's package mirror. Check whether the server can reach
public PyPI:

```bash
curl -sI https://pypi.org/simple/ | head -1
```

- It prints something like `HTTP/2 200`: **delete the three `PIP_` lines**. Public PyPI works, and the company
  mirror may not be reachable from outside the office network.
- It prints nothing or an error: **keep them**, as on your laptop.

**Setting up Telegram (optional):** in Telegram, message **@BotFather** and send `/newbot` to get the token.
Then send your new bot any message, and open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser.
The number after `"chat":{"id":` is your chat ID.

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

---

## Step 4. Build the bot

```bash
sudo docker compose build
```

The first build takes about 5 minutes and stays quiet while it installs packages. That is normal.

---

## Step 5. Validate the server and the agents

```bash
sudo docker compose run --rm bot python validate.py
```

This checks, in plain words with PASS / WARN / FAIL:

1. **Connections:** Binance prices, the news feeds and Telegram.
2. **The four AI agents**, on test situations with obvious right answers.
3. **The money rules**, against Binance's real order rules.
4. **One full check** on live prices.

It runs on a throwaway paper account and makes about 40 small AI calls.

**You want to see `Ready for paper trading.` at the end.** A WARN for Telegram just means alerts are not set up.
Any FAIL: see "When something goes wrong" below, fix it, and run the check again.

---

## Step 6. Start paper trading

```bash
sudo docker compose up -d
```

After a minute, check that it is running:

```bash
sudo docker compose ps
```

The `STATUS` column should say `Up`. The bot checks the market once at start, then every 15 minutes
(at :00, :15, :30 and :45 UTC), day and night.

**Write down today's date: this is day 1 of the 4-week trial.**

---

## Step 7. Open the dashboard from your laptop

The dashboard only listens inside the server, so nobody else can reach it. Open a tunnel from your **laptop**,
and leave this terminal open:

```bash
ssh -N -L 8081:localhost:8080 you@your-server
```

Then open **http://127.0.0.1:8081** in your laptop's browser. It shows the account value, what each agent
thought on every check, every trade, and your money rules.

**Never open port 8080 in the server's firewall or cloud security group.** The tunnel is the safe way in.

---

## Step 8. Stop the bot on your laptop

Once the server bot runs, stop the laptop one, so you do not have two bots and two sets of alerts.
On your **laptop**:

```bash
cd ~/Documents/TR-automation && sudo docker compose down
```

---

## Everyday commands (on the server)

Run these from the project folder, so start with `cd ~/Trading_Bot`.

| To do this | Run |
|---|---|
| See if it is running | `sudo docker compose ps` |
| See the last log lines | `sudo docker compose logs --tail 50 bot` |
| **Stop all trading now (kill switch)** | `sudo docker compose exec bot sh -c 'echo manual > state/KILL'` |
| Resume after the kill switch | `sudo docker compose exec bot rm state/KILL` |
| Apply a change to `.env` | `sudo docker compose up -d --force-recreate` |
| Apply a change to `config/settings.yaml` | `sudo docker compose restart` |
| Stop the bot completely | `sudo docker compose stop` |
| Start it again | `sudo docker compose up -d` |
| Check everything again | `sudo docker compose run --rm bot python validate.py` |
| **Start the paper account over at $100** (deletes its history) | `sudo docker compose down -v && sudo docker compose up -d` |

Changing `starting_cash` in the settings only takes effect after starting the paper account over.

### Updating the code later

Push your changes to GitHub from the laptop, then on the server:

```bash
cd ~/Trading_Bot && git pull && sudo docker compose up -d --build
```

`git pull` never touches the server's `.env`. It does update `config/settings.yaml`, so make settings changes
on the laptop and push them, rather than editing them on the server.

---

## The 4-week trial

**Every day (2 minutes on the dashboard):**
- Bot status says **Running**, and "Last check" is under 20 minutes ago.
- Read any orange alerts.
- Look at the trades, if there were any. Many "Waited" checks are normal: the agents only buy when the chart
  and the news agree.

**Every week, write down:**
- The account value, and its change since the start
- The number of trades, and how many ended in profit
- Any "Blocked by the safety rules", errors or warnings

**Keep the setup the same for all 4 weeks.** If you change the AI model or the money rules, start the paper
account over and count the 4 weeks again. Otherwise the result mixes two setups.

**Before ever going live** (from BUILD.md):
1. Positive results after fees, both in the backtest and over the 4 weeks of paper trading.
2. No safety-rule bypasses and no execution bugs in those 4 weeks.
3. The kill switch and alerts tested by hand: turn the kill switch on, see the dashboard and Telegram say
   "Paused", then turn it off again.
4. Then go live with a very small amount, a trading-only Binance key limited to the server's IP address, and
   `live_trading: true` plus `LIVE_TRADING=true`. Grow the amount slowly, and only if live results match paper.

---

## When something goes wrong

| What you see | What it means and what to do |
|---|---|
| `validate.py`: Binance prices FAIL with "restricted location" or 451 | Binance blocks the server's country or region. Use a server in another region. |
| `validate.py`: AI checks FAIL "no valid answer", with a log line saying 401 | The AI key in `.env` is wrong. Fix it and run the check again. |
| Log says "Model is temporarily unavailable" | The AI provider's model is down. Wait, or pick another model in `.env` and run `sudo docker compose up -d --force-recreate`. |
| The build stops at `pip install`, "No matching distribution" or "Network is unreachable" | The server cannot reach PyPI. Add the `PIP_` mirror lines to `.env` (Step 3). |
| The build fails pulling `python:3.12-slim` | Docker Hub is blocked. Ask your IT team for a Docker registry mirror. |
| Compose complains about `env_file` or `required` | Docker Compose is too old. Install 2.24 or newer (Step 2). |
| A warning about "Bake" and "buildx isn't installed" | Harmless. Docker uses its built-in builder instead. |
| The dashboard does not open | Check that the tunnel terminal (Step 7) is still open, and that `sudo docker compose ps` shows `Up`. |
| "Last check" is more than 20 minutes ago | Something stopped. Read `sudo docker compose logs --tail 50 bot`. |
| Status says **Paused** | Read the reason on the dashboard. A daily loss pause lifts by itself at midnight UTC. A drawdown stop or your kill switch needs `sudo docker compose exec bot rm state/KILL`, after you have looked at what happened. |
