# RUSHTACH Assets Management System

A modern, responsive assets management system built with Flask, PyMySQL, and Tailwind CSS.

## Features

- **Modern UI**: Built with Tailwind CSS, fully responsive for mobile, tablet, and desktop
- **User Authentication**: Secure employee login system with password hashing
- **Base Layout**: Consistent header, footer, and sidebar across all pages
- **Live Date/Time**: Real-time date and time display in the header
- **Dashboard**: Overview with statistics and quick actions
- **Asset Management**: Add, view, and manage company assets
- **Employee Management**: View employee information
- **Reports**: Analytics and insights

## Requirements

- Python 3.7+
- MySQL Server 5.7+ or MariaDB
- pip

## Repository

- **GitHub:** [https://github.com/mbaekimathi/rush_tech](https://github.com/mbaekimathi/rush_tech)

## Installation

### Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/mbaekimathi/rush_tech.git
   cd rush_tech
   ```

2. **Create virtual environment (recommended):**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set environment variables** (export in your shell or set in your IDE/run config):
   ```bash
   export SECRET_KEY=your-secret-key-here
   export DB_HOST=localhost
   export DB_USER=root
   export DB_PASSWORD=your_mysql_password
   export DB_NAME=assets_management
   export ADMIN_PASSWORD=admin123
   ```
   See `env.example` for the full list of variable names.

6. **Run the application:**
   ```bash
   python app.py
   ```

7. **Access the application:**
   - Open your browser and navigate to `http://localhost:5000`
   - Default login credentials:
     - Username: `admin`
     - Password: `admin123` (or the password set in ADMIN_PASSWORD)

## Deploy from Git (Hosting)

To deploy on your hosting using the GitHub repo:

1. **Clone on the server** (or use your host’s “Clone Repository” with `https://github.com/mbaekimathi/rush_tech.git`).
2. **Set environment variables** in your hosting panel (see `env.example` for names):
   - `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` — your MySQL credentials
   - `SECRET_KEY` — a long random string for sessions
3. **Install and run:**
   ```bash
   pip install -r requirements.txt
   gunicorn -c gunicorn_config.py wsgi:app
   ```
4. **Updates:** `git pull` then restart the app.

**Important:** The app reads credentials only from environment variables (set in your hosting panel or shell). Do not commit real passwords.

## Production Deployment

### Using Gunicorn (Recommended)

1. **Install Gunicorn:**
   ```bash
   pip install gunicorn
   ```

2. **Run with Gunicorn:**
   ```bash
   gunicorn -c gunicorn_config.py wsgi:app
   ```

   Or with custom settings:
   ```bash
   gunicorn -w 4 -b 0.0.0.0:5000 wsgi:app
   ```

### Using uWSGI

```bash
uwsgi --http :5000 --wsgi-file wsgi.py --callable app --processes 4 --threads 2
```

### Using systemd (Linux)

Create `/etc/systemd/system/rushtach-assets.service`:

```ini
[Unit]
Description=RUSHTACH Assets Management System
After=network.target mysql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/path/to/RUSH TECH
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/gunicorn -c gunicorn_config.py wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable rushtach-assets
sudo systemctl start rushtach-assets
```

### Behind Nginx Reverse Proxy

Example Nginx configuration:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Flask secret key for sessions | Random (change in production!) |
| `FLASK_DEBUG` | Enable debug mode | `False` |
| `HOST` | Host to bind to | `0.0.0.0` |
| `PORT` | Port to bind to | `5000` |
| `DB_HOST` | MySQL host | `localhost` |
| `DB_USER` | MySQL username | `root` |
| `DB_PASSWORD` | MySQL password | (empty) |
| `DB_NAME` | Database name | `assets_management` |
| `ADMIN_PASSWORD` | Default admin password | `admin123` |

## Security Features

- ✅ Password hashing using Werkzeug
- ✅ SQL injection protection (parameterized queries)
- ✅ Session management
- ✅ Environment-based configuration
- ✅ Production-ready error handling
- ✅ Logging for security events

## Project Structure

```
RUSH TECH/
├── app.py                 # Flask application
├── wsgi.py               # WSGI entry point for production
├── gunicorn_config.py    # Gunicorn configuration
├── requirements.txt      # Python dependencies
├── env.example           # Environment variable names (set in panel/shell)
├── .gitignore           # Git ignore file
├── README.md            # This file
└── templates/           # HTML templates
    ├── base.html        # Base layout template
    ├── login.html       # Login page
    ├── dashboard.html   # Dashboard page
    ├── assets.html      # Assets listing page
    ├── add_asset.html   # Add asset page
    ├── employees.html   # Employees page
    └── reports.html     # Reports page
```

## Database

The application automatically creates the database and tables on first run:
- `employees` table: Stores employee information with hashed passwords
- `assets` table: Stores asset information with foreign key to employees

## Production Checklist

- [ ] Set strong `SECRET_KEY` as an environment variable
- [ ] Set `FLASK_DEBUG=False` in production
- [ ] Use strong database passwords
- [ ] Configure firewall rules
- [ ] Set up SSL/HTTPS (recommended)
- [ ] Configure proper logging
- [ ] Set up database backups
- [ ] Use environment variables for all secrets
- [ ] Configure reverse proxy (Nginx/Apache)
- [ ] Set up process manager (systemd/supervisor)

## Troubleshooting

### Database Connection Issues
- Verify MySQL is running: `sudo systemctl status mysql`
- Check database credentials in your environment variables
- Ensure database user has proper permissions

### Port Already in Use
```bash
# Find process using port 5000
netstat -ano | findstr :5000  # Windows
lsof -i :5000                  # Linux/Mac

# Kill the process or set a different PORT environment variable
```

### Permission Errors
- Ensure the application user has read/write permissions
- Check file ownership and permissions

## License

© 2026 RUSHTACH. All rights reserved.
