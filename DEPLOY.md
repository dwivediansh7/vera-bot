# Deploying Vera (Render free web service + keep-alive)

The bot keeps all state in memory, so it must run as **one** instance with **one** worker and must not be restarted during judging.

## 1. Push to GitHub
```bash
cd magicpin
git status            # .env must NOT be listed (it is in .gitignore)
# Create an EMPTY repo on github.com (no README), e.g. vera-bot, then:
git remote add origin https://github.com/<your-username>/vera-bot.git
git branch -M main
git push -u origin main
```
Check on GitHub that `.env` and `llm_cache.json` are **not** in the repo.

## 2. Create the Render web service (free)
1. Go to https://dashboard.render.com, then **New → Blueprint**, and connect the `vera-bot` repo. Render reads `render.yaml`: a Docker runtime, the **free** plan, health check `/v1/healthz`, auto-deploy off.
   - Or use **New → Web Service**, choose the repo, set **Runtime: Docker**, **Instance type: Free**, and **Health Check Path: `/v1/healthz`**.
2. Under **Environment**, set:

   | Key | Value |
   |---|---|
   | `LLM_API_KEY` | your Gemini key (paste it only here, never in the repo) |
   | `LLM_PROVIDER` | `gemini` |
   | `LLM_MODEL` | `gemini-3.5-flash-lite` |
   | `LLM_OUTBOUND` | `off` |
   | `LLM_RPM` | `10` |
   | `CONTACT_EMAIL` | `dwivediansh0@gmail.com` |
   | `TEAM_NAME` | `ANSH DWIVEDI` |
   | `TEAM_MEMBERS` | `Ansh Dwivedi` |

   Render provides `PORT`; the container listens on it automatically.
3. Click **Create**. The first build takes about 3–5 minutes. Your base URL is `https://<service-name>.onrender.com`.
4. Leave **Auto-Deploy off**. A redeploy restarts the process and wipes state.

## 3. Keep it awake (the free plan sleeps after ~15 min idle)
1. Create a free account at https://uptimerobot.com.
2. Choose **Add New Monitor → HTTP(s)**:
   - URL: `https://<service-name>.onrender.com/v1/healthz`
   - Monitoring interval: **5 minutes**
3. Save. The ping keeps the instance warm. The judge's own healthz polls, every 60 s during the test, also keep it awake.

Note: Render's free plan allows about 750 instance-hours a month, which is enough for one always-on service.

## 4. Verify from outside
```bash
curl https://<service-name>.onrender.com/v1/healthz
curl https://<service-name>.onrender.com/v1/metadata
BOT_URL=https://<service-name>.onrender.com PYTHONUTF8=1 python -m tests.harness
BOT_URL=https://<service-name>.onrender.com TEST_SCENARIO=all python judge_simulator.py
```
All checks should pass. Then submit the base URL `https://<service-name>.onrender.com` on the challenge portal.

## 5. During and after judging
- Do not redeploy, restart or change environment variables; any of these restarts the process.
- If you must update code, do it before the warmup and re-verify step 4.
- Keep the UptimeRobot monitor running for as long as the bot must stay live.
