# Deploying with Passenger (404 and setup)

## If you get 404

### 1. Use the correct URL

- If the app is the **main site** for your domain, open:  
  `https://yourdomain.com/` or `https://yourdomain.com/login`
- If the app is in a **subfolder** (e.g. `rush_tech`), open:  
  `https://yourdomain.com/rush_tech/` or `https://yourdomain.com/rush_tech/login`  
  (Use the exact path shown in your hosting panel for the app.)

**If the app is in a subfolder and you still get 404:** Set `APPLICATION_ROOT` so the app knows its base path. In your hosting panel’s Environment variables (or in `.env` on the server), add:
- **Name:** `APPLICATION_ROOT`  
- **Value:** `/rush_tech` (use your app’s path, no trailing slash)

Then restart the app (`touch tmp/restart.txt`). This makes routes like `/login` match when you open `https://yourdomain.com/rush_tech/login`.

### 2. Ensure `passenger_wsgi.py` is in the app root

On the server, the app root might be something like `/home/mmuchafu/rush_tech/`.  
That directory must contain:

- `passenger_wsgi.py`
- `app.py`
- `requirements.txt`
- `templates/`
- `static/`

### 3. Install Python dependencies

SSH into the server, go to the app directory, then:

```bash
cd /home/mmuchafu/rush_tech
pip install -r requirements.txt --user
# or, if you use a virtualenv:
# source venv/bin/activate && pip install -r requirements.txt
```

Passenger must use the same Python environment where Flask and pymysql are installed.

### 4. Set environment variables

Set these so the app can connect to the database and run:

- `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` (MySQL)
- `SECRET_KEY` (a long random string)

**Option A – Hosting panel:** Add them in the “Environment variables” section.  
**Option B – If you still see “Access denied (using password: NO)”:** Many hosts don’t pass panel env vars to the app. Create a `.env` file in the app directory (e.g. `/home/mmuchafu/rush_tech/.env`) with the same names and values:

```
DB_HOST=localhost
DB_USER=mmuchafu_rush_tech
DB_PASSWORD=your_actual_password
DB_NAME=mmuchafu_rush_tech
SECRET_KEY=your_secret_key_here
```

Do not commit `.env` to git. Then run `touch tmp/restart.txt` to restart the app.

### 5. Check the error log

- **Passenger / stderr log**: often under the app directory, e.g. `~/rush_tech/log/passenger.log`, `~/rush_tech/stderr.log`, or the panel’s “Error log”.
- Look for `ImportError` (e.g. missing `flask` or `pymysql`), `ModuleNotFoundError`, or database “Access denied”.  
  Fix by installing the missing package in the same Python that Passenger uses, or by adding a `.env` file with `DB_*` and `SECRET_KEY` (see step 4).

### 6. Restart the app

After changing code or env vars:

```bash
cd /home/mmuchafu/rush_tech
mkdir -p tmp
touch tmp/restart.txt
```

Or use the “Restart” / “Reload” option in your hosting panel if available.

---

## Quick checklist

- [ ] URL includes the right path (e.g. `https://yourdomain.com/rush_tech/` if the app is in a subfolder).
- [ ] If in a subfolder and you get 404, set `APPLICATION_ROOT=/rush_tech` (your path) in env or `.env`.
- [ ] `passenger_wsgi.py` is in the app root on the server.
- [ ] `pip install -r requirements.txt` was run in the Python environment used by Passenger.
- [ ] `DB_*` and `SECRET_KEY` are set (panel env vars or `.env` file in app directory).
- [ ] Error log (passenger.log or stderr.log) shows no import or database errors.
- [ ] App was restarted after changes (`touch tmp/restart.txt` or panel restart).
