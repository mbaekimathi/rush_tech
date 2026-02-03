#!/bin/bash
# Production deployment script for RUSHTACH Assets Management System

set -e

echo "=========================================="
echo "RUSHTACH Assets Management - Deployment"
echo "=========================================="

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found!"
    echo "📝 Creating .env from .env.example..."
    cp .env.example .env
    echo "✅ Please edit .env with your production settings before continuing!"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Install/upgrade dependencies
echo "📥 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Initialize database
echo "🗄️  Initializing database..."
python -c "from app import init_database; init_database()"

echo ""
echo "✅ Deployment setup complete!"
echo ""
echo "🚀 To start the server:"
echo "   gunicorn -c gunicorn_config.py wsgi:app"
echo ""
echo "📋 Or for development:"
echo "   python app.py"
echo ""
