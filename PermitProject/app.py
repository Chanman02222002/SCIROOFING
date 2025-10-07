from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash
)
import os
import random
import pandas as pd
from copy import deepcopy
from faker import Faker
from datetime import datetime
from jinja2 import DictLoader

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-before-deploy")

fake = Faker("en_US")

# ==========================================================
# CONFIG — Relative Path for GitHub Deploy
# ==========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUNSIE_FILE_PATH = os.path.join(BASE_DIR, "data", "ACTUALSTEVELISTcoralsprings.xlsx")

# ==========================================================
# HELPERS
# ==========================================================
def fake_contact():
    return {
        "email": fake.unique.email(),
        "phone": fake.numerify("###-###-####"),
        "job_title": fake.job()
    }

def make_property(i: int):
    """Generate random property for non-Munsie brands."""
    return {
        "id": i,
        "address": fake.street_address(),
        "city": fake.city(),
        "roof_material": random.choice(["Tile", "Shingle", "Metal"]),
        "roof_type": random.choice(["Hip", "Gable", "Flat"]),
        "last_roof_date": fake.date_between(start_date='-30y', end_date='today').strftime('%Y-%m-%d'),
        "owner": fake.name(),
        "parcel_name": fake.company(),
        "llc_mailing_address": fake.address(),
        "property_use": random.choice(["01-01 Single Family", "02-03 Duplex", "03-04 Multi-Family"]),
        "adj_bldg_sf": str(random.randint(1000, 5000)),
        "year_built": str(random.randint(1950, 2023)),
        "contact_info": [fake_contact() for _ in range(random.randint(1, 3))],
        "notes": []
    }

# ==========================================================
# LOAD MUNSIE DATA
# ==========================================================
def load_munsie_properties(filepath):
    """Load real Munsie property + contact data from Excel."""
    df = pd.read_excel(filepath)
    props = []
    for i, row in df.iterrows():
        contacts = []
        for n in range(1, 6):
            email = row.get(f"VOTER{n}_EMAIL")
            phone = row.get(f"VOTER{n}_PHONE")
            name = row.get(f"VOTER{n}_NAME")
            if pd.notna(email) or pd.notna(phone) or pd.notna(name):
                contacts.append({
                    "email": str(email).strip() if pd.notna(email) else "",
                    "phone": str(phone).strip() if pd.notna(phone) else "",
                    "name": str(name).strip() if pd.notna(name) else ""
                })

        prop = {
            "id": i + 1,
            "address": str(row.get("PHY_ADDR1", "")),
            "city": str(row.get("PHY_CITY", "")),
            "roof_material": str(row.get("SCRAPED TYPE", "")),
            "roof_type": str(row.get("SCRAPED SUBTYPE", "")),
            "last_roof_date": str(row.get("LATEST_ROOF_DATE", ""))[:10],
            "owner": str(row.get("OWN_NAME", "")),
            "parcel_name": str(row.get("PERMIT_NUMBER", "")),
            "llc_mailing_address": str(row.get("OWN_ADDR1", "")),
            "property_use": str(row.get("DOR_UC", "")),
            "adj_bldg_sf": str(row.get("TOT_LVG_AREA", "")),
            "year_built": str(row.get("ACT_YR_BLT", "")),
            "contact_info": contacts,
            "notes": []
        }
        props.append(prop)
    return props


# Load Munsie Excel file
try:
    munsie_properties = load_munsie_properties(MUNSIE_FILE_PATH)
    print(f"✅ Loaded {len(munsie_properties)} Munsie records from {MUNSIE_FILE_PATH}")
except Exception as e:
    print(f"⚠️ Could not load Munsie Excel: {e}")
    munsie_properties = []

# Default fake data for other brands
properties = [make_property(i) for i in range(1, 51)]

# ==========================================================
# USERS
# ==========================================================
USERS = {
    "admin": {"password": "admin123", "role": "admin", "brand": "generic"},
    "sci": {"password": "sci123", "role": "client", "brand": "sci"},
    "munsie": {"password": "munsie123", "role": "client", "brand": "munsie"},
}

# ==========================================================
# TEMPLATES
# ==========================================================
app.jinja_loader = DictLoader({
"base.html": """
<!doctype html>
<html>
<head>
  <title>{{ title or 'Florida Sales Leads' }}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body>
<nav class="navbar navbar-dark bg-dark fixed-top">
  <div class="container-fluid">
    <a class="navbar-brand" href="{{ url_for('dashboard') }}">Florida Sales Leads</a>
    <div class="d-flex">
      {% if session.get('username') %}
        <span class="navbar-text me-3">Hi, {{ session['username'] }}</span>
        <a class="btn btn-outline-warning" href="{{ url_for('logout') }}">Logout</a>
      {% endif %}
    </div>
  </div>
</nav>
<div class="container" style="margin-top:80px;">
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      {% for m in messages %}
        <div class="alert alert-info mt-2">{{ m }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  {% block content %}{% endblock %}
</div>
</body>
</html>
""",

"login.html": """
{% extends 'base.html' %}
{% block content %}
<div class="row justify-content-center">
  <div class="col-md-5">
    <div class="card p-4">
      <h3 class="mb-3 text-center">Login</h3>
      <form method="post">
        <input name="username" placeholder="Username" class="form-control mb-2" required>
        <input name="password" type="password" placeholder="Password" class="form-control mb-3" required>
        <button class="btn btn-primary w-100">Login</button>
      </form>
    </div>
  </div>
</div>
{% endblock %}
""",

"sci_dashboard.html": """
{% extends 'base.html' %}
{% block content %}
<img src="{{ url_for('static', filename='SCILOGO.png') }}" style="max-height:60px;">
<h2 class="my-3">SCI Permit Database</h2>
{% include 'table.html' %}
{% endblock %}
""",

"munsie_dashboard.html": """
{% extends 'base.html' %}
{% block content %}
<img src="{{ url_for('static', filename='munsielogo.webp') }}" style="max-height:60px;">
<h2 class="my-3">Munsie Permit Database</h2>
{% include 'table.html' %}
{% endblock %}
""",

"generic_dashboard.html": """
{% extends 'base.html' %}
{% block content %}
<h2 class="my-3">Generic Permit Database</h2>
{% include 'table.html' %}
{% endblock %}
""",

"table.html": """
<table class="table table-striped table-bordered table-hover">
  <thead class="table-dark">
    <tr>
      <th>Address</th>
      <th>City</th>
      <th>Owner</th>
      <th>Roof Material</th>
      <th>Last Roof Date</th>
      <th>Contacts</th>
    </tr>
  </thead>
  <tbody>
    {% for p in properties %}
      <tr>
        <td>{{p.address}}</td>
        <td>{{p.city}}</td>
        <td>{{p.owner}}</td>
        <td>{{p.roof_material}}</td>
        <td>{{p.last_roof_date}}</td>
        <td>
          {% for c in p.contact_info %}
            <div>{{c.name}} {{c.email}} {{c.phone}}</div>
          {% endfor %}
        </td>
      </tr>
    {% endfor %}
  </tbody>
</table>
"""
})

# ==========================================================
# ROUTES
# ==========================================================
@app.route("/")
def home():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        info = USERS.get(u)
        if info and info["password"] == p:
            session["username"] = u
            session["brand"] = info["brand"]
            flash("Logged in successfully.")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.")
    return render_template("login.html", title="Login")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if not session.get("username"):
        return redirect(url_for("login"))
    brand = session.get("brand")
    if brand == "munsie":
        props = munsie_properties
        template = "munsie_dashboard.html"
    elif brand == "sci":
        props = properties
        template = "sci_dashboard.html"
    else:
        props = properties
        template = "generic_dashboard.html"
    return render_template(template, title="Dashboard", properties=props)

# ==========================================================
# RUN
# ==========================================================
if __name__ == "__main__":
    app.run(debug=True, port=5001)

