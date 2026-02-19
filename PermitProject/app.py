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
import json
import urllib.request
import urllib.parse
import logging
import pandas as pd
import re
import hashlib
import smtplib
from email.message import EmailMessage

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

fake = Faker("en_US")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME or "")
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
MUNSIE_FILE_PATH = os.environ.get(
    "MUNSIE_FILE_PATH",
    os.path.join(BASE_DIR, "data", "ACTUALSTEVELISTcoralsprings.xlsx"),
)

MUNSIE_CONTACT_SLOTS = 5   # VOTER1_* ... VOTER5_*
SCI_TRACKING_FILE_PATH = os.environ.get(
    "SCI_TRACKING_FILE_PATH",
    os.path.join(BASE_DIR, "data", "SCI Tracking of Projects v3.xlsx"),
)
SCI_PROJECT_SHEETS = {
    "Commercial": "Commercial",
    "Residential": "Residential",
    "Repairs": "Repairs",
    "Maintenance": "Maintenance",
}


def _s(val):
    """Stringify a value safely (handle NaN / None)."""
    if pd.isna(val):
        return ""
    return str(val).strip()


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", _s(text).lower()).strip("-")
    return slug or "project"


def _extract_name_and_address(raw_job_name):
    raw = _s(raw_job_name)
    if not raw:
        return "", ""

    lines = [line.strip(" ,") for line in raw.splitlines() if line and line.strip()]
    if len(lines) >= 2:
        project_name = lines[0].strip()
        address_line = " ".join(lines[1:3]).strip(" ,")
        if any(ch.isdigit() for ch in address_line):
            return project_name, address_line

    normalized = re.sub(r"\s+", " ", raw)
    address_match = re.search(
        r"(\d{1,6}\s+[^,]+,\s*[^,]+,\s*F[Ll]\.?\s*\d{5}(?:-\d{4})?)",
        normalized,
    )
    if not address_match:
        address_match = re.search(
            r"(\d{1,6}\s+.*?\bF[Ll]\b\.?\s*\d{5}(?:-\d{4})?)",
            normalized,
        )

    if address_match:
        address = address_match.group(1).strip(" ,")
        project_name = normalized[: address_match.start()].strip(" ,-/")
        return project_name, address

    return normalized, ""


_geocode_cache = {}
_geocode_calls = 0
ENABLE_SCI_GEOCODING = os.environ.get("SCI_ENABLE_GEOCODING", "").strip().lower() in {"1", "true", "yes", "on"}
SCI_GEOCODE_TIMEOUT_SECONDS = float(os.environ.get("SCI_GEOCODE_TIMEOUT_SECONDS", "1.5"))
SCI_GEOCODE_MAX_CALLS = int(os.environ.get("SCI_GEOCODE_MAX_CALLS", "10"))


def _geocode_address(address):
    global _geocode_calls

    if not ENABLE_SCI_GEOCODING:
        return None

    key = _s(address)
    if not key:
        return None
    if key in _geocode_cache:
        return _geocode_cache[key]
    if _geocode_calls >= SCI_GEOCODE_MAX_CALLS:
        _geocode_cache[key] = None
        return None

    encoded = urllib.parse.quote(key)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&limit=1"
    request_obj = urllib.request.Request(
        url,
        headers={"User-Agent": "SCIROOFING/1.0 (project-map)"},
    )
    try:
        _geocode_calls += 1
        with urllib.request.urlopen(request_obj, timeout=SCI_GEOCODE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload:
            coords = [float(payload[0]["lat"]), float(payload[0]["lon"])]
            _geocode_cache[key] = coords
            return coords
    except Exception:
        logger.debug("Geocode lookup failed for address: %s", key, exc_info=True)

    _geocode_cache[key] = None
    return None


def _estimate_coords(address, project_type):
    city_centers = {
        "fort lauderdale": (26.1224, -80.1373),
        "ft. lauderdale": (26.1224, -80.1373),
        "coral springs": (26.2712, -80.2706),
        "sunrise": (26.1669, -80.2564),
        "hollywood": (26.0112, -80.1495),
        "pompano beach": (26.2379, -80.1248),
        "boca raton": (26.3683, -80.1289),
        "weston": (26.1004, -80.3998),
        "tamarac": (26.2129, -80.2498),
        "parkland": (26.31, -80.2373),
        "cooper city": (26.0573, -80.271),
        "miami beach": (25.7907, -80.1300),
        "north miami": (25.8901, -80.1867),
        "boynton beach": (26.5318, -80.0905),
        "lantana": (26.5828, -80.0514),
        "palm beach gardens": (26.8234, -80.1387),
        "key largo": (25.0865, -80.4473),
        "homestead": (25.4687, -80.4776),
    }
    base_lat, base_lng = 26.125, -80.21
    address_lower = _s(address).lower()
    for city, center in city_centers.items():
        if city in address_lower:
            base_lat, base_lng = center
            break

    digest = hashlib.md5(f"{project_type}:{address_lower}".encode("utf-8")).hexdigest()
    lat_offset = (int(digest[:4], 16) / 65535.0 - 0.5) * 0.04
    lng_offset = (int(digest[4:8], 16) / 65535.0 - 0.5) * 0.04
    return [round(base_lat + lat_offset, 6), round(base_lng + lng_offset, 6)]


def load_sci_project_locations(filepath):
    if not os.path.exists(filepath):
        logger.warning("SCI tracking file not found at %s", filepath)
        return []

    projects = []
    seen_ids = set()

    for sheet_name, project_type in SCI_PROJECT_SHEETS.items():
        try:
            df = pd.read_excel(filepath, sheet_name=sheet_name, header=6)
        except Exception:
            logger.exception("Failed reading SCI sheet '%s'", sheet_name)
            continue

        for _, row in df.iterrows():
            raw_job_name = _s(row.get("Job Name"))
            if not raw_job_name or raw_job_name.lower().startswith("total"):
                continue

            project_name, address = _extract_name_and_address(raw_job_name)
            if not project_name:
                project_name = raw_job_name
            if not address:
                continue

            city_match = re.search(r",\s*([^,]+),\s*F[Ll]", address)
            city = city_match.group(1).strip() if city_match else "Florida"
            status = _s(row.get("Project Status")) or _s(row.get("Repair Status")) or _s(row.get("Maint. Status"))

            base_id = _slugify(f"{project_type}-{project_name}-{address}")
            location_id = base_id
            counter = 2
            while location_id in seen_ids:
                location_id = f"{base_id}-{counter}"
                counter += 1
            seen_ids.add(location_id)

            projects.append({
                "id": location_id,
                "name": project_name,
                "type": project_type,
                "address": address,
                "city": city,
                "status": status,
                "coords": _geocode_address(address) or _estimate_coords(address, project_type),
            })

    return projects


_sci_projects_cache = None


def get_sci_project_locations():
    global _sci_projects_cache
    if _sci_projects_cache is None:
        _sci_projects_cache = load_sci_project_locations(SCI_TRACKING_FILE_PATH)
        logger.info("Loaded %s SCI map projects", len(_sci_projects_cache))
    return _sci_projects_cache


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

_munsie_cache = None

def get_munsie_properties():
    """Lazy load Munsie data to avoid import-time crashes."""
    global _munsie_cache
    if _munsie_cache is not None:
        return _munsie_cache
    if not os.path.exists(MUNSIE_FILE_PATH):
        logger.warning(
            "Munsie Excel file not found at %s; continuing with empty dataset.",
            MUNSIE_FILE_PATH,
        )
        _munsie_cache = []
        return _munsie_cache
    try:
        _munsie_cache = load_munsie_properties(MUNSIE_FILE_PATH)
        logger.info(
            "Loaded %s Munsie properties from %s",
            len(_munsie_cache),
            MUNSIE_FILE_PATH,
        )
    except Exception:
        logger.exception("Failed to load Munsie data from %s", MUNSIE_FILE_PATH)
        _munsie_cache = []
    return _munsie_cache

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

           body.landing-page {
                background:
                  radial-gradient(1200px 700px at 10% 10%, #eaf6ff 0%, transparent 45%),
                  radial-gradient(900px 600px at 90% 20%, #fff0eb 0%, transparent 40%),
                  radial-gradient(900px 700px at 70% 85%, #eef9f1 0%, transparent 40%),
                  #f7f8fb;
            }
            body.estimator-page {
                background:
                  radial-gradient(900px 500px at 10% 20%, rgba(56, 189, 248, .18), transparent 55%),
                  radial-gradient(1000px 650px at 90% 10%, rgba(244, 114, 182, .18), transparent 55%),
                  radial-gradient(900px 700px at 80% 80%, rgba(34, 197, 94, .12), transparent 60%),
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

            /* Landing page styles */
            .hero-card {
                position: relative;
                background: linear-gradient(135deg, rgba(255,255,255,.9), rgba(248,250,252,.95));
                border-radius: 28px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                box-shadow: 0 28px 70px rgba(15, 23, 42, 0.18);
                padding: 3.5rem;
                overflow: hidden;
            }
            .hero-card::after {
                content: "";
                position: absolute;
                inset: 0;
                background: radial-gradient(500px 220px at 80% 20%, rgba(59,130,246,.12), transparent 60%);
                pointer-events: none;
            }
            .hero-eyebrow {
                font-size: .8rem;
                text-transform: uppercase;
                letter-spacing: .18em;
                color: #64748b;
                font-weight: 600;
            }
            .hero-title {
                font-size: clamp(2.3rem, 4vw, 3.6rem);
                font-weight: 700;
                color: #0f172a;
            }
            .hero-lead {
                font-size: 1.1rem;
                color: #475569;
            }
            .hero-cta .btn {
                border-radius: 999px;
                padding: .8rem 1.8rem;
                font-weight: 600;
            }
            .pill-card {
                background: #fff;
                border-radius: 18px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                padding: 1.5rem;
                box-shadow: 0 18px 45px rgba(15, 23, 42, 0.08);
                height: 100%;
            }
            .pill-card h6 {
                font-weight: 700;
                margin-bottom: .6rem;
            }
            .brand-badge {
                display: inline-flex;
                align-items: center;
                gap: .5rem;
                padding: .35rem .85rem;
                border-radius: 999px;
                background: rgba(14, 165, 233, .12);
                color: #0284c7;
                font-weight: 600;
                font-size: .85rem;
            }
            .metrics-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                gap: 1rem;
            }
            .metric-card {
                background: rgba(255,255,255,.95);
                border-radius: 16px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                padding: 1rem 1.1rem;
                box-shadow: 0 16px 30px rgba(15, 23, 42, 0.08);
            }
            .metric-card strong {
                font-size: 1.4rem;
                color: #0f172a;
            }
            .metric-card span {
                display: block;
                color: #64748b;
                font-size: .85rem;
                margin-top: .25rem;
            }
            .feature-card {
                background: #fff;
                border-radius: 20px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                padding: 1.75rem;
                box-shadow: 0 20px 40px rgba(15, 23, 42, 0.08);
                height: 100%;
            }
            .feature-card h5 {
                font-weight: 700;
                margin-bottom: .75rem;
            }
            .cta-slab {
                background: linear-gradient(135deg, rgba(14,116,144,.1), rgba(59,130,246,.15));
                border-radius: 24px;
                padding: 2.5rem;
                border: 1px solid rgba(14,116,144,.2);
            }
            .estimator-shell {
                background: linear-gradient(145deg, rgba(255,255,255,.95), rgba(248,250,252,.9));
                border-radius: 28px;
                border: 1px solid rgba(15, 23, 42, 0.06);
                padding: 2.5rem;
                box-shadow: 0 35px 80px rgba(15, 23, 42, 0.12);
                position: relative;
                overflow: hidden;
            }
            .estimator-shell::after {
                content: "";
                position: absolute;
                inset: -40% 30% 40% -10%;
                background: radial-gradient(360px 220px at 20% 20%, rgba(59, 130, 246, .16), transparent 70%);
                pointer-events: none;
            }
            .estimator-panel {
                background: #fff;
                border-radius: 18px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                padding: 1.75rem;
                box-shadow: 0 20px 40px rgba(15, 23, 42, 0.08);
                position: relative;
                z-index: 1;
            }
            .estimate-badge {
                display: inline-flex;
                align-items: center;
                gap: .5rem;
                padding: .35rem .8rem;
                border-radius: 999px;
                background: rgba(59, 130, 246, .15);
                color: #1d4ed8;
                font-weight: 600;
                font-size: .85rem;
            }
            .estimator-header {
                background: linear-gradient(135deg, rgba(30, 64, 175, .92), rgba(59, 130, 246, .92));
                color: #fff;
                border-radius: 20px;
                padding: 1.75rem 2rem;
                box-shadow: 0 24px 40px rgba(30, 64, 175, 0.25);
                margin-bottom: 1.75rem;
            }
            .estimator-header h2 {
                font-weight: 700;
                margin-bottom: .35rem;
            }
            .estimator-header p {
                margin-bottom: 0;
                color: rgba(255,255,255,.85);
            }
            .estimator-steps {
                display: grid;
                gap: .85rem;
            }
            .estimator-step {
                display: flex;
                align-items: center;
                gap: .75rem;
                background: rgba(15, 23, 42, .04);
                border-radius: 12px;
                padding: .6rem .8rem;
                font-size: .9rem;
                color: #1f2937;
            }
            .step-index {
                width: 28px;
                height: 28px;
                border-radius: 50%;
                background: rgba(59, 130, 246, .15);
                color: #1d4ed8;
                display: grid;
                place-items: center;
                font-weight: 700;
            }
            .estimator-kpis {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                gap: .9rem;
            }
            .estimator-kpi {
                background: rgba(255,255,255,.9);
                border-radius: 14px;
                border: 1px solid rgba(15,23,42,.08);
                padding: .9rem 1rem;
                box-shadow: 0 12px 24px rgba(15,23,42,.08);
                font-size: .9rem;
            }
            .estimator-kpi strong {
                display: block;
                font-size: 1.2rem;
                color: #0f172a;
            }
            .estimate-result {
                background: linear-gradient(135deg, rgba(14,116,144,.08), rgba(59,130,246,.08));
                border-radius: 16px;
                padding: 1.5rem;
                border: 1px solid rgba(59,130,246,.15);
            }
            .estimate-kpi {
                background: #fff;
                border-radius: 14px;
                border: 1px solid rgba(15,23,42,.08);
                padding: 1rem 1.2rem;
                box-shadow: 0 12px 24px rgba(15, 23, 42, 0.08);
            }
            .broward-chip {
                display: inline-flex;
                align-items: center;
                gap: .4rem;
                border-radius: 999px;
                font-size: .75rem;
                font-weight: 700;
                letter-spacing: .04em;
                text-transform: uppercase;
                padding: .35rem .7rem;
                color: #0f766e;
                background: rgba(20, 184, 166, .14);
            }
            .loading-overlay {
                position: fixed;
                inset: 0;
                background: rgba(2, 6, 23, .72);
                backdrop-filter: blur(3px);
                display: none;
                z-index: 2000;
                align-items: center;
                justify-content: center;
                color: #fff;
            }
            .loading-overlay.active { display: flex; }
            .loading-card {
                background: rgba(15, 23, 42, .92);
                border: 1px solid rgba(148, 163, 184, .35);
                border-radius: 18px;
                padding: 1.4rem 1.6rem;
                min-width: 290px;
                box-shadow: 0 20px 50px rgba(2, 6, 23, .35);
                text-align: center;
            }
            .spinner {
                width: 44px;
                height: 44px;
                border-radius: 50%;
                border: 3px solid rgba(255, 255, 255, .25);
                border-top-color: #38bdf8;
                animation: spin 1s linear infinite;
                margin: 0 auto .85rem;
            }
            @keyframes spin { to { transform: rotate(360deg); } }
            .waste-table-wrap {
                overflow-x: auto;
                border-radius: 14px;
                border: 1px solid rgba(15, 23, 42, .08);
                background: #fff;
            }
            .waste-table {
                min-width: 660px;
                margin: 0;
            }
            .waste-table th,
            .waste-table td {
                text-align: center;
                padding: .7rem .5rem;
                border-color: rgba(148, 163, 184, .22);
            }
            .waste-label-cell {
                text-align: left !important;
                font-weight: 600;
                color: #334155;
                min-width: 120px;
            }
            .waste-recommended {
                background: rgba(59, 130, 246, .12);
                font-weight: 700;
            }
            .map-shell {
                position: relative;
                border-radius: 18px;
                overflow: hidden;
                border: 1px solid rgba(15, 23, 42, 0.08);
                box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
            }
            .custom-map-pin {
                width: 24px;
                height: 32px;
                position: relative;
            }
            .custom-map-pin .pin-core {
                position: absolute;
                left: 50%;
                top: 2px;
                width: 18px;
                height: 18px;
                background: var(--pin-color);
                border-radius: 50%;
                transform: translateX(-50%);
                border: 2px solid #fff;
                box-shadow: 0 6px 16px rgba(15, 23, 42, 0.25);
            }
            .custom-map-pin .pin-core::after {
                content: "";
                position: absolute;
                left: 50%;
                bottom: -9px;
                width: 14px;
                height: 14px;
                background: var(--pin-color);
                transform: translateX(-50%) rotate(45deg);
                border-radius: 2px;
                box-shadow: 0 6px 12px rgba(15, 23, 42, 0.2);
            }
            .custom-map-pin .pin-core::before {
                content: "";
                position: absolute;
                left: 50%;
                top: 50%;
                width: 6px;
                height: 6px;
                background: #fff;
                border-radius: 50%;
                transform: translate(-50%, -50%);
            }
            #project-map {
                min-height: 420px;
                width: 100%;
            }
            .map-legend {
                display: inline-flex;
                gap: 1rem;
                align-items: center;
                padding: .5rem .85rem;
                background: rgba(255, 255, 255, 0.9);
                border-radius: 999px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                box-shadow: 0 10px 24px rgba(15, 23, 42, 0.1);
                font-size: .85rem;
                font-weight: 600;
            }
            .legend-dot {
                display: inline-block;
                width: 10px;
                height: 10px;
                border-radius: 50%;
                margin-right: .35rem;
            }
            .legend-residential {
                background: #2563eb;
            }
            .legend-commercial {
                background: #f97316;
            }
            .legend-repairs {
                background: #dc2626;
            }
            .legend-maintenance {
                background: #059669;
            }
            .map-results {
                display: grid;
                gap: .75rem;
            }
            .map-result-card {
                background: #fff;
                border-radius: 14px;
                border: 1px solid rgba(15, 23, 42, 0.08);
                padding: .85rem 1rem;
                box-shadow: 0 10px 22px rgba(15, 23, 42, 0.08);
            }
            .map-result-card.active {
                border-color: rgba(37, 99, 235, 0.6);
                box-shadow: 0 12px 30px rgba(37, 99, 235, 0.25);
            }
            .map-filter .btn {
                font-size: .85rem;
                font-weight: 600;
            }
            .map-result-card span {
                display: inline-flex;
                align-items: center;
                gap: .35rem;
                font-size: .8rem;
                font-weight: 600;
                color: #475569;
            }
        </style>
    </head>
    <body class="{{ body_class or '' }}">
        <nav class="navbar navbar-expand-lg navbar-dark bg-dark fixed-top">
          <div class="container-fluid">
            <a class="navbar-brand" href="{% if session.get('brand') == 'sci' %}{{ url_for('sci_landing') }}{% else %}{{ url_for('dashboard') }}{% endif %}">Florida Sales Leads</a>
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
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
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

    # ---------- LANDING ----------
    "landing.html": """
    {% extends "base.html" %}
    {% block content %}
      <section class="py-4">
        <div class="hero-card">
          <div class="row g-4 align-items-center">
            <div class="col-lg-7 position-relative">
              <div class="brand-badge mb-3">Florida Sales Leads</div>
              <h1 class="hero-title mb-3">Sleek lead generation and automation built for modern growth teams.</h1>
              <p class="hero-lead mb-4">
                We deliver high-intent prospects, optimize your sales workflow, and automate follow-up so your team
                can spend more time closing. We also build AI integrations that solve real revenue problems—like
                targeted email sequences, smart segmentation, and next-best-action prompts—using cutting-edge tools.
                Think targeted data, clean handoffs, and systems that scale, powered by AI.
              </p>
              <div class="hero-cta d-flex flex-wrap gap-3 mb-4">
                <a class="btn btn-primary btn-lg" href="{{ url_for('login') }}">Client Login</a>
                <a class="btn btn-outline-secondary btn-lg" href="mailto:chandler@floridasalesleads.com">Book a Consultation</a>
              </div>
              <div class="metrics-grid">
                <div class="metric-card">
                  <strong>7-14 days</strong>
                  <span>Typical launch timeline</span>
                </div>
                <div class="metric-card">
                  <strong>3x faster</strong>
                  <span>Lead-to-close workflows</span>
                </div>
                <div class="metric-card">
                  <strong>95%</strong>
                  <span>Client retention rate</span>
                </div>
              </div>
            </div>
            <div class="col-lg-5 text-center">
              <img src="{{ url_for('static', filename='floridasalesleadslogo.webp') }}"
                   class="brand-logo mb-4"
                   alt="Florida Sales Leads logo"
                   loading="lazy"
                   style="max-height: 190px;">
              <div class="pill-card text-start">
                <h6 class="mb-2">Trusted growth partner</h6>
                <p class="text-muted mb-3">
                  Built for service businesses, contractors, home services, healthcare, legal, and more.
                </p>
                <p class="mb-0">
                  “The fastest way we’ve ever turned data into revenue-ready conversations.”
                </p>
                <small class="text-muted">— Ops Lead, Home Services</small>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="py-4">
        <div class="row g-4">
          <div class="col-md-4">
            <div class="feature-card">
              <h5>Precision Lead Delivery</h5>
                <p class="text-muted mb-0">
                Hyper-targeted lists aligned to your ICP, enriched with decision-maker context and next-step guidance.
                Our Florida leads are powerful because they include direct contact information for the people you
                want to reach across the state.
                </p>
            </div>
          </div>
          <div class="col-md-4">
            <div class="feature-card">
              <h5>Sales Systems & Automation</h5>
                <p class="text-muted mb-0">
                Automated outreach, follow-ups, and reporting pipelines that keep every lead warm and visible, with
                AI-driven targeted emails and personalization that keeps replies high.
                </p>
            </div>
          </div>
          <div class="col-md-4">
            <div class="feature-card">
              <h5>Custom Tools & Portals</h5>
              <p class="text-muted mb-0">
                Lightweight dashboards, client portals, and web apps that keep your team aligned with your sales motion.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section class="py-4">
        <div class="cta-slab">
          <div class="row g-3 align-items-center">
            <div class="col-lg-8">
              <h4 class="mb-2">Ready for a sleeker sales pipeline?</h4>
              <p class="text-muted mb-0">
                Let’s map your current workflow, identify the quickest win, and ship a lead engine that scales with you.
              </p>
            </div>
            <div class="col-lg-4 text-lg-end">
              <a class="btn btn-primary btn-lg" href="mailto:hello@floridasalesleads.com">Schedule a Consultation</a>
            </div>
          </div>
        </div>
      </section>
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
      <h2 class="mb-3">SCI Dashboard</h2>
      <ul class="nav nav-tabs mb-4" id="sciTabs" role="tablist">
        <li class="nav-item" role="presentation">
          <button class="nav-link active" id="permit-tab" data-bs-toggle="tab" data-bs-target="#permit-pane" type="button" role="tab" aria-controls="permit-pane" aria-selected="true">
            Permit Database
          </button>
        </li>
        <li class="nav-item" role="presentation">
          <button class="nav-link" id="project-map-tab" data-bs-toggle="tab" data-bs-target="#project-map-pane" type="button" role="tab" aria-controls="project-map-pane" aria-selected="false">
            Project Map
          </button>
        </li>
      </ul>
      <div class="tab-content" id="sciTabContent">
        <div class="tab-pane fade show active" id="permit-pane" role="tabpanel" aria-labelledby="permit-tab">
          {% include "search_form.html" %}
          {% include "table.html" %}
        </div>
        <div class="tab-pane fade" id="project-map-pane" role="tabpanel" aria-labelledby="project-map-tab">
          <div class="card border-0 shadow-sm">
            <div class="card-body">
              <div id="project-map-locked" class="text-center py-5">
                <h5 class="mb-2">Project Map Locked</h5>
                <p class="text-muted mb-0">Select the Project Map tab to enter the password and view completed roofs.</p>
              </div>
              <div id="project-map-content" class="d-none">
                <link
                  rel="stylesheet"
                  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
                  integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
                  crossorigin=""
                />
                <h5 class="mb-3">Project Map</h5>
                <div class="row g-4">
                  <div class="col-lg-8">
                    <div class="map-shell">
                      <div id="project-map" aria-label="Broward County project map"></div>
                    </div>
                    <div class="mt-3">
                      <div class="map-legend">
                        <span><span class="legend-dot legend-residential"></span>Residential</span>
                        <span><span class="legend-dot legend-commercial"></span>Commercial</span>
                        <span><span class="legend-dot legend-repairs"></span>Repairs</span>
                        <span><span class="legend-dot legend-maintenance"></span>Maintenance</span>
                      </div>
                    </div>
                  </div>
                  <div class="col-lg-4">
                    <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
                      <div class="fw-semibold">Listings</div>
                      <div class="btn-group map-filter" role="group" aria-label="Filter projects">
                        <button type="button" class="btn btn-outline-primary btn-sm active" data-filter="All">All</button>
                        <button type="button" class="btn btn-outline-primary btn-sm" data-filter="Residential">Residential</button>
                        <button type="button" class="btn btn-outline-primary btn-sm" data-filter="Commercial">Commercial</button>
                        <button type="button" class="btn btn-outline-primary btn-sm" data-filter="Repairs">Repairs</button>
                        <button type="button" class="btn btn-outline-primary btn-sm" data-filter="Maintenance">Maintenance</button>
                      </div>
                    </div>
                    <div class="map-results" id="project-map-results"></div>
                  </div>
                </div>
                <script
                  src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
                  integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
                  crossorigin=""
                ></script>
              </div>
            </div>
          </div>
        </div>
      </div>
      <script>
        (() => {
          const projectTab = document.getElementById("project-map-tab");
          const lockedState = document.getElementById("project-map-locked");
          const mapContent = document.getElementById("project-map-content");
          let mapInstance = null;
          let unlocked = false;

          const projectLocations = {{ sci_project_locations|tojson }};

          const iconColors = {
            Residential: "#2563eb",
            Commercial: "#f97316",
            Repairs: "#dc2626",
            Maintenance: "#059669",
          };

          const resultsContainer = document.getElementById("project-map-results");
          const filterGroup = document.querySelector(".map-filter");
          const filterButtons = document.querySelectorAll(".map-filter [data-filter]");
          const markerById = new Map();
          const cardById = new Map();
          let activeFilter = "all";
          let activeLocationId = null;

          const normalizeFilter = (value) => (value ?? "").toString().trim().toLowerCase();
          const isAllFilter = (value) => normalizeFilter(value) === "all";

          const buildIcon = (color) =>
            L.divIcon({
              className: "custom-map-pin",
              html: `<span class="pin-core" style="--pin-color:${color};"></span>`,
              iconSize: [24, 32],
              iconAnchor: [12, 30],
            });

          const setActiveLocation = (locationId, { scroll = false, openPopup = false } = {}) => {
            if (activeLocationId && cardById.has(activeLocationId)) {
              cardById.get(activeLocationId).classList.remove("active");
            }
            activeLocationId = locationId;
            const card = cardById.get(locationId);
            if (card) {
              card.classList.add("active");
              if (scroll) {
                card.scrollIntoView({ behavior: "smooth", block: "center" });
              }
            }
            const marker = markerById.get(locationId);
            if (marker && openPopup) {
              marker.openPopup();
            }
          };

          const applyFilter = (filter) => {
            if (!filter) {
              return;
            }
            activeFilter = normalizeFilter(filter);
            filterButtons.forEach((button) => {
              button.classList.toggle(
                "active",
                normalizeFilter(button.dataset.filter) === activeFilter
              );
            });

            if (mapInstance) {
              mapInstance.closePopup();
              projectLocations.forEach((location) => {
                const matches =
                  isAllFilter(activeFilter) ||
                  normalizeFilter(location.type) === activeFilter;
                const marker = markerById.get(location.id);
                if (marker) {
                  if (matches) {
                    marker.addTo(mapInstance);
                  } else {
                    mapInstance.removeLayer(marker);
                  }
                }
              });
            }

            renderResults();
            if (activeLocationId) {
              const activeLocation = projectLocations.find((loc) => loc.id === activeLocationId);
              if (
                !activeLocation ||
                (!isAllFilter(activeFilter) &&
                  normalizeFilter(activeLocation.type) !== activeFilter)
              ) {
                if (cardById.has(activeLocationId)) {
                  cardById.get(activeLocationId).classList.remove("active");
                }
                activeLocationId = null;
              } else if (cardById.has(activeLocationId)) {
                cardById.get(activeLocationId).classList.add("active");
              }
            }
          };
          const renderResults = () => {
            if (!resultsContainer) {
              return;
            }
            resultsContainer.innerHTML = "";
            cardById.clear();
            projectLocations
              .filter(
                (location) =>
                  isAllFilter(activeFilter) ||
                  normalizeFilter(location.type) === activeFilter
              )
              .forEach((location) => {
                const card = document.createElement("div");
                card.className = "map-result-card";
                card.dataset.locationId = location.id;
                const legendClass = `legend-${(location.type || "").toString().toLowerCase()}`;
                card.innerHTML = `
                  <div class="fw-semibold">${location.name}</div>
                  <span>
                    <span class="legend-dot ${legendClass}"></span>
                    ${location.type} · ${location.city}
                  </span>
                  <div class="text-muted small mt-1">${location.address || ""}</div>
                  <div class="text-muted small">Status: ${location.status || "Unknown"}</div>
                `;
                card.addEventListener("click", () => {
                  setActiveLocation(location.id, { openPopup: true });
                  if (mapInstance) {
                    mapInstance.setView(location.coords, 12.5, { animate: true });
                  }
                });
                resultsContainer.appendChild(card);
                cardById.set(location.id, card);
              });
          };

          const initializeProjectMap = () => {
            if (mapInstance || !window.L) {
              return;
            }
            mapInstance = L.map("project-map", { scrollWheelZoom: false }).setView([26.125, -80.210], 10.5);
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
              maxZoom: 18,
              attribution: "&copy; OpenStreetMap contributors",
            }).addTo(mapInstance);

            projectLocations.forEach((location) => {
              const color = iconColors[location.type] || "#0ea5e9";
              const marker = L.marker(location.coords, { icon: buildIcon(color) }).addTo(mapInstance);
              marker.bindPopup(
                `<strong>${location.name}</strong><br>${location.type} · ${location.city}<br>${location.address || ""}<br>Status: ${location.status || "Unknown"}`
              );
              marker.on("click", () => {
                setActiveLocation(location.id, { scroll: true, openPopup: false });
              });
              markerById.set(location.id, marker);
            });

            renderResults();
            applyFilter(activeFilter);
          };

          if (projectTab) {
            projectTab.addEventListener("show.bs.tab", (event) => {
              if (unlocked) {
                window.setTimeout(() => {
                  mapInstance?.invalidateSize();
                }, 150);
                return;
              }
              event.preventDefault();
              const password = window.prompt("Enter the Project Map password:");
              if (password === "4321") {
                unlocked = true;
                lockedState?.classList.add("d-none");
                mapContent?.classList.remove("d-none");
                initializeProjectMap();
                const tab = new bootstrap.Tab(projectTab);
                tab.show();
              } else if (password !== null) {
                window.alert("Incorrect password. Please try again.");
              }
            });

            const params = new URLSearchParams(window.location.search);
            const tabParam = params.get("tab");
            if (tabParam === "project-map") {
              const tab = new bootstrap.Tab(projectTab);
              tab.show();
            }
          }

          if (filterGroup && filterButtons.length) {
            filterButtons.forEach((button) => {
              button.addEventListener("click", () => {
                applyFilter(button.dataset.filter);
              });
            });
          }
        })();
      </script>
    {% endblock %}
    """,

    # ---------- SCI LANDING ----------
    "sci_landing.html": """
    {% extends "base.html" %}
    {% block content %}
      <img src="{{ url_for('static', filename='SCILOGO.png') }}" alt="SCI Roofing Logo" class="mb-3" style="max-height:70px;">
      <h2 class="mb-2">SCI Dashboard</h2>
      <p class="text-muted mb-4">Choose a function to continue.</p>
      <div class="row g-3">
        <div class="col-md-6">
          <div class="card h-100">
            <div class="card-body d-flex flex-column">
              <h5 class="card-title">Roofing Leads</h5>
              <p class="card-text text-muted flex-grow-1">
                Access the SCI permit database and explore active roofing opportunities.
              </p>
              <a class="btn btn-primary" href="{{ url_for('dashboard') }}">Open Roofing Leads</a>
            </div>
          </div>
        </div>
        <div class="col-md-6">
          <div class="card h-100">
            <div class="card-body d-flex flex-column">
              <h5 class="card-title">Roof Estimator Tool</h5>
              <p class="card-text text-muted flex-grow-1">
                Generate fast estimates with AI-powered guidance.
              </p>
              <a class="btn btn-outline-primary" href="{{ url_for('roof_estimator') }}">Launch Estimator</a>
            </div>
          </div>
        </div>
        <div class="col-md-6">
          <div class="card h-100">
            <div class="card-body d-flex flex-column">
              <h5 class="card-title">Project Map</h5>
              <p class="card-text text-muted flex-grow-1">
                Jump straight into the Project Map tab to review completed roof projects.
              </p>
              <a class="btn btn-outline-primary" href="{{ url_for('dashboard', tab='project-map') }}">
                Open Project Map
              </a>
            </div>
          </div>
        </div>
      </div>
    {% endblock %}
    """,
    # ---------- ROOF ESTIMATOR ----------
    "estimator.html": """
    {% extends "base.html" %}
    {% block content %}
      <section class="py-4">
        <div class="estimator-shell">
          <div class="estimator-header">
            <div class="d-flex flex-wrap align-items-center justify-content-between gap-3">
              <div>
                <div class="estimate-badge mb-2">Estimator Command Center</div>
                <h2>Roof Estimator Tool</h2>
                <p>Use standard estimating or launch the dedicated Broward AI Search beta for address-driven takeoff guidance.</p>
              </div>
            </div>
          </div>
          <div class="row g-4">
            <div class="col-lg-5">
              <div class="estimator-panel h-100">
                <div class="d-flex align-items-center justify-content-between mb-3">
                  <div class="estimate-badge">Powered by GPT-4.1-mini</div>
                  <span class="text-muted small">Version 3.0</span>
                </div>

                <h5 class="mb-2">Standard Estimate</h5>
                <p class="text-muted mb-3">Classic manual estimator for quick proposal pricing.</p>
                <form method="post" class="vstack gap-3 estimator-form" data-loading-message="Generating your estimate...">
                  <input type="hidden" name="action" value="standard_estimate">
                  <div>
                    <label class="form-label">Project Type</label>
                    <select name="project_type" class="form-select" required>
                      <option value="" disabled {% if not form.project_type %}selected{% endif %}>Select type</option>
                      <option value="Residential" {% if form.project_type == 'Residential' %}selected{% endif %}>Residential</option>
                      <option value="Commercial" {% if form.project_type == 'Commercial' %}selected{% endif %}>Commercial</option>
                    </select>
                  </div>
                  <div>
                    <label class="form-label">Material Type</label>
                    <select name="material_type" class="form-select" required>
                      <option value="" disabled {% if not form.material_type %}selected{% endif %}>Select material</option>
                      <option value="Shingle" {% if form.material_type == 'Shingle' %}selected{% endif %}>Shingle</option>
                      <option value="Tile" {% if form.material_type == 'Tile' %}selected{% endif %}>Tile</option>
                      <option value="Metal" {% if form.material_type == 'Metal' %}selected{% endif %}>Metal</option>
                    </select>
                  </div>
                  <div class="row g-3">
                    <div class="col-md-6">
                      <label class="form-label">Square Footage</label>
                      <input type="number" min="200" step="50" name="square_footage" class="form-control" placeholder="e.g. 2400" value="{{ form.square_footage or '' }}" required>
                    </div>
                    <div class="col-md-6">
                      <label class="form-label">Roof Pitch</label>
                      <select name="pitch" class="form-select" required>
                        <option value="" disabled {% if not form.pitch %}selected{% endif %}>Select pitch</option>
                        <option value="Low (0-4/12)" {% if form.pitch == 'Low (0-4/12)' %}selected{% endif %}>Low (0-4/12)</option>
                        <option value="Moderate (5-8/12)" {% if form.pitch == 'Moderate (5-8/12)' %}selected{% endif %}>Moderate (5-8/12)</option>
                        <option value="Steep (9+/12)" {% if form.pitch == 'Steep (9+/12)' %}selected{% endif %}>Steep (9+/12)</option>
                      </select>
                    </div>
                  </div>
                  <div>
                    <label class="form-label">Stories</label>
                    <select name="stories" class="form-select" required>
                      <option value="" disabled {% if not form.stories %}selected{% endif %}>Select stories</option>
                      <option value="1" {% if form.stories == '1' %}selected{% endif %}>1 Story</option>
                      <option value="2" {% if form.stories == '2' %}selected{% endif %}>2 Stories</option>
                      <option value="3" {% if form.stories == '3' %}selected{% endif %}>3 Stories</option>
                      <option value="4+" {% if form.stories == '4+' %}selected{% endif %}>4+ Stories</option>
                    </select>
                  </div>
                  <button class="btn btn-primary btn-lg">Generate Estimate</button>
                </form>

                <hr class="my-4">

                <div class="d-flex align-items-center justify-content-between mb-2">
                  <h5 class="mb-0">Broward AI Search</h5>
                  <span class="broward-chip">Broward Beta</span>
                </div>
                <p class="text-muted mb-3">Separate AI search flow for Broward properties with address + city enrichment and email-ready results.</p>
                <form method="post" class="vstack gap-3 estimator-form" data-loading-message="Running Broward AI Search...">
                  <input type="hidden" name="action" value="broward_ai_search">
                  <div>
                    <label class="form-label">Property Address</label>
                    <input type="text" name="search_address" class="form-control" placeholder="123 Main St" value="{{ broward_form.search_address or '' }}" required>
                  </div>
                  <div>
                    <label class="form-label">City (Broward)</label>
                    <div class="input-group">
                      <input type="text" id="broward-city" name="search_city" class="form-control" placeholder="Fort Lauderdale" value="{{ broward_form.search_city or '' }}" required>
                      <button class="btn btn-outline-primary" id="add-city-btn" type="button">Add City</button>
                    </div>
                    <div class="form-text" id="city-preview">{{ broward_query or 'Address, City will appear here' }}</div>
                  </div>
                  <div>
                    <label class="form-label">Email Result To (optional)</label>
                    <input type="email" name="result_email" class="form-control" placeholder="estimates@company.com" value="{{ broward_form.result_email or '' }}">
                  </div>
                  <button class="btn btn-dark btn-lg">Run Broward AI Search</button>
                </form>
              </div>
            </div>
            <div class="col-lg-7">
              <div class="estimator-panel">
                <div class="d-flex flex-wrap align-items-center justify-content-between mb-3">
                  <h4 class="mb-0">Results Studio</h4>
                  <span class="text-muted small">Client-ready output</span>
                </div>

                {% if broward_result %}
                  <div class="estimate-result mb-3">
                    <div class="row g-3">
                      <div class="col-md-4"><div class="estimate-kpi"><div class="text-muted small">Pitch</div><strong>{{ broward_result.pitch }}/12</strong></div></div>
                      <div class="col-md-4"><div class="estimate-kpi"><div class="text-muted small">Area (sqft)</div><strong>{{ '{:,.0f}'.format(broward_result.final_area) }}</strong></div></div>
                      <div class="col-md-4"><div class="estimate-kpi"><div class="text-muted small">Squares</div><strong>{{ '%.1f'|format(broward_result.final_squares) }}</strong></div></div>
                    </div>
                  </div>
                  <div class="mb-3 small text-muted">
                    <strong>Property:</strong> {{ broward_result.address }}, {{ broward_result.city }}<br>
                    <strong>Ground Plane:</strong> {{ '{:,.0f}'.format(broward_result.ground_area) }} sqft ·
                    <strong>Complexity:</strong> {{ broward_result.complexity|capitalize }} ·
                    <strong>Recommended Waste:</strong> {{ broward_result.recommended_waste }}%
                  </div>
                  <div class="waste-table-wrap mb-3">
                    <table class="table waste-table align-middle">
                      <tr>
                        <th class="waste-label-cell">Waste %</th>
                        {% for row in broward_result.waste_breakdown %}
                          <th class="{% if row.recommended %}waste-recommended{% endif %}">{{ row.waste }}%</th>
                        {% endfor %}
                      </tr>
                      <tr>
                        <td class="waste-label-cell">Area (sqft)</td>
                        {% for row in broward_result.waste_breakdown %}
                          <td class="{% if row.recommended %}waste-recommended{% endif %}">{{ '{:,.0f}'.format(row.area) }}</td>
                        {% endfor %}
                      </tr>
                      <tr>
                        <td class="waste-label-cell">Squares</td>
                        {% for row in broward_result.waste_breakdown %}
                          <td class="{% if row.recommended %}waste-recommended{% endif %}">{{ '%.1f'|format(row.squares) }}</td>
                        {% endfor %}
                      </tr>
                    </table>
                  </div>
                  <div class="text-muted small">Broward AI Search is in beta. Validate on-site before ordering materials.</div>
                {% elif estimate %}
                  <div class="estimate-result mb-4">
                    <div class="row g-3">
                      <div class="col-md-4">
                        <div class="estimate-kpi"><div class="text-muted small">Estimated Range</div><strong>{{ estimate.range }}</strong></div>
                      </div>
                      <div class="col-md-4">
                        <div class="estimate-kpi"><div class="text-muted small">Base Cost / Sq Ft</div><strong>{{ estimate.rate }}</strong></div>
                      </div>
                      <div class="col-md-4">
                        <div class="estimate-kpi"><div class="text-muted small">Confidence</div><strong>{{ estimate.confidence }}</strong></div>
                      </div>
                    </div>
                  </div>
                  <div class="mb-3">{{ estimate.summary | safe }}</div>
                  <div class="text-muted small">This estimate is informational and should be validated with a site inspection.</div>
                {% else %}
                  <div class="text-muted mb-4">Generate a standard estimate or run Broward AI Search to see polished results here.</div>
                  <div class="estimate-result">
                    <h6 class="mb-2">What you will get</h6>
                    <ul class="mb-0 text-muted">
                      <li>Dedicated Broward AI search button and loading state.</li>
                      <li>Professional result layout with waste overage options.</li>
                      <li>Optional email delivery to your chosen recipient.</li>
                    </ul>
                  </div>
                {% endif %}
              </div>
            </div>
          </div>
        </div>
      </section>

      <div class="loading-overlay" id="loading-overlay" aria-live="polite" aria-hidden="true">
        <div class="loading-card">
          <div class="spinner"></div>
          <div class="fw-semibold" id="loading-message">Running...</div>
          <div class="small text-white-50 mt-2">Please wait while we process your request.</div>
        </div>
      </div>

      <script>
        (function () {
          const overlay = document.getElementById('loading-overlay');
          const loadingMessage = document.getElementById('loading-message');
          const forms = document.querySelectorAll('.estimator-form');
          forms.forEach((form) => {
            form.addEventListener('submit', () => {
              loadingMessage.textContent = form.dataset.loadingMessage || 'Working...';
              overlay.classList.add('active');
              overlay.setAttribute('aria-hidden', 'false');
            });
          });

          const cityInput = document.getElementById('broward-city');
          const addressInput = document.querySelector('input[name="search_address"]');
          const addCityBtn = document.getElementById('add-city-btn');
          const cityPreview = document.getElementById('city-preview');
          if (addCityBtn && cityInput && cityPreview && addressInput) {
            addCityBtn.addEventListener('click', () => {
              const address = addressInput.value.trim();
              const city = cityInput.value.trim();
              cityPreview.textContent = (address && city) ? `${address}, ${city}` : (city || 'Address, City will appear here');
            });
          }
        })();
      </script>
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

def _estimate_base_rate(material_type: str):
    rate_table = {
        "shingle": (4.5, 7.5),
        "tile": (8.5, 13.5),
        "metal": (9.5, 15.5),
    }
    return rate_table.get(material_type.lower(), (6.0, 10.0))

def _estimate_pitch_multiplier(pitch: str):
    pitch_map = {
        "low (0-4/12)": 1.0,
        "moderate (5-8/12)": 1.15,
        "steep (9+/12)": 1.3,
    }
    return pitch_map.get(pitch.lower(), 1.1)

def _estimate_story_multiplier(stories: str):
    stories_map = {
        "1": 1.0,
        "2": 1.08,
        "3": 1.15,
        "4+": 1.25,
    }
    return stories_map.get(stories, 1.1)

def calculate_estimate_inputs(payload):
    base_min, base_max = _estimate_base_rate(payload["material_type"])
    pitch_mult = _estimate_pitch_multiplier(payload["pitch"])
    story_mult = _estimate_story_multiplier(payload["stories"])
    project_mult = 1.1 if payload["project_type"].lower() == "commercial" else 1.0
    sqft = max(payload["square_footage"], 200)
    min_total = sqft * base_min * pitch_mult * story_mult * project_mult
    max_total = sqft * base_max * pitch_mult * story_mult * project_mult
    return {
        "base_rate": (base_min, base_max),
        "pitch_multiplier": pitch_mult,
        "story_multiplier": story_mult,
        "project_multiplier": project_mult,
        "min_total": round(min_total, 0),
        "max_total": round(max_total, 0),
    }

def format_currency(value):
    return f"${value:,.0f}"

def generate_estimate(payload):
    estimate_inputs = calculate_estimate_inputs(payload)
    base_min, base_max = estimate_inputs["base_rate"]
    base_rate_label = f"${base_min:.2f} - ${base_max:.2f}"
    range_label = f"{format_currency(estimate_inputs['min_total'])} - {format_currency(estimate_inputs['max_total'])}"
    summary_html = (
        f"<p><strong>Scope:</strong> {payload['project_type']} {payload['material_type']} roof, "
        f"{payload['square_footage']:,} sq ft, {payload['pitch']}, {payload['stories']} stories.</p>"
        "<ul>"
        f"<li>Base material rate: {base_rate_label} per sq ft.</li>"
        f"<li>Pitch factor: {estimate_inputs['pitch_multiplier']:.2f}.</li>"
        f"<li>Access factor: {estimate_inputs['story_multiplier']:.2f}.</li>"
        f"<li>Project type factor: {estimate_inputs['project_multiplier']:.2f}.</li>"
        "</ul>"
    )

    if OPENAI_API_KEY:
        prompt = (
            "You are an expert roofing estimator. Provide a concise estimate summary with a price range, "
            "key cost drivers, and 2-3 bullet points of assumptions. Use a friendly, professional tone. "
            "Avoid legal advice. Keep the answer under 140 words."
        )
        user_payload = (
            f"Project type: {payload['project_type']}\n"
            f"Material: {payload['material_type']}\n"
            f"Square footage: {payload['square_footage']}\n"
            f"Pitch: {payload['pitch']}\n"
            f"Stories: {payload['stories']}\n"
            f"Base rate range per sq ft: {base_rate_label}\n"
            f"Calculated estimate range: {range_label}\n"
        )
        try:
            request_body = json.dumps({
                "model": "gpt-4.1-mini",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_payload},
                ],
                "temperature": 0.4,
            }).encode("utf-8")
            request_obj = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(request_obj, timeout=20) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            ai_text = response_data["choices"][0]["message"]["content"].strip()
            summary_html = f"<p>{ai_text.replace(chr(10), '<br>')}</p>"
        except Exception as exc:
            print(f"⚠️ OpenAI estimate failed: {exc}")

    return {
        "range": range_label,
        "rate": f"{base_rate_label}/sq ft",
        "confidence": "Medium",
        "summary": summary_html,
    }


BROWARD_PITCH_MULTIPLIERS = {
    0: 1.000, 2: 1.014, 3: 1.031, 4: 1.054,
    5: 1.083, 6: 1.118, 7: 1.158, 8: 1.202,
    9: 1.250, 10: 1.302, 12: 1.414,
}
BROWARD_COMPLEXITY_MULTIPLIERS = {
    "simple": 1.00,
    "moderate": 1.05,
    "complex": 1.10,
}
BROWARD_ESTIMATOR_ADJUSTMENT = 1.05
BROWARD_WASTE_OPTIONS = [0, 10, 12, 15, 17, 20, 22]


def _safe_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _fake_bcpa_ground_area(address, city):
    seed = hashlib.md5(f"{address}|{city}".lower().encode("utf-8")).hexdigest()
    return 1400 + (int(seed[:6], 16) % 2800)


def _ai_guess_pitch_complexity(address, city):
    fallback = {"pitch": 5, "complexity": "moderate", "waste_percent": 12}
    if not OPENAI_API_KEY:
        return fallback

    prompt = (
        "You are a Broward County roof estimator assistant. "
        "Given an address and city, provide a rough estimating guess in strict JSON only: "
        "{\"pitch\": integer, \"complexity\": \"simple|moderate|complex\", \"waste_percent\": number}. "
        "Use conservative assumptions for unknown details."
    )
    user_payload = f"Address: {address}\nCity: {city}\nCounty: Broward"

    try:
        request_body = json.dumps({
            "model": "gpt-4.1-mini",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_payload},
            ],
            "temperature": 0.2,
        }).encode("utf-8")
        request_obj = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request_obj, timeout=15) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        ai_text = response_data["choices"][0]["message"]["content"]
        json_match = re.search(r"\{.*\}", ai_text, re.DOTALL)
        parsed = json.loads(json_match.group(0)) if json_match else fallback
        return {
            "pitch": _safe_int(parsed.get("pitch"), 5),
            "complexity": str(parsed.get("complexity", "moderate")).strip().lower(),
            "waste_percent": _safe_float(parsed.get("waste_percent"), 12.0),
        }
    except Exception:
        logger.exception("Broward AI guess failed; using defaults")
        return fallback


def generate_broward_estimate(address, city):
    cleaned_city = city.strip()
    if "broward" not in cleaned_city.lower() and cleaned_city.lower() not in {
        "fort lauderdale", "hollywood", "pompano beach", "coral springs", "sunrise", "weston", "davie",
        "plantation", "miramar", "coconut creek", "deerfield beach", "oakland park", "lauderhill", "tamarac",
    }:
        cleaned_city = f"{cleaned_city} (Broward)" if cleaned_city else "Broward"

    ground_area = _fake_bcpa_ground_area(address, cleaned_city)
    ai_guess = _ai_guess_pitch_complexity(address, cleaned_city)

    pitch = _safe_int(ai_guess.get("pitch"), 5)
    complexity = str(ai_guess.get("complexity", "moderate")).lower()
    waste_percent = _safe_float(ai_guess.get("waste_percent"), 12.0)

    pitch_multiplier = BROWARD_PITCH_MULTIPLIERS.get(pitch, 1.118)
    complexity_multiplier = BROWARD_COMPLEXITY_MULTIPLIERS.get(complexity, 1.05)

    roof_surface = ground_area * pitch_multiplier * complexity_multiplier
    adjusted_surface = roof_surface * BROWARD_ESTIMATOR_ADJUSTMENT
    final_area = adjusted_surface * (1 + waste_percent / 100)
    final_squares = final_area / 100

    waste_breakdown = []
    for option in BROWARD_WASTE_OPTIONS:
        area_option = adjusted_surface * (1 + option / 100)
        waste_breakdown.append({
            "waste": option,
            "area": round(area_option, 0),
            "squares": round(area_option / 100, 1),
            "recommended": abs(option - waste_percent) < 1.1,
        })

    return {
        "address": address,
        "city": cleaned_city,
        "ground_area": round(ground_area, 0),
        "pitch": pitch,
        "complexity": complexity,
        "recommended_waste": round(waste_percent, 1),
        "adjusted_surface": round(adjusted_surface, 0),
        "final_area": round(final_area, 0),
        "final_squares": round(final_squares, 1),
        "waste_breakdown": waste_breakdown,
    }


def build_broward_email_summary(result):
    lines = [
        f"Subject: Broward AI Roof Estimate - {result['address']}, {result['city']}",
        "",
        "Team,",
        "",
        "Below is the Broward AI estimate summary.",
        "",
        f"Property: {result['address']}, {result['city']}",
        f"Ground Plane Area: {result['ground_area']:,.0f} sq ft",
        f"Pitch: {result['pitch']}/12",
        f"Complexity: {result['complexity'].capitalize()}",
        f"Adjusted Surface: {result['adjusted_surface']:,.0f} sq ft",
        f"Recommended Waste: {result['recommended_waste']}%",
        f"Final Quantity: {result['final_area']:,.0f} sq ft ({result['final_squares']} squares)",
        "",
        "Waste Breakdown:",
    ]
    for row in result["waste_breakdown"]:
        lines.append(f"{row['waste']}% -> {row['area']:,.0f} sq ft ({row['squares']} squares)")
    lines.extend([
        "",
        "Notes:",
        "- Broward AI Search is currently in beta.",
        "- Figures are directional and should be field-verified.",
    ])
    return "\n".join(lines)


def send_estimate_email(recipient, subject, body):
    if not (SMTP_HOST and SMTP_FROM_EMAIL):
        return False, "SMTP is not configured. Set SMTP_HOST and SMTP_FROM_EMAIL to enable outbound emails."

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM_EMAIL
        msg["To"] = recipient
        msg.set_content(body)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True, f"Estimate emailed to {recipient}."
    except Exception as exc:
        logger.exception("Failed sending estimate email")
        return False, f"Unable to send email: {exc}"
# ==========================================================
# ROUTES
# ==========================================================
@app.route("/")
def home():
    if session.get("username"):
        if session.get("brand") == "sci":
            return redirect(url_for("sci_landing"))
        return redirect(url_for("dashboard"))
    return render_template("landing.html", title="Florida Sales Leads", body_class="landing-page")

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
            if info["brand"] == "sci":
                return redirect(url_for("sci_landing"))
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.")
    # Give login page a special body class so only it uses the gradient & bigger logo
    return render_template("login.html", title="Login", body_class="login-page")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))

@app.route("/sci")
def sci_landing():
    if not require_login():
        return redirect(url_for("login"))
    if current_brand() != "sci":
        return redirect(url_for("dashboard"))
    return render_template("sci_landing.html", title="SCI Dashboard")

@app.route("/estimator", methods=["GET", "POST"])
def roof_estimator():
    if not require_login():
        return redirect(url_for("login"))

    form_data = {
        "project_type": "",
        "material_type": "",
        "square_footage": "",
        "pitch": "",
        "stories": "",
    }
    broward_form = {
        "search_address": "",
        "search_city": "",
        "result_email": "",
    }
    estimate = None
    broward_result = None
    broward_query = ""

    if request.method == "POST":
        action = request.form.get("action", "standard_estimate").strip()

        if action == "broward_ai_search":
            broward_form = {
                "search_address": request.form.get("search_address", "").strip(),
                "search_city": request.form.get("search_city", "").strip(),
                "result_email": request.form.get("result_email", "").strip(),
            }
            broward_query = ", ".join(part for part in [broward_form["search_address"], broward_form["search_city"]] if part)
            if not broward_form["search_address"] or not broward_form["search_city"]:
                flash("Please provide both address and city for Broward AI Search.")
            else:
                broward_result = generate_broward_estimate(broward_form["search_address"], broward_form["search_city"])
                flash("Broward AI Search complete.")
                if broward_form["result_email"]:
                    summary = build_broward_email_summary(broward_result)
                    subject = f"Broward AI Roof Estimate - {broward_result['address']}, {broward_result['city']}"
                    sent, email_message = send_estimate_email(broward_form["result_email"], subject, summary)
                    flash(email_message)
                    if not sent:
                        flash("Tip: configure SMTP_HOST / SMTP_FROM_EMAIL to enable email delivery.")

        else:
            form_data = {
                "project_type": request.form.get("project_type", "").strip(),
                "material_type": request.form.get("material_type", "").strip(),
                "square_footage": request.form.get("square_footage", "").strip(),
                "pitch": request.form.get("pitch", "").strip(),
                "stories": request.form.get("stories", "").strip(),
            }
            try:
                sqft = int(form_data["square_footage"])
            except ValueError:
                sqft = 0
            if not all([form_data["project_type"], form_data["material_type"], form_data["pitch"], form_data["stories"]]) or sqft <= 0:
                flash("Please complete all fields with valid values.")
            else:
                estimate = generate_estimate({
                    "project_type": form_data["project_type"],
                    "material_type": form_data["material_type"],
                    "square_footage": sqft,
                    "pitch": form_data["pitch"],
                    "stories": form_data["stories"],
                })
                form_data["square_footage"] = sqft

    return render_template(
        "estimator.html",
        title="Roof Estimator",
        form=form_data,
        broward_form=broward_form,
        estimate=estimate,
        broward_result=broward_result,
        broward_query=broward_query,
        body_class="estimator-page",
    )
@app.route("/dashboard")
def dashboard():
    if not require_login():
        return redirect(url_for("login"))

    brand = current_brand()

    # Choose dataset by brand
    if brand == "munsie":
        dataset = get_munsie_properties()
    else:
        dataset = fake_properties

    # Apply brand presentation tweaks (non-destructive copy)
    brand_props = brand_adjusted_properties(dataset, brand)

    # Filters from query params
    ctx = filter_properties_from_request(brand_props)

    # Choose the correct client page by brand
    extra_context = {}
    if brand == "sci":
        template = "sci_dashboard.html"
        extra_context["sci_project_locations"] = get_sci_project_locations()
    elif brand == "munsie":
        template = "munsie_dashboard.html"
    else:
        template = "generic_dashboard.html"

    return render_template(template, title="Permit Database", **ctx, **extra_context)

@app.route("/property/<int:prop_id>", methods=["GET","POST"])
def edit_property(prop_id):
    if not require_login():
        return redirect(url_for("login"))

    # Locate correct backing list by brand
    brand = current_brand()
    if brand == "munsie":
        backing = get_munsie_properties()
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
    data = get_munsie_properties() if brand == "munsie" else fake_properties
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
    port = int(os.environ.get("PORT", "5001"))
    app.run(debug=False, use_reloader=False, port=port)
































