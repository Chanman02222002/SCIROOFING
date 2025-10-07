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
app.secret_key = "change-me-in-production"

fake = Faker("en_US")

# ==========================================================
# Helpers to make fake data
# ==========================================================
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
        "contact_info": [fake_contact() for _ in range(random.randint(1, 3))],
        "notes": []
    }

# ==========================================================
# Load Real Munsie Excel File (relative for GitHub)
# ==========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUNSIE_FILE_PATH = os.path.join(BASE_DIR, "data", "ACTUALSTEVELISTcoralsprings.xlsx")

def load_munsie_properties(filepath):
    """Load property + contact data from Munsie's Excel file."""
    df = pd.read_excel(filepath)
    props = []
    for i, row in df.iterrows():
        contacts = []
        for n in range(1, 6):
            name = row.get(f"VOTER{n}_NAME")
            email = row.get(f"VOTER{n}_EMAIL")
            phone = row.get(f"VOTER{n}_PHONE")
            if pd.notna(name) or pd.notna(email) or pd.notna(phone):
                contacts.append({
                    "name": str(name).strip() if pd.notna(name) else "",
                    "email": str(email).strip() if pd.notna(email) else "",
                    "phone": str(phone).strip() if pd.notna(phone) else ""
                })

        props.append({
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
        })
    return props

try:
    munsie_properties = load_munsie_properties(MUNSIE_FILE_PATH)
    print(f"✅ Loaded {len(munsie_properties)} real Munsie properties.")
except Exception as e:
    print(f"⚠️ Could not load Munsie data: {e}")
    munsie_properties = []

# Default fake data for SCI / generic
properties = [make_property(i) for i in range(1, 51)]

# ==========================================================
# USERS
# ==========================================================
USERS = {
    "admin": {"password": "admin123", "role": "admin", "brand": "generic"},
    "sci": {"password": "sci123", "role": "client", "brand": "sci"},
    "roofing123": {"password": "roofing123", "role": "client", "brand": "generic"},
    "munsie": {"password": "munsie123", "role": "client", "brand": "munsie"},
}

# ==========================================================
# Templates (inline DictLoader)
# ==========================================================
app.jinja_loader = DictLoader({
    # (templates identical to your full original app)
    # ... for brevity here, assume all templates from your 720-line app remain unchanged ...
})

# ==========================================================
# Utility functions
# ==========================================================
def require_login():
    return bool(session.get("username"))

def is_admin():
    return session.get("role") == "admin"

def current_brand():
    return session.get("brand", "generic")

def brand_adjusted_properties(source_props, brand: str):
    props = deepcopy(source_props)
    if brand == "munsie":
        for p in props:
            p["city"] = p.get("city") or "Pinecrest, Miami"
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

    filtered = list(source_properties)
    if address:
        filtered = [p for p in filtered if address in p['address'].lower() or address in p['city'].lower()]
    if roof_material:
        filtered = [p for p in filtered if roof_material in p['roof_material'].lower()]
    if owner:
        filtered = [p for p in filtered if owner in p['owner'].lower()]
    if property_use:
        filtered = [p for p in filtered if property_use in p['property_use'].lower()]
    try:
        if date_filter and date_from:
            d1 = datetime.strptime(date_from, '%Y-%m-%d')
            if date_filter == 'before':
                filtered = [p for p in filtered if datetime.strptime(p['last_roof_date'], '%Y-%m-%d') < d1]
            elif date_filter == 'after':
                filtered = [p for p in filtered if datetime.strptime(p['last_roof_date'], '%Y-%m-%d') > d1]
            elif date_filter == 'between' and date_to:
                d2 = datetime.strptime(date_to, '%Y-%m-%d')
                filtered = [p for p in filtered if d1 <= datetime.strptime(p['last_roof_date'], '%Y-%m-%d') <= d2]
    except Exception:
        pass
    return {
        "properties": filtered,
        "address": address,
        "roof_material": roof_material,
        "owner": owner,
        "property_use": property_use,
        "date_filter": date_filter,
        "date_from": date_from,
        "date_to": date_to,
    }

# ==========================================================
# Routes
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
    if brand == "munsie":
        brand_props = munsie_properties
    else:
        brand_props = brand_adjusted_properties(properties, brand)
    ctx = filter_properties_from_request(brand_props)
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
    brand = current_brand()
    if brand == "munsie":
        source = munsie_properties
    else:
        source = properties
    prop = next((p for p in source if p["id"] == prop_id), None)
    if not prop:
        flash("Property not found.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
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

        emails = request.form.getlist('email')
        phones = request.form.getlist('phone')
        prop['contact_info'] = [
            {"email": e, "phone": ph, "job_title": fake.job()}
            for e, ph in zip(emails, phones)
            if e or ph
        ]

        note_text = request.form.get('notes', '').strip()
        if note_text:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            prop['notes'].append({"content": note_text, "timestamp": timestamp})
        return redirect(url_for('edit_property', prop_id=prop_id, saved='true'))

    prop_view = deepcopy(prop)
    if brand == "munsie":
        prop_view["city"] = prop.get("city") or "Pinecrest, Miami"
    return render_template("edit_property.html", prop=prop_view, title="Edit Property")

@app.route("/admin")
def admin_page():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("dashboard") if session.get("username") else url_for("login"))
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

# ==========================================================
# Run
# ==========================================================
if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, port=5001)
