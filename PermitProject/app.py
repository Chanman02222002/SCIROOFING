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

app = Flask(__name__)
app.secret_key = "change-me-in-production"

fake = Faker("en_US")

# --------------------------
# Helpers to make fake data
# --------------------------
def fake_contact():
    """Return a dict with fake email, phone, and job title."""
    return {
        "email": fake.unique.email(),
        "phone": fake.numerify("###-###-####"),
        "job_title": fake.job()
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
        "llc_mailing_address": fake.address(),
        "property_use": random.choice(["01-01 Single Family", "02-03 Duplex", "03-04 Multi-Family"]),
        "adj_bldg_sf": str(random.randint(1000, 5000)),
        "year_built": str(random.randint(1950, 2023)),
        # Pre-populate with fake contacts (1-3 contacts each), each has email + phone + job title
        "contact_info": [fake_contact() for _ in range(random.randint(1, 3))],
        "notes": []
    }

# --------------------------
# Base data (global)
# --------------------------
properties = [make_property(i) for i in range(1, 51)]

# --------------------------
# Users
# role: "admin" or "client"
# brand: "sci", "generic", or "munsie"
# --------------------------
USERS = {
    "admin":      {"password": "admin123",   "role": "admin",  "brand": "generic"},
    "sci":        {"password": "sci123",     "role": "client", "brand": "sci"},
    "roofing123": {"password": "roofing123", "role": "client", "brand": "generic"},
    # New Munsie account
    "munsie":     {"password": "munsie123",  "role": "client", "brand": "munsie"},
}

# --------------------------
# Templates
# --------------------------
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
            <!-- Default login logo (Florida Sales Leads). You can change this to a static asset if you prefer. -->
            <img src="{{ url_for('brand_logo_fsl') }}" class="brand-logo" alt="Florida Sales Leads logo" loading="lazy">
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
        <img src="{{ url_for('brand_logo_munsie') }}" alt="Munsie Logo" style="max-height:60px" class="me-2">
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
            <textarea class="form-control mb-2" name="notes"></textarea>

            <label class="form-label">Previous Notes</label>
            <div id="note-box" style="max-height:200px; overflow-y:auto; border:1px solid #ddd; padding:10px; background:#f8f9fa; margin-bottom:1rem;">
                {% for note in prop.notes|reverse %}
                    <div class="border rounded p-2 mb-2">
                        <small class="text-muted">{{ note.timestamp }}</small>
                        <div>{{ note.content }}</div>
                    </div>
                {% endfor %}
            </div>

            <label class="form-label">Contact Info</label>
            <div id="contacts">
                {% for c in prop.contact_info %}
                    <div class="input-group mb-2">
                        <input class="form-control" name="email" value="{{ c.email }}" placeholder="Email">
                        <input class="form-control" name="phone" value="{{ c.phone }}" placeholder="Phone">
                        <input class="form-control" name="job_title" value="{{ c.job_title }}" placeholder="Job Title">
                    </div>
                {% endfor %}
            </div>
            <button type="button" class="btn btn-info mb-3"
              onclick="document.getElementById('contacts').insertAdjacentHTML('beforeend', '<div class=\\'input-group mb-2\\'><input class=\\'form-control\\' name=\\'email\\' placeholder=\\'Email\\'><input class=\\'form-control\\' name=\\'phone\\' placeholder=\\'Phone\\'><input class=\\'form-control\\' name=\\'job_title\\' placeholder=\\'Job Title\\'></div>')">
              Add Contact
            </button><br>
            <button class="btn btn-success" name="save" value="1">Save Changes</button>
        </form>
      </div>
    {% endblock %}
    """
})

# --------------------------
# Utility / brand helpers
# --------------------------
def require_login():
    return bool(session.get("username"))

def is_admin():
    return session.get("role") == "admin"

def current_brand():
    return session.get("brand", "generic")

def brand_adjusted_properties(source_props, brand: str):
    """Return a deep-copied list of properties with brand-specific tweaks."""
    props = deepcopy(source_props)
    if brand == "munsie":
        # Force all cities to Pinecrest, Miami for this brand's view
        for p in props:
            p["city"] = "Pinecrest, Miami"
    return props

def filter_properties_from_request(source_properties=None):
    source_properties = source_properties if source_properties is not None else properties

    address = request.args.get('address', '').lower()
    roof_material = request.args.get('roof_material', '').lower()
    owner = request.args.get('owner', '').lower()
    property_use = request.args.get('property_use', '').lower()
    date_filter = request.args.get('date_filter', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    filtered_properties = list(source_properties)

    if address:
        filtered_properties = [p for p in filtered_properties if address in p['address'].lower() or address in p['city'].lower()]
    if roof_material:
        filtered_properties = [p for p in filtered_properties if roof_material in p['roof_material'].lower()]
    if owner:
        filtered_properties = [p for p in filtered_properties if owner in p['owner'].lower()]
    if property_use:
        filtered_properties = [p for p in filtered_properties if property_use in p['property_use'].lower()]

    try:
        if date_filter and date_from:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            if date_filter == 'before':
                filtered_properties = [p for p in filtered_properties if datetime.strptime(p['last_roof_date'], '%Y-%m-%d') < date_from_obj]
            elif date_filter == 'after':
                filtered_properties = [p for p in filtered_properties if datetime.strptime(p['last_roof_date'], '%Y-%m-%d') > date_from_obj]
            elif date_filter == 'between' and date_to:
                date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
                filtered_properties = [p for p in filtered_properties if date_from_obj <= datetime.strptime(p['last_roof_date'], '%Y-%m-%d') <= date_to_obj]
    except ValueError:
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

# --------------------------
# Routes
# --------------------------
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
    # Brand-specific view of properties (e.g., Munsie => Pinecrest, Miami)
    brand_props = brand_adjusted_properties(properties, brand)
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

    # Always edit the underlying global property object
    prop = next((p for p in properties if p["id"] == prop_id), None)
    if not prop:
        flash("Property not found.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        # Update fields from the form
        prop['address'] = request.form.get('address', prop['address'])
        prop['city'] = request.form.get('city', prop['city'])
        prop['roof_material'] = request.form.get('roof_material', prop['roof_material'])
        prop['roof_type'] = request.form.get('roof_type', prop['roof_type'])
        prop['last_roof_date'] = request.form.get('last_roof_date', prop['last_roof_date'])
        prop['owner'] = request.form.get('owner', prop['owner'])
        prop['parcel_name'] = request.form.get('parcel_name', prop['parcel_name'])
        prop['llc_mailing_address'] = request.form.get('llc_mailing_address', prop['llc_mailing_address'])
        prop['property_use'] = request.form.get('property_use', prop['property_use'])
        prop['adj_bldg_sf'] = request.form.get('adj_bldg_sf', prop['adj_bldg_sf'])
        prop['year_built'] = request.form.get('year_built', prop['year_built'])

        # Contact list (ensure each contact still has email/phone/job_title if provided)
        emails = request.form.getlist('email')
        phones = request.form.getlist('phone')
        job_titles = request.form.getlist('job_title')
        prop['contact_info'] = [
            {"email": e or fake.email(), "phone": ph or fake.numerify("###-###-####"), "job_title": jt or fake.job()}
            for e, ph, jt in zip(emails, phones, job_titles)
            if (e or ph or jt)  # keep only filled/partially filled lines
        ]

        # Notes
        note_text = request.form.get('notes', '').strip()
        if note_text:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            prop['notes'].append({"content": note_text, "timestamp": timestamp})

        return redirect(url_for('edit_property', prop_id=prop_id, saved='true'))

    # For GET display, if brand is munsie, present Pinecrest, Miami but DO NOT overwrite real data
    prop_view = deepcopy(prop)
    if current_brand() == "munsie":
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

# -------- Serve brand logos from your Windows paths --------
# In production, prefer placing files in /static and using url_for('static', filename='...').
@app.route("/brand-logo-fsl")
def brand_logo_fsl():
    path = r"C:\Users\Tubam\Downloads\PermitProject\floridasalesleadslogo.webp"
    try:
        if os.path.exists(path):
            return send_file(path, mimetype="image/webp", conditional=True)
    except Exception:
        pass
    abort(404)

@app.route("/brand-logo-munsie")
def brand_logo_munsie():
    path = r"C:\Users\Tubam\Downloads\PermitProject\munsielogo.webp"
    try:
        if os.path.exists(path):
            return send_file(path, mimetype="image/webp", conditional=True)
    except Exception:
        pass
    abort(404)

# --------------------------
# Run
# --------------------------
if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, port=5001)

    # For Jupyter compatibility: no reloader
