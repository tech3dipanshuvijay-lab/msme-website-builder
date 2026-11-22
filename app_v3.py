import os
import json
import re
import sqlite3
from datetime import date, datetime
from io import BytesIO
from functools import wraps

from flask import (
    Flask,
    request,
    render_template_string,
    redirect,
    url_for,
    session,
    flash,
)
from werkzeug.security import generate_password_hash, check_password_hash

# Import battle-tested parsers and generators from app_v2
from app_v2 import (
    extract_pdf_data,
    parse_udyam_text,
    parse_gst_text,
    merge_documents,
    validate_document,
    validate_pdf_upload,
    build_website_markdown,
    build_google_sites_html,
    sanitize_text,
    lookup_nic_description,
    NIC_MASTER_DB,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "msme-studio-saas-super-secret-key-2026")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload limit
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "msme_studio.db")

BASIC_UPLOAD_LIMIT = 10


# ---------------------------------------------------------------------
# DATABASE SETUP & HELPERS
# ---------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            plan TEXT DEFAULT 'basic',
            upload_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_email TEXT,
            doc_type TEXT,
            business_name TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    conn.close()


init_db()


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please sign in to access the Content Studio.", "info")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user["role"] != "admin":
            flash("Admin privilege required to access this page.", "danger")
            return redirect(url_for("studio"))
        return f(*args, **kwargs)
    return decorated_function


# ---------------------------------------------------------------------
# COMMON STYLES
# ---------------------------------------------------------------------

COMMON_CSS = """
:root {
    --primary: #0284c7;
    --primary-hover: #0369a1;
    --primary-light: #e0f2fe;
    --primary-dark: #0c4a6e;
    --accent: #10b981;
    --accent-hover: #059669;
    --slate-50: #f8fafc;
    --slate-100: #f1f5f9;
    --slate-200: #e2e8f0;
    --slate-300: #cbd5e1;
    --slate-400: #94a3b8;
    --slate-500: #64748b;
    --slate-600: #475569;
    --slate-700: #334155;
    --slate-800: #1e293b;
    --slate-900: #0f172a;
    --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: var(--font-sans);
    background-color: #f8fafc;
    color: var(--slate-800);
    line-height: 1.6;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
}

.main-navbar {
    background: #ffffff;
    border-bottom: 1px solid var(--slate-200);
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 1px 3px rgba(0,0,0,0.02);
}

.nav-container {
    max-width: 1200px;
    margin: 0 auto;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.nav-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: var(--slate-900);
    font-weight: 700;
    font-size: 18px;
}

.nav-brand-icon {
    width: 32px;
    height: 32px;
    background: linear-gradient(135deg, #0284c7, #0f766e);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
}

.nav-links {
    display: flex;
    align-items: center;
    gap: 20px;
    list-style: none;
}

.nav-link {
    text-decoration: none;
    color: var(--slate-600);
    font-size: 14px;
    font-weight: 500;
    transition: color 0.15s;
}
.nav-link:hover { color: var(--primary); }

.nav-actions {
    display: flex;
    align-items: center;
    gap: 12px;
}

.btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    font-family: inherit;
    font-size: 14px;
    font-weight: 600;
    padding: 8px 18px;
    border-radius: 8px;
    cursor: pointer;
    text-decoration: none;
    border: 1px solid transparent;
    transition: all 0.15s ease;
}

.btn-primary {
    background: var(--primary);
    color: white;
}
.btn-primary:hover {
    background: var(--primary-hover);
}

.btn-secondary {
    background: white;
    color: var(--slate-700);
    border-color: var(--slate-300);
}
.btn-secondary:hover {
    background: var(--slate-100);
    color: var(--slate-900);
}

.btn-outline {
    background: transparent;
    color: var(--primary);
    border-color: var(--primary);
}
.btn-outline:hover {
    background: var(--primary-light);
}

.btn-danger {
    background: #ef4444;
    color: white;
}
.btn-danger:hover {
    background: #dc2626;
}

.btn-sm {
    padding: 5px 12px;
    font-size: 12.5px;
}

.plan-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 9999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.plan-badge.basic {
    background: #f1f5f9;
    color: #475569;
    border: 1px solid #cbd5e1;
}

.plan-badge.pro {
    background: linear-gradient(135deg, #fef3c7, #fde68a);
    color: #92400e;
    border: 1px solid #f59e0b;
}

.plan-badge.admin {
    background: #e0e7ff;
    color: #3730a3;
    border: 1px solid #c7d2fe;
}

.alert-bar {
    max-width: 1200px;
    margin: 16px auto 0 auto;
    padding: 12px 18px;
    border-radius: 8px;
    font-size: 13.5px;
    display: flex;
    align-items: center;
    gap: 10px;
}
.alert-bar.info { background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; }
.alert-bar.warning { background: #fef3c7; color: #92400e; border: 1px solid #fde68a; }
.alert-bar.danger { background: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }
.alert-bar.success { background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }

.page-wrapper {
    flex: 1;
    max-width: 1200px;
    width: 100%;
    margin: 0 auto;
    padding: 24px 20px;
}

/* Footer */
.main-footer {
    background: #ffffff;
    border-top: 1px solid var(--slate-200);
    padding: 30px 20px;
    margin-top: auto;
    font-size: 13px;
    color: var(--slate-500);
}
.footer-inner {
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 15px;
}
"""

# ---------------------------------------------------------------------
# TEMPLATE: LANDING PAGE
# ---------------------------------------------------------------------

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>MSME Content Studio | Document-Grounded Website Builder</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        """ + COMMON_CSS + """
        .hero {
            text-align: center;
            padding: 60px 20px 40px 20px;
            max-width: 900px;
            margin: 0 auto;
        }
        .hero-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: #f0fdf4;
            border: 1px solid #bbf7d0;
            color: #15803d;
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 20px;
        }
        .hero h1 {
            font-size: 42px;
            font-weight: 800;
            letter-spacing: -1px;
            color: var(--slate-900);
            line-height: 1.2;
            margin-bottom: 18px;
        }
        .hero h1 span {
            background: linear-gradient(135deg, #0284c7, #0f766e);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .hero p {
            font-size: 18px;
            color: var(--slate-600);
            max-width: 720px;
            margin: 0 auto 30px auto;
            line-height: 1.6;
        }
        .hero-actions {
            display: flex;
            justify-content: center;
            gap: 14px;
            flex-wrap: wrap;
            margin-bottom: 40px;
        }
        .trust-grid {
            display: flex;
            justify-content: center;
            gap: 25px;
            flex-wrap: wrap;
            padding-top: 15px;
            border-top: 1px solid var(--slate-200);
            font-size: 13.5px;
            color: var(--slate-600);
        }
        .trust-item {
            display: flex;
            align-items: center;
            gap: 7px;
        }
        .trust-item svg { color: #10b981; }

        /* Feature Cards */
        .section-header {
            text-align: center;
            margin: 60px 0 35px 0;
        }
        .section-header h2 {
            font-size: 28px;
            font-weight: 700;
            color: var(--slate-900);
            letter-spacing: -0.5px;
        }
        .section-header p {
            color: var(--slate-500);
            font-size: 15px;
            margin-top: 6px;
        }
        .feature-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 20px;
            margin-bottom: 60px;
        }
        .feature-card {
            background: #ffffff;
            border: 1px solid var(--slate-200);
            border-radius: 12px;
            padding: 24px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.02);
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .feature-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 15px -3px rgba(0,0,0,0.05);
        }
        .feature-icon {
            width: 44px;
            height: 44px;
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            margin-bottom: 16px;
        }
        .feature-card h3 {
            font-size: 17px;
            font-weight: 600;
            color: var(--slate-900);
            margin-bottom: 8px;
        }
        .feature-card p {
            font-size: 14px;
            color: var(--slate-600);
            line-height: 1.5;
        }

        /* Pricing Section */
        .pricing-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 24px;
            max-width: 800px;
            margin: 0 auto 60px auto;
        }
        .pricing-card {
            background: #ffffff;
            border: 1px solid var(--slate-200);
            border-radius: 14px;
            padding: 32px 28px;
            position: relative;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03);
        }
        .pricing-card.featured {
            border: 2px solid var(--primary);
            box-shadow: 0 10px 25px -5px rgba(2, 132, 199, 0.15);
        }
        .badge-popular {
            position: absolute;
            top: -12px;
            right: 24px;
            background: linear-gradient(135deg, #0284c7, #0f766e);
            color: white;
            font-size: 11px;
            font-weight: 700;
            padding: 4px 12px;
            border-radius: 9999px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .pricing-plan {
            font-size: 20px;
            font-weight: 700;
            color: var(--slate-900);
            margin-bottom: 6px;
        }
        .pricing-desc {
            font-size: 13.5px;
            color: var(--slate-500);
            margin-bottom: 18px;
        }
        .pricing-price {
            font-size: 36px;
            font-weight: 800;
            color: var(--slate-900);
            margin-bottom: 22px;
        }
        .pricing-price span {
            font-size: 14px;
            font-weight: 500;
            color: var(--slate-500);
        }
        .pricing-features {
            list-style: none;
            margin-bottom: 26px;
        }
        .pricing-features li {
            font-size: 14px;
            color: var(--slate-700);
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .pricing-features svg {
            color: #10b981;
            flex-shrink: 0;
        }
    </style>
</head>
<body>

    <!-- NAVBAR -->
    <nav class="main-navbar">
        <div class="nav-container">
            <a href="/" class="nav-brand">
                <div class="nav-brand-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><polyline points="10 2 10 10 13 7 16 10 16 2"/></svg>
                </div>
                <span>MSME Content Studio</span>
            </a>
            <div class="nav-actions">
                {% if user %}
                    <span class="plan-badge {{ user.plan }}">{{ user.plan }} Plan</span>
                    <a href="/studio" class="btn btn-primary btn-sm">Go to Studio</a>
                    {% if user.role == 'admin' %}
                        <a href="/admin" class="btn btn-secondary btn-sm">Admin</a>
                    {% endif %}
                    <a href="/logout" class="btn btn-outline btn-sm">Logout</a>
                {% else %}
                    <a href="/login" class="btn btn-secondary btn-sm">Sign In</a>
                    <a href="/register" class="btn btn-primary btn-sm">Get Started Free</a>
                {% endif %}
            </div>
        </div>
    </nav>

    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="alert-bar {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <!-- HERO -->
    <section class="hero">
        <div class="hero-pill">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
            100% Certificate Grounded & NIC-2008 Standardized
        </div>
        <h1>Generate High-Converting Websites from <span>Udyam & GST Certificates</span></h1>
        <p>No hallucinated products. No invented services. Upload official MSME government certificates and export complete, compliant website pages ready for 1-click Google Sites embed.</p>
        
        <div class="hero-actions">
            {% if user %}
                <a href="/studio" class="btn btn-primary" style="padding: 12px 28px; font-size: 16px;">Open Website Content Studio ➔</a>
            {% else %}
                <a href="/register" class="btn btn-primary" style="padding: 12px 28px; font-size: 16px;">Start Free (10 Certificates) ➔</a>
                <a href="/login" class="btn btn-secondary" style="padding: 12px 24px; font-size: 16px;">Sign In to Account</a>
            {% endif %}
        </div>

        <div class="trust-grid">
            <div class="trust-item">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                <span>Official NIC-2008 Master Lookup</span>
            </div>
            <div class="trust-item">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                <span>Google Sites 1-Click HTML Embed</span>
            </div>
            <div class="trust-item">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                <span>Zero Syntax Clean Plain Text</span>
            </div>
            <div class="trust-item">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                <span>Multi-Unit Branch Extraction</span>
            </div>
        </div>
    </section>

    <!-- FEATURES -->
    <div class="page-wrapper">
        <div class="section-header">
            <h2>Why MSME Content Studio?</h2>
            <p>Built specifically for Indian enterprises to establish compliant, trustworthy online presence.</p>
        </div>

        <div class="feature-grid">
            <div class="feature-card">
                <div class="feature-icon" style="background: #e0f2fe; color: #0284c7;">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                </div>
                <h3>Dual Certificate Support</h3>
                <p>Upload Udyam, GST, or both simultaneously. Intelligently merges records and flags mismatches automatically.</p>
            </div>

            <div class="feature-card">
                <div class="feature-icon" style="background: #dcfce7; color: #10b981;">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                </div>
                <h3>1-Click Google Sites Embed</h3>
                <p>Ready-to-paste responsive HTML embed codes with styled stat cards, tables, and contact details.</p>
            </div>

            <div class="feature-card">
                <div class="feature-icon" style="background: #fef3c7; color: #d97706;">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
                </div>
                <h3>Clean Text for Text Boxes</h3>
                <p>Copies pure text formatted with clean bullets (•) without messy markdown symbols (##, **, |---|).</p>
            </div>

            <div class="feature-card">
                <div class="feature-icon" style="background: #ede9fe; color: #7c3aed;">
                    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="m4.93 4.93 4.24 4.24"/><path d="m14.83 9.17 4.24-4.24"/><path d="m14.83 14.83 4.24 4.24"/><path d="m9.17 14.83-4.24 4.24"/></svg>
                </div>
                <h3>Statutory Compliance</h3>
                <p>Includes built-in Indian law compliance notices, privacy policy templates, and disclaimer generators.</p>
            </div>
        </div>

        <!-- PRICING -->
        <div class="section-header">
            <h2>Transparent Plans for Growing Businesses</h2>
            <p>Start free today and upgrade whenever your business demands unlimited generation.</p>
        </div>

        <div class="pricing-grid">
            <!-- Basic Plan -->
            <div class="pricing-card">
                <div class="pricing-plan">Basic Plan</div>
                <div class="pricing-desc">Perfect for small business owners and freelancers.</div>
                <div class="pricing-price">₹0 <span>/ free</span></div>
                <ul class="pricing-features">
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        <strong>Up to 10 Certificate Uploads</strong>
                    </li>
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        Udyam & GST Certificate Extraction
                    </li>
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        Google Sites 1-Click HTML Embeds
                    </li>
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        Clean Plain Text Cards Export
                    </li>
                </ul>
                {% if user %}
                    <a href="/studio" class="btn btn-secondary" style="width: 100%;">Use Studio</a>
                {% else %}
                    <a href="/register" class="btn btn-secondary" style="width: 100%;">Get Started Free</a>
                {% endif %}
            </div>

            <!-- Pro Plan -->
            <div class="pricing-card featured">
                <span class="badge-popular">Unlimited Access</span>
                <div class="pricing-plan">Pro Plan</div>
                <div class="pricing-desc">For web designers, CA firms, and high-volume agencies.</div>
                <div class="pricing-price">₹999 <span>/ year</span></div>
                <ul class="pricing-features">
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        <strong>Unlimited Certificate Uploads</strong>
                    </li>
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        Multi-Unit & Complex GST Parsing
                    </li>
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        Priority NIC-2008 Database Resolution
                    </li>
                    <li>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        Fast Customer Support
                    </li>
                </ul>
                <a href="/register" class="btn btn-primary" style="width: 100%;">Upgrade to Pro</a>
            </div>
        </div>
    </div>

    <!-- FOOTER -->
    <footer class="main-footer">
        <div class="footer-inner">
            <div>
                <strong>MSME Content Studio</strong> — Official Document Grounded Website Builder
            </div>
            <div>
                © {{ current_year }} MSME Content Studio. All rights reserved.
            </div>
        </div>
    </footer>

</body>
</html>"""

# ---------------------------------------------------------------------
# TEMPLATE: AUTH (LOGIN & REGISTER)
# ---------------------------------------------------------------------

AUTH_FORM_CSS = """
.auth-container {
    max-width: 420px;
    margin: 60px auto;
    background: #ffffff;
    border: 1px solid var(--slate-200);
    border-radius: 14px;
    padding: 32px 28px;
    box-shadow: 0 4px 6px -1px rgba(0,0,0,0.03);
}
.auth-header {
    text-align: center;
    margin-bottom: 24px;
}
.auth-header h1 {
    font-size: 22px;
    font-weight: 700;
    color: var(--slate-900);
}
.auth-header p {
    font-size: 13.5px;
    color: var(--slate-500);
    margin-top: 4px;
}
.form-group {
    margin-bottom: 16px;
}
.form-group label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    color: var(--slate-700);
    margin-bottom: 6px;
}
.form-input {
    width: 100%;
    padding: 9px 12px;
    border: 1px solid var(--slate-300);
    border-radius: 8px;
    font-family: inherit;
    font-size: 14px;
    color: var(--slate-800);
    outline: none;
    transition: border-color 0.15s;
}
.form-input:focus {
    border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.1);
}
.auth-footer {
    text-align: center;
    margin-top: 20px;
    font-size: 13px;
    color: var(--slate-500);
}
.auth-footer a {
    color: var(--primary);
    font-weight: 600;
    text-decoration: none;
}
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sign In | MSME Content Studio</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        """ + COMMON_CSS + AUTH_FORM_CSS + """
    </style>
</head>
<body>

    <nav class="main-navbar">
        <div class="nav-container">
            <a href="/" class="nav-brand">
                <div class="nav-brand-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><polyline points="10 2 10 10 13 7 16 10 16 2"/></svg>
                </div>
                <span>MSME Content Studio</span>
            </a>
            <a href="/register" class="btn btn-outline btn-sm">Create Free Account</a>
        </div>
    </nav>

    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="alert-bar {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <div class="auth-container">
        <div class="auth-header">
            <h1>Welcome Back</h1>
            <p>Sign in to your MSME Content Studio account</p>
        </div>

        <form method="POST">
            <div class="form-group">
                <label for="email">Email Address</label>
                <input type="email" id="email" name="email" class="form-input" required placeholder="you@company.com">
            </div>

            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" class="form-input" required placeholder="••••••••">
            </div>

            <button type="submit" class="btn btn-primary" style="width: 100%; margin-top: 8px;">Sign In ➔</button>
        </form>

        <div class="auth-footer">
            Don't have an account? <a href="/register">Create one free</a>
        </div>
    </div>

</body>
</html>"""

REGISTER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Create Free Account | MSME Content Studio</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        """ + COMMON_CSS + AUTH_FORM_CSS + """
    </style>
</head>
<body>

    <nav class="main-navbar">
        <div class="nav-container">
            <a href="/" class="nav-brand">
                <div class="nav-brand-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><polyline points="10 2 10 10 13 7 16 10 16 2"/></svg>
                </div>
                <span>MSME Content Studio</span>
            </a>
            <a href="/login" class="btn btn-outline btn-sm">Sign In</a>
        </div>
    </nav>

    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="alert-bar {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <div class="auth-container">
        <div class="auth-header">
            <h1>Create Free Account</h1>
            <p>Get started with 10 free certificate extractions</p>
        </div>

        <form method="POST">
            <div class="form-group">
                <label for="name">Your Full Name</label>
                <input type="text" id="name" name="name" class="form-input" required placeholder="Rajesh Kumar">
            </div>

            <div class="form-group">
                <label for="email">Work Email</label>
                <input type="email" id="email" name="email" class="form-input" required placeholder="rajesh@enterprise.com">
            </div>

            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" class="form-input" required placeholder="At least 6 characters" minlength="6">
            </div>

            <button type="submit" class="btn btn-primary" style="width: 100%; margin-top: 8px;">Create Account ➔</button>
        </form>

        <div class="auth-footer">
            Already have an account? <a href="/login">Sign In</a>
        </div>
    </div>

</body>
</html>"""

# ---------------------------------------------------------------------
# TEMPLATE: ADMIN DASHBOARD
# ---------------------------------------------------------------------

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Admin Dashboard | MSME Content Studio</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        """ + COMMON_CSS + """
        .admin-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 24px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: #ffffff;
            border: 1px solid var(--slate-200);
            border-radius: 12px;
            padding: 20px;
        }
        .stat-label {
            font-size: 13px;
            font-weight: 500;
            color: var(--slate-500);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .stat-val {
            font-size: 28px;
            font-weight: 800;
            color: var(--slate-900);
            margin-top: 4px;
        }
        .panel {
            background: #ffffff;
            border: 1px solid var(--slate-200);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 30px;
        }
        .panel h3 {
            font-size: 17px;
            font-weight: 700;
            color: var(--slate-900);
            margin-bottom: 16px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13.5px;
        }
        th {
            text-align: left;
            padding: 10px 12px;
            background: var(--slate-50);
            color: var(--slate-600);
            font-weight: 600;
            border-bottom: 1px solid var(--slate-200);
        }
        td {
            padding: 12px;
            border-bottom: 1px solid var(--slate-200);
            color: var(--slate-800);
        }
        tr:hover { background: #f8fafc; }
    </style>
</head>
<body>

    <nav class="main-navbar">
        <div class="nav-container">
            <a href="/" class="nav-brand">
                <div class="nav-brand-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><polyline points="10 2 10 10 13 7 16 10 16 2"/></svg>
                </div>
                <span>MSME Content Studio</span>
                <span class="plan-badge admin" style="margin-left: 8px;">Admin Console</span>
            </a>
            <div class="nav-actions">
                <a href="/studio" class="btn btn-primary btn-sm">Content Studio</a>
                <a href="/logout" class="btn btn-outline btn-sm">Logout</a>
            </div>
        </div>
    </nav>

    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            {% for category, message in messages %}
                <div class="alert-bar {{ category }}">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}

    <div class="page-wrapper">
        <div class="admin-header">
            <div>
                <h1 style="font-size: 24px; font-weight: 800; color: var(--slate-900);">User Activity & Quota Management</h1>
                <p style="font-size: 14px; color: var(--slate-500);">Monitor users, control Basic (10) and Pro (Unlimited) plan access, and review extraction logs.</p>
            </div>
        </div>

        <!-- STATS -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total Users</div>
                <div class="stat-val">{{ stats.total_users }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Total Extractions</div>
                <div class="stat-val">{{ stats.total_extractions }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Basic Users</div>
                <div class="stat-val">{{ stats.basic_users }}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Pro Users</div>
                <div class="stat-val" style="color: #d97706;">{{ stats.pro_users }}</div>
            </div>
        </div>

        <!-- USERS TABLE -->
        <div class="panel">
            <h3>Registered Users</h3>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Name</th>
                            <th>Email</th>
                            <th>Role</th>
                            <th>Current Plan</th>
                            <th>Uploads Used</th>
                            <th>Joined</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for u in users %}
                        <tr>
                            <td>#{{ u.id }}</td>
                            <td><strong>{{ u.name }}</strong></td>
                            <td>{{ u.email }}</td>
                            <td>
                                <span class="plan-badge {% if u.role == 'admin' %}admin{% else %}basic{% endif %}">
                                    {{ u.role }}
                                </span>
                            </td>
                            <td>
                                <span class="plan-badge {{ u.plan }}">
                                    {{ u.plan }}
                                </span>
                            </td>
                            <td>
                                {% if u.plan == 'pro' %}
                                    <strong style="color: #059669;">{{ u.upload_count }} (Unlimited)</strong>
                                {% else %}
                                    <strong>{{ u.upload_count }} / 10</strong>
                                    {% if u.upload_count >= 10 %}
                                        <span style="color: #ef4444; font-size: 11px; font-weight: 700; margin-left: 4px;">[LIMIT REACHED]</span>
                                    {% endif %}
                                {% endif %}
                            </td>
                            <td>{{ u.created_at[:10] }}</td>
                            <td>
                                <div style="display: flex; gap: 6px;">
                                    <form method="POST" action="/admin/user/{{ u.id }}/toggle-plan" style="display: inline;">
                                        {% if u.plan == 'basic' %}
                                            <button type="submit" class="btn btn-primary btn-sm">Upgrade to Pro</button>
                                        {% else %}
                                            <button type="submit" class="btn btn-secondary btn-sm">Downgrade to Basic</button>
                                        {% endif %}
                                    </form>
                                    <form method="POST" action="/admin/user/{{ u.id }}/reset-quota" style="display: inline;">
                                        <button type="submit" class="btn btn-outline btn-sm" onclick="return confirm('Reset upload quota for {{ u.name }}?')">Reset Quota</button>
                                    </form>
                                </div>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- RECENT ACTIVITY LOGS -->
        <div class="panel">
            <h3>Recent Certificate Extractions</h3>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>User Email</th>
                            <th>Document Type</th>
                            <th>Business Name Extracted</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for a in activities %}
                        <tr>
                            <td>{{ a.created_at }}</td>
                            <td>{{ a.user_email }}</td>
                            <td><span class="plan-badge basic">{{ a.doc_type|upper }}</span></td>
                            <td><strong>{{ a.business_name or 'Not specified' }}</strong></td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="4" style="text-align: center; color: var(--slate-400);">No certificate generation activity yet.</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

    </div>

</body>
</html>"""

# ---------------------------------------------------------------------
# FLASK AUTH & ADMIN ROUTES
# ---------------------------------------------------------------------

@app.route("/")
def home():
    user = get_current_user()
    return render_template_string(
        LANDING_HTML,
        user=user,
        current_year=date.today().year,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("studio"))

    if request.method == "POST":
        name = sanitize_text(request.form.get("name", ""), 100)
        email = sanitize_text(request.form.get("email", ""), 120).lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("All fields are required.", "danger")
            return render_template_string(REGISTER_HTML)

        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "danger")
            return render_template_string(REGISTER_HTML)

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            conn.close()
            flash("Email address already registered. Please sign in.", "warning")
            return redirect(url_for("login"))

        # First user is automatically Admin + Pro plan
        total_users = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()["count"]
        role = "admin" if total_users == 0 else "user"
        plan = "pro" if total_users == 0 else "basic"

        password_hash = generate_password_hash(password)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (name, email, password_hash, role, plan, upload_count) VALUES (?, ?, ?, ?, ?, 0)",
            (name, email, password_hash, role, plan),
        )
        conn.commit()
        new_user_id = cursor.lastrowid
        conn.close()

        session["user_id"] = new_user_id
        flash(f"Welcome {name}! Your account has been created ({plan.capitalize()} Plan).", "success")
        return redirect(url_for("studio"))

    return render_template_string(REGISTER_HTML)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("studio"))

    if request.method == "POST":
        email = sanitize_text(request.form.get("email", ""), 120).lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now(), user["id"]))
            conn.commit()
            conn.close()
            flash(f"Welcome back, {user['name']}!", "success")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("studio"))

        conn.close()
        flash("Invalid email or password.", "danger")

    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("home"))


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    activities = conn.execute("SELECT * FROM activity_logs ORDER BY id DESC LIMIT 50").fetchall()

    stats = {
        "total_users": len(users),
        "total_extractions": sum(u["upload_count"] for u in users),
        "basic_users": sum(1 for u in users if u["plan"] == "basic"),
        "pro_users": sum(1 for u in users if u["plan"] == "pro"),
    }
    conn.close()

    return render_template_string(
        ADMIN_HTML,
        users=users,
        activities=activities,
        stats=stats,
        current_year=date.today().year,
    )


@app.route("/admin/user/<int:user_id>/toggle-plan", methods=["POST"])
@login_required
@admin_required
def admin_toggle_plan(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        new_plan = "pro" if user["plan"] == "basic" else "basic"
        conn.execute("UPDATE users SET plan = ? WHERE id = ?", (new_plan, user_id))
        conn.commit()
        flash(f"Updated {user['name']}'s plan to {new_plan.upper()}.", "success")
    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/reset-quota", methods=["POST"])
@login_required
@admin_required
def admin_reset_quota(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        conn.execute("UPDATE users SET upload_count = 0 WHERE id = ?", (user_id,))
        conn.commit()
        flash(f"Reset upload quota for {user['name']} to 0.", "info")
    conn.close()
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------------
# STUDIO TEMPLATE (WITH QUOTA STATUS BAR & UPGRADE MODAL)
# ---------------------------------------------------------------------

# Read existing studio template from app_v2 HTML_UI to guarantee exact fidelity
from app_v2 import HTML_UI as V2_HTML_UI

# Inject user quota status bar at top of studio panel
STUDIO_HEADER_BAR = """
<!-- USER ACCOUNT & QUOTA BAR -->
<div style="background: #ffffff; border-bottom: 1px solid var(--slate-200); padding: 12px 20px; margin-bottom: 24px; box-shadow: 0 1px 2px rgba(0,0,0,0.03);">
    <div style="max-width: 1100px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 14px;">
        <div style="display: flex; align-items: center; gap: 12px;">
            <a href="/" style="text-decoration: none; display: flex; align-items: center; gap: 8px; color: var(--slate-900); font-weight: 700; font-size: 16px;">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><polyline points="10 2 10 10 13 7 16 10 16 2"/></svg>
                MSME Content Studio
            </a>
            <span class="plan-badge {{ current_user.plan }}">{{ current_user.plan }} Plan</span>
        </div>

        <div style="display: flex; align-items: center; gap: 16px;">
            <!-- Quota display -->
            <div style="font-size: 13.5px; color: var(--slate-600); display: flex; align-items: center; gap: 8px;">
                {% if current_user.plan == 'pro' %}
                    <span style="display: inline-flex; align-items: center; gap: 5px; color: #059669; font-weight: 600;">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
                        Unlimited Extractions Active
                    </span>
                    <span style="color: var(--slate-400);">({{ current_user.upload_count }} processed)</span>
                {% else %}
                    <span>Certificates: <strong>{{ current_user.upload_count }} / 10 Used</strong></span>
                    <span style="background: {% if current_user.upload_count >= 10 %}#fee2e2{% else %}#e0f2fe{% endif %}; color: {% if current_user.upload_count >= 10 %}#b91c1c{% else %}#0369a1{% endif %}; padding: 2px 8px; border-radius: 6px; font-weight: 700; font-size: 12px;">
                        {% if current_user.upload_count >= 10 %}
                            LIMIT REACHED
                        {% else %}
                            {{ 10 - current_user.upload_count }} left
                        {% endif %}
                    </span>
                {% endif %}
            </div>

            {% if current_user.plan == 'basic' and current_user.upload_count >= 8 %}
                <a href="#upgradeModal" class="btn btn-primary btn-sm" style="background: linear-gradient(135deg, #f59e0b, #d97706); border: none;">Upgrade to Pro</a>
            {% endif %}

            {% if current_user.role == 'admin' %}
                <a href="/admin" class="btn btn-secondary btn-sm">Admin Console</a>
            {% endif %}

            <div style="display: flex; align-items: center; gap: 8px; border-left: 1px solid var(--slate-300); padding-left: 14px;">
                <span style="font-size: 13.5px; font-weight: 600; color: var(--slate-800);">{{ current_user.name }}</span>
                <a href="/logout" style="color: var(--slate-500); font-size: 12.5px; text-decoration: none;">(Sign Out)</a>
            </div>
        </div>
    </div>
</div>
"""

# Quota Limit Modal when user reaches 10
QUOTA_LIMIT_ALERT = """
{% if limit_reached %}
<div style="background: #fef2f2; border: 2px solid #f87171; border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px;">
    <div>
        <h3 style="color: #991b1b; font-size: 17px; font-weight: 700; margin-bottom: 4px;">⚠️ Basic Plan Quota Exceeded (10/10 Used)</h3>
        <p style="color: #b91c1c; font-size: 14px; margin: 0;">Aapki 10 certificate generation ki free limit poori ho gayi hai. Naye certificates process karne ke liye Pro Plan me upgrade karein.</p>
    </div>
    <div>
        <a href="mailto:admin@msmecontentstudio.com?subject=Upgrade%20to%20Pro%20Plan%20for%20{{ current_user.email }}" class="btn btn-primary" style="background: #dc2626;">Upgrade to Pro (Unlimited) ➔</a>
    </div>
</div>
{% endif %}
"""

# Adapt V2_HTML_UI into V3 Studio
STUDIO_HTML = V2_HTML_UI.replace(
    '</head>',
    '<style>\n' + COMMON_CSS + '\n</style>\n</head>'
).replace(
    '<body>',
    '<body>\n' + STUDIO_HEADER_BAR
).replace(
    '<div class="container">',
    """<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
        <div style="margin-bottom: 16px;">
        {% for category, message in messages %}
            <div class="alert-bar {{ category }}">{{ message }}</div>
        {% endfor %}
        </div>
    {% endif %}
{% endwith %}
""" + QUOTA_LIMIT_ALERT
)


# ---------------------------------------------------------------------
# CONTENT STUDIO ROUTE (/studio)
# ---------------------------------------------------------------------

@app.route("/studio", methods=["GET", "POST"])
@login_required
def studio():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    content = None
    clean_full_text = None
    sections = []
    full_html_embed = None
    html_sections = []
    error = None
    warnings = []
    conflicts = []
    extracted_data = {}
    selected_type = "udyam"
    form_values = {}
    limit_reached = (user["plan"] == "basic" and user["upload_count"] >= BASIC_UPLOAD_LIMIT)

    if request.method == "POST":
        if limit_reached:
            error = "You have reached your 10-certificate limit on the Basic plan. Please upgrade to Pro for unlimited generation."
        else:
            try:
                selected_type = request.form.get("doc_type", "udyam")
                if selected_type not in ("gst", "udyam", "both"):
                    raise ValueError("Please select GST, Udyam, or Both.")

                form_values = {
                    "manual_unit": sanitize_text(request.form.get("manual_unit", ""), 150),
                    "manual_phone": sanitize_text(request.form.get("manual_phone", ""), 50),
                    "manual_email": sanitize_text(request.form.get("manual_email", ""), 100),
                    "manual_address": sanitize_text(request.form.get("manual_address", ""), 300),
                    "manual_services": sanitize_text(request.form.get("manual_services", ""), 2000),
                }

                gst_data = {}
                udyam_data = {}

                if selected_type in ("gst", "both"):
                    gst_file = request.files.get("gst_file")
                    validate_pdf_upload(gst_file, "GST certificate")
                    gst_text, gst_tables = extract_pdf_data(gst_file)
                    gst_data = parse_gst_text(gst_text, pages_tables=gst_tables)
                    errs = validate_document(gst_data, "gst")
                    if errs:
                        warnings.extend(errs)

                if selected_type in ("udyam", "both"):
                    udyam_file = request.files.get("udyam_file")
                    validate_pdf_upload(udyam_file, "Udyam certificate")
                    udyam_text, udyam_tables = extract_pdf_data(udyam_file)
                    udyam_data = parse_udyam_text(udyam_text, pages_tables=udyam_tables)
                    errs = validate_document(udyam_data, "udyam")
                    if errs:
                        warnings.extend(errs)

                merged, doc_conflicts = merge_documents(gst_data, udyam_data)
                conflicts.extend(doc_conflicts)

                for k, v in form_values.items():
                    if v:
                        merged[k] = v

                missing_label = "Not specified in GST certificate" if selected_type == "gst" else "Not stated in uploaded document"

                nic_display = ""
                if merged.get("nic_entries"):
                    nic_display = "; ".join([f"{item['nic_code']} - {item['nic_description']}" for item in merged["nic_entries"]])
                elif merged.get("nic_code"):
                    nic_display = f"{merged['nic_code']} - {merged.get('nic_description', '')}"

                extracted_data = {
                    "Enterprise Name": merged.get("legal_name", ""),
                    "Unit / Trade Name": merged.get("unit_name") or merged.get("trade_name") or merged.get("legal_name", ""),
                    "Enterprise Type": merged.get("enterprise_type", missing_label),
                    "Organisation Type": merged.get("organisation_type", missing_label),
                    "Major Activity": merged.get("major_activity", missing_label),
                    "NIC 5-Digit Classification": nic_display or missing_label,
                    "GSTIN": merged.get("gstin", "Not specified"),
                    "Type of Registration": merged.get("gst_registration_type", "Not specified"),
                    "Date of Liability": merged.get("gst_liability_date", "Not specified"),
                    "GST Validity From": merged.get("gst_registration_date", "Not specified"),
                    "GST Issue Date": merged.get("gst_issue_date", "Not specified"),
                    "Official Address": merged.get("address", "Not specified"),
                    "Phone Number": merged.get("phone", "Not specified"),
                    "Email Address": merged.get("email", "Not specified"),
                }
                if merged.get("additional_address"):
                    extracted_data["Additional Address"] = merged["additional_address"]
                if merged.get("udyam_number"):
                    extracted_data["Udyam Registration Number"] = merged["udyam_number"]
                if merged.get("commencement_date"):
                    extracted_data["Date of Commencement"] = merged["commencement_date"]

                content, clean_full_text, sections = build_website_markdown(merged)
                full_html_embed, html_sections = build_google_sites_html(merged)

                # SUCCESSFUL GENERATION: Increment user quota & log activity
                conn = get_db()
                conn.execute(
                    "UPDATE users SET upload_count = upload_count + 1 WHERE id = ?",
                    (user["id"],)
                )
                conn.execute(
                    "INSERT INTO activity_logs (user_id, user_email, doc_type, business_name) VALUES (?, ?, ?, ?)",
                    (user["id"], user["email"], selected_type, extracted_data.get("Unit / Trade Name") or extracted_data.get("Enterprise Name")),
                )
                conn.commit()
                conn.close()

                # Refresh current user for updated upload_count display
                user = get_current_user()

            except Exception as exc:
                error = str(exc)

    return render_template_string(
        STUDIO_HTML,
        current_user=user,
        limit_reached=limit_reached,
        content=content,
        clean_full_text=clean_full_text,
        sections=sections,
        full_html_embed=full_html_embed,
        html_sections=html_sections,
        error=error,
        warnings=warnings,
        conflicts=conflicts,
        extracted_data=extracted_data,
        selected_type=selected_type,
        form_values=form_values,
        current_year=date.today().year,
    )


@app.errorhandler(413)
def request_entity_too_large(_error):
    return "Upload too large. Maximum total upload size is 20 MB.", 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
