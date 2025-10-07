from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file, abort
)
import os
import random
from copy import deepcopy
from faker import Faker
from datetime import datetime
from jinja2 import DictLoader
import pandas as pd

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

fake = Faker("en_US")

# ==========================================================
# HELPERS: Fake data for non-Munsie brands
# ==========================================================
def fake_contact():
    """Return a dict with fake email, phone, and job title."""
    return {
        "name": fake.name(),
        "email": fake.unique.email(),
        "phone": fake.numerify("###-###-####"),
        "job_title": fake.job(),
    }

def make_property(i: int):
    """One fake property with 1-3 fake contacts."""
    return {
        "id": i,
        "address": fake.street_address(),
        "city": fake.city(),
        "roof_material": random.choice(["Tile", "Shingle", "Metal"]),
        "roof_type": random.choice(["Hip", "Gable", "Flat", "Mansard"]),
        "last_roof_date": fake.date_between(start_date='-30y', end_date='today').strftime('%Y-%m-%d'),
        "owner": fake.name(),
        "parcel_name": fake.company(),
        "llc_mailing_address": fake.address().replace("\n", ", "),
        "property_use": random.choice(["01-01 Single Family", "02-03 Duplex", "03-04 Multi-Family"]),
        "adj_bldg_sf": str(random.randint(1000, 5000)),
        "year_built": str(random.randint(1950, 2023)),
        "contact_info": [fake_contact() for _ in range(random.randint(1, 3))],
        "notes": []
    }

# ==========================================================
# MUNSIE: Load Real Excel (relative path for GitHub)
# ==========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUNSIE_FILE_PATH = os.path.join(BASE_DIR, "data", "ACTUALSTEVELISTcoralsprings.xlsx")

MUNSIE_CONTACT_SLOTS = 5   # VOTER1_* ... VOTER5_*

def _s(val):
    """Stringify a value safely (handle NaN / None)."""
    if pd.isna(val):
        return ""
    return str(val).strip()

def load_munsie_properties(filepath):
    """
    Load property + contact data from the Excel file.
    Expected headers (confirmed):
      PHY_ADDR1, PHY_CITY, SCRAPED TYPE, SCRAPED SUBTYPE,
      LATEST_ROOF_DATE, OWN_NAME, PERMIT_NUMBER, OWN_ADDR1,
      DOR_UC, TOT_LVG_AREA, ACT_YR_BLT,
      VOTERn_NAME, VOTERn_EMAIL, VOTERn_PHONE for n=1..5
    """
    df = pd.read_excel(filepath)
    props = []
    for i, row in df.iterrows():
        # Contacts
        contacts = []
        for n in range(1, MUNSIE_CONTACT_SLOTS + 1):
            name = row.get(f"VOTER{n}_NAME")
            email = row.get(f"VOTER{n}_EMAIL")
            phone = row.get(f"VOTER{n}_PHONE")
            if not (pd.isna(name) and pd.isna(email) and pd.isna(phone)):
                contacts.append({
                    "name": _s(name),
                    "email": _s(email).lower(),
                    "phone": _s(phone),
                    # No job title in the sheet; keep field for UI parity
                    "job_title": ""
                })

        # Property dict
        prop = {
            "id": i + 1,
            "address": _s(row.get("PHY_ADDR1")),
            "city": _s(row.get("PHY_CITY")),
            "roof_material": _s(row.get("SCRAPED TYPE")),
            "roof_type": _s(row.get("SCRAPED SUBTYPE")),
            "last_roof_date": _s(row.get("LATEST_ROOF_DATE"))[:10],  # YYYY-MM-DD
            "owner": _s(row.get("OWN_NAME")),
            "parcel_name": _s(row.get("PERMIT_NUMBER")),  # re-using for display
            "llc_mailing_address": _s(row.get("OWN_ADDR1")),
            "property_use": _s(row.get("DOR_UC")),
            "adj_bldg_sf": _s(row.get("TOT_LVG_AREA")),
            "year_built": _s(row.get("ACT_YR_BLT")),
            "contact_info": contacts,
            "notes": [],
        }
        # If date missing, pad with 0001-01-01 to avoid filter errors
        if not prop["last_roof_date"]:
            prop["last_roof_date"] = "0001-01-01"
        props.append(prop)
    return props

# Attempt to load Munsie data at startup (safe fallback)
try:
    munsie_properties = load_munsie_properties(MUNSIE_FILE_PATH)
    print(f"✅ Loaded {len(munsie_properties)} Munsie properties from {MUNSIE_FILE_PATH}")
except Exception as e:
    print(f"⚠️ Could not load Munsie data: {e}")
    munsie_properties = []

# Default fake data for SCI / GENERIC
fake_properties = [make_property(i) for i in range(1, 51)]

# ==========================================================
# USERS / AUTH
# ==========================================================
USERS = {
    "admin":      {"password": "admin123",   "role": "admin",  "brand": "generic"},
    "sci":        {"password": "sci123",     "role": "client", "brand": "sci"},
    "roofing123": {"password": "roofing123", "role": "client", "brand": "generic"},
    "munsie":     {"password": "munsie123",  "role": "client", "brand": "munsie"},
}

# ==========================================================
# JINJA TEMPLATES (inline, full UI)
# ==========================================================
app.jinja_loader = DictLoader({
    # ---------- BASE ----------
    "base.html": """
    <!doctype html>
    <html>
    <head>
        <title>{{ title or "Florida Sales Leads" }}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">

        <!-- Bootstrap -->
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">

        <!-- Optional modern font -->
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">

        <style>
            :root{
                --brand:#0d6efd;          /* primary accent */
                --card-bg: rgba(255,255,255,.92);
                --ring: rgba(13,110,253,.25);
            }

            body {
                padding-top: 60px;
                font-family: 'Inter', system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
                background-color:#f7f8fb;
            }

            /* Only the login page gets a gradient background */
            body.login-page {
                background:
                  radial-gradient(1100px 600px at 15% 10%, #eaf6ff 0%, transparent 40%),
                  radial-gradient(900px 500px at 85% 90%, #fff0eb 0%, transparent 35%),
                  #f7f8fb;
            }

            .auth-wrapper{
                min-height: calc(100vh - 70px);
                display:flex;
                align-items:center;
                justify-content:center;
                padding: 2rem 1rem;
            }
            .auth-card{
                width:100%;
                max-width: 460px;
                border: 1px solid rgba(0,0,0,.05);
                border-radius: 16px;
                background: var(--card-bg);
                backdrop-filter: blur(6px);
                box-shadow: 0 12px 30px rgba(0,0,0,.08);
                padding: 2rem;
            }

            /* Default logo size */
            .brand-logo{ max-height: 56px; width: auto; display: inline-block; filter: drop-shadow(0 1px 1px rgba(0,0,0,.08)); }
            /* Bigger logo on the login page */
            body.login-page .brand-logo{ max-height: 120px; }

            /* Input polish */
            .form-control, .form-select { border-radius: 10px; }
            .form-control:focus, .form-select:focus {
                border-color: var(--brand);
                box-shadow: 0 0 0 .2rem var(--ring);
            }

            .btn-primary{ border-radius: 10px; }

            /* Table cursor (kept) */
            tr { cursor: pointer; }

            /* Placeholder for generic brand dashboard */
            .logo-placeholder {
                width: 250px; height: 80px; border: 2px dashed #bbb;
                display: flex; align-items: center; justify-content: center;
                border-radius: 10px; font-weight: 600; color:#666; margin-bottom: 20px;
            }

            .note-card { background:#fff; border:1px solid #e9ecef; border-radius:8px; padding:.5rem .75rem; }
        </style>
    </head>
    <body class="{{ body_class or '' }}">
        <nav class="navbar navbar-expand-lg navbar-dark bg-dark fixed-top">
          <div class="container-fluid">
            <a class="navbar-brand" href="{{ url_for('dashboard') }}">Florida Sales Leads</a>
            <div class="d-flex">
              {% if session.get('username') %}
                <span class="navbar-text me-3">Hi, {{ session['username'] }}{% if session.get('role')=='admin' %} (Admin){% endif %}</span>
                {% if session.get('role') == 'admin' %}
                  <a class="btn btn-outline-light me-2" href="{{ url_for('admin_page') }}">Admin</a>
                {% endif %}
                <a class="btn btn-outline-warning" href="{{ url_for('logout') }}">Logout</a>
              {% endif %}
            </div>
          </div>
        </nav>
        <div class="container">
          {% with messages = get_flashed_messages() %}
            {% if messages %}
              <div class="mt-2">
                {% for m in messages %}
                  <div class="alert alert-info">{{ m }}</div>
                {% endfor %}
              </div>
            {% endif %}
          {% endwith %}
          {% block content %}{% endblock %}
        </div>
        <script>
          // placeholder for small page scripts (e.g., toggles)
        </script>
    </body>
    </html>
    """,

    # ---------- LOGIN ----------
    "login.html": """
    {% extends "base.html" %}
    {% block content %}
      <div class="auth-wrapper">
        <div class="auth-card">
          <div class="text-center mb-3">
            <img src="{{ url_for('static', filename='floridasalesleadslogo.webp') }}" 
                    class="brand-logo" alt="Florida Sales Leads logo" loading="lazy">
          </div>
          <h1 class="h4 text-center mb-1">Welcome back</h1>
          <p class="text-muted text-center mb-4">Sign in to access your permit database.</p>

          <form method="post" novalidate>
            <div class="form-floating mb-3">
              <input name="username" id="username" class="form-control" placeholder="Username" required autocomplete="username" autofocus>
              <label for="username">Username</label>
            </div>

            <div class="form-floating mb-3 position-relative">
              <input type="password" name="password" id="password" class="form-control" placeholder="Password" required autocomplete="current-password">
              <label for="password">Password</label>
              <button type="button"
                      class="btn btn-sm btn-outline-secondary position-absolute top-50 end-0 translate-middle-y me-2"
                      onclick="const i=document.getElementById('password'); i.type=(i.type==='password'?'text':'password'); this.textContent=(i.type==='password'?'Show':'Hide');"
                      aria-label="Show password">Show</button>
            </div>

            <button class="btn btn-primary w-100 btn-lg">Login</button>
          </form>
        </div>
      </div>
    {% endblock %}
    """,

    # ---------- ADMIN ----------
    "admin.html": """
    {% extends "base.html" %}
    {% block content %}
      <h3 class="mb-3">Admin — Manage Logins</h3>
      <div class="row">
        <div class="col-lg-6">
          <div class="card mb-4">
            <div class="card-body">
              <h5>Add New Credential</h5>
              <form method="post" action="{{ url_for('admin_add') }}">
                <div class="row g-2">
                  <div class="col-md-6">
                    <label class="form-label">Username</label>
                    <input name="username" class="form-control" required>
                  </div>
                  <div class="col-md-6">
                    <label class="form-label">Password</label>
                    <input name="password" class="form-control" required>
                  </div>
                  <div class="col-md-6">
                    <label class="form-label">Role</label>
                    <select name="role" class="form-select">
                      <option value="client">client</option>
                      <option value="admin">admin</option>
                    </select>
                  </div>
                  <div class="col-md-6">
                    <label class="form-label">Brand</label>
                    <select name="brand" class="form-select">
                      <option value="sci">sci</option>
                      <option value="generic">generic</option>
                      <option value="munsie">munsie</option>
                    </select>
                  </div>
                </div>
                <button class="btn btn-success mt-3">Add</button>
              </form>
            </div>
          </div>
        </div>

        <div class="col-lg-6">
          <div class="card">
            <div class="card-body">
              <h5>Current Users</h5>
              <table class="table table-sm">
                <thead>
                  <tr><th>User</th><th>Role</th><th>Brand</th><th class="text-end">Actions</th></tr>
                </thead>
                <tbody>
                  {% for u, info in users.items() %}
                    <tr>
                      <td>{{ u }}</td>
                      <td>{{ info.role }}</td>
                      <td>{{ info.brand }}</td>
                      <td class="text-end">
                        {% if u != 'admin' %}
                        <form method="post" action="{{ url_for('admin_delete') }}" onsubmit="return confirm('Delete {{u}}?');" class="d-inline">
                          <input type="hidden" name="username" value="{{ u }}">
                          <button class="btn btn-sm btn-outline-danger">Delete</button>
                        </form>
                        {% else %}
                          <span class="text-muted">protected</span>
                        {% endif %}
                      </td>
                    </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    {% endblock %}
    """,

    # ---------- SCI DASHBOARD ----------
    "sci_dashboard.html": """
    {% extends "base.html" %}
    {% block content %}
      <img src="{{ url_for('static', filename='SCILOGO.png') }}" alt="SCI Roofing Logo" class="mb-2" style="max-height:60px;">
      <h2 class="mb-4">Permit Database</h2>
      {% include "search_form.html" %}
      {% include "table.html" %}
    {% endblock %}
    """,

    # ---------- MUNSIE DASHBOARD ----------
    "munsie_dashboard.html": """
    {% extends "base.html" %}
    {% block content %}
      <div class="d-flex align-items-center mb-3">
        <img src="{{ url_for('static', filename='munsielogo.webp') }}" 
            alt="Munsie Logo" style="max-height:60px" class="me-2">
        <h2 class="mb-0">Permit Database</h2>
      </div>
      {% include "search_form.html" %}
      {% include "table.html" %}
    {% endblock %}
    """,

    # ---------- GENERIC DASHBOARD ----------
    "generic_dashboard.html": """
    {% extends "base.html" %}
    {% block content %}
      <div class="logo-placeholder">Your Logo Here</div>
      <h2 class="mb-4">Permit Database</h2>
      {% include "search_form.html" %}
      {% include "table.html" %}
    {% endblock %}
    """,

    # ---------- SHARED: Search form ----------
    "search_form.html": """
      <form method="get" class="row g-2 align-items-end mb-4" action="{{ url_for('dashboard') }}">
        <div class="col-md-2">
            <input type="text" class="form-control" name="address" placeholder="Address or City" value="{{ address or '' }}">
        </div>
        <div class="col-md-2">
            <input type="text" class="form-control" name="roof_material" placeholder="Roof Material" value="{{ roof_material or '' }}">
        </div>
        <div class="col-md-2">
            <input type="text" class="form-control" name="owner" placeholder="Owner" value="{{ owner or '' }}">
        </div>
        <div class="col-md-2">
            <input type="text" class="form-control" name="property_use" placeholder="Property Use" value="{{ property_use or '' }}">
        </div>
        <div class="col-md-1">
            <select class="form-select" name="date_filter" id="date_filter" onchange="toggleDateInputs()">
                <option value="">Date</option>
                <option value="before" {% if date_filter == 'before' %}selected{% endif %}>Before</option>
                <option value="after" {% if date_filter == 'after' %}selected{% endif %}>After</option>
                <option value="between" {% if date_filter == 'between' %}selected{% endif %}>Between</option>
            </select>
        </div>
        <div class="col-md-2">
            <input type="date" class="form-control mb-1" name="date_from" value="{{ date_from or '' }}">
            <input type="date" class="form-control" name="date_to" id="date_to" value="{{ date_to or '' }}"
                   {% if date_filter != 'between' %}style="display:none;"{% endif %}>
        </div>
        <div class="col-md-1 text-end">
            <button class="btn btn-primary w-100">Search</button>
        </div>
      </form>
      <script>
        function toggleDateInputs() {
            const filter = document.getElementById("date_filter").value;
            const toField = document.getElementById("date_to");
            if (filter === "between") {
                toField.style.display = "block";
            } else {
                toField.style.display = "none";
                toField.value = "";
            }
        }
        window.addEventListener("DOMContentLoaded", toggleDateInputs);
      </script>
    """,

    # ---------- SHARED: Table ----------
    "table.html": """
      <table class="table table-striped table-bordered table-hover">
        <thead class="table-dark">
            <tr>
                <th>Property Address</th>
                <th>City</th>
                <th>Roof Material</th>
                <th>Date of Last Roof Permit</th>
                <th>Owner</th>
                <th>Property Use</th>
            </tr>
        </thead>
        <tbody>
            {% for prop in properties %}
            <tr onclick="window.location.href='{{ url_for('edit_property', prop_id=prop.id) }}'">
                <td>{{ prop.address }}</td>
                <td>{{ prop.city }}</td>
                <td>{{ prop.roof_material }}</td>
                <td>{{ prop.last_roof_date }}</td>
                <td>{{ prop.owner }}</td>
                <td>{{ prop.property_use }}</td>
            </tr>
            {% endfor %}
        </tbody>
      </table>
    """,

    # ---------- EDIT PROPERTY ----------
    "edit_property.html": """
    {% extends "base.html" %}
    {% block content %}
      <div class="py-2">
        <h2>Edit Property Details</h2>
        {% if request.args.get('saved') == 'true' %}
          <div class="alert alert-success">Changes saved successfully!</div>
        {% endif %}
        <a href="{{ url_for('dashboard') }}" class="btn btn-outline-primary mb-3">Return to Dashboard</a>
        <form method="post">
            <label class="form-label">Address</label>
            <input class="form-control mb-2" name="address" value="{{ prop.address }}">

            <label class="form-label">City</label>
            <input class="form-control mb-2" name="city" value="{{ prop.city }}">

            <label class="form-label">Roof Material</label>
            <input class="form-control mb-2" name="roof_material" value="{{ prop.roof_material }}">

            <label class="form-label">Roof Type</label>
            <input class="form-control mb-2" name="roof_type" value="{{ prop.roof_type }}">

            <label class="form-label">Last Roof Date</label>
            <input class="form-control mb-2" type="date" name="last_roof_date" value="{{ prop.last_roof_date }}">

            <label class="form-label">Owner</label>
            <input class="form-control mb-2" name="owner" value="{{ prop.owner }}">

            <label class="form-label">Parcel Name</label>
            <input class="form-control mb-2" name="parcel_name" value="{{ prop.parcel_name }}">

            <label class="form-label">LLC Mailing Address</label>
            <input class="form-control mb-2" name="llc_mailing_address" value="{{ prop.llc_mailing_address }}">

            <label class="form-label">Property Use</label>
            <input class="form-control mb-2" name="property_use" value="{{ prop.property_use }}">

            <label class="form-label">Adj. Bldg. S.F.</label>
            <input class="form-control mb-2" name="adj_bldg_sf" value="{{ prop.adj_bldg_sf }}">

            <label class="form-label">Year Built</label>
            <input class="form-control mb-3" name="year_built" value="{{ prop.year_built }}">

            <label class="form-label">Add Note</label>
            <textarea class="form-control mb-2" name="notes" placeholder="Add a note..."></textarea>

            <label class="form-label">Previous Notes</label>
            <div id="note-box" style="max-height:220px; overflow-y:auto; border:1px solid #ddd; padding:10px; background:#f8f9fa; margin-bottom:1rem;">
                {% for note in prop.notes|reverse %}
                    <div class="note-card mb-2">
                        <small class="text-muted d-block">{{ note.timestamp }}</small>
                        <div>{{ note.content }}</div>
                    </div>
                {% endfor %}
                {% if not prop.notes %}
                    <div class="text-muted">No notes yet.</div>
                {% endif %}
            </div>

            <label class="form-label">Contact Info</label>
            <div id="contacts">
                {% for c in prop.contact_info %}
                    <div class="row g-2 align-items-center mb-2">
                        <div class="col-md-4">
                            <input class="form-control" name="contact_name" value="{{ c.name }}" placeholder="Name">
                        </div>
                        <div class="col-md-4">
                            <input class="form-control" name="email" value="{{ c.email }}" placeholder="Email">
                        </div>
                        <div class="col-md-3">
                            <input class="form-control" name="phone" value="{{ c.phone }}" placeholder="Phone">
                        </div>
                        <div class="col-md-1 text-muted small">{{ c.job_title }}</div>
                    </div>
                {% endfor %}
            </div>
            <button type="button" class="btn btn-info mb-3"
                onclick="document.getElementById('contacts').insertAdjacentHTML('beforeend', `
                    <div class='row g-2 align-items-center mb-2'>
                        <div class='col-md-4'><input class='form-control' name='contact_name' placeholder='Name'></div>
                        <div class='col-md-4'><input class='form-control' name='email' placeholder='Email'></div>
                        <div class='col-md-3'><input class='form-control' name='phone' placeholder='Phone'></div>
                        <div class='col-md-1 text-muted small'></div>
                    </div>
                `)">
                Add Contact
            </button>
            <button class="btn btn-success" name="save" value="1">Save Changes</button>
        </form>
      </div>
    {% endblock %}
    """,
})

# ==========================================================
# UTILS / BRAND HELPERS
# ==========================================================
def require_login():
    return bool(session.get("username"))

def is_admin():
    return session.get("role") == "admin"

def current_brand():
    return session.get("brand", "generic")

def brand_adjusted_properties(source_props, brand: str):
    """Deep-copy and apply brand-specific presentation tweaks (non-destructive)."""
    props = deepcopy(source_props)
    if brand == "munsie":
        # Historically you showed Pinecrest, but now real data is used.
        # Keep the city if present; if blank, default to Pinecrest, Miami.
        for p in props:
            if not p.get("city"):
                p["city"] = "Pinecrest, Miami"
    return props

def filter_properties_from_request(source_properties=None):
    source_properties = source_properties if source_properties is not None else fake_properties

    address = request.args.get('address', '').lower()
    roof_material = request.args.get('roof_material', '').lower()
    owner = request.args.get('owner', '').lower()
    property_use = request.args.get('property_use', '').lower()
    date_filter = request.args.get('date_filter', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    filtered_properties = list(source_properties)

    if address:
        filtered_properties = [p for p in filtered_properties if address in p.get('address','').lower() or address in p.get('city','').lower()]
    if roof_material:
        filtered_properties = [p for p in filtered_properties if roof_material in p.get('roof_material','').lower()]
    if owner:
        filtered_properties = [p for p in filtered_properties if owner in p.get('owner','').lower()]
    if property_use:
        filtered_properties = [p for p in filtered_properties if property_use in p.get('property_use','').lower()]

    def _parse_date(d):
        try:
            return datetime.strptime(d, '%Y-%m-%d')
        except Exception:
            return None

    try:
        if date_filter and date_from:
            d1 = _parse_date(date_from)
            if d1:
                if date_filter == 'before':
                    filtered_properties = [p for p in filtered_properties if _parse_date(p.get('last_roof_date','')) and _parse_date(p.get('last_roof_date','')) < d1]
                elif date_filter == 'after':
                    filtered_properties = [p for p in filtered_properties if _parse_date(p.get('last_roof_date','')) and _parse_date(p.get('last_roof_date','')) > d1]
                elif date_filter == 'between' and date_to:
                    d2 = _parse_date(date_to)
                    if d2:
                        filtered_properties = [p for p in filtered_properties if _parse_date(p.get('last_roof_date','')) and d1 <= _parse_date(p.get('last_roof_date','')) <= d2]
    except Exception:
        pass

    return {
        "properties": filtered_properties,
        "address": address,
        "roof_material": roof_material,
        "owner": owner,
        "property_use": property_use,
        "date_filter": date_filter,
        "date_from": date_from,
        "date_to": date_to,
    }

# ==========================================================
# ROUTES
# ==========================================================
@app.route("/")
def home():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        info = USERS.get(u)
        if info and info["password"] == p:
            session["username"] = u
            session["role"] = info["role"]
            session["brand"] = info["brand"]
            flash("Logged in successfully.")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.")
    # Give login page a special body class so only it uses the gradient & bigger logo
    return render_template("login.html", title="Login", body_class="login-page")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if not require_login():
        return redirect(url_for("login"))

    brand = current_brand()

    # Choose dataset by brand
    if brand == "munsie":
        dataset = munsie_properties
    else:
        dataset = fake_properties

    # Apply brand presentation tweaks (non-destructive copy)
    brand_props = brand_adjusted_properties(dataset, brand)

    # Filters from query params
    ctx = filter_properties_from_request(brand_props)

    # Choose the correct client page by brand
    if brand == "sci":
        template = "sci_dashboard.html"
    elif brand == "munsie":
        template = "munsie_dashboard.html"
    else:
        template = "generic_dashboard.html"

    return render_template(template, title="Permit Database", **ctx)

@app.route("/property/<int:prop_id>", methods=["GET","POST"])
def edit_property(prop_id):
    if not require_login():
        return redirect(url_for("login"))

    # Locate correct backing list by brand
    brand = current_brand()
    if brand == "munsie":
        backing = munsie_properties
    else:
        backing = fake_properties

    # Always edit the underlying property object
    prop = next((p for p in backing if p["id"] == prop_id), None)
    if not prop:
        flash("Property not found.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        # Update primitive fields
        prop['address'] = request.form.get('address', prop.get('address',''))
        prop['city'] = request.form.get('city', prop.get('city',''))
        prop['roof_material'] = request.form.get('roof_material', prop.get('roof_material',''))
        prop['roof_type'] = request.form.get('roof_type', prop.get('roof_type',''))
        prop['last_roof_date'] = request.form.get('last_roof_date', prop.get('last_roof_date',''))
        prop['owner'] = request.form.get('owner', prop.get('owner',''))
        prop['parcel_name'] = request.form.get('parcel_name', prop.get('parcel_name',''))
        prop['llc_mailing_address'] = request.form.get('llc_mailing_address', prop.get('llc_mailing_address',''))
        prop['property_use'] = request.form.get('property_use', prop.get('property_use',''))
        prop['adj_bldg_sf'] = request.form.get('adj_bldg_sf', prop.get('adj_bldg_sf',''))
        prop['year_built'] = request.form.get('year_built', prop.get('year_built',''))

        # Rebuild contacts from parallel lists
        names  = request.form.getlist('contact_name')
        emails = request.form.getlist('email')
        phones = request.form.getlist('phone')

        new_contacts = []
        for nm, em, ph in zip(names, emails, phones):
            nm = (nm or "").strip()
            em = (em or "").strip().lower()
            ph = (ph or "").strip()
            if nm or em or ph:
                new_contacts.append({
                    "name": nm,
                    "email": em,
                    "phone": ph,
                    "job_title": ""  # keep field for UI consistency
                })
        prop['contact_info'] = new_contacts

        # Notes (append only)
        note_text = (request.form.get('notes', '') or '').strip()
        if note_text:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            prop.setdefault('notes', []).append({"content": note_text, "timestamp": timestamp})

        return redirect(url_for('edit_property', prop_id=prop_id, saved='true'))

    # For GET display (non-destructive)
    prop_view = deepcopy(prop)
    if brand == "munsie" and not prop_view.get("city"):
        prop_view["city"] = "Pinecrest, Miami"

    return render_template("edit_property.html", prop=prop_view, title="Edit Property")

# -------- Admin area --------
@app.route("/admin")
def admin_page():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("dashboard") if session.get("username") else url_for("login"))

    # For display convenience in template
    users_view = {u: type("obj", (), info) for u, info in USERS.items()}
    return render_template("admin.html", users=users_view, title="Admin")

@app.route("/admin/add", methods=["POST"])
def admin_add():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("login"))

    username = request.form.get("username","").strip()
    password = request.form.get("password","").strip()
    role = request.form.get("role","client")
    brand = request.form.get("brand","generic")

    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("admin_page"))
    if username in USERS:
        flash("User already exists.")
        return redirect(url_for("admin_page"))
    if role not in ("admin","client"):
        flash("Invalid role.")
        return redirect(url_for("admin_page"))
    if brand not in ("sci","generic","munsie"):
        flash("Invalid brand.")
        return redirect(url_for("admin_page"))

    USERS[username] = {"password": password, "role": role, "brand": brand}
    flash(f"User '{username}' added.")
    return redirect(url_for("admin_page"))

@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("login"))

    username = request.form.get("username","")
    if username == "admin":
        flash("Cannot delete the primary admin.")
        return redirect(url_for("admin_page"))
    if username in USERS:
        USERS.pop(username)
        flash(f"Deleted '{username}'.")
    else:
        flash("User not found.")
    return redirect(url_for("admin_page"))

# -------- (Optional) Download endpoint (stub demonstrating send_file) --------
# You can adapt this route if you later want to export CSV/Excel snapshots.
@app.route("/download/<brand>")
def download_data(brand):
    if not require_login():
        return redirect(url_for("login"))
    if brand not in ("munsie","sci","generic"):
        abort(404)
    data = munsie_properties if brand == "munsie" else fake_properties
    # Convert to DataFrame
    rows = []
    for p in data:
        # flatten contacts for a quick export example
        base = {k: v for k, v in p.items() if k not in ("contact_info", "notes")}
        # join emails for quick view (you can expand if needed)
        base["contacts_emails"] = ", ".join(c.get("email","") for c in p.get("contact_info", []))
        rows.append(base)
    df = pd.DataFrame(rows)
    out_path = os.path.join(BASE_DIR, f"export_{brand}.csv")
    df.to_csv(out_path, index=False)
    return send_file(out_path, as_attachment=True, download_name=f"{brand}_properties.csv")

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    # When running locally:
    #   pip install -r requirements.txt
    #   python app.py
    # For Render: set start command to "gunicorn app:app"
    app.run(debug=False, use_reloader=False, port=5001)

