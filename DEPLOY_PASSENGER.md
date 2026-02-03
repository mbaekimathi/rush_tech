# Deploying with Passenger (404 and setup)

## If you get 404

### 1. Use the correct URL

- If the app is the **main site** for your domain, open:  
  `https://yourdomain.com/` or `https://yourdomain.com/login`
- If the app is in a **subfolder** (e.g. `rushtech`), open:  
  `https://yourdomain.com/rushtech/` or `https://yourdomain.com/rushtech/login`  
  (Use the exact path shown in your hosting panel for the app.)

### 2. Ensure `passenger_wsgi.py` is in the app root

On the server, the app root might be something like `/home/mmuchafu/rushtech/`.  
That directory must contain:

- `passenger_wsgi.py`
- `app.py`
- `requirements.txt`
- `templates/`
- `static/`

### 3. Install Python dependencies

SSH into the server, go to the app directory, then:

```bash
cd /home/mmuchafu/rushtech
pip install -r requirements.txt --user
# or, if you use a virtualenv:
# source venv/bin/activate && pip install -r requirements.txt
```

Passenger must use the same Python environment where Flask and pymysql are installed.

### 4. Set environment variables

In your hosting panel (or in a `.env` file that your app loads), set at least:

- `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` (MySQL)
- `SECRET_KEY` (a long random string)

Without these, the app may fail to start or redirect incorrectly.

### 5. Check the error log

- **Passenger log**: often under the app directory, e.g. `~/rushtech/log/passenger.log` or in the panel’s “Error log”.
- Look for `ImportError` (e.g. missing `flask` or `pymysql`) or `ModuleNotFoundError`.  
  Fix by installing the missing package in the same Python that Passenger uses.

### 6. Restart the app

After changing code or env vars:

```bash
cd /home/mmuchafu/rushtech
mkdir -p tmp
touch tmp/restart.txt
```

Or use the “Restart” / “Reload” option in your hosting panel if available.

---

## Quick checklist

- [ ] URL includes the right path (e.g. `/rushtech/` if the app is in a subfolder).
- [ ] `passenger_wsgi.py` is in the app root on the server.
- [ ] `pip install -r requirements.txt` was run in the Python environment used by Passenger.
- [ ] `DB_*` and `SECRET_KEY` are set in the environment.
- [ ] Error log shows no import or startup errors.
- [ ] App was restarted after changes (`touch tmp/restart.txt` or panel restart).
