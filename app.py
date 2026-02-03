from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import pymysql
from functools import wraps
import os
import logging
import glob
import socket
import time
import csv
import argparse
from datetime import date, datetime
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import re

# Load .env if present (many hosts don't pass panel "Environment variables" to the app process)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

def normalize_serial_number(value: str) -> str:
    """
    Normalize a scanned/typed serial number (e.g. 485754437F1140B5).
    - Trims whitespace/newlines
    - Removes common prefixes like "SN:", "S/N:"
    - Removes spaces, dots, dashes and other separators
    - Keeps only 0-9 and A-Za-z (hex-style alphanumeric)
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    # Remove common prefixes
    s = re.sub(r"^\s*(s\/n|sn)\s*[:#\-.]?\s*", "", s, flags=re.IGNORECASE)
    # Remove whitespace and separator characters (dots, dashes, colons, etc.)
    s = re.sub(r"[\s\-_:.]+", "", s)
    # Keep only alphanumerics
    s = re.sub(r"[^0-9A-Za-z]", "", s)
    return s

# Load configuration from environment variables
# Use a fixed secret key file for development to maintain sessions across reloads
SECRET_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')

def get_or_create_secret_key():
    """Get secret key from env, file, or generate a new one"""
    # First, try environment variable
    secret_key = os.environ.get('SECRET_KEY')
    if secret_key:
        return secret_key
    
    # Second, try to read from file (for development persistence)
    if os.path.exists(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, 'r') as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Could not read secret key file: {e}")
    
    # Third, generate a new one and save it for future use
    secret_key = os.urandom(24).hex()
    try:
        with open(SECRET_KEY_FILE, 'w') as f:
            f.write(secret_key)
        logger.info("Generated and saved new secret key for session persistence")
    except Exception as e:
        logger.warning(f"Could not save secret key file: {e}")
    
    return secret_key

app.config['SECRET_KEY'] = get_or_create_secret_key()
app.config['DEBUG'] = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'  # Default to True for development
app.config['TEMPLATES_AUTO_RELOAD'] = True  # Auto-reload templates
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Disable caching for development
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max file size
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}

# Disable caching in development mode
@app.after_request
def after_request(response):
    """Add headers to prevent caching in development"""
    if app.config['DEBUG']:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# Create uploads directory if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Use ProxyFix for production behind reverse proxy
if not app.config['DEBUG']:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Detect if running locally or on hosted server
def is_local_environment():
    """Check if running in local development environment"""
    # Priority 1: Check explicit environment variable
    env_mode = os.environ.get('ENVIRONMENT', '').lower()
    if env_mode == 'production' or env_mode == 'hosted':
        return False
    if env_mode == 'local' or env_mode == 'development':
        return True
    
    # Priority 2: Check for common hosting environment indicators
    hosting_indicators = [
        'SERVER_SOFTWARE',  # Common in many hosting platforms
        'WEBSITE_HOSTNAME',  # Azure
        'DYNO',  # Heroku
        'RAILWAY_ENVIRONMENT',  # Railway
        'RENDER',  # Render
        'FLY_APP_NAME',  # Fly.io
        'VERCEL',  # Vercel
        'PLATFORM',  # Platform.sh
        'cPanel',  # cPanel hosting
    ]
    
    # If any hosting indicator exists, we're on a hosted server
    for indicator in hosting_indicators:
        if os.environ.get(indicator):
            return False
    
    # Priority 3: Check if DB_HOST is explicitly set to something other than localhost
    db_host = os.environ.get('DB_HOST', '').lower().strip()
    if db_host and db_host not in ['localhost', '127.0.0.1', '']:
        return False
    
    # Priority 4: Check if we're explicitly told we're in production
    flask_env = os.environ.get('FLASK_ENV', '').lower()
    if flask_env == 'production':
        return False
    
    # Priority 5: Check hostname (if not localhost, likely hosted)
    try:
        hostname = socket.gethostname()
        if hostname and hostname not in ['localhost', '127.0.0.1']:
            # Check if it's a typical hosting hostname pattern
            if any(indicator in hostname.lower() for indicator in ['server', 'host', 'web', 'prod', 'production']):
                return False
    except:
        pass
    
    # Default to local if no indicators found
    return True

# Database configuration - automatically detect environment
# Environment variables always take precedence over defaults
is_local = is_local_environment()

# Define default configurations
LOCAL_DEFAULTS = {
    'host': 'localhost',
    'user': 'root',
    'password': '',
    'database': 'assets_management'
}

# Hosted/production: set DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in environment (e.g. .env or hosting panel).
# Do not commit real credentials. Use empty defaults so env vars are required on the server.
HOSTED_DEFAULTS = {
    'host': 'localhost',
    'user': '',
    'password': '',
    'database': ''
}

# Build initial configuration
if is_local:
    # Local development configuration defaults
    # But environment variables override these if set
    DB_CONFIG = {
        'host': os.environ.get('DB_HOST', LOCAL_DEFAULTS['host']),
        'user': os.environ.get('DB_USER') or LOCAL_DEFAULTS['user'],
        'password': os.environ.get('DB_PASSWORD') or LOCAL_DEFAULTS['password'],
        'database': os.environ.get('DB_NAME') or LOCAL_DEFAULTS['database'],
        'charset': 'utf8mb4',
        'cursorclass': pymysql.cursors.DictCursor
    }
    
    # Check if any DB credentials are explicitly set via environment
    env_creds_set = any([
        os.environ.get('DB_USER'),
        os.environ.get('DB_PASSWORD'),
        os.environ.get('DB_NAME'),
        os.environ.get('DB_HOST') and os.environ.get('DB_HOST') not in ['localhost', '127.0.0.1']
    ])
    
    if env_creds_set:
        logger.info("Using LOCAL environment with custom database credentials from environment variables")
    else:
        logger.info("Using LOCAL database configuration (defaults)")
        # Test connection and fallback to hosted if local fails
        try:
            test_conn = pymysql.connect(**DB_CONFIG)
            test_conn.close()
            logger.info("Local database connection successful")
        except Exception as e:
            logger.warning(f"Local database connection failed: {e}")
            logger.info("Automatically falling back to HOSTED database configuration")
            # Fallback to hosted defaults
            DB_CONFIG = {
                'host': os.environ.get('DB_HOST', HOSTED_DEFAULTS['host']),
                'user': os.environ.get('DB_USER') or HOSTED_DEFAULTS['user'],
                'password': os.environ.get('DB_PASSWORD') or HOSTED_DEFAULTS['password'],
                'database': os.environ.get('DB_NAME') or HOSTED_DEFAULTS['database'],
                'charset': 'utf8mb4',
                'cursorclass': pymysql.cursors.DictCursor
            }
            # Test hosted connection
            try:
                test_conn = pymysql.connect(**DB_CONFIG)
                test_conn.close()
                logger.info("Hosted database connection successful")
            except Exception as e2:
                logger.error(f"Hosted database connection also failed: {e2}")
else:
    # Hosted/production configuration defaults
    # But environment variables override these if set
    DB_CONFIG = {
        'host': os.environ.get('DB_HOST', HOSTED_DEFAULTS['host']),
        'user': os.environ.get('DB_USER') or HOSTED_DEFAULTS['user'],
        'password': os.environ.get('DB_PASSWORD') or HOSTED_DEFAULTS['password'],
        'database': os.environ.get('DB_NAME') or HOSTED_DEFAULTS['database'],
        'charset': 'utf8mb4',
        'cursorclass': pymysql.cursors.DictCursor
    }
    
    # Check if any DB credentials are explicitly set via environment
    env_creds_set = any([
        os.environ.get('DB_USER'),
        os.environ.get('DB_PASSWORD'),
        os.environ.get('DB_NAME'),
        os.environ.get('DB_HOST') and os.environ.get('DB_HOST') not in ['localhost', '127.0.0.1']
    ])
    
    if env_creds_set:
        logger.info("Using HOSTED environment with custom database credentials from environment variables")
    else:
        logger.info("Using HOSTED database configuration (defaults)")

def get_db_connection():
    """Create and return a database connection"""
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Database connection error: {e}")
        
        # Provide helpful guidance based on error
        if "Access denied" in error_msg:
            logger.info("TIP: To use custom database credentials, set these environment variables:")
            logger.info("  - DB_USER=your_username")
            logger.info("  - DB_PASSWORD=your_password")
            logger.info("  - DB_NAME=your_database")
            logger.info("  - DB_HOST=your_host (optional, defaults to localhost)")
            logger.info("Or set ENVIRONMENT=hosted to use hosted configuration defaults")
        
        return None

def _parse_payment_date(value: str) -> date:
    """
    Accepts values like:
    - 2026-01-24
    - 24TH JAN
    - 24 JAN
    - 24/01/2026
    """
    if value is None:
        raise ValueError("payment_date is required")
    s = str(value).strip()
    if not s:
        raise ValueError("payment_date is required")

    # ISO
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        pass

    # common slash format
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass

    # Excel-like: "24TH JAN" / "24 JAN"
    m = re.match(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,})\s*(\d{4})?\s*$", s, flags=re.IGNORECASE)
    if m:
        day = int(m.group(1))
        month_text = m.group(2).strip().lower()
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        month_map = {
            "jan": 1, "january": 1,
            "feb": 2, "february": 2,
            "mar": 3, "march": 3,
            "apr": 4, "april": 4,
            "may": 5,
            "jun": 6, "june": 6,
            "jul": 7, "july": 7,
            "aug": 8, "august": 8,
            "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10,
            "nov": 11, "november": 11,
            "dec": 12, "december": 12,
        }
        if month_text not in month_map:
            raise ValueError(f"Unrecognized month in payment_date: {value!r}")
        return date(year, month_map[month_text], day)

    raise ValueError(f"Unrecognized payment_date format: {value!r}")

def reset_client_data(connection) -> None:
    """
    Wipes client operational data while keeping employees/settings intact.
    This deletes:
    - client_connections, client_relocations, client_renewals, client_reversals
    - assets (client-linked assets)
    - clients
    """
    with connection.cursor() as cursor:
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        # Delete children first (even with FK checks off, this is clearer)
        for table in [
            "client_connections",
            "client_relocations",
            "client_renewals",
            "client_reversals",
            "assets",
            "clients",
        ]:
            cursor.execute(f"TRUNCATE TABLE {table}")
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    connection.commit()

def seed_virtual_clients_from_csv(connection, csv_path: str) -> int:
    """
    CSV columns supported (case-insensitive):
    - full_name, phone_number, account_number, package
    - virtual_location, ground_location, payment_date
    Missing columns default to empty string except payment_date which is required.
    All inserted rows are client_category='Virtual' and status='Pending'.
    """
    if not csv_path or not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    inserted = 0
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")
        # normalize header keys
        fieldnames = {name: name.strip().lower() for name in reader.fieldnames}

        def get(row, key, default=""):
            # find original header that matches `key`
            for orig, norm in fieldnames.items():
                if norm == key:
                    v = row.get(orig)
                    return ("" if v is None else str(v)).strip() or default
            return default

        rows_by_account: dict[str, tuple] = {}
        for row in reader:
            full_name = get(row, "full_name")
            phone_number = get(row, "phone_number")
            account_number = get(row, "account_number")
            package = get(row, "package")
            virtual_location = get(row, "virtual_location")
            ground_location = get(row, "ground_location")
            payment_date_raw = get(row, "payment_date")

            if not full_name or not phone_number or not account_number:
                # skip completely blank rows
                if not any([full_name, phone_number, account_number, package, virtual_location, ground_location, payment_date_raw]):
                    continue
                raise ValueError(f"Missing required fields in CSV row: full_name/phone_number/account_number. Row={row}")

            payment_date_value = _parse_payment_date(payment_date_raw)
            payload = (
                full_name,
                phone_number,
                account_number,
                package,
                "Virtual",
                virtual_location,
                ground_location,
                payment_date_value,
            )

            # De-dupe within CSV by account_number (clients.account_number is UNIQUE)
            if account_number in rows_by_account:
                logger.warning(f"Duplicate account_number in CSV, skipping later row: {account_number}")
                continue
            rows_by_account[account_number] = payload

    rows = list(rows_by_account.values())
    if not rows:
        return 0

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO clients (
                full_name, phone_number, account_number, package, client_category,
                virtual_location, ground_location, payment_date, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Pending')
            """,
            rows,
        )
    connection.commit()
    inserted = len(rows)
    return inserted

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_status_options():
    """Get valid status options"""
    return ['Active', 'Pending', 'Suspended']

def get_role_options():
    """Get valid role options"""
    return ['Admin', 'Manager', 'Dispatcher', 'Technician', 'Accounts', 'IT Support', 'Employee']

def get_client_status_options():
    """Get valid client status options"""
    return ['Pending', 'Connected', 'Relocated', 'Reversed', 'Renewed', 'Closed']

def get_effective_role():
    """Get the effective role (switched role for IT Support, or actual role)"""
    actual_role = session.get('role', 'Employee')
    switched_role = session.get('switched_role')
    
    # Only IT Support can use role switching
    if actual_role == 'IT Support' and switched_role:
        return switched_role
    return actual_role

@app.context_processor
def inject_role_and_page():
    """Make role, current page, and company data available to all templates"""
    effective_role = get_effective_role()
    
    # Fetch company settings
    company_name = 'RUSHTACH'
    company_logo = None
    notification_count = 0
    
    connection = get_db_connection()
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT company_name, company_logo FROM company_settings ORDER BY id DESC LIMIT 1")
                company_data = cursor.fetchone()
                if company_data:
                    company_name = company_data.get('company_name', 'RUSHTACH')
                    company_logo = company_data.get('company_logo')

                # Compute live notifications badge count (matches /notifications logic)
                try:
                    from datetime import date, datetime

                    cursor.execute(
                        "SELECT relocation_days, renewal_days, closing_days "
                        "FROM notification_settings ORDER BY id DESC LIMIT 1"
                    )
                    ns = cursor.fetchone() or {}

                    relocation_days = ns.get('relocation_days', 30)
                    renewal_days = ns.get('renewal_days', 30)
                    closing_days = ns.get('closing_days', 30)

                    cursor.execute("SELECT created_at FROM clients")
                    clients = cursor.fetchall() or []

                    today = date.today()
                    count = 0
                    for c in clients:
                        created_at = c.get('created_at')
                        if not created_at:
                            continue
                        reg_date = created_at.date() if isinstance(created_at, datetime) else created_at
                        days_diff = (today - reg_date).days

                        # Count per-notification-type, same as notifications page list length
                        if days_diff >= relocation_days:
                            count += 1
                        if days_diff >= renewal_days:
                            count += 1
                        if days_diff >= closing_days:
                            count += 1

                    notification_count = count
                except Exception as e:
                    logger.warning(f"Could not compute notification count: {e}")
        except Exception as e:
            logger.warning(f"Could not fetch company settings: {e}")
        finally:
            connection.close()
    
    return {
        'user_role': effective_role,
        'actual_role': session.get('role', 'Employee'),  # Original role
        'switched_role': session.get('switched_role'),  # Currently switched role
        'current_endpoint': request.endpoint if request else None,
        'company_name': company_name,
        'company_logo': company_logo,
        'notification_count': notification_count
    }

@app.route('/dev-reload-check')
def dev_reload_check():
    """Development endpoint to check if auto-reload is working"""
    import time
    return jsonify({
        'timestamp': time.time(),
        'message': 'Auto-reload is working! Server restarted automatically.',
        'debug_mode': app.config['DEBUG']
    })

def init_database():
    """Initialize database and create tables if they don't exist"""
    connection = get_db_connection()
    if not connection:
        logger.error("Failed to connect to database")
        return False
    
    try:
        with connection.cursor() as cursor:
            # Check if employees table exists
            cursor.execute("SHOW TABLES LIKE 'employees'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                # Create employees table with ENUM columns
                cursor.execute("""
                    CREATE TABLE employees (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        username VARCHAR(50) UNIQUE NOT NULL,
                        password VARCHAR(255) NOT NULL,
                        full_name VARCHAR(100) NOT NULL,
                        email VARCHAR(100) UNIQUE,
                        phone_number VARCHAR(20),
                        profile_picture VARCHAR(255),
                        status ENUM('Active', 'Pending', 'Suspended') DEFAULT 'Pending',
                        role ENUM('Admin', 'Manager', 'Dispatcher', 'Technician', 'Accounts', 'IT Support', 'Employee') DEFAULT 'Employee',
                        verification_code VARCHAR(6),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.info("Created employees table with ENUM columns")
            else:
                # Check and add missing columns
                cursor.execute("SHOW COLUMNS FROM employees LIKE 'phone_number'")
                if not cursor.fetchone():
                    cursor.execute("ALTER TABLE employees ADD COLUMN phone_number VARCHAR(20)")
                    cursor.execute("ALTER TABLE employees ADD COLUMN profile_picture VARCHAR(255)")
                    cursor.execute("ALTER TABLE employees ADD COLUMN verification_code VARCHAR(6)")
                    logger.info("Added new columns to employees table")
                
                # Migrate status column to ENUM if it's VARCHAR
                cursor.execute("SHOW COLUMNS FROM employees WHERE Field = 'status'")
                status_col = cursor.fetchone()
                if status_col and 'varchar' in status_col['Type'].lower():
                    try:
                        cursor.execute("""
                            ALTER TABLE employees 
                            MODIFY COLUMN status ENUM('Active', 'Pending', 'Suspended') DEFAULT 'Pending'
                        """)
                        logger.info("Migrated status column to ENUM")
                    except Exception as e:
                        logger.warning(f"Could not migrate status column: {e}")
                
                # Migrate role column to ENUM if it's VARCHAR
                cursor.execute("SHOW COLUMNS FROM employees WHERE Field = 'role'")
                role_col = cursor.fetchone()
                if role_col and 'varchar' in role_col['Type'].lower():
                    try:
                        cursor.execute("""
                            ALTER TABLE employees 
                            MODIFY COLUMN role ENUM('Admin', 'Manager', 'Dispatcher', 'Technician', 'Accounts', 'IT Support', 'Employee') DEFAULT 'Employee'
                        """)
                        logger.info("Migrated role column to ENUM")
                    except Exception as e:
                        logger.warning(f"Could not migrate role column: {e}")
            
            # Ensure status and role columns exist with ENUM type (for new tables)
            cursor.execute("SHOW COLUMNS FROM employees LIKE 'status'")
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE employees 
                    ADD COLUMN status ENUM('Active', 'Pending', 'Suspended') DEFAULT 'Pending'
                """)
            
            cursor.execute("SHOW COLUMNS FROM employees LIKE 'role'")
            if not cursor.fetchone():
                cursor.execute("""
                    ALTER TABLE employees 
                    ADD COLUMN role ENUM('Admin', 'Manager', 'Dispatcher', 'Technician', 'Accounts', 'IT Support', 'Employee') DEFAULT 'Employee'
                """)
            
            # Create assets table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS assets (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    asset_name VARCHAR(200) NOT NULL,
                    asset_type VARCHAR(100),
                    serial_number VARCHAR(100) UNIQUE,
                    status ENUM('In Use', 'Relocated', 'Renewed', 'Closed', 'Reversed') DEFAULT 'In Use',
                    assigned_to INT,
                    client_id INT,
                    purchase_date DATE,
                    purchase_price DECIMAL(10, 2),
                    location VARCHAR(100),
                    power_levels VARCHAR(50),
                    router_used VARCHAR(100),
                    port_number VARCHAR(50),
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (assigned_to) REFERENCES employees(id) ON DELETE SET NULL,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL
                )
            """)
            
            # Migrate status column to ENUM if it's VARCHAR (for existing tables)
            cursor.execute("SHOW COLUMNS FROM assets WHERE Field = 'status'")
            status_col = cursor.fetchone()
            if status_col:
                if 'varchar' in status_col['Type'].lower() or 'text' in status_col['Type'].lower():
                    try:
                        cursor.execute("""
                            ALTER TABLE assets 
                            MODIFY COLUMN status ENUM('In Use', 'Relocated', 'Renewed', 'Closed', 'Reversed') DEFAULT 'In Use'
                        """)
                        logger.info("Migrated assets status column to ENUM")
                    except Exception as e:
                        logger.warning(f"Could not migrate assets status column: {e}")
                elif 'enum' not in status_col['Type'].lower():
                    # Column exists but is not ENUM, try to modify it
                    try:
                        cursor.execute("""
                            ALTER TABLE assets 
                            MODIFY COLUMN status ENUM('In Use', 'Relocated', 'Renewed', 'Closed', 'Reversed') DEFAULT 'In Use'
                        """)
                        logger.info("Updated assets status column to ENUM")
                    except Exception as e:
                        logger.warning(f"Could not update assets status column: {e}")
            
            # Add new columns to existing assets table if they don't exist
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'client_id'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN client_id INT, ADD FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE SET NULL")
                logger.info("Added client_id column to assets table")
            
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'power_levels'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN power_levels VARCHAR(50)")
                logger.info("Added power_levels column to assets table")
            
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'router_used'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN router_used VARCHAR(100)")
                logger.info("Added router_used column to assets table")
            
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'port_number'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN port_number VARCHAR(50)")
                logger.info("Added port_number column to assets table")
            
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'router_name'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN router_name VARCHAR(100)")
                logger.info("Added router_name column to assets table")
            
            # Add buyer_name column to assets table if it doesn't exist
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'buyer_name'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN buyer_name VARCHAR(200)")
                logger.info("Added buyer_name column to assets table")
            
            cursor.execute("SHOW COLUMNS FROM assets LIKE 'router_password'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE assets ADD COLUMN router_password VARCHAR(255)")
                logger.info("Added router_password column to assets table")
            
            # Also add router_name and router_password to client_connections table
            cursor.execute("SHOW COLUMNS FROM client_connections LIKE 'router_name'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE client_connections ADD COLUMN router_name VARCHAR(100)")
                logger.info("Added router_name column to client_connections table")
            
            cursor.execute("SHOW COLUMNS FROM client_connections LIKE 'router_password'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE client_connections ADD COLUMN router_password VARCHAR(255)")
                logger.info("Added router_password column to client_connections table")
            
            # Create clients table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS clients (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    full_name VARCHAR(100) NOT NULL,
                    phone_number VARCHAR(20) NOT NULL,
                    account_number VARCHAR(50) UNIQUE NOT NULL,
                    package VARCHAR(100),
                    client_category ENUM('Actual', 'Virtual') DEFAULT 'Actual',
                    virtual_location VARCHAR(200),
                    ground_location VARCHAR(200),
                    payment_date DATE NOT NULL,
                    status ENUM('Pending', 'Connected', 'Relocated', 'Reversed', 'Renewed', 'Closed') DEFAULT 'Pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """)

            # Add client_category column if it doesn't exist (for existing databases)
            cursor.execute("SHOW COLUMNS FROM clients LIKE 'client_category'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE clients ADD COLUMN client_category ENUM('Actual', 'Virtual') DEFAULT 'Actual' AFTER package")
                logger.info("Added client_category column to clients table")
            
            # Create client_connections table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS client_connections (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id INT NOT NULL,
                    technician_id INT NOT NULL,
                    serial_number VARCHAR(100) NOT NULL,
                    power_levels VARCHAR(50),
                    router_used VARCHAR(100),
                    router_name VARCHAR(100),
                    router_password VARCHAR(255),
                    ground_location VARCHAR(200),
                    port_number VARCHAR(50),
                    connection_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    FOREIGN KEY (technician_id) REFERENCES employees(id) ON DELETE CASCADE
                )
            """)
            
            # Create client_relocations table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS client_relocations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id INT NOT NULL,
                    old_location VARCHAR(200),
                    new_location VARCHAR(200) NOT NULL,
                    old_port VARCHAR(50),
                    new_port VARCHAR(50) NOT NULL,
                    assigned_to INT,
                    old_router VARCHAR(100),
                    new_router VARCHAR(100) NOT NULL,
                    old_serial_number VARCHAR(100),
                    new_serial_number VARCHAR(100) NOT NULL,
                    relocated_by INT NOT NULL,
                    relocation_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    FOREIGN KEY (assigned_to) REFERENCES employees(id) ON DELETE SET NULL,
                    FOREIGN KEY (relocated_by) REFERENCES employees(id) ON DELETE CASCADE
                )
            """)
            
            # Create client_renewals table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS client_renewals (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id INT NOT NULL,
                    renewal_amount DECIMAL(10, 2) NOT NULL,
                    renewed_by INT NOT NULL,
                    renewal_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    FOREIGN KEY (renewed_by) REFERENCES employees(id) ON DELETE CASCADE
                )
            """)
            
            # Create client_reversals table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS client_reversals (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id INT NOT NULL,
                    reversal_amount DECIMAL(10, 2) NOT NULL,
                    reversed_by INT NOT NULL,
                    reversal_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
                    FOREIGN KEY (reversed_by) REFERENCES employees(id) ON DELETE CASCADE
                )
            """)
            
            # Drop client_closures table if it exists (no longer needed)
            cursor.execute("DROP TABLE IF EXISTS client_closures")
            logger.info("Dropped client_closures table")
            
            # Add relocation_count and renewal_count columns to clients table if they don't exist
            cursor.execute("SHOW COLUMNS FROM clients LIKE 'relocation_count'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE clients ADD COLUMN relocation_count INT DEFAULT 0")
                logger.info("Added relocation_count column to clients table")
            
            cursor.execute("SHOW COLUMNS FROM clients LIKE 'renewal_count'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE clients ADD COLUMN renewal_count INT DEFAULT 0")
                logger.info("Added renewal_count column to clients table")
            
            cursor.execute("SHOW COLUMNS FROM clients LIKE 'work_order'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE clients ADD COLUMN work_order VARCHAR(100) DEFAULT NULL")
                logger.info("Added work_order column to clients table")
            
            # Check if status column exists, if not add it (for existing tables)
            cursor.execute("SHOW COLUMNS FROM clients LIKE 'status'")
            if not cursor.fetchone():
                try:
                    cursor.execute("""
                        ALTER TABLE clients 
                        ADD COLUMN status ENUM('Pending', 'Connected', 'Relocated', 'Reversed', 'Renewed', 'Closed') DEFAULT 'Pending'
                    """)
                    logger.info("Added status column to clients table")
                except Exception as e:
                    logger.warning(f"Could not add status column to clients table: {e}")
            
            # Migrate status column to ENUM if it's VARCHAR (for existing tables)
            cursor.execute("SHOW COLUMNS FROM clients WHERE Field = 'status'")
            status_col = cursor.fetchone()
            if status_col and 'varchar' in status_col['Type'].lower():
                try:
                    cursor.execute("""
                        ALTER TABLE clients 
                        MODIFY COLUMN status ENUM('Pending', 'Connected', 'Relocated', 'Reversed', 'Renewed', 'Closed') DEFAULT 'Pending'
                    """)
                    logger.info("Migrated clients status column to ENUM")
                except Exception as e:
                    logger.warning(f"Could not migrate clients status column: {e}")
            
            # Create company_settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS company_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_name VARCHAR(200) NOT NULL DEFAULT 'RUSHTACH',
                    company_logo VARCHAR(255),
                    company_address TEXT,
                    company_phone VARCHAR(20),
                    company_email VARCHAR(100),
                    company_website VARCHAR(200),
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_by INT,
                    FOREIGN KEY (updated_by) REFERENCES employees(id) ON DELETE SET NULL
                )
            """)
            
            # Insert default company settings if table is empty
            cursor.execute("SELECT COUNT(*) as count FROM company_settings")
            if cursor.fetchone()['count'] == 0:
                cursor.execute("""
                    INSERT INTO company_settings (company_name, company_logo)
                    VALUES ('RUSHTACH', NULL)
                """)
                logger.info("Created default company settings")
            
            # Create notification_settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notification_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    sound_enabled BOOLEAN DEFAULT TRUE,
                    notification_sound VARCHAR(100) DEFAULT 'default',
                    volume INT DEFAULT 50,
                    email_new_client BOOLEAN DEFAULT TRUE,
                    email_payment BOOLEAN DEFAULT TRUE,
                    email_status_change BOOLEAN DEFAULT TRUE,
                    system_alerts BOOLEAN DEFAULT TRUE,
                    browser_notifications BOOLEAN DEFAULT TRUE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_by INT,
                    FOREIGN KEY (updated_by) REFERENCES employees(id) ON DELETE SET NULL
                )
            """)
            
            # Add new columns for day-based notifications if they don't exist
            cursor.execute("SHOW COLUMNS FROM notification_settings LIKE 'relocation_days'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE notification_settings ADD COLUMN relocation_days INT DEFAULT 30")
                logger.info("Added relocation_days column to notification_settings table")
            
            cursor.execute("SHOW COLUMNS FROM notification_settings LIKE 'renewal_days'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE notification_settings ADD COLUMN renewal_days INT DEFAULT 30")
                logger.info("Added renewal_days column to notification_settings table")
            
            cursor.execute("SHOW COLUMNS FROM notification_settings LIKE 'closing_days'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE notification_settings ADD COLUMN closing_days INT DEFAULT 30")
                logger.info("Added closing_days column to notification_settings table")
            
            # Insert default notification settings if table is empty
            cursor.execute("SELECT COUNT(*) as count FROM notification_settings")
            if cursor.fetchone()['count'] == 0:
                cursor.execute("""
                    INSERT INTO notification_settings 
                    (sound_enabled, notification_sound, volume, email_new_client, email_payment, 
                     email_status_change, system_alerts, browser_notifications, relocation_days, renewal_days, closing_days)
                    VALUES (TRUE, 'default', 50, TRUE, TRUE, TRUE, TRUE, TRUE, 30, 30, 30)
                """)
                logger.info("Created default notification settings")
            
            # Create packages table for package pricing
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS packages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    package_name VARCHAR(100) UNIQUE NOT NULL,
                    sale_price DECIMAL(10, 2) NOT NULL DEFAULT 0.00,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
            """)
            
            # Create assets_settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS assets_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    default_asset_price DECIMAL(10, 2) DEFAULT 0.00,
                    asset_depreciation_rate DECIMAL(5, 2) DEFAULT 0.00,
                    auto_assign_assets BOOLEAN DEFAULT TRUE,
                    require_asset_approval BOOLEAN DEFAULT FALSE,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_by INT,
                    FOREIGN KEY (updated_by) REFERENCES employees(id) ON DELETE SET NULL
                )
            """)
            
            # Insert default assets settings if table is empty
            cursor.execute("SELECT COUNT(*) as count FROM assets_settings")
            if cursor.fetchone()['count'] == 0:
                cursor.execute("""
                    INSERT INTO assets_settings 
                    (default_asset_price, asset_depreciation_rate, auto_assign_assets, require_asset_approval)
                    VALUES (0.00, 0.00, TRUE, FALSE)
                """)
                logger.info("Created default assets settings")
            
            # Create technical_settings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS technical_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    price_per_ticket DECIMAL(10, 2) DEFAULT 0.00,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    updated_by INT,
                    FOREIGN KEY (updated_by) REFERENCES employees(id) ON DELETE SET NULL
                )
            """)
            
            # Check if price_per_ticket column exists, add it if missing
            try:
                cursor.execute("SHOW COLUMNS FROM technical_settings LIKE 'price_per_ticket'")
                if not cursor.fetchone():
                    cursor.execute("ALTER TABLE technical_settings ADD COLUMN price_per_ticket DECIMAL(10, 2) DEFAULT 0.00")
                    logger.info("Added price_per_ticket column to technical_settings table")
            except Exception as e:
                logger.warning(f"Could not check/add price_per_ticket column: {e}")
            
            # Drop unnecessary columns if they exist (from previous version)
            columns_to_drop = ['system_maintenance_mode', 'api_enabled', 'backup_frequency', 
                              'log_retention_days', 'session_timeout_minutes', 'max_login_attempts',
                              'enable_two_factor_auth', 'system_timezone']
            for col in columns_to_drop:
                try:
                    cursor.execute(f"SHOW COLUMNS FROM technical_settings LIKE '{col}'")
                    if cursor.fetchone():
                        cursor.execute(f"ALTER TABLE technical_settings DROP COLUMN {col}")
                        logger.info(f"Dropped column {col} from technical_settings table")
                except Exception as e:
                    logger.warning(f"Could not drop column {col}: {e}")
            
            # Insert default technical settings if table is empty
            cursor.execute("SELECT COUNT(*) as count FROM technical_settings")
            if cursor.fetchone()['count'] == 0:
                cursor.execute("""
                    INSERT INTO technical_settings (price_per_ticket)
                    VALUES (0.00)
                """)
                logger.info("Created default technical settings")
            
            # Create expenses table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    category VARCHAR(50) NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    amount DECIMAL(12, 2) NOT NULL,
                    details TEXT,
                    registered_by INT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (registered_by) REFERENCES employees(id) ON DELETE SET NULL
                )
            """)
            cursor.execute("SHOW COLUMNS FROM expenses LIKE 'registered_by'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE expenses ADD COLUMN registered_by INT NULL")
                try:
                    cursor.execute("ALTER TABLE expenses ADD CONSTRAINT fk_expenses_registered_by FOREIGN KEY (registered_by) REFERENCES employees(id) ON DELETE SET NULL")
                except Exception:
                    pass
                logger.info("Added registered_by column to expenses table")
            
            # Create default admin user if no users exist
            cursor.execute("SELECT COUNT(*) as count FROM employees")
            if cursor.fetchone()['count'] == 0:
                default_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
                default_code = os.environ.get('ADMIN_CODE', '000000')
                hashed_password = generate_password_hash(default_password)
                cursor.execute("""
                    INSERT INTO employees (username, password, full_name, email, status, role, verification_code)
                    VALUES ('admin', %s, 'Administrator', 'admin@rushtach.com', 'Active', 'Admin', %s)
                """, (hashed_password, default_code))
                logger.info(f"Default admin user created (Code: {default_code}, Password: admin123)")
            
            connection.commit()
            logger.info("Database initialized successfully")
            return True
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        connection.rollback()
        return False
    finally:
        connection.close()

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    """Serve uploaded profile pictures"""
    from flask import send_from_directory
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

def get_role_redirect(role):
    """Redirect user to role-specific page based on their role"""
    role_routes = {
        'Admin': 'dashboard',
        'Manager': 'dashboard',
        'Dispatcher': 'dashboard',
        'Technician': 'dashboard',
        'Accounts': 'dashboard',
        'IT Support': 'dashboard',
        'Employee': 'dashboard'
    }
    return role_routes.get(role, 'dashboard')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        verification_code = request.form.get('verification_code', '').strip()
        password = request.form.get('password', '')
        
        if not verification_code or not password:
            flash('Please enter both verification code and password', 'error')
            return render_template('login.html')
        
        # Validate 6-digit code format
        if not re.match(r'^\d{6}$', verification_code):
            flash('Verification code must be exactly 6 digits', 'error')
            return render_template('login.html')
        
        connection = get_db_connection()
        if connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM employees WHERE verification_code = %s",
                        (verification_code,)
                    )
                    user = cursor.fetchone()
                    
                    if user:
                        # Check if account status is Active
                        if user.get('status') != 'Active':
                            if user.get('status') == 'Pending':
                                flash('Your account is pending approval. Please wait for admin approval.', 'error')
                            elif user.get('status') == 'Suspended':
                                flash('Your account has been suspended. Please contact administrator.', 'error')
                            else:
                                flash('Your account is not active. Please contact administrator.', 'error')
                            return render_template('login.html')
                        
                        if check_password_hash(user['password'], password):
                            session.permanent = True  # Make session persist
                            session['user_id'] = user['id']
                            session['username'] = user['username']
                            session['full_name'] = user['full_name']
                            session['role'] = user.get('role', 'Employee')
                            session['profile_picture'] = user.get('profile_picture')  # Store profile picture path
                            logger.info(f"User {user['username']} (Role: {session['role']}) logged in successfully")
                            flash('Login successful!', 'success')
                            
                            # Redirect based on role
                            role = session['role']
                            redirect_route = get_role_redirect(role)
                            return redirect(url_for(redirect_route))
                        else:
                            logger.warning(f"Failed login attempt for verification code: {verification_code}")
                            flash('Invalid verification code or password', 'error')
                    else:
                        logger.warning(f"Failed login attempt for verification code: {verification_code}")
                        flash('Invalid verification code or password', 'error')
            except Exception as e:
                logger.error(f"Login error: {e}")
                flash('Login error. Please try again.', 'error')
            finally:
                connection.close()
        else:
            flash('Database connection error', 'error')
    
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        email = request.form.get('email', '').strip()
        verification_code = request.form.get('verification_code', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        profile_picture = request.files.get('profile_picture')
        
        # Validation
        errors = []
        
        if not full_name:
            errors.append('Full name is required')
        
        if not phone_number:
            errors.append('Phone number is required')
        elif not re.match(r'^\+?[\d\s\-\(\)]+$', phone_number):
            errors.append('Invalid phone number format')
        
        if not email:
            errors.append('Email is required')
        elif not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            errors.append('Invalid email format')
        
        if not verification_code:
            errors.append('6-digit verification code is required')
        elif not re.match(r'^\d{6}$', verification_code):
            errors.append('Verification code must be exactly 6 digits')
        
        if not password:
            errors.append('Password is required')
        elif len(password) < 6:
            errors.append('Password must be at least 6 characters long')
        
        if password != confirm_password:
            errors.append('Passwords do not match')
        
        if errors:
            for error in errors:
                flash(error, 'error')
            return render_template('signup.html')
        
        # Generate username from email
        base_username = email.split('@')[0].lower()
        username = base_username
        
        # Check if email already exists
        connection = get_db_connection()
        if connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id FROM employees WHERE email = %s", (email,))
                    if cursor.fetchone():
                        flash('An account with this email already exists', 'error')
                        return render_template('signup.html')
                    
                    # Check if username exists, if so append number
                    counter = 1
                    while True:
                        cursor.execute("SELECT id FROM employees WHERE username = %s", (username,))
                        if not cursor.fetchone():
                            break
                        username = f"{base_username}{counter}"
                        counter += 1
                        if counter > 999:  # Safety limit
                            flash('Unable to generate unique username. Please contact support.', 'error')
                            return render_template('signup.html')
                    
                    # Handle profile picture upload
                    profile_picture_path = None
                    if profile_picture and profile_picture.filename:
                        if allowed_file(profile_picture.filename):
                            filename = secure_filename(f"{username}_{profile_picture.filename}")
                            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                            profile_picture.save(filepath)
                            profile_picture_path = f"uploads/{filename}"
                        else:
                            flash('Invalid file type. Allowed: PNG, JPG, JPEG, GIF', 'error')
                            return render_template('signup.html')
                    
                    # Hash password
                    hashed_password = generate_password_hash(password)
                    
                    # Insert new employee
                    cursor.execute("""
                        INSERT INTO employees (username, password, full_name, email, phone_number, 
                                              profile_picture, status, role, verification_code)
                        VALUES (%s, %s, %s, %s, %s, %s, 'Pending', 'Employee', %s)
                    """, (username, hashed_password, full_name, email, phone_number, 
                          profile_picture_path, verification_code))
                    
                    connection.commit()
                    logger.info(f"New employee signup: {email} (Pending approval)")
                    flash('Registration successful! Your account is pending approval. You will be notified once approved.', 'success')
                    return redirect(url_for('login'))
            except Exception as e:
                connection.rollback()
                logger.error(f"Signup error: {e}")
                flash('Registration error. Please try again.', 'error')
            finally:
                connection.close()
        else:
            flash('Database connection error', 'error')
    
    return render_template('signup.html')

@app.route('/profile')
@login_required
def profile():
    """Redirect to my-profile"""
    return redirect(url_for('my_profile'))

@app.route('/my-profile', methods=['GET', 'POST'])
@login_required
def my_profile():
    """My Profile page - view and edit own profile"""
    user_id = session.get('user_id')
    
    if request.method == 'POST':
        # Handle profile update
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        profile_picture = request.files.get('profile_picture')
        
        # Validation
        if not full_name:
            flash('Full name is required', 'error')
            return redirect(url_for('my_profile'))
        
        if not email or '@' not in email:
            flash('Valid email is required', 'error')
            return redirect(url_for('my_profile'))
        
        # Validate phone number (optional but if provided, should be valid)
        if phone_number and not re.match(r'^[\d\s\-\+\(\)]+$', phone_number):
            flash('Invalid phone number format', 'error')
            return redirect(url_for('my_profile'))
        
        # Password change validation
        if new_password:
            if not current_password:
                flash('Current password is required to change password', 'error')
                return redirect(url_for('my_profile'))
            
            if len(new_password) < 6:
                flash('New password must be at least 6 characters', 'error')
                return redirect(url_for('my_profile'))
            
            if new_password != confirm_password:
                flash('New passwords do not match', 'error')
                return redirect(url_for('my_profile'))
        
        connection = get_db_connection()
        if connection:
            try:
                with connection.cursor() as cursor:
                    # Get current user data
                    cursor.execute("SELECT * FROM employees WHERE id = %s", (user_id,))
                    user = cursor.fetchone()
                    
                    if not user:
                        flash('User not found', 'error')
                        return redirect(url_for('my_profile'))
                    
                    # Verify current password if changing password
                    if new_password:
                        if not check_password_hash(user['password'], current_password):
                            flash('Current password is incorrect', 'error')
                            return redirect(url_for('my_profile'))
                    
                    # Check if email is already taken by another user
                    cursor.execute("SELECT id FROM employees WHERE email = %s AND id != %s", (email, user_id))
                    if cursor.fetchone():
                        flash('Email is already taken by another user', 'error')
                        return redirect(url_for('my_profile'))
                    
                    # Handle profile picture upload
                    profile_picture_path = user.get('profile_picture')
                    if profile_picture and profile_picture.filename:
                        if allowed_file(profile_picture.filename):
                            # Delete old profile picture if exists
                            if profile_picture_path:
                                old_filepath = os.path.join(app.config['UPLOAD_FOLDER'], 
                                                          os.path.basename(profile_picture_path))
                                if os.path.exists(old_filepath):
                                    try:
                                        os.remove(old_filepath)
                                    except Exception as e:
                                        logger.warning(f"Could not delete old profile picture: {e}")
                            
                            # Save new profile picture
                            filename = secure_filename(f"{user['username']}_{profile_picture.filename}")
                            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                            profile_picture.save(filepath)
                            profile_picture_path = f"uploads/{filename}"
                        else:
                            flash('Invalid file type. Allowed: PNG, JPG, JPEG, GIF', 'error')
                            return redirect(url_for('my_profile'))
                    
                    # Update password if provided
                    password_hash = user['password']
                    if new_password:
                        password_hash = generate_password_hash(new_password)
                    
                    # Update employee record
                    cursor.execute("""
                        UPDATE employees 
                        SET full_name = %s, email = %s, phone_number = %s, 
                            profile_picture = %s, password = %s
                        WHERE id = %s
                    """, (full_name, email, phone_number, profile_picture_path, password_hash, user_id))
                    
                    connection.commit()
                    
                    # Update session with new data
                    session['full_name'] = full_name
                    session['profile_picture'] = profile_picture_path
                    
                    logger.info(f"User {user['username']} updated their profile")
                    flash('Profile updated successfully!', 'success')
                    return redirect(url_for('my_profile'))
                    
            except Exception as e:
                connection.rollback()
                logger.error(f"Profile update error: {e}")
                flash('Error updating profile. Please try again.', 'error')
            finally:
                connection.close()
        else:
            flash('Database connection error', 'error')
    
    # GET request - fetch and display user data
    connection = get_db_connection()
    user_data = None
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM employees WHERE id = %s", (user_id,))
                user_data = cursor.fetchone()
        except Exception as e:
            logger.error(f"Error fetching user data: {e}")
            flash('Error loading profile data', 'error')
        finally:
            connection.close()
    
    if not user_data:
        flash('User not found', 'error')
        return redirect(url_for('dashboard'))
    
    return render_template('my_profile.html', user=user_data)

@app.route('/settings')
@login_required
def settings():
    """Settings page"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support and Admin can access settings
    allowed_roles = ['IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to access settings', 'error')
        return redirect(url_for('dashboard'))
    
    return render_template('settings.html')

@app.route('/settings/company-profile', methods=['GET', 'POST'])
@login_required
def company_profile_settings():
    """Company Profile Settings page"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support and Admin can access settings
    allowed_roles = ['IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to access settings', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    company_data = None
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch company settings
                cursor.execute("SELECT * FROM company_settings ORDER BY id DESC LIMIT 1")
                company_data = cursor.fetchone()
                
                if request.method == 'POST':
                    company_name = request.form.get('company_name', '').strip()
                    
                    if not company_name:
                        flash('Company name is required', 'error')
                        return redirect(url_for('company_profile_settings'))
                    
                    # Handle logo upload
                    logo_filename = None
                    if 'company_logo' in request.files:
                        logo_file = request.files['company_logo']
                        if logo_file and logo_file.filename:
                            # Check if file is an image
                            allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'}
                            if '.' in logo_file.filename and logo_file.filename.rsplit('.', 1)[1].lower() in allowed_extensions:
                                filename = secure_filename(logo_file.filename)
                                # Add timestamp to make filename unique
                                timestamp = int(time.time())
                                filename = f"company_logo_{timestamp}_{filename}"
                                logo_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                                logo_file.save(logo_path)
                                logo_filename = filename  # Store just filename, path handled in template
                                
                                # Delete old logo if exists
                                if company_data and company_data.get('company_logo'):
                                    old_logo_path = os.path.join(app.config['UPLOAD_FOLDER'], company_data['company_logo'])
                                    if os.path.exists(old_logo_path):
                                        try:
                                            os.remove(old_logo_path)
                                        except Exception as e:
                                            logger.warning(f"Could not delete old logo: {e}")
                    
                    # Update or insert company settings
                    user_id = session.get('user_id')
                    if company_data:
                        # Update existing
                        if logo_filename:
                            cursor.execute("""
                                UPDATE company_settings 
                                SET company_name = %s, company_logo = %s, updated_by = %s
                                WHERE id = %s
                            """, (company_name, logo_filename, user_id, company_data['id']))
                        else:
                            cursor.execute("""
                                UPDATE company_settings 
                                SET company_name = %s, updated_by = %s
                                WHERE id = %s
                            """, (company_name, user_id, company_data['id']))
                    else:
                        # Insert new
                        cursor.execute("""
                            INSERT INTO company_settings 
                            (company_name, company_logo, updated_by)
                            VALUES (%s, %s, %s)
                        """, (company_name, logo_filename, user_id))
                    
                    connection.commit()
                    flash('Company settings updated successfully', 'success')
                    return redirect(url_for('company_profile_settings'))
                    
        except Exception as e:
            logger.error(f"Error in company_profile_settings: {e}")
            flash('An error occurred while processing your request', 'error')
            if connection:
                connection.rollback()
        finally:
            if connection:
                connection.close()
    
    return render_template('company_profile_settings.html', company_data=company_data)

@app.route('/settings/notifications', methods=['GET', 'POST'])
@login_required
def notification_settings():
    """Notification Settings page"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support and Admin can access settings
    allowed_roles = ['IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to access settings', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    notification_data = None
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch notification settings
                cursor.execute("SELECT * FROM notification_settings ORDER BY id DESC LIMIT 1")
                notification_data = cursor.fetchone()
                
                if request.method == 'POST':
                    sound_enabled = request.form.get('sound_enabled') == 'on'
                    notification_sound = request.form.get('notification_sound', 'default').strip()
                    volume = int(request.form.get('volume', 50))
                    email_new_client = request.form.get('email_new_client') == 'on'
                    email_payment = request.form.get('email_payment') == 'on'
                    email_status_change = request.form.get('email_status_change') == 'on'
                    system_alerts = request.form.get('system_alerts') == 'on'
                    browser_notifications = request.form.get('browser_notifications') == 'on'
                    
                    # Get day-based notification settings
                    relocation_days = int(request.form.get('relocation_days', 30))
                    renewal_days = int(request.form.get('renewal_days', 30))
                    closing_days = int(request.form.get('closing_days', 30))
                    
                    # Validate inputs
                    volume = max(0, min(100, volume))
                    relocation_days = max(0, min(365, relocation_days))
                    renewal_days = max(0, min(365, renewal_days))
                    closing_days = max(0, min(365, closing_days))
                    
                    user_id = session.get('user_id')
                    if notification_data:
                        # Update existing
                        cursor.execute("""
                            UPDATE notification_settings 
                            SET sound_enabled = %s, notification_sound = %s, volume = %s,
                                email_new_client = %s, email_payment = %s, email_status_change = %s,
                                system_alerts = %s, browser_notifications = %s, 
                                relocation_days = %s, renewal_days = %s, closing_days = %s,
                                updated_by = %s
                            WHERE id = %s
                        """, (sound_enabled, notification_sound, volume, email_new_client, 
                              email_payment, email_status_change, system_alerts, 
                              browser_notifications, relocation_days, renewal_days, closing_days,
                              user_id, notification_data['id']))
                    else:
                        # Insert new
                        cursor.execute("""
                            INSERT INTO notification_settings 
                            (sound_enabled, notification_sound, volume, email_new_client, email_payment,
                             email_status_change, system_alerts, browser_notifications, 
                             relocation_days, renewal_days, closing_days, updated_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (sound_enabled, notification_sound, volume, email_new_client,
                              email_payment, email_status_change, system_alerts,
                              browser_notifications, relocation_days, renewal_days, closing_days, user_id))
                    
                    connection.commit()
                    flash('Notification settings updated successfully', 'success')
                    return redirect(url_for('notification_settings'))
                    
        except Exception as e:
            logger.error(f"Error in notification_settings: {e}")
            flash('An error occurred while processing your request', 'error')
            if connection:
                connection.rollback()
        finally:
            if connection:
                connection.close()
    
    return render_template('notification_settings.html', notification_data=notification_data)

@app.route('/notifications')
@login_required
def notifications():
    """Notifications page - Display user notifications"""
    from datetime import datetime, date
    
    connection = get_db_connection()
    notifications_list = []
    notification_settings = None
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch notification settings
                cursor.execute("SELECT relocation_days, renewal_days, closing_days FROM notification_settings ORDER BY id DESC LIMIT 1")
                notification_settings = cursor.fetchone()
                
                # Get default values if no settings exist
                relocation_days = notification_settings.get('relocation_days', 30) if notification_settings else 30
                renewal_days = notification_settings.get('renewal_days', 30) if notification_settings else 30
                closing_days = notification_settings.get('closing_days', 30) if notification_settings else 30
                
                # Fetch all clients
                cursor.execute("""
                    SELECT id, full_name, account_number, phone_number, status, created_at, package, client_category
                    FROM clients
                    ORDER BY created_at DESC
                """)
                clients = cursor.fetchall()
                
                # Calculate days from registration and check if they match notification thresholds
                today = date.today()
                
                for client in clients:
                    if client['created_at']:
                        # Convert datetime to date if needed
                        if isinstance(client['created_at'], datetime):
                            registration_date = client['created_at'].date()
                        else:
                            registration_date = client['created_at']
                        
                        # Calculate days from registration
                        days_diff = (today - registration_date).days
                        
                        # Check for relocation notification
                        if days_diff >= relocation_days:
                            notifications_list.append({
                                'type': 'relocation',
                                'icon': 'truck-moving',
                                'color': 'blue',
                                'title': 'Relocation Notification',
                                'message': f"Client {client['full_name']} (Account: {client['account_number']}) has been registered for {days_diff} days. Consider relocation.",
                                'client_id': client['id'],
                                'client_name': client['full_name'],
                                'account_number': client['account_number'],
                                'phone_number': client['phone_number'],
                                'client_category': client.get('client_category', 'Actual'),
                                'status': client['status'],
                                'days': days_diff,
                                'registration_date': registration_date,
                                'package': client.get('package', 'N/A')
                            })
                        
                        # Check for renewal notification
                        if days_diff >= renewal_days:
                            notifications_list.append({
                                'type': 'renewal',
                                'icon': 'sync-alt',
                                'color': 'yellow',
                                'title': 'Renewal Notification',
                                'message': f"Client {client['full_name']} (Account: {client['account_number']}) has been registered for {days_diff} days. Consider renewal.",
                                'client_id': client['id'],
                                'client_name': client['full_name'],
                                'account_number': client['account_number'],
                                'phone_number': client['phone_number'],
                                'client_category': client.get('client_category', 'Actual'),
                                'status': client['status'],
                                'days': days_diff,
                                'registration_date': registration_date,
                                'package': client.get('package', 'N/A')
                            })
                        
                        # Check for closing notification
                        if days_diff >= closing_days:
                            notifications_list.append({
                                'type': 'closing',
                                'icon': 'times-circle',
                                'color': 'red',
                                'title': 'Closing Notification',
                                'message': f"Client {client['full_name']} (Account: {client['account_number']}) has been registered for {days_diff} days. Consider closing.",
                                'client_id': client['id'],
                                'client_name': client['full_name'],
                                'account_number': client['account_number'],
                                'phone_number': client['phone_number'],
                                'client_category': client.get('client_category', 'Actual'),
                                'status': client['status'],
                                'days': days_diff,
                                'registration_date': registration_date,
                                'package': client.get('package', 'N/A')
                            })
                
                # Sort notifications by days (most urgent first)
                notifications_list.sort(key=lambda x: x['days'], reverse=True)
                
                # Calculate statistics
                stats = {
                    'total': len(notifications_list),
                    'relocation': len([n for n in notifications_list if n['type'] == 'relocation']),
                    'renewal': len([n for n in notifications_list if n['type'] == 'renewal']),
                    'closing': len([n for n in notifications_list if n['type'] == 'closing'])
                }
                
        except Exception as e:
            logger.error(f"Error in notifications: {e}")
            flash('An error occurred while loading notifications', 'error')
            stats = {'total': 0, 'relocation': 0, 'renewal': 0, 'closing': 0}
        finally:
            if connection:
                connection.close()
    else:
        stats = {'total': 0, 'relocation': 0, 'renewal': 0, 'closing': 0}
    
    return render_template('notifications.html', notifications=notifications_list, stats=stats)

@app.route('/settings/reminders')
@login_required
def reminder_settings():
    """Reminder Settings page"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support and Admin can access settings
    allowed_roles = ['IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to access settings', 'error')
        return redirect(url_for('dashboard'))
    
    return render_template('reminder_settings.html')

@app.route('/settings/finance', methods=['GET', 'POST'])
@login_required
def finance_settings():
    """Finance Settings page"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support and Admin can access settings
    allowed_roles = ['IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to access settings', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    packages = []
    assets_settings = None
    technical_settings = None
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all unique packages from clients table
                cursor.execute("SELECT DISTINCT package FROM clients WHERE package IS NOT NULL AND package != '' ORDER BY package")
                existing_packages = cursor.fetchall()
                
                # Fetch all packages with prices
                cursor.execute("SELECT * FROM packages ORDER BY package_name")
                packages_with_prices = cursor.fetchall()
                
                # Create a dictionary of packages with prices
                packages_dict = {pkg['package_name']: pkg for pkg in packages_with_prices}
                
                # Combine existing packages from clients with packages table
                all_package_names = set()
                for pkg in existing_packages:
                    if pkg['package']:
                        all_package_names.add(pkg['package'])
                for pkg in packages_with_prices:
                    all_package_names.add(pkg['package_name'])
                
                # Build packages list
                for pkg_name in sorted(all_package_names):
                    if pkg_name in packages_dict:
                        packages.append({
                            'name': pkg_name,
                            'price': float(packages_dict[pkg_name]['sale_price']),
                            'id': packages_dict[pkg_name]['id'],
                            'is_active': packages_dict[pkg_name]['is_active']
                        })
                    else:
                        packages.append({
                            'name': pkg_name,
                            'price': 0.00,
                            'id': None,
                            'is_active': True
                        })
                
                # Fetch assets settings
                cursor.execute("SELECT * FROM assets_settings ORDER BY id DESC LIMIT 1")
                assets_settings = cursor.fetchone()
                
                # Fetch technical settings
                cursor.execute("SELECT * FROM technical_settings ORDER BY id DESC LIMIT 1")
                technical_settings = cursor.fetchone()
                
                # Convert Decimal to float for template rendering
                if technical_settings and 'price_per_ticket' in technical_settings:
                    if hasattr(technical_settings['price_per_ticket'], '__float__'):
                        technical_settings['price_per_ticket'] = float(technical_settings['price_per_ticket'])
                
                # Convert Decimal to float for assets_settings template rendering
                if assets_settings:
                    if 'default_asset_price' in assets_settings and hasattr(assets_settings['default_asset_price'], '__float__'):
                        assets_settings['default_asset_price'] = float(assets_settings['default_asset_price'])
                    if 'asset_depreciation_rate' in assets_settings and hasattr(assets_settings['asset_depreciation_rate'], '__float__'):
                        assets_settings['asset_depreciation_rate'] = float(assets_settings['asset_depreciation_rate'])
                
                if request.method == 'POST':
                    # Handle package pricing updates
                    package_prices = request.form.getlist('package_price')
                    package_names = request.form.getlist('package_name')
                    package_ids = request.form.getlist('package_id')
                    
                    user_id = session.get('user_id')
                    
                    # Get new package names from input fields
                    new_package_names = request.form.getlist('package_name_input')
                    
                    # Get all package prices (submitted in order: existing packages first, then new ones)
                    all_package_prices = request.form.getlist('package_price')
                    
                    # Process existing packages (from hidden package_name fields)
                    for i, pkg_name in enumerate(package_names):
                        if pkg_name and pkg_name.strip():
                            try:
                                price = float(all_package_prices[i]) if i < len(all_package_prices) else 0.00
                                pkg_id = int(package_ids[i]) if i < len(package_ids) and package_ids[i] and package_ids[i].strip() else None
                                
                                if pkg_id:
                                    # Update existing package
                                    cursor.execute("""
                                        UPDATE packages 
                                        SET sale_price = %s, updated_at = CURRENT_TIMESTAMP
                                        WHERE id = %s
                                    """, (price, pkg_id))
                                else:
                                    # Insert new package (from existing clients)
                                    cursor.execute("""
                                        INSERT INTO packages (package_name, sale_price, is_active)
                                        VALUES (%s, %s, TRUE)
                                        ON DUPLICATE KEY UPDATE sale_price = %s, updated_at = CURRENT_TIMESTAMP
                                    """, (pkg_name.strip(), price, price))
                            except (ValueError, IndexError) as e:
                                logger.warning(f"Error processing package {pkg_name}: {e}")
                                continue
                    
                    # Process new packages from input fields
                    # New packages come after existing ones in the form, so prices start from len(package_names)
                    existing_count = len(package_names)
                    for i, new_pkg_name in enumerate(new_package_names):
                        if new_pkg_name and new_pkg_name.strip():
                            try:
                                # Find corresponding price - new packages are after existing ones
                                price_index = existing_count + i
                                price = float(all_package_prices[price_index]) if price_index < len(all_package_prices) else 0.00
                                
                                # Insert new package
                                cursor.execute("""
                                    INSERT INTO packages (package_name, sale_price, is_active)
                                    VALUES (%s, %s, TRUE)
                                    ON DUPLICATE KEY UPDATE sale_price = %s, updated_at = CURRENT_TIMESTAMP
                                """, (new_pkg_name.strip(), price, price))
                            except (ValueError, IndexError) as e:
                                logger.warning(f"Error processing new package {new_pkg_name}: {e}")
                                continue
                    
                    # Handle assets settings
                    try:
                        default_asset_price = float(request.form.get('default_asset_price', 0) or 0)
                        asset_depreciation_rate = float(request.form.get('asset_depreciation_rate', 0) or 0)
                        auto_assign_assets = request.form.get('auto_assign_assets') == 'on'
                        require_asset_approval = request.form.get('require_asset_approval') == 'on'
                        
                        # Validate depreciation rate
                        asset_depreciation_rate = max(0, min(100, asset_depreciation_rate))
                        
                        if assets_settings:
                            # Update existing
                            cursor.execute("""
                                UPDATE assets_settings 
                                SET default_asset_price = %s, asset_depreciation_rate = %s,
                                    auto_assign_assets = %s, require_asset_approval = %s,
                                    updated_by = %s, updated_at = CURRENT_TIMESTAMP
                                WHERE id = %s
                            """, (default_asset_price, asset_depreciation_rate, auto_assign_assets,
                                  require_asset_approval, user_id, assets_settings['id']))
                            logger.info(f"Updated assets_settings ID {assets_settings['id']}")
                        else:
                            # Insert new
                            cursor.execute("""
                                INSERT INTO assets_settings 
                                (default_asset_price, asset_depreciation_rate, auto_assign_assets, 
                                 require_asset_approval, updated_by)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (default_asset_price, asset_depreciation_rate, auto_assign_assets,
                                  require_asset_approval, user_id))
                            logger.info("Inserted new assets_settings")
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error processing assets settings: {e}")
                        flash('Invalid assets settings values', 'error')
                        return redirect(url_for('finance_settings'))
                    except Exception as e:
                        logger.error(f"Unexpected error saving assets settings: {e}")
                        flash('Error saving assets settings', 'error')
                        return redirect(url_for('finance_settings'))
                    
                    # Handle technical settings - Price Per Ticket
                    try:
                        price_per_ticket_str = request.form.get('price_per_ticket', '0').strip()
                        price_per_ticket = float(price_per_ticket_str) if price_per_ticket_str else 0.00
                        logger.info(f"Processing price_per_ticket: {price_per_ticket}, technical_settings exists: {technical_settings is not None}")
                        
                        if technical_settings:
                            # Update existing
                            cursor.execute("""
                                UPDATE technical_settings 
                                SET price_per_ticket = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP
                                WHERE id = %s
                            """, (price_per_ticket, user_id, technical_settings['id']))
                            logger.info(f"Successfully updated technical_settings ID {technical_settings['id']} with price_per_ticket: {price_per_ticket}")
                        else:
                            # Insert new
                            cursor.execute("""
                                INSERT INTO technical_settings 
                                (price_per_ticket, updated_by)
                                VALUES (%s, %s)
                            """, (price_per_ticket, user_id))
                            logger.info(f"Successfully inserted new technical_settings with price_per_ticket: {price_per_ticket}")
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error processing price_per_ticket: {e}")
                        flash('Invalid price per ticket value', 'error')
                        return redirect(url_for('finance_settings'))
                    except Exception as e:
                        logger.error(f"Unexpected error saving price_per_ticket: {e}")
                        flash('Error saving price per ticket', 'error')
                        return redirect(url_for('finance_settings'))
                    
                    # Commit all changes
                    connection.commit()
                    logger.info("All finance settings committed to database successfully")
                    flash('Finance settings updated successfully', 'success')
                    return redirect(url_for('finance_settings'))
                    
        except Exception as e:
            logger.error(f"Error in finance_settings: {e}")
            flash('An error occurred while processing your request', 'error')
            if connection:
                connection.rollback()
        finally:
            if connection:
                connection.close()
    
    return render_template('finance_settings.html', packages=packages, assets_settings=assets_settings, technical_settings=technical_settings)

@app.route('/logout')
def logout():
    username = session.get('username', 'Unknown')
    session.clear()
    logger.info(f"User {username} logged out")
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

@app.route('/switch-role/<role>')
@login_required
def switch_role(role):
    """Switch role for IT Support employees"""
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support can switch roles
    if actual_role != 'IT Support':
        flash('You do not have permission to switch roles', 'error')
        return redirect(url_for('dashboard'))
    
    # Validate the role to switch to
    valid_roles = get_role_options()
    if role not in valid_roles:
        flash('Invalid role selected', 'error')
        return redirect(url_for('dashboard'))
    
    # Store the switched role
    session['switched_role'] = role
    logger.info(f"IT Support user {session.get('username')} switched to role: {role}")
    flash(f'Switched to {role} view', 'success')
    return redirect(url_for('dashboard'))

@app.route('/clear-role-switch')
@login_required
def clear_role_switch():
    """Clear role switch and return to IT Support view"""
    actual_role = session.get('role', 'Employee')
    
    # Only IT Support can clear role switch
    if actual_role != 'IT Support':
        flash('You do not have permission', 'error')
        return redirect(url_for('dashboard'))
    
    if 'switched_role' in session:
        switched_role = session.pop('switched_role')
        logger.info(f"IT Support user {session.get('username')} cleared role switch (was: {switched_role})")
        flash('Returned to IT Support view', 'success')
    return redirect(url_for('dashboard'))

@app.route('/connect-client/<int:client_id>', methods=['GET', 'POST'])
@login_required
def connect_client(client_id):
    """Connect a pending client - Technician, Employee, and IT Support"""
    effective_role = get_effective_role()
    
    # Technician, Employee, and IT Support (or IT Support viewing as these roles) can connect clients
    if effective_role not in ['Technician', 'Employee', 'IT Support']:
        flash('You do not have permission to connect clients', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        with connection.cursor() as cursor:
            # Get client details
            cursor.execute("SELECT * FROM clients WHERE id = %s AND status = 'Pending'", (client_id,))
            client = cursor.fetchone()
            
            if not client:
                flash('Client not found or already connected', 'error')
                return redirect(url_for('dashboard'))
            
            # Handle POST request (form submission)
            if request.method == 'POST':
                serial_number = normalize_serial_number(request.form.get('serial_number', ''))
                power_levels = request.form.get('power_levels', '').strip()
                router_type = request.form.get('router_type', '').strip()
                router_name = request.form.get('router_name', '').strip()
                router_password = request.form.get('router_password', '').strip()
                port_number = request.form.get('port_number', '').strip()
                
                # Validation
                if not serial_number:
                    flash('Serial number is required', 'error')
                    return render_template('connect_client.html', client=client)
                if not router_type:
                    flash('Router type is required', 'error')
                    return render_template('connect_client.html', client=client)
                if not router_name:
                    flash('Router name is required', 'error')
                    return render_template('connect_client.html', client=client)
                if not router_password:
                    flash('Router password is required', 'error')
                    return render_template('connect_client.html', client=client)
                
                # Get ground location from form (user can edit; pre-filled from client)
                ground_location = request.form.get('ground_location', '').strip() or client.get('ground_location', '')
                
                # Get technician/employee ID from session (who is connecting the client)
                technician_id = session.get('user_id')
                
                # Validate that user_id exists in session
                if not technician_id:
                    flash('Session error. Please log in again.', 'error')
                    logger.error("Attempted to connect client without user_id in session")
                    return redirect(url_for('login'))
                
                # Insert connection record with technician/employee ID for reference
                cursor.execute("""
                    INSERT INTO client_connections 
                    (client_id, technician_id, serial_number, power_levels, router_used, router_name, router_password, ground_location, port_number, connection_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURDATE())
                """, (client_id, technician_id, serial_number, power_levels, router_name, router_name, router_password, ground_location, port_number))
                
                # Insert new asset for new registration
                cursor.execute("""
                    INSERT INTO assets 
                    (asset_name, asset_type, serial_number, status, assigned_to, client_id, 
                     location, power_levels, router_used, router_name, router_password, port_number)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (f"{router_type} - {client.get('account_number', 'N/A')}", router_type, serial_number, 'In Use', 
                      technician_id, client_id, ground_location, power_levels, router_name, router_name, router_password, port_number))
                
                # Update client status to Connected and ground_location if user edited it
                cursor.execute("""
                    UPDATE clients 
                    SET status = 'Connected', ground_location = %s
                    WHERE id = %s
                """, (ground_location, client_id))
                
                connection.commit()
                logger.info(f"Client {client_id} connected by technician {technician_id} (Serial: {serial_number})")
                flash(f'Client {client["account_number"]} connected successfully!', 'success')
                return redirect(url_for('dashboard'))
            
            # Handle GET request (show form)
            return render_template('connect_client.html', client=client)
            
    except Exception as e:
        connection.rollback()
        logger.error(f"Client connection error: {e}")
        flash('Error processing request. Please try again.', 'error')
        return redirect(url_for('dashboard'))
    finally:
        connection.close()

@app.route('/dashboard')
@login_required
def dashboard():
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    user_id = session.get('user_id')
    
    connection = get_db_connection()
    stats = {}
    role_data = {}
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Common stats for all roles
                cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Pending'")
                stats['pending_clients'] = cursor.fetchone()['count']
                
                # Get ALL pending clients (no limit)
                cursor.execute("""
                    SELECT * FROM clients 
                    WHERE status = 'Pending'
                    ORDER BY created_at DESC
                """)
                role_data['pending_clients'] = cursor.fetchall()
                
                # Get all connections with status Connected, Renewed, or Relocated with days from registration
                cursor.execute("""
                    SELECT c.*, 
                           DATEDIFF(CURDATE(), DATE(c.created_at)) as days_from_registration
                    FROM clients c
                    WHERE c.status IN ('Connected', 'Renewed', 'Relocated')
                    ORDER BY c.created_at DESC
                """)
                role_data['connections'] = cursor.fetchall()
                
                # Role-specific statistics
                if effective_role in ['Admin', 'IT Support']:
                    # Admin/IT Support - Full system overview
                    cursor.execute("SELECT COUNT(*) as count FROM assets")
                    stats['total_assets'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM assets WHERE status = 'Available'")
                    stats['available_assets'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM assets WHERE assigned_to IS NOT NULL")
                    stats['assigned_assets'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM employees")
                    stats['total_employees'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM clients")
                    stats['total_clients'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Connected'")
                    stats['connected_clients'] = cursor.fetchone()['count']
                    
                    # Finance stats
                    cursor.execute("""
                        SELECT COALESCE(SUM(p.sale_price), 0) as total
                        FROM clients c
                        LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                    """)
                    result = cursor.fetchone()
                    stats['total_sales'] = float(result['total']) if result and result['total'] else 0.00
                    
                elif effective_role == 'Accounts':
                    # Accounts - Financial focus
                    cursor.execute("SELECT COUNT(*) as count FROM clients")
                    stats['total_clients'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Connected'")
                    stats['connected_clients'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Renewed'")
                    stats['renewed_clients'] = cursor.fetchone()['count']
                    
                    # Finance stats
                    cursor.execute("""
                        SELECT COALESCE(SUM(p.sale_price), 0) as total
                        FROM clients c
                        LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                    """)
                    result = cursor.fetchone()
                    stats['total_sales'] = float(result['total']) if result and result['total'] else 0.00
                    
                    cursor.execute("""
                        SELECT COALESCE(SUM(renewal_amount), 0) as total
                        FROM client_renewals
                    """)
                    result = cursor.fetchone()
                    stats['total_renewals'] = float(result['total']) if result and result['total'] else 0.00
                    
                    # Recent payments
                    cursor.execute("""
                        SELECT c.*, p.sale_price
                        FROM clients c
                        LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                        WHERE c.payment_date IS NOT NULL
                        ORDER BY c.payment_date DESC
                        LIMIT 10
                    """)
                    role_data['recent_payments'] = cursor.fetchall()
                    
                elif effective_role == 'Dispatcher':
                    # Dispatcher - Connected accounts and assets
                    cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Connected'")
                    stats['connected_clients'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM assets WHERE status = 'Available'")
                    stats['available_assets'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM assets WHERE assigned_to IS NOT NULL")
                    stats['assigned_assets'] = cursor.fetchone()['count']
                    
                    # Recent connected accounts
                    cursor.execute("""
                        SELECT c.*, cc.connection_date, e.full_name as technician_name
                        FROM clients c
                        LEFT JOIN client_connections cc ON c.id = cc.client_id
                        LEFT JOIN employees e ON cc.technician_id = e.id
                        WHERE c.status = 'Connected'
                        ORDER BY cc.connection_date DESC
                        LIMIT 10
                    """)
                    role_data['recent_connections'] = cursor.fetchall()
                    
                elif effective_role == 'Technician':
                    # Technician - Assigned clients and tasks
                    cursor.execute("""
                        SELECT COUNT(*) as count 
                        FROM clients c
                        LEFT JOIN client_connections cc ON c.id = cc.client_id
                        WHERE cc.technician_id = %s
                    """, (user_id,))
                    stats['my_connected_clients'] = cursor.fetchone()['count']
                    
                    cursor.execute("""
                        SELECT COUNT(*) as count 
                        FROM clients 
                        WHERE status = 'Pending'
                    """)
                    stats['pending_clients'] = cursor.fetchone()['count']
                    
                    # My recent connections
                    cursor.execute("""
                        SELECT c.*, cc.connection_date
                        FROM clients c
                        LEFT JOIN client_connections cc ON c.id = cc.client_id
                        WHERE cc.technician_id = %s
                        ORDER BY cc.connection_date DESC
                        LIMIT 10
                    """, (user_id,))
                    role_data['my_recent_connections'] = cursor.fetchall()
                    
                elif effective_role == 'Manager':
                    # Manager - Overview stats
                    cursor.execute("SELECT COUNT(*) as count FROM clients")
                    stats['total_clients'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Connected'")
                    stats['connected_clients'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM employees")
                    stats['total_employees'] = cursor.fetchone()['count']
                    cursor.execute("SELECT COUNT(*) as count FROM assets")
                    stats['total_assets'] = cursor.fetchone()['count']
                    
                else:  # Employee or other roles
                    # Employee - Basic stats
                    cursor.execute("SELECT COUNT(*) as count FROM clients WHERE status = 'Pending'")
                    stats['pending_clients'] = cursor.fetchone()['count']
                    
        except Exception as e:
            logger.error(f"Stats error: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            connection.close()
    
    return render_template('dashboard.html', 
                         stats=stats, 
                         role_data=role_data,
                         effective_role=effective_role,
                         actual_role=actual_role)

@app.route('/assets')
@login_required
def assets():
    connection = get_db_connection()
    assets_list = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT a.*, e.full_name as assigned_employee_name,
                           c.account_number as client_account_number, c.full_name as client_full_name
                    FROM assets a
                    LEFT JOIN employees e ON a.assigned_to = e.id
                    LEFT JOIN clients c ON a.client_id = c.id
                    ORDER BY a.created_at DESC
                """)
                assets_list = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching assets: {e}")
            flash('Error loading assets', 'error')
        finally:
            connection.close()
    
    last_serial = (assets_list[0].get('serial_number') or '') if assets_list else ''
    return render_template('assets.html', assets=assets_list, last_serial=last_serial)

# Serial lookup (barcode scan confirmation) - used by Assets page
@app.route('/api/serial-lookup', methods=['GET'])
@login_required
def api_serial_lookup():
    """
    Lookup a serial number in the assets table and return whether it's in use,
    and (if applicable) which client it's associated with.
    """
    raw = request.args.get('serial', '')
    serial = normalize_serial_number(raw)
    if not serial:
        return jsonify({'ok': False, 'error': 'serial is required'}), 400

    connection = get_db_connection()
    if not connection:
        return jsonify({'ok': False, 'error': 'database connection error'}), 500

    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT a.id,
                       a.serial_number,
                       a.status,
                       a.client_id,
                       a.asset_type,
                       a.asset_name,
                       a.location,
                       c.account_number,
                       c.full_name AS client_name,
                       c.phone_number
                FROM assets a
                LEFT JOIN clients c ON a.client_id = c.id
                WHERE a.serial_number = %s
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT 1
            """, (serial,))
            row = cursor.fetchone()

            if not row:
                return jsonify({
                    'ok': True,
                    'serial': serial,
                    'exists': False,
                    'in_use': False
                })

            in_use = bool(row.get('client_id')) and (row.get('status') not in (None, '', 'Available'))
            client = None
            if row.get('client_id'):
                client = {
                    'id': row.get('client_id'),
                    'account_number': row.get('account_number'),
                    'full_name': row.get('client_name'),
                    'phone_number': row.get('phone_number'),
                }

            return jsonify({
                'ok': True,
                'serial': row.get('serial_number') or serial,
                'exists': True,
                'in_use': in_use,
                'asset': {
                    'id': row.get('id'),
                    'status': row.get('status'),
                    'asset_type': row.get('asset_type'),
                    'asset_name': row.get('asset_name'),
                    'location': row.get('location'),
                },
                'client': client
            })
    except Exception as e:
        logger.error(f"Error in api_serial_lookup: {e}")
        return jsonify({'ok': False, 'error': 'lookup failed'}), 500
    finally:
        connection.close()

# Add Asset route removed - assets are now created automatically when clients are connected

@app.route('/employees')
@login_required
def employees():
    connection = get_db_connection()
    employees_list = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT e.*, COUNT(a.id) as asset_count
                    FROM employees e
                    LEFT JOIN assets a ON e.id = a.assigned_to
                    GROUP BY e.id
                    ORDER BY e.created_at DESC
                """)
                employees_list = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching employees: {e}")
            flash('Error loading employees', 'error')
        finally:
            connection.close()
    
    return render_template('employees.html', 
                          employees=employees_list,
                          status_options=get_status_options(),
                          role_options=get_role_options())

@app.route('/update-employee/<int:employee_id>', methods=['POST'])
@login_required
def update_employee(employee_id):
    """Update employee status and role"""
    # Check if user has permission (Admin or Manager only, or IT Support with switched role)
    effective_role = get_effective_role()
    if effective_role not in ['Admin', 'Manager']:
        flash('You do not have permission to update employees', 'error')
        return redirect(url_for('employees'))
    
    status = request.form.get('status')
    role = request.form.get('role')
    
    # Validate status and role
    if status not in get_status_options():
        flash('Invalid status', 'error')
        return redirect(url_for('employees'))
    
    if role not in get_role_options():
        flash('Invalid role', 'error')
        return redirect(url_for('employees'))
    
    connection = get_db_connection()
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE employees 
                    SET status = %s, role = %s 
                    WHERE id = %s
                """, (status, role, employee_id))
                connection.commit()
                logger.info(f"Employee {employee_id} updated: Status={status}, Role={role} by {session.get('username')}")
                flash('Employee updated successfully!', 'success')
        except Exception as e:
            connection.rollback()
            logger.error(f"Error updating employee: {e}")
            flash('Error updating employee', 'error')
        finally:
            connection.close()
    else:
        flash('Database connection error', 'error')
    
    return redirect(url_for('employees'))

@app.route('/check-phone-number', methods=['GET'])
@login_required
def check_phone_number():
    """Check how many times a phone number has been used"""
    phone_number = request.args.get('phone_number', '').strip()
    
    if not phone_number:
        return jsonify({'count': 0, 'used': False})
    
    connection = get_db_connection()
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) as count FROM clients WHERE phone_number = %s", (phone_number,))
                result = cursor.fetchone()
                count = result['count'] if result else 0
                return jsonify({'count': count, 'used': count > 0})
        except Exception as e:
            logger.error(f"Error checking phone number: {e}")
            return jsonify({'count': 0, 'used': False, 'error': str(e)})
        finally:
            connection.close()
    
    return jsonify({'count': 0, 'used': False})

@app.route('/client-registration', methods=['GET', 'POST'])
@login_required
def client_registration():
    """Client Registration page"""
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        account_number = request.form.get('account_number', '').strip()
        package = request.form.get('package', '').strip()
        work_order = request.form.get('work_order', '').strip()
        client_category = request.form.get('client_category', 'Actual').strip()
        virtual_location = request.form.get('virtual_location', '').strip()
        ground_location = request.form.get('ground_location', '').strip()
        payment_date = request.form.get('payment_date', '').strip()
        
        # Validation
        if not full_name:
            flash('Client full name is required', 'error')
            return render_template('client_registration.html')
        
        if not phone_number:
            flash('Phone number is required', 'error')
            return render_template('client_registration.html')
        
        if not account_number:
            flash('Account number is required', 'error')
            return render_template('client_registration.html')
        
        if not payment_date:
            flash('Payment date is required', 'error')
            return render_template('client_registration.html')

        if client_category not in ('Actual', 'Virtual'):
            flash('Please select a valid client category', 'error')
            return render_template('client_registration.html')

        # Location validation based on category
        if client_category == 'Actual':
            if not ground_location:
                flash('Ground location is required for Actual clients', 'error')
                return render_template('client_registration.html')
            # For Actual clients, ignore any submitted virtual location
            virtual_location = ''
        else:  # Virtual
            if not virtual_location or not ground_location:
                flash('Both virtual and ground locations are required for Virtual clients', 'error')
                return render_template('client_registration.html')
        
        # Validate phone number format
        if not re.match(r'^[\d\s\-\+\(\)]+$', phone_number):
            flash('Invalid phone number format', 'error')
            return render_template('client_registration.html')
        
        connection = get_db_connection()
        if connection:
            try:
                with connection.cursor() as cursor:
                    # Check if account number already exists
                    cursor.execute("SELECT id FROM clients WHERE account_number = %s", (account_number,))
                    if cursor.fetchone():
                        flash('An account with this account number already exists', 'error')
                        return render_template('client_registration.html')
                    
                    # Insert new client with status set to 'Pending'
                    cursor.execute("""
                        INSERT INTO clients (full_name, phone_number, account_number, package, work_order, client_category,
                                           virtual_location, ground_location, payment_date, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Pending')
                    """, (full_name, phone_number, account_number, package, work_order or None, client_category,
                          virtual_location, ground_location, payment_date))
                    
                    connection.commit()
                    logger.info(f"New client registered: {full_name} (Account: {account_number}) by {session.get('username')}")
                    flash('Client registered successfully!', 'success')
                    return redirect(url_for('accounts'))
            except Exception as e:
                connection.rollback()
                logger.error(f"Client registration error: {e}")
                flash('Registration error. Please try again.', 'error')
            finally:
                connection.close()
        else:
            flash('Database connection error', 'error')
    
    return render_template('client_registration.html')

@app.route('/accounts')
@login_required
def accounts():
    """Accounts page - Display all pending clients"""
    connection = get_db_connection()
    pending_clients = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT * FROM clients 
                    WHERE status = 'Pending'
                    ORDER BY created_at DESC
                """)
                pending_clients = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching pending clients: {e}")
            flash('Error loading pending clients', 'error')
        finally:
            connection.close()
    
    return render_template('accounts.html', pending_clients=pending_clients)

@app.route('/all-clients')
@login_required
def all_clients():
    """All Clients page - Display all clients regardless of status"""
    connection = get_db_connection()
    all_clients_list = []
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT * FROM clients
                    ORDER BY created_at DESC
                """)
                all_clients_list = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching all clients: {e}")
            flash('Error loading clients', 'error')
        finally:
            connection.close()
    return render_template('all_clients.html', all_clients=all_clients_list)


@app.route('/edit-client/<int:client_id>', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    """Edit client details - load client and update on POST."""
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('all_clients'))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
            client = cursor.fetchone()
            if not client:
                flash('Client not found', 'error')
                return redirect(url_for('all_clients'))
            if request.method == 'POST':
                full_name = request.form.get('full_name', '').strip()
                phone_number = request.form.get('phone_number', '').strip()
                account_number = request.form.get('account_number', '').strip()
                package = request.form.get('package', '').strip()
                work_order = request.form.get('work_order', '').strip()
                client_category = request.form.get('client_category', 'Actual').strip()
                virtual_location = request.form.get('virtual_location', '').strip()
                ground_location = request.form.get('ground_location', '').strip()
                payment_date = request.form.get('payment_date', '').strip()
                status = request.form.get('status', '').strip() or client.get('status', 'Pending')
                if not full_name:
                    flash('Full name is required', 'error')
                    return render_template('edit_client.html', client=client)
                if not phone_number:
                    flash('Phone number is required', 'error')
                    return render_template('edit_client.html', client=client)
                if not account_number:
                    flash('Account number is required', 'error')
                    return render_template('edit_client.html', client=client)
                if not payment_date:
                    flash('Payment date is required', 'error')
                    return render_template('edit_client.html', client=client)
                if client_category not in ('Actual', 'Virtual'):
                    client_category = client.get('client_category', 'Actual')
                if client_category == 'Actual':
                    virtual_location = ''
                if status not in ('Pending', 'Connected', 'Relocated', 'Reversed', 'Renewed', 'Closed'):
                    status = client.get('status', 'Pending')
                cursor.execute(
                    "SELECT id FROM clients WHERE account_number = %s AND id != %s",
                    (account_number, client_id)
                )
                if cursor.fetchone():
                    flash('Another client already has this account number', 'error')
                    return render_template('edit_client.html', client=client)
                cursor.execute("""
                    UPDATE clients SET
                        full_name = %s, phone_number = %s, account_number = %s,
                        package = %s, work_order = %s, client_category = %s,
                        virtual_location = %s, ground_location = %s, payment_date = %s, status = %s
                    WHERE id = %s
                """, (full_name, phone_number, account_number, package, work_order or None,
                      client_category, virtual_location or None, ground_location or None, payment_date, status, client_id))
                connection.commit()
                flash('Client updated successfully', 'success')
                return redirect(url_for('all_clients'))
        return render_template('edit_client.html', client=client)
    except Exception as e:
        connection.rollback()
        logger.error(f"Error editing client: {e}")
        flash('Error updating client. Please try again.', 'error')
        return redirect(url_for('all_clients'))
    finally:
        connection.close()


@app.route('/delete-client/<int:client_id>', methods=['POST'])
@login_required
def delete_client(client_id):
    """Delete a client and related records (cascade)."""
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('all_clients'))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, account_number, full_name FROM clients WHERE id = %s", (client_id,))
            row = cursor.fetchone()
            if not row:
                flash('Client not found', 'error')
                return redirect(url_for('all_clients'))
            cursor.execute("DELETE FROM clients WHERE id = %s", (client_id,))
            connection.commit()
            flash(f'Client {row.get("account_number", "")} ({row.get("full_name", "")}) deleted.', 'success')
    except Exception as e:
        connection.rollback()
        logger.error(f"Error deleting client: {e}")
        flash('Error deleting client. Please try again.', 'error')
    finally:
        connection.close()
    return redirect(url_for('all_clients'))


@app.route('/connected-clients')
@login_required
def connected_clients():
    """Connected Clients page - Display all clients except Closed status with days in system"""
    effective_role = get_effective_role()
    
    # Only IT Support and Admin can view connected clients
    if effective_role not in ['IT Support', 'Admin']:
        flash('You do not have permission to view connected clients', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    connected_clients = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT c.*, 
                           cc.serial_number, 
                           cc.power_levels, 
                           cc.router_used, 
                           cc.port_number, 
                           cc.connection_date,
                           e.full_name as technician_name,
                           DATEDIFF(CURDATE(), DATE(c.created_at)) as days_in_system,
                           CASE 
                               WHEN cc.connection_date IS NOT NULL THEN DATEDIFF(CURDATE(), DATE(cc.connection_date))
                               ELSE NULL
                           END as days_since_connection
                    FROM clients c
                    LEFT JOIN client_connections cc ON c.id = cc.client_id
                    LEFT JOIN employees e ON cc.technician_id = e.id
                    WHERE c.status != 'Closed'
                    ORDER BY c.created_at DESC
                """)
                connected_clients = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching connected clients: {e}")
            flash('Error loading connected clients', 'error')
        finally:
            connection.close()
    
    return render_template('connected_clients.html', connected_clients=connected_clients)

@app.route('/failed-connections')
@login_required
def failed_connections():
    """Failed Connections page - Display clients with failed connection status"""
    effective_role = get_effective_role()
    
    # Only IT Support and Admin can view failed connections
    if effective_role not in ['IT Support', 'Admin']:
        flash('You do not have permission to view failed connections', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    failed_clients = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # For now, we'll check for clients with status 'Closed' or any other status that might indicate failure
                # You can adjust this query based on your business logic for what constitutes a "failed connection"
                cursor.execute("""
                    SELECT * FROM clients 
                    WHERE status IN ('Closed', 'Reversed')
                    ORDER BY updated_at DESC, created_at DESC
                """)
                failed_clients = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching failed connections: {e}")
            flash('Error loading failed connections', 'error')
        finally:
            connection.close()
    
    return render_template('failed_connections.html', failed_clients=failed_clients)

@app.route('/dispatcher-connected-accounts')
@login_required
def dispatcher_connected_accounts():
    """Dispatcher Connected Accounts page - Display all connected accounts for dispatchers"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Accounts, IT Support, Admin, and Dispatcher can view connected accounts
    allowed_roles = ['Dispatcher', 'Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view connected accounts', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    connected_accounts = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all connected clients with connection details
                cursor.execute("""
                    SELECT c.*, 
                           cc.serial_number, 
                           cc.power_levels, 
                           cc.router_used, 
                           cc.router_name,
                           cc.router_password,
                           cc.ground_location, 
                           cc.port_number,
                           cc.connection_date,
                           e.full_name as technician_name,
                           COALESCE(c.relocation_count, 0) as relocation_count,
                           COALESCE(c.renewal_count, 0) as renewal_count
                    FROM clients c
                    LEFT JOIN client_connections cc ON c.id = cc.client_id
                    LEFT JOIN employees e ON cc.technician_id = e.id
                    WHERE c.status = 'Connected'
                    ORDER BY cc.connection_date DESC, c.created_at DESC
                """)
                connected_accounts = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching dispatcher connected accounts: {e}")
            flash('Error loading connected accounts', 'error')
        finally:
            connection.close()
    
    return render_template('dispatcher_connected_accounts.html', connected_accounts=connected_accounts)

@app.route('/relocated-routers')
@login_required
def relocated_routers():
    """Relocated Routers page - Display all assets with Relocated status"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Accounts, IT Support, Admin, and Dispatcher can view relocated routers
    allowed_roles = ['Dispatcher', 'Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view relocated routers', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    relocated_assets = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all assets with Relocated status, joined with clients and employees
                cursor.execute("""
                    SELECT a.*, 
                           c.id as client_id,
                           c.account_number, 
                           c.full_name as client_name, 
                           c.phone_number,
                           c.client_category,
                           a.router_name,
                           a.router_password,
                           a.location,
                           e.full_name as assigned_employee_name
                    FROM assets a
                    LEFT JOIN clients c ON a.client_id = c.id
                    LEFT JOIN employees e ON a.assigned_to = e.id
                    WHERE a.status = 'Relocated'
                    ORDER BY a.created_at DESC
                """)
                relocated_assets = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching relocated routers: {e}")
            flash('Error loading relocated routers', 'error')
        finally:
            connection.close()
    
    return render_template('relocated_routers.html', relocated_assets=relocated_assets)

@app.route('/renewed-routers')
@login_required
def renewed_routers():
    """Renewed Routers page - Display all assets with Renewed status"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Accounts, IT Support, Admin, and Dispatcher can view renewed routers
    allowed_roles = ['Dispatcher', 'Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view renewed routers', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    renewed_assets = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all assets with Renewed status, joined with clients and employees
                cursor.execute("""
                    SELECT a.*, 
                           c.id as client_id,
                           c.account_number, 
                           c.full_name as client_name, 
                           c.phone_number,
                           c.client_category,
                           a.router_name,
                           a.router_password,
                           a.location,
                           e.full_name as assigned_employee_name
                    FROM assets a
                    LEFT JOIN clients c ON a.client_id = c.id
                    LEFT JOIN employees e ON a.assigned_to = e.id
                    WHERE a.status = 'Renewed'
                    ORDER BY a.created_at DESC
                """)
                renewed_assets = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching renewed routers: {e}")
            flash('Error loading renewed routers', 'error')
        finally:
            connection.close()
    
    return render_template('renewed_routers.html', renewed_assets=renewed_assets)

@app.route('/reversed-routers')
@login_required
def reversed_routers():
    """Reversed Routers page - Display all assets with Reversed status"""
    effective_role = get_effective_role()
    
    # Only Dispatcher can view reversed routers
    if effective_role not in ['Dispatcher']:
        flash('You do not have permission to view reversed routers', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    reversed_assets = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all assets with Reversed status, joined with clients and employees
                cursor.execute("""
                    SELECT a.*, 
                           c.id as client_id,
                           c.account_number, 
                           c.full_name as client_name, 
                           c.phone_number,
                           a.router_name,
                           a.router_password,
                           a.location,
                           e.full_name as assigned_employee_name
                    FROM assets a
                    LEFT JOIN clients c ON a.client_id = c.id
                    LEFT JOIN employees e ON a.assigned_to = e.id
                    WHERE a.status = 'Reversed'
                    ORDER BY a.created_at DESC
                """)
                reversed_assets = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching reversed routers: {e}")
            flash('Error loading reversed routers', 'error')
        finally:
            connection.close()
    
    return render_template('reversed_routers.html', reversed_assets=reversed_assets)

@app.route('/closed-routers')
@login_required
def closed_routers():
    """Closed Routers page - Display all assets with Closed status"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Accounts, IT Support, Admin, and Dispatcher can view closed routers
    allowed_roles = ['Dispatcher', 'Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view closed routers', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    closed_assets = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all assets with Closed status, joined with clients and employees
                cursor.execute("""
                    SELECT a.*, 
                           c.id as client_id,
                           c.account_number, 
                           c.full_name as client_name, 
                           c.phone_number,
                           c.client_category,
                           a.router_name,
                           a.router_password,
                           a.location,
                           e.full_name as assigned_employee_name
                    FROM assets a
                    LEFT JOIN clients c ON a.client_id = c.id
                    LEFT JOIN employees e ON a.assigned_to = e.id
                    WHERE a.status = 'Closed'
                    ORDER BY a.created_at DESC
                """)
                closed_assets = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching closed routers: {e}")
            flash('Error loading closed routers', 'error')
        finally:
            connection.close()
    
    return render_template('closed_routers.html', closed_assets=closed_assets)

@app.route('/relocate-client/<int:client_id>', methods=['GET', 'POST'])
@login_required
def relocate_client(client_id):
    """Relocate client page - Form to relocate a connected client"""
    effective_role = get_effective_role()
    
    # Only Dispatcher can relocate clients
    if effective_role not in ['Dispatcher']:
        flash('You do not have permission to relocate clients', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('dispatcher_connected_accounts'))
    
    try:
        with connection.cursor() as cursor:
            # Get client details and current connection info (latest connection)
            cursor.execute("""
                SELECT c.*, 
                       cc.serial_number as current_serial_number,
                       cc.router_used as current_router,
                       cc.port_number as current_port,
                       cc.ground_location as current_location
                FROM clients c
                LEFT JOIN (
                    SELECT client_id, serial_number, router_used, port_number, ground_location
                    FROM client_connections
                    WHERE client_id = %s
                    ORDER BY connection_date DESC, created_at DESC
                    LIMIT 1
                ) cc ON c.id = cc.client_id
                WHERE c.id = %s
            """, (client_id, client_id))
            client = cursor.fetchone()
            
            if not client:
                flash('Client not found', 'error')
                return redirect(url_for('dispatcher_connected_accounts'))
            
            # Check if client status allows relocation
            client_status = client.get('status', '')
            if client_status in ('Reversed', 'Closed'):
                flash(f'Cannot relocate client with status: {client_status}. Only clients with status Connected, Relocated, or Renewed can be relocated.', 'error')
                return redirect(url_for('dispatcher_connected_accounts'))
            
            if client_status not in ('Connected', 'Relocated', 'Renewed'):
                flash('Client cannot be relocated. Only clients with status Connected, Relocated, or Renewed can be relocated.', 'error')
                return redirect(url_for('dispatcher_connected_accounts'))
            
            # Get current user info for default assignment
            user_id = session.get('user_id')
            cursor.execute("SELECT id, full_name FROM employees WHERE id = %s", (user_id,))
            current_user = cursor.fetchone()
            
            if request.method == 'POST':
                # Re-check client status before processing relocation (status may have changed)
                cursor.execute("SELECT status FROM clients WHERE id = %s", (client_id,))
                current_status = cursor.fetchone()
                if current_status and current_status.get('status') in ('Reversed', 'Closed'):
                    flash(f'Cannot relocate client. Current status is {current_status.get("status")}. Only clients with status Connected, Relocated, or Renewed can be relocated.', 'error')
                    return redirect(url_for('dispatcher_connected_accounts'))
                
                new_location = request.form.get('new_location', '').strip()
                new_port = request.form.get('new_port', '').strip()
                assigned_to_id = request.form.get('assigned_to', '').strip()
                new_router_type = request.form.get('new_router_type', '').strip()
                new_router = request.form.get('new_router', '').strip()
                new_router_password = request.form.get('new_router_password', '').strip()
                new_serial_number = normalize_serial_number(request.form.get('new_serial_number', ''))
                
                # Validation
                if not new_location or not new_port or not new_router_type or not new_router or not new_router_password or not new_serial_number:
                    flash('All required fields must be filled', 'error')
                    return render_template('relocate_client.html', client=client, current_user=current_user)
                
                # Use current user if assigned_to is not provided
                if not assigned_to_id:
                    assigned_to_id = user_id
                
                # Get old values from current connection
                old_location = client.get('current_location', '')
                old_port = client.get('current_port', '')
                old_router = client.get('current_router', '')
                old_serial_number = client.get('current_serial_number', '')
                
                # Insert relocation record
                cursor.execute("""
                    INSERT INTO client_relocations 
                    (client_id, old_location, new_location, old_port, new_port, assigned_to, 
                     old_router, new_router, old_serial_number, new_serial_number, relocated_by, relocation_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURDATE())
                """, (client_id, old_location, new_location, old_port, new_port, assigned_to_id,
                      old_router, new_router, old_serial_number, new_serial_number, user_id))
                
                # Update client status and location
                cursor.execute("""
                    UPDATE clients 
                    SET status = 'Relocated', 
                        ground_location = %s,
                        relocation_count = relocation_count + 1
                    WHERE id = %s
                """, (new_location, client_id))
                
                # Update the latest connection record
                cursor.execute("""
                    UPDATE client_connections 
                    SET ground_location = %s,
                        port_number = %s,
                        router_used = %s,
                        router_name = %s,
                        router_password = %s,
                        serial_number = %s,
                        technician_id = %s
                    WHERE client_id = %s
                    ORDER BY connection_date DESC
                    LIMIT 1
                """, (new_location, new_port, new_router, new_router, new_router_password, new_serial_number, assigned_to_id, client_id))
                
                # Insert new asset for relocation (do not update existing asset)
                cursor.execute("""
                    INSERT INTO assets 
                    (asset_name, asset_type, serial_number, status, assigned_to, client_id, 
                     location, router_used, router_name, router_password, port_number)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (f"Router - {client.get('account_number', 'N/A')}", 'Router', new_serial_number, 
                      'Relocated', assigned_to_id, client_id, new_location, new_router, new_router, new_router_password, new_port))
                
                connection.commit()
                logger.info(f"Client {client_id} relocated by user {user_id}")
                flash('Client relocated successfully!', 'success')
                return redirect(url_for('dispatcher_connected_accounts'))
            
    except Exception as e:
        connection.rollback()
        logger.error(f"Error relocating client: {e}")
        flash('Error relocating client. Please try again.', 'error')
    finally:
        connection.close()
    
    return render_template('relocate_client.html', client=client, current_user=current_user)

@app.route('/renew-account/<int:client_id>', methods=['GET', 'POST'])
@login_required
def renew_account(client_id):
    """Renew account page - Form to renew a client account"""
    effective_role = get_effective_role()
    
    # Only Dispatcher can renew accounts
    if effective_role not in ['Dispatcher']:
        flash('You do not have permission to renew accounts', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('dispatcher_connected_accounts'))
    
    try:
        with connection.cursor() as cursor:
            # Get client details
            cursor.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
            client = cursor.fetchone()
            
            if not client:
                flash('Client not found', 'error')
                return redirect(url_for('dispatcher_connected_accounts'))
            
            if request.method == 'POST':
                renewal_amount = request.form.get('renewal_amount', '').strip()
                
                # Validation
                if not renewal_amount:
                    flash('Renewal amount is required', 'error')
                    return render_template('renew_account.html', client=client)
                
                try:
                    renewal_amount = float(renewal_amount)
                    if renewal_amount <= 0:
                        raise ValueError("Amount must be positive")
                except ValueError:
                    flash('Invalid renewal amount', 'error')
                    return render_template('renew_account.html', client=client)
                
                user_id = session.get('user_id')
                
                # Update client status and renewal count
                cursor.execute("""
                    UPDATE clients 
                    SET status = 'Renewed',
                        renewal_count = renewal_count + 1
                    WHERE id = %s
                """, (client_id,))
                
                # Update asset status to Renewed
                cursor.execute("""
                    UPDATE assets 
                    SET status = 'Renewed',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE client_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (client_id,))
                
                connection.commit()
                logger.info(f"Client {client_id} renewed by user {user_id} with amount {renewal_amount}")
                flash('Account renewed successfully!', 'success')
                return redirect(url_for('dispatcher_connected_accounts'))
            
    except Exception as e:
        connection.rollback()
        logger.error(f"Error renewing account: {e}")
        flash('Error renewing account. Please try again.', 'error')
    finally:
        connection.close()
    
    return render_template('renew_account.html', client=client)

@app.route('/reverse-account/<int:client_id>', methods=['GET', 'POST'])
@login_required
def reverse_account(client_id):
    """Reverse account page - Form to reverse a client account"""
    effective_role = get_effective_role()
    
    # Only Dispatcher can reverse accounts
    if effective_role not in ['Dispatcher']:
        flash('You do not have permission to reverse accounts', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('dispatcher_connected_accounts'))
    
    try:
        with connection.cursor() as cursor:
            # Get client details
            cursor.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
            client = cursor.fetchone()
            
            if not client:
                flash('Client not found', 'error')
                return redirect(url_for('dispatcher_connected_accounts'))
            
            if request.method == 'POST':
                reversal_amount = request.form.get('reversal_amount', '').strip()
                
                # Validation
                if not reversal_amount:
                    flash('Reversal amount is required', 'error')
                    return render_template('reverse_account.html', client=client)
                
                try:
                    reversal_amount = float(reversal_amount)
                    if reversal_amount <= 0:
                        raise ValueError("Amount must be positive")
                except ValueError:
                    flash('Invalid reversal amount', 'error')
                    return render_template('reverse_account.html', client=client)
                
                user_id = session.get('user_id')
                
                # Update client status to Reversed
                cursor.execute("""
                    UPDATE clients 
                    SET status = 'Reversed'
                    WHERE id = %s
                """, (client_id,))
                
                # Update asset status to Reversed
                cursor.execute("""
                    UPDATE assets 
                    SET status = 'Reversed',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE client_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (client_id,))
                
                connection.commit()
                logger.info(f"Client {client_id} reversed by user {user_id} with amount {reversal_amount}")
                flash('Account reversed successfully!', 'success')
                return redirect(url_for('dispatcher_connected_accounts'))
            
    except Exception as e:
        connection.rollback()
        logger.error(f"Error reversing account: {e}")
        flash('Error reversing account. Please try again.', 'error')
    finally:
        connection.close()
    
    return render_template('reverse_account.html', client=client)

@app.route('/close-account/<int:client_id>', methods=['GET', 'POST'])
@login_required
def close_account(client_id):
    """Close account page - Form to close a client account"""
    effective_role = get_effective_role()
    
    # Only Dispatcher can close accounts
    if effective_role not in ['Dispatcher']:
        flash('You do not have permission to close accounts', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    if not connection:
        flash('Database connection error', 'error')
        return redirect(url_for('dispatcher_connected_accounts'))
    
    try:
        with connection.cursor() as cursor:
            # Get client details
            cursor.execute("SELECT * FROM clients WHERE id = %s", (client_id,))
            client = cursor.fetchone()
            
            if not client:
                flash('Client not found', 'error')
                return redirect(url_for('dispatcher_connected_accounts'))
            
            if request.method == 'POST':
                purchase_price = request.form.get('purchase_price', '').strip()
                buyer_name = request.form.get('buyer_name', '').strip()
                
                # Validation
                if not purchase_price:
                    flash('Purchase price is required', 'error')
                    return render_template('close_account.html', client=client)
                
                if not buyer_name:
                    flash('Buyer name is required', 'error')
                    return render_template('close_account.html', client=client)
                
                # Validate purchase price
                try:
                    purchase_price = float(purchase_price)
                    if purchase_price < 0:
                        raise ValueError("Price cannot be negative")
                except ValueError:
                    flash('Invalid purchase price. Please enter a valid number', 'error')
                    return render_template('close_account.html', client=client)
                
                user_id = session.get('user_id')
                
                # Update client status to Closed
                cursor.execute("""
                    UPDATE clients 
                    SET status = 'Closed'
                    WHERE id = %s
                """, (client_id,))
                
                # Update asset status to Closed with purchase price and buyer name
                cursor.execute("""
                    UPDATE assets 
                    SET status = 'Closed',
                        purchase_price = %s,
                        buyer_name = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE client_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (purchase_price, buyer_name, client_id))
                
                connection.commit()
                logger.info(f"Client {client_id} closed by user {user_id}")
                flash('Account closed successfully!', 'success')
                return redirect(url_for('dispatcher_connected_accounts'))
            
    except Exception as e:
        connection.rollback()
        logger.error(f"Error closing account: {e}")
        flash('Error closing account. Please try again.', 'error')
    finally:
        connection.close()
    
    return render_template('close_account.html', client=client)

@app.route('/api/search-employees')
@login_required
def search_employees():
    """API endpoint for live employee search"""
    query = request.args.get('q', '').strip()
    
    if not query or len(query) < 2:
        return jsonify([])
    
    connection = get_db_connection()
    employees = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT id, full_name, username, role 
                    FROM employees 
                    WHERE (full_name LIKE %s OR username LIKE %s) 
                    AND status = 'Active'
                    ORDER BY full_name
                    LIMIT 10
                """, (f'%{query}%', f'%{query}%'))
                employees = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error searching employees: {e}")
        finally:
            connection.close()
    
    return jsonify(employees)

@app.route('/my-connected-clients')
@login_required
def my_connected_clients():
    """My Connected Clients page - Display clients connected by the current technician/employee"""
    effective_role = get_effective_role()
    
    # Only Technician and Employee can view their own connected clients
    if effective_role not in ['Technician', 'Employee']:
        flash('You do not have permission to view connected clients', 'error')
        return redirect(url_for('dashboard'))
    
    # Get current user ID from session
    user_id = session.get('user_id')
    if not user_id:
        flash('Session error. Please log in again.', 'error')
        return redirect(url_for('login'))
    
    connection = get_db_connection()
    my_connected_clients = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch clients connected by this specific technician/employee
                cursor.execute("""
                    SELECT c.*, 
                           cc.serial_number, 
                           cc.power_levels, 
                           cc.router_used, 
                           cc.port_number, 
                           cc.connection_date
                    FROM clients c
                    INNER JOIN client_connections cc ON c.id = cc.client_id
                    WHERE cc.technician_id = %s
                    AND c.status = 'Connected'
                    ORDER BY cc.connection_date DESC, c.created_at DESC
                """, (user_id,))
                my_connected_clients = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching my connected clients: {e}")
            flash('Error loading your connected clients', 'error')
        finally:
            connection.close()
    
    return render_template('my_connected_clients.html', my_connected_clients=my_connected_clients)

@app.route('/finance')
@login_required
def finance():
    """Finance page - Display all clients with their prices and asset counts"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only Accounts, IT Support, and Admin can view finance section
    allowed_roles = ['Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view the finance section', 'error')
        return redirect(url_for('dashboard'))
    
    # Get filter parameters
    filter_type = request.args.get('filter_type', 'all')  # 'all', 'day', 'period', 'month'
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    
    connection = get_db_connection()
    clients_data = []
    total_sales = 0.00
    total_technical_cost = 0.00
    price_per_ticket = 0.00
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch price per ticket from technical settings
                # Check if column exists first
                try:
                    cursor.execute("SHOW COLUMNS FROM technical_settings LIKE 'price_per_ticket'")
                    column_exists = cursor.fetchone()
                    
                    if column_exists:
                        cursor.execute("SELECT price_per_ticket FROM technical_settings ORDER BY id DESC LIMIT 1")
                        tech_settings = cursor.fetchone()
                        if tech_settings and tech_settings.get('price_per_ticket'):
                            if hasattr(tech_settings['price_per_ticket'], '__float__'):
                                price_per_ticket = float(tech_settings['price_per_ticket'])
                            else:
                                price_per_ticket = float(tech_settings['price_per_ticket'])
                    else:
                        # Column doesn't exist, add it
                        logger.warning("price_per_ticket column missing, attempting to add it")
                        try:
                            cursor.execute("ALTER TABLE technical_settings ADD COLUMN price_per_ticket DECIMAL(10, 2) DEFAULT 0.00")
                            connection.commit()
                            logger.info("Added price_per_ticket column to technical_settings table")
                            price_per_ticket = 0.00
                        except Exception as e:
                            logger.error(f"Could not add price_per_ticket column: {e}")
                            price_per_ticket = 0.00
                except Exception as e:
                    logger.error(f"Error checking/fetching price_per_ticket: {e}")
                    price_per_ticket = 0.00
                
                # Build date filter conditions
                date_filter = ""
                date_params = []
                
                if filter_type == 'day' and start_date:
                    date_filter = "AND DATE(c.created_at) = %s"
                    date_params = [start_date]
                elif filter_type == 'period' and start_date and end_date:
                    date_filter = "AND DATE(c.created_at) BETWEEN %s AND %s"
                    date_params = [start_date, end_date]
                elif filter_type == 'month' and start_date:
                    # start_date format: YYYY-MM from month input
                    # Extract year and month
                    try:
                        year, month = start_date.split('-')
                        date_filter = "AND YEAR(c.created_at) = %s AND MONTH(c.created_at) = %s"
                        date_params = [int(year), int(month)]
                    except (ValueError, AttributeError):
                        # Fallback if format is unexpected
                        date_filter = "AND YEAR(c.created_at) = YEAR(%s) AND MONTH(c.created_at) = MONTH(%s)"
                        date_params = [start_date + '-01', start_date + '-01']
                
                # Fetch all clients with their asset counts, total renewal amounts, and package prices
                query = f"""
                    SELECT 
                        c.id,
                        c.full_name,
                        c.account_number,
                        c.phone_number,
                        c.package,
                        c.client_category,
                        c.status,
                        c.payment_date,
                        c.created_at,
                        COUNT(DISTINCT a.id) as asset_count,
                        COALESCE(SUM(cr.renewal_amount), 0) as total_renewal_amount,
                        COALESCE(p.sale_price, 0) as package_price
                    FROM clients c
                    LEFT JOIN assets a ON c.id = a.client_id
                    LEFT JOIN client_renewals cr ON c.id = cr.client_id
                    LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                    WHERE 1=1 {date_filter}
                    GROUP BY c.id, c.full_name, c.account_number, c.phone_number, 
                             c.package, c.client_category, c.status, c.payment_date, c.created_at, p.sale_price
                    ORDER BY c.created_at DESC
                """
                cursor.execute(query, date_params)
                raw_data = cursor.fetchall()
                
                # Convert Decimal to float for JSON serialization and template rendering
                for client in raw_data:
                    if 'total_renewal_amount' in client and client['total_renewal_amount'] is not None:
                        if hasattr(client['total_renewal_amount'], '__float__'):
                            client['total_renewal_amount'] = float(client['total_renewal_amount'])
                    if 'asset_count' in client and client['asset_count'] is not None:
                        client['asset_count'] = int(client['asset_count'])
                    else:
                        client['asset_count'] = 0
                    if 'package_price' in client and client['package_price'] is not None:
                        if hasattr(client['package_price'], '__float__'):
                            client['package_price'] = float(client['package_price'])
                        else:
                            client['package_price'] = 0.00
                    else:
                        client['package_price'] = 0.00
                    
                    # Calculate technical cost: price_per_ticket * asset_count
                    technical_cost = price_per_ticket * client['asset_count']
                    client['technical_cost'] = technical_cost
                    
                    # Calculate totals
                    total_sales += client['package_price']
                    total_technical_cost += technical_cost
                
                clients_data = raw_data
        except Exception as e:
            logger.error(f"Error fetching finance data: {e}")
            import traceback
            logger.error(traceback.format_exc())
            flash('Error loading finance data', 'error')
        finally:
            connection.close()
    else:
        flash('Database connection error', 'error')
    
    return render_template('finance.html', 
                         clients_data=clients_data, 
                         total_sales=total_sales, 
                         total_technical_cost=total_technical_cost,
                         filter_type=filter_type,
                         start_date=start_date,
                         end_date=end_date)

@app.route('/finance-transactions')
@login_required
def finance_transactions():
    """Finance Transactions page - Display daily transactions summary"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only Accounts, IT Support, and Admin can view finance transactions
    allowed_roles = ['Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view finance transactions', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    transactions_data = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch daily transactions summary - accounts created with total renewal amounts
                cursor.execute("""
                    SELECT 
                        DATE(c.created_at) as transaction_date,
                        COUNT(DISTINCT c.id) as accounts_created,
                        COALESCE(SUM(cr.renewal_amount), 0) as total_amount_used
                    FROM clients c
                    LEFT JOIN client_renewals cr ON c.id = cr.client_id
                    GROUP BY DATE(c.created_at)
                    ORDER BY transaction_date DESC
                """)
                accounts_data = cursor.fetchall()
                
                # Fetch daily transactions summary - assets closed
                cursor.execute("""
                    SELECT 
                        DATE(updated_at) as transaction_date,
                        COUNT(*) as assets_closed,
                        COALESCE(SUM(purchase_price), 0) as closed_assets_purchase_price
                    FROM assets
                    WHERE status = 'Closed'
                    GROUP BY DATE(updated_at)
                    ORDER BY transaction_date DESC
                """)
                assets_data = cursor.fetchall()
                
                # Combine the data
                transactions_dict = {}
                
                # Add accounts data
                for row in accounts_data:
                    date_key = str(row['transaction_date'])
                    total_amount = row['total_amount_used'] or 0
                    transactions_dict[date_key] = {
                        'transaction_date': row['transaction_date'],
                        'accounts_created': int(row['accounts_created']),
                        'total_amount_used': float(total_amount) if total_amount else 0.0,
                        'assets_closed': 0,
                        'closed_assets_purchase_price': 0.0
                    }
                
                # Add/update assets data
                for row in assets_data:
                    date_key = str(row['transaction_date'])
                    if date_key in transactions_dict:
                        transactions_dict[date_key]['assets_closed'] = int(row['assets_closed'])
                        price = row['closed_assets_purchase_price'] or 0
                        transactions_dict[date_key]['closed_assets_purchase_price'] = float(price) if price else 0.0
                    else:
                        transactions_dict[date_key] = {
                            'transaction_date': row['transaction_date'],
                            'accounts_created': 0,
                            'total_amount_used': 0.0,
                            'assets_closed': int(row['assets_closed']),
                            'closed_assets_purchase_price': float(row['closed_assets_purchase_price'] or 0)
                        }
                
                raw_data = list(transactions_dict.values())
                
                # Get filter parameters
                from datetime import datetime
                filter_type = request.args.get('filter_type', 'all')  # all, day, period, month
                start_date = request.args.get('start_date', '')
                end_date = request.args.get('end_date', '')
                selected_date = request.args.get('selected_date', '')
                selected_month = request.args.get('selected_month', '')
                
                # Apply filters
                if filter_type == 'day' and selected_date:
                    try:
                        filter_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
                        raw_data = [t for t in raw_data if t['transaction_date'] == filter_date]
                    except ValueError:
                        pass
                elif filter_type == 'period' and start_date and end_date:
                    try:
                        start = datetime.strptime(start_date, '%Y-%m-%d').date()
                        end = datetime.strptime(end_date, '%Y-%m-%d').date()
                        raw_data = [t for t in raw_data if start <= t['transaction_date'] <= end]
                    except ValueError:
                        pass
                elif filter_type == 'month' and selected_month:
                    try:
                        month_year = datetime.strptime(selected_month, '%Y-%m').date()
                        raw_data = [t for t in raw_data if t['transaction_date'].year == month_year.year and t['transaction_date'].month == month_year.month]
                    except ValueError:
                        pass
                # else filter_type == 'all' - show all data
                
                # Sort by date descending
                raw_data.sort(key=lambda x: x['transaction_date'], reverse=True)
                transactions_data = raw_data
        except Exception as e:
            logger.error(f"Error fetching finance transactions: {e}")
            import traceback
            logger.error(traceback.format_exc())
            flash('Error loading finance transactions', 'error')
        finally:
            connection.close()
    else:
        flash('Database connection error', 'error')
    
    # Pass filter values to template
    filter_type = request.args.get('filter_type', 'all')
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    selected_date = request.args.get('selected_date', '')
    selected_month = request.args.get('selected_month', '')
    
    return render_template('finance_transactions.html', 
                          transactions_data=transactions_data,
                          filter_type=filter_type,
                          start_date=start_date,
                          end_date=end_date,
                          selected_date=selected_date,
                          selected_month=selected_month)

@app.route('/closed-assets')
@login_required
def closed_assets():
    """Closed Assets page - Display all closed assets with detailed information"""
    effective_role = get_effective_role()
    actual_role = session.get('role', 'Employee')
    
    # Only Accounts, IT Support, and Admin can view closed assets
    allowed_roles = ['Accounts', 'IT Support', 'Admin']
    if effective_role not in allowed_roles and actual_role not in allowed_roles:
        flash('You do not have permission to view closed assets', 'error')
        return redirect(url_for('dashboard'))
    
    connection = get_db_connection()
    closed_assets_list = []
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # Fetch all closed assets with detailed information
                cursor.execute("""
                    SELECT 
                        a.id,
                        a.asset_name,
                        a.asset_type,
                        a.serial_number,
                        a.status,
                        a.purchase_date,
                        a.purchase_price,
                        a.location,
                        a.router_used,
                        a.router_name,
                        a.router_password,
                        a.port_number,
                        a.description,
                        a.created_at,
                        a.updated_at,
                        c.id as client_id,
                        c.account_number,
                        c.full_name as client_name,
                        c.phone_number,
                        e.id as employee_id,
                        e.full_name as assigned_employee_name
                    FROM assets a
                    LEFT JOIN clients c ON a.client_id = c.id
                    LEFT JOIN employees e ON a.assigned_to = e.id
                    WHERE a.status = 'Closed'
                    ORDER BY a.updated_at DESC, a.created_at DESC
                """)
                raw_data = cursor.fetchall()
                
                # Convert Decimal to float for purchase_price
                for asset in raw_data:
                    if 'purchase_price' in asset and asset['purchase_price'] is not None:
                        if hasattr(asset['purchase_price'], '__float__'):
                            asset['purchase_price'] = float(asset['purchase_price'])
                
                closed_assets_list = raw_data
        except Exception as e:
            logger.error(f"Error fetching closed assets: {e}")
            import traceback
            logger.error(traceback.format_exc())
            flash('Error loading closed assets', 'error')
        finally:
            connection.close()
    else:
        flash('Database connection error', 'error')
    
    return render_template('closed_assets.html', closed_assets=closed_assets_list)

EXPENSE_CATEGORIES = ['EQUIPMENTS', 'FUEL', 'SALARY', 'TRANSPORT', 'OTHER']

@app.route('/expenses', methods=['GET', 'POST'])
@login_required
def expenses():
    """Expenses page: list expenses and register new expense."""
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip().upper()
        name = (request.form.get('name') or '').strip()
        amount_raw = (request.form.get('amount') or '').strip()
        details = (request.form.get('details') or '').strip()
        if category not in EXPENSE_CATEGORIES:
            flash('Please select a valid category (EQUIPMENTS, FUEL, SALARY, TRANSPORT, OTHER).', 'error')
            return redirect(url_for('expenses'))
        if not name:
            flash('Name of expense is required.', 'error')
            return redirect(url_for('expenses'))
        try:
            amount = float(amount_raw)
            if amount < 0:
                raise ValueError('Amount must be non-negative')
        except (ValueError, TypeError):
            flash('Please enter a valid amount (number).', 'error')
            return redirect(url_for('expenses'))
        user_id = session.get('user_id')
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                cursor.execute(
                    "INSERT INTO expenses (category, name, amount, details, registered_by) VALUES (%s, %s, %s, %s, %s)",
                    (category, name, amount, details or None, user_id)
                )
                connection.commit()
                flash('Expense registered successfully.', 'success')
            except Exception as e:
                logger.error(f"Error registering expense: {e}")
                flash('Failed to register expense. Please try again.', 'error')
            finally:
                connection.close()
        else:
            flash('Database connection error.', 'error')
        return redirect(url_for('expenses'))
    
    return render_template('expenses.html')


@app.route('/view-all-expenses')
@login_required
def view_all_expenses():
    """View All Expenses page - lists all registered expenses with optional date filter (day, month, range)."""
    filter_type = request.args.get('filter_type', '').strip().lower()
    filter_date = request.args.get('date', '').strip()
    filter_month = request.args.get('month', '').strip()
    filter_start = request.args.get('start_date', '').strip()
    filter_end = request.args.get('end_date', '').strip()
    date_where, date_params = _analysis_date_filter(request)
    date_where = date_where.replace('{table}', 'e') if date_where else ''
    date_filter_active = bool(date_where)
    expenses_list = []
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            sql = """
                SELECT e.id, e.category, e.name, e.amount, e.details, e.created_at, e.registered_by,
                       emp.full_name AS registered_by_name
                FROM expenses e
                LEFT JOIN employees emp ON emp.id = e.registered_by
                WHERE 1=1
            """ + date_where + " ORDER BY e.created_at DESC"
            cursor.execute(sql, date_params)
            expenses_list = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error loading expenses: {e}")
        finally:
            connection.close()
    total_amount = sum(float(exp.get('amount', 0) or 0) for exp in expenses_list)
    return render_template(
        'view_all_expenses.html',
        expenses_list=expenses_list,
        total_amount=total_amount,
        filter_type=filter_type or None,
        filter_date=filter_date or None,
        filter_month=filter_month or None,
        filter_start=filter_start or None,
        filter_end=filter_end or None,
        date_filter_active=date_filter_active
    )


@app.route('/my-expenses')
@login_required
def my_expenses():
    """My Expenses page - lists only the current user's registered expenses with optional date filter."""
    user_id = session.get('user_id')
    filter_type = request.args.get('filter_type', '').strip().lower()
    filter_date = request.args.get('date', '').strip()
    filter_month = request.args.get('month', '').strip()
    filter_start = request.args.get('start_date', '').strip()
    filter_end = request.args.get('end_date', '').strip()
    date_where, date_params = _analysis_date_filter(request)
    date_where = date_where.replace('{table}', 'e') if date_where else ''
    date_filter_active = bool(date_where)
    expenses_list = []
    connection = get_db_connection()
    if connection and user_id:
        try:
            cursor = connection.cursor()
            sql = """
                SELECT e.id, e.category, e.name, e.amount, e.details, e.created_at, e.registered_by,
                       emp.full_name AS registered_by_name
                FROM expenses e
                LEFT JOIN employees emp ON emp.id = e.registered_by
                WHERE e.registered_by = %s
            """ + date_where + " ORDER BY e.created_at DESC"
            params = [user_id] + list(date_params)
            cursor.execute(sql, params)
            expenses_list = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error loading my expenses: {e}")
        finally:
            connection.close()
    total_amount = sum(float(exp.get('amount', 0) or 0) for exp in expenses_list)
    return render_template(
        'my_expenses.html',
        expenses_list=expenses_list,
        total_amount=total_amount,
        filter_type=filter_type or None,
        filter_date=filter_date or None,
        filter_month=filter_month or None,
        filter_start=filter_start or None,
        filter_end=filter_end or None,
        date_filter_active=date_filter_active
    )


def _analysis_date_filter(request):
    """Build (WHERE snippet, params) for filtering by day, month, or range. Table placeholder: {table}."""
    filter_type = request.args.get('filter_type', '').strip().lower()
    filter_date = request.args.get('date', '').strip()
    filter_month = request.args.get('month', '').strip()
    filter_start = request.args.get('start_date', '').strip()
    filter_end = request.args.get('end_date', '').strip()
    if filter_type == 'day' and filter_date:
        return (" AND DATE({table}.created_at) = %s ", [filter_date])
    if filter_type == 'month' and filter_month:
        parts = filter_month.split('-')
        if len(parts) == 2 and len(parts[0]) == 4 and len(parts[1]) == 2:
            try:
                return (" AND YEAR({table}.created_at) = %s AND MONTH({table}.created_at) = %s ",
                        [int(parts[0]), int(parts[1])])
            except ValueError:
                pass
    if filter_type == 'range' and filter_start and filter_end:
        return (" AND DATE({table}.created_at) BETWEEN %s AND %s ", [filter_start, filter_end])
    return ("", [])


@app.route('/analysis')
@login_required
def analysis():
    """Analysis page - Comprehensive system analytics with optional date filter (day, month, range)."""
    connection = get_db_connection()
    client_date_where, client_date_params = _analysis_date_filter(request)
    filter_type = request.args.get('filter_type', '').strip().lower()
    filter_date = request.args.get('date', '').strip()
    filter_month = request.args.get('month', '').strip()
    filter_start = request.args.get('start_date', '').strip()
    filter_end = request.args.get('end_date', '').strip()
    date_filter_active = bool(client_date_where)

    # Renewal date filter (for client_renewals.renewal_date)
    renewal_where, renewal_params = "", []
    if date_filter_active:
        if filter_type == 'day' and filter_date:
            renewal_where, renewal_params = " AND DATE(renewal_date) = %s ", [filter_date]
        elif filter_type == 'month' and filter_month:
            parts = filter_month.split('-')
            if len(parts) == 2:
                try:
                    renewal_where, renewal_params = " AND YEAR(renewal_date) = %s AND MONTH(renewal_date) = %s ", [int(parts[0]), int(parts[1])]
                except ValueError:
                    pass
        elif filter_type == 'range' and filter_start and filter_end:
            renewal_where, renewal_params = " AND DATE(renewal_date) BETWEEN %s AND %s ", [filter_start, filter_end]

    # Initialize analytics data structure
    analytics = {
        'clients': {
            'total': 0,
            'by_status': [],
            'by_package': [],
            'recent_growth': [],
            'total_this_month': 0,
            'total_last_month': 0
        },
        'assets': {
            'total': 0,
            'by_status': [],
            'by_type': [],
            'utilization_rate': 0,
            'total_value': 0,
            'available': 0,
            'assigned': 0,
            'closed': 0
        },
        'finance': {
            'total_sales': 0,
            'total_renewals': 0,
            'total_technical_cost': 0,
            'net_revenue': 0,
            'by_package': [],
            'by_phone': []  # All transactions grouped by phone: account_count, total_sale_amount
        }
    }
    
    if connection:
        try:
            with connection.cursor() as cursor:
                # ========== CLIENTS ANALYTICS ==========
                # Total clients (with optional date filter)
                cursor.execute(
                    "SELECT COUNT(*) as count FROM clients WHERE 1=1" + client_date_where.format(table='clients'),
                    client_date_params
                )
                analytics['clients']['total'] = cursor.fetchone()['count']
                
                # Clients by status
                cursor.execute("""
                    SELECT status, COUNT(*) as count 
                    FROM clients 
                    WHERE 1=1""" + client_date_where.format(table='clients') + """
                    GROUP BY status 
                    ORDER BY count DESC
                """, client_date_params)
                analytics['clients']['by_status'] = cursor.fetchall()
                
                # Clients by package
                cursor.execute("""
                    SELECT package, COUNT(*) as count 
                    FROM clients 
                    WHERE package IS NOT NULL""" + client_date_where.format(table='clients') + """
                    GROUP BY package 
                    ORDER BY count DESC
                """, client_date_params)
                analytics['clients']['by_package'] = cursor.fetchall()
                
                # Clients growth (last 6 months)
                cursor.execute("""
                    SELECT 
                        DATE_FORMAT(created_at, '%Y-%m') as month,
                        COUNT(*) as count
                    FROM clients
                    WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
                    GROUP BY DATE_FORMAT(created_at, '%Y-%m')
                    ORDER BY month ASC
                """)
                analytics['clients']['recent_growth'] = cursor.fetchall()
                
                # Clients this month vs last month
                cursor.execute("""
                    SELECT COUNT(*) as count 
                    FROM clients 
                    WHERE YEAR(created_at) = YEAR(CURDATE()) 
                    AND MONTH(created_at) = MONTH(CURDATE())
                """)
                analytics['clients']['total_this_month'] = cursor.fetchone()['count']
                
                cursor.execute("""
                    SELECT COUNT(*) as count 
                    FROM clients 
                    WHERE YEAR(created_at) = YEAR(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))
                    AND MONTH(created_at) = MONTH(DATE_SUB(CURDATE(), INTERVAL 1 MONTH))
                """)
                analytics['clients']['total_last_month'] = cursor.fetchone()['count']
                
                # ========== ASSETS ANALYTICS ==========
                # Total assets
                cursor.execute("SELECT COUNT(*) as count FROM assets")
                analytics['assets']['total'] = cursor.fetchone()['count']
                
                # Assets by status
                cursor.execute("""
                    SELECT status, COUNT(*) as count 
                    FROM assets 
                    GROUP BY status 
                    ORDER BY count DESC
                """)
                analytics['assets']['by_status'] = cursor.fetchall()
                
                # Assets by type
                cursor.execute("""
                    SELECT asset_type, COUNT(*) as count 
                    FROM assets 
                    WHERE asset_type IS NOT NULL
                    GROUP BY asset_type 
                    ORDER BY count DESC
                """)
                analytics['assets']['by_type'] = cursor.fetchall()
                
                # Asset utilization
                cursor.execute("SELECT COUNT(*) as count FROM assets WHERE assigned_to IS NOT NULL")
                assigned_count = cursor.fetchone()['count']
                analytics['assets']['assigned'] = assigned_count
                
                cursor.execute("SELECT COUNT(*) as count FROM assets WHERE status = 'Available'")
                analytics['assets']['available'] = cursor.fetchone()['count']
                
                cursor.execute("SELECT COUNT(*) as count FROM assets WHERE status = 'Closed'")
                analytics['assets']['closed'] = cursor.fetchone()['count']
                
                if analytics['assets']['total'] > 0:
                    analytics['assets']['utilization_rate'] = round((assigned_count / analytics['assets']['total']) * 100, 2)
                
                # Total asset value
                cursor.execute("""
                    SELECT COALESCE(SUM(purchase_price), 0) as total 
                    FROM assets 
                    WHERE purchase_price IS NOT NULL
                """)
                result = cursor.fetchone()
                if result and result['total']:
                    if hasattr(result['total'], '__float__'):
                        analytics['assets']['total_value'] = float(result['total'])
                    else:
                        analytics['assets']['total_value'] = float(result['total'])
                
                # ========== FINANCE ANALYTICS ==========
                # Get price per ticket
                price_per_ticket = 0.00
                try:
                    cursor.execute("SHOW COLUMNS FROM technical_settings LIKE 'price_per_ticket'")
                    if cursor.fetchone():
                        cursor.execute("SELECT price_per_ticket FROM technical_settings ORDER BY id DESC LIMIT 1")
                        tech_settings = cursor.fetchone()
                        if tech_settings and tech_settings.get('price_per_ticket'):
                            if hasattr(tech_settings['price_per_ticket'], '__float__'):
                                price_per_ticket = float(tech_settings['price_per_ticket'])
                            else:
                                price_per_ticket = float(tech_settings['price_per_ticket'])
                except Exception as e:
                    logger.warning(f"Could not fetch price_per_ticket: {e}")
                
                # Total sales (from packages, with optional date filter)
                cursor.execute("""
                    SELECT COALESCE(SUM(p.sale_price), 0) as total
                    FROM clients c
                    LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                    WHERE 1=1""" + client_date_where.format(table='c') + """
                """, client_date_params)
                result = cursor.fetchone()
                if result and result['total']:
                    if hasattr(result['total'], '__float__'):
                        analytics['finance']['total_sales'] = float(result['total'])
                    else:
                        analytics['finance']['total_sales'] = float(result['total'])
                
                # Total renewals (with optional date filter)
                cursor.execute("""
                    SELECT COALESCE(SUM(renewal_amount), 0) as total
                    FROM client_renewals
                    WHERE 1=1""" + renewal_where + """
                """, renewal_params)
                result = cursor.fetchone()
                if result and result['total']:
                    if hasattr(result['total'], '__float__'):
                        analytics['finance']['total_renewals'] = float(result['total'])
                    else:
                        analytics['finance']['total_renewals'] = float(result['total'])
                
                # Total technical cost (price_per_ticket * total assets assigned)
                cursor.execute("""
                    SELECT COUNT(*) as count
                    FROM assets
                    WHERE client_id IS NOT NULL
                """)
                assets_assigned = cursor.fetchone()['count']
                analytics['finance']['total_technical_cost'] = price_per_ticket * assets_assigned
                
                # Net revenue (sales + renewals - technical costs)
                analytics['finance']['net_revenue'] = (
                    analytics['finance']['total_sales'] + 
                    analytics['finance']['total_renewals'] - 
                    analytics['finance']['total_technical_cost']
                )
                
                # Sales by package (with optional date filter)
                cursor.execute("""
                    SELECT 
                        c.package,
                        COUNT(*) as client_count,
                        COALESCE(SUM(p.sale_price), 0) as total_sales
                    FROM clients c
                    LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                    WHERE c.package IS NOT NULL""" + client_date_where.format(table='c') + """
                    GROUP BY c.package
                    ORDER BY total_sales DESC
                """, client_date_params)
                package_sales = cursor.fetchall()
                for item in package_sales:
                    if item['total_sales'] and hasattr(item['total_sales'], '__float__'):
                        item['total_sales'] = float(item['total_sales'])
                    else:
                        item['total_sales'] = 0.00
                analytics['finance']['by_package'] = package_sales
                
                # All transactions grouped by phone number (with optional date filter)
                cursor.execute("""
                    SELECT 
                        c.phone_number,
                        COUNT(*) as account_count,
                        COALESCE(SUM(p.sale_price), 0) as total_sale_amount
                    FROM clients c
                    LEFT JOIN packages p ON c.package = p.package_name AND p.is_active = TRUE
                    WHERE c.phone_number IS NOT NULL AND c.phone_number != ''""" + client_date_where.format(table='c') + """
                    GROUP BY c.phone_number
                    ORDER BY total_sale_amount DESC, account_count DESC
                """, client_date_params)
                by_phone = cursor.fetchall()
                for item in by_phone:
                    if item.get('total_sale_amount') is not None and hasattr(item['total_sale_amount'], '__float__'):
                        item['total_sale_amount'] = float(item['total_sale_amount'])
                    else:
                        item['total_sale_amount'] = float(item['total_sale_amount']) if item.get('total_sale_amount') is not None else 0.00
                analytics['finance']['by_phone'] = by_phone
                
        except Exception as e:
            logger.error(f"Error fetching analytics data: {e}")
            import traceback
            logger.error(traceback.format_exc())
            flash('Error loading analytics data', 'error')
        finally:
            connection.close()
    else:
        flash('Database connection error', 'error')
    
    return render_template('analysis.html',
        analytics=analytics,
        filter_type=filter_type or None,
        filter_date=filter_date or None,
        filter_month=filter_month or None,
        filter_start=filter_start or None,
        filter_end=filter_end or None,
        date_filter_active=date_filter_active
    )

@app.route('/reports')
@login_required
def reports():
    connection = get_db_connection()
    report_data = {'assets_by_type': [], 'assets_by_status': [], 'total_value': 0}
    
    if connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT asset_type, COUNT(*) as count
                    FROM assets WHERE asset_type IS NOT NULL
                    GROUP BY asset_type ORDER BY count DESC
                """)
                report_data['assets_by_type'] = cursor.fetchall()
                
                cursor.execute("""
                    SELECT status, COUNT(*) as count
                    FROM assets GROUP BY status ORDER BY count DESC
                """)
                report_data['assets_by_status'] = cursor.fetchall()
                
                cursor.execute("SELECT SUM(purchase_price) as total FROM assets WHERE purchase_price IS NOT NULL")
                result = cursor.fetchone()
                report_data['total_value'] = float(result['total']) if result['total'] else 0
        except Exception as e:
            logger.error(f"Error fetching reports: {e}")
        finally:
            connection.close()
    
    return render_template('reports.html', report_data=report_data)

@app.errorhandler(404)
def not_found(error):
    return render_template('login.html'), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    flash('An error occurred. Please try again later.', 'error')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    logger.info("Starting RUSHTACH Assets Management System...")

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--reset-client-data", action="store_true", help="Wipe client operational tables (clients/assets/connection history).")
    parser.add_argument("--seed-virtual-clients-csv", type=str, default="", help="Path to CSV to seed Virtual clients into clients table.")
    parser.add_argument("--yes-really-reset", action="store_true", help="Required confirmation flag for reset operations.")
    args, _unknown = parser.parse_known_args()

    if args.reset_client_data or args.seed_virtual_clients_csv:
        if not args.yes_really_reset and args.reset_client_data:
            raise SystemExit("Refusing to reset without --yes-really-reset")

        conn = get_db_connection()
        if not conn:
            raise SystemExit("Database connection failed; cannot run reset/seed")
        try:
            if args.reset_client_data:
                logger.warning("Resetting client operational data...")
                reset_client_data(conn)
                logger.warning("Client operational data wiped.")

            if args.seed_virtual_clients_csv:
                logger.warning(f"Seeding virtual clients from CSV: {args.seed_virtual_clients_csv}")
                count = seed_virtual_clients_from_csv(conn, args.seed_virtual_clients_csv)
                logger.warning(f"Seed complete. Inserted {count} clients.")
        finally:
            conn.close()
        raise SystemExit(0)
    
    if init_database():
        logger.info("Database initialized successfully")
    else:
        logger.warning("Database initialization had issues, but continuing...")
    
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    # Force debug mode for development (always enable auto-reload)
    # Check if we're in development (not production)
    is_production = os.environ.get('FLASK_ENV') == 'production'
    debug_mode = not is_production  # Always True unless explicitly production
    
    # Override with explicit FLASK_DEBUG if set
    if 'FLASK_DEBUG' in os.environ:
        debug_mode = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
    
    # Get all template files to watch for changes
    template_files = []
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    if os.path.exists(templates_dir):
        template_files = glob.glob(os.path.join(templates_dir, '**/*.html'), recursive=True)
    
    # Also watch app.py itself
    app_file = os.path.abspath(__file__)
    if app_file not in (template_files or []):
        template_files = template_files + [app_file] if template_files else [app_file]
    
    logger.info(f"Server starting on {host}:{port}")
    logger.info(f"Debug mode: {debug_mode} | Auto-reload: Enabled")
    logger.info(f"Watching {len(template_files)} files for changes")
    logger.info("💡 Changes will auto-reload - just refresh your browser!")
    
    app.run(
        debug=debug_mode,
        host=host,
        port=port,
        threaded=True,
        use_reloader=True,  # Auto-reload on code changes
        use_debugger=debug_mode,
        reloader_type='stat',  # Use stat-based reloader (more reliable)
        extra_files=template_files if template_files else None  # Watch template files for changes
    )
