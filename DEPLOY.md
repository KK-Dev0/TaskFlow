# 🚀 Deploying TaskFlow

Two parts: **(A)** put the code on GitHub, **(B)** run it on Render. Total time: about 15 minutes.

> **Before you start:** this is a fresh project with a new database layout. Your old to-do items are **not** migrated. If you plan to delete the old `todo-app` repository, download it as a ZIP first (GitHub → green **Code** button → **Download ZIP**), and delete any Render service that pointed at it.

---

## A. Put the code on GitHub

### Option 1 — GitHub website (no Git needed)

1. Unzip `TaskFlow_v2.zip`. Open the `TaskFlow` folder; you should see `app.py`, `api.py`, `templates/`, `static/`, `README.md`, and so on.
2. Go to **https://github.com/new**. Name the repository **`TaskFlow`**, set it to **Public**, and leave "Add a README", ".gitignore" and "license" **unchecked** (the zip already has them). Click **Create repository**.
3. On the empty repository page, click the **uploading an existing file** link.
4. Drag **everything inside** the `TaskFlow` folder into the browser window — including the `templates`, `static`, `tests`, `docs` and `.github` folders. Wait until every file shows as uploaded.
   - Don't upload the ZIP itself, and don't upload the `TaskFlow` folder; `app.py` must sit at the repository root.
   - If your file manager hides folders starting with a dot, make sure `.github` and `.gitignore` are included (on Windows: **View → Show → Hidden items**).
5. Commit message: `Initial commit: TaskFlow` → **Commit changes**.
6. Reload the repository page. You should see the README with screenshots. Open the **Actions** tab: the **CI** workflow runs the tests automatically.

### Option 2 — Git on your computer

Create the empty `TaskFlow` repository on GitHub first (step 2 above), then:

```bash
cd path/to/TaskFlow              # the unzipped folder
git init -b main
git add -A
git commit -m "Initial commit: TaskFlow"
git remote add origin https://github.com/KK-Dev0/TaskFlow.git
git push -u origin main
```

---

## B. Deploy on Render

### Using the blueprint (easiest)

1. Sign in at **https://render.com** and connect your GitHub account.
2. **New + → Blueprint** → choose `KK-Dev0/TaskFlow` → **Apply**.
3. Wait for the build (2–4 minutes). Render creates `SECRET_KEY` automatically.
4. Open the URL Render shows, e.g. `https://taskflow-xxxx.onrender.com`, and **create an account**.
5. Visit `/health` — you should see `{"status":"ok"}`.

### Or set it up by hand

**New + → Web Service** → pick the repository, then:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --workers 2 --threads 4 --timeout 60` |
| Health check path | `/health` |

Under **Environment**, add `SECRET_KEY` with a long random value. Generate one with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Optional: turn on the AI model

Without a key the agents work using built-in rules. For model-written plans, add one environment variable in Render:

- `ANTHROPIC_API_KEY` (optional `AI_MODEL`), **or**
- `OPENAI_API_KEY` (optional `AI_MODEL`, `OPENAI_BASE_URL`)

Then open **AI Agents** in the app: the *Mode* card shows which model is active. Keep keys out of GitHub.

---

## ⚠️ Keeping your data

Render's **free** instances use a temporary filesystem. A SQLite database stored there **disappears when the service redeploys or restarts**. Choose one:

1. **Paid instance + persistent disk** (recommended for real use). In `render.yaml` change `plan: free` to `plan: starter`, uncomment the `disk:` block and the `DB_PATH` env var (`/var/data/taskflow.db`), then redeploy.
2. **Use the built-in backup** on the free plan: **Settings → Export backup (JSON)** now and then. After a reset, create your account again and use **Import backup**.
3. Host on a platform that gives you a persistent disk.

A free instance also "sleeps" after inactivity and takes about a minute to wake on the next visit. That is normal.

---

## ✅ Smoke test after deploying

- [ ] `/health` returns `{"status":"ok"}`
- [ ] Register, sign out, sign in
- [ ] Quick add: `read chapter 4 tomorrow 6pm #study for 45m` creates a task with a due time
- [ ] Open the task, click **Break down with AI**, then approve it in **AI Agents**
- [ ] Planner shows your task in the schedule
- [ ] Focus → Start → Pause works; the timer pill appears in the top bar
- [ ] Add an event in Calendar; it shows on that day
- [ ] Create a task due in 5 minutes with the default 30-minute lead — a reminder toast appears within 30 seconds
- [ ] Settings → Export backup downloads a JSON file

## 🔧 Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails on Render | Open **Logs**; confirm `requirements.txt` is at the repository root |
| "Application failed to respond" | Start command must be `gunicorn app:app …` and `app.py` must be at the root |
| Signed out after every redeploy | `SECRET_KEY` isn't set (a fresh random one is generated each deploy without a persistent disk) |
| Tasks vanished | Free-plan filesystem reset — see **Keeping your data** |
| No desktop notifications | Settings → **Enable desktop alerts**, and allow them in the browser |
| Old page still shown on GitHub | Hard refresh, and check you committed to the branch Render deploys (usually `main`) |
