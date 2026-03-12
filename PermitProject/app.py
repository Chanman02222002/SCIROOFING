from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, send_file, abort, send_from_directory, render_template_string,
    jsonify
)
import os
import random
import shutil
from copy import deepcopy
from faker import Faker
from datetime import datetime
import threading
from jinja2 import DictLoader
import json
import urllib.request
import urllib.parse
import logging
import pandas as pd
import re
import hashlib
import hmac
import base64
import html
import requests
import time
import smtplib
from email.message import EmailMessage
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

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
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.sendgrid.net")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "apikey" if SENDGRID_API_KEY else "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", SENDGRID_API_KEY)
SMTP_FROM_EMAIL = "Shawn@sciroof.com"
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
SCI_CUSTOM_SPOTS_FILE = os.path.join(BASE_DIR, "data", "sci_custom_spots.json")
SCI_EMBED_TOKEN_TTL_SECONDS = int(os.environ.get("SCI_EMBED_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30)))

def _s(val):
    """Stringify a value safely (handle NaN / None)."""
    if pd.isna(val):
        return ""
    return str(val).strip()


def _slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", _s(text).lower()).strip("-")
    return slug or "project"

def _build_sci_embed_token(expires_at=None):
    if expires_at is None:
        expires_at = int(time.time()) + SCI_EMBED_TOKEN_TTL_SECONDS
    payload = str(int(expires_at))
    signature = hmac.new(
        app.secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def _is_valid_sci_embed_token(token):
    if not token or "." not in token:
        return False
    expires_raw, provided_sig = token.split(".", 1)
    if not expires_raw.isdigit():
        return False
    expected_sig = hmac.new(
        app.secret_key.encode("utf-8"),
        expires_raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False
    return int(expires_raw) >= int(time.time())


def _extract_name_and_address(raw_job_name):
    raw = _s(raw_job_name)
    if not raw:
        return "", ""
    # Multi-line entries: first line is project name, remaining lines are address
    lines = [line.strip(" ,") for line in raw.splitlines() if line and line.strip()]
    if len(lines) >= 2:
        project_name = lines[0].strip()
        address_line = ", ".join(lines[1:]).strip(" ,")
        if any(ch.isdigit() for ch in address_line):
            return project_name, address_line

    normalized = re.sub(r"\s+", " ", raw)
    
    _STREET_SUFFIXES = r"(?:St(?:reet)?|Ave(?:nue)?|Dr(?:ive)?|Blvd|Rd|Road|Way|Ln|Lane|Pl(?:ace)?|Ct|Court|Terr(?:ace)?|Cir(?:cle)?|Trail|Pkwy|Hwy)"

    # Pattern 1: Full address with state + zip  "123 Main St, City, FL 33065"
    address_match = re.search(
        r"(\d{1,6}[-\d]*\s+[^,]+,\s*[^,]+,?\s*[Ff][Ll]\.?\s*\d{5}(?:-\d{4})?)",
        normalized,
    )
    if not address_match:
        address_match = re.search(
            r"(\d{1,6}[-\d]*\s+.*?\b[Ff][Ll]\b\.?\s*\d{5}(?:-\d{4})?)",
            normalized,
        )
        # Reference: https://claude.ai/code/session_017ujexvAJvvx7sZy5F5XcJV#:~:text=%2B-,%23%20Pattern%202b%3A%20Address%20ending%20with%20just%20a%20zip%20code%20(no%20FL,),-141
        # Pattern 2: Address with FL + zip but fewer commas  "123 Main St City FL 33065"
    if address_match:
        address = address_match.group(1).strip(" ,")
        project_name = normalized[: address_match.start()].strip(" ,-/")
        return project_name, address

    return normalized, ""


_geocode_cache = {}
_geocode_calls = 0
ENABLE_SCI_GEOCODING = os.environ.get("SCI_ENABLE_GEOCODING", "true").strip().lower() in {"1", "true", "yes", "on"}
SCI_GEOCODE_TIMEOUT_SECONDS = float(os.environ.get("SCI_GEOCODE_TIMEOUT_SECONDS", "5"))
SCI_GEOCODE_MAX_CALLS = int(os.environ.get("SCI_GEOCODE_MAX_CALLS", "200"))


_geocode_last_call_time = 0

def _normalize_address_for_geocoding(address):
    """Clean up an address string for better geocoding results."""
    query = _s(address)
    if not query:
        return query
    # Expand common abbreviations that confuse geocoders
    abbrevs = {
        r'\bFt\.?\s': 'Fort ',
        r'\bSt\.\s': 'Street ',
        r'\bDr\.\s': 'Drive ',
        r'\bAve\.\s': 'Avenue ',
        r'\bBlvd\.\s': 'Boulevard ',
        r'\bRd\.\s': 'Road ',
        r'\bLn\.\s': 'Lane ',
        r'\bCt\.\s': 'Court ',
        r'\bPl\.\s': 'Place ',
        r'\bN\s': 'North ',
        r'\bS\s': 'South ',
        r'\bE\s': 'East ',
        r'\bW\s': 'West ',
        r'\bNE\s': 'NE ',
        r'\bNW\s': 'NW ',
        r'\bSE\s': 'SE ',
        r'\bSW\s': 'SW ',
        r'\bSt\s*Rd\b': 'State Road',
    }
    for pattern, replacement in abbrevs.items():
        query = re.sub(pattern, replacement, query, count=1)
    # Strip double+ spaces
    query = re.sub(r'\s{2,}', ' ', query).strip()
    # Ensure Florida hint is present
    if not re.search(r'\bFL\b', query, re.IGNORECASE) and not re.search(r'\bFlorida\b', query, re.IGNORECASE):
        query = f"{query}, Florida"
    return query

def _geocode_address(address):
    global _geocode_calls, _geocode_last_call_time

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

    # Nominatim usage policy requires max 1 request per second
    now = time.time()
    elapsed = now - _geocode_last_call_time
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    query = _normalize_address_for_geocoding(key)
    encoded = urllib.parse.quote(query)
    url = f"https://nominatim.openstreetmap.org/search?q={encoded}&format=json&addressdetails=1&limit=1"
    request_obj = urllib.request.Request(
        url,
        headers={"User-Agent": "SCIROOFING/1.0 (project-map)"},
    )
    try:
        _geocode_calls += 1
        _geocode_last_call_time = time.time()
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
        # Broward County
        "fort lauderdale": (26.1224, -80.1373),
        "ft. lauderdale": (26.1224, -80.1373),
        "ft lauderdale": (26.1224, -80.1373),
        "coral springs": (26.2712, -80.2706),
        "sunrise": (26.1669, -80.2564),
        "hollywood": (26.0112, -80.1495),
        "pompano beach": (26.2379, -80.1248),
        "boca raton": (26.3683, -80.1289),
        "weston": (26.1004, -80.3998),
        "tamarac": (26.2129, -80.2498),
        "parkland": (26.31, -80.2373),
        "cooper city": (26.0573, -80.271),
        "pembroke pines": (26.0131, -80.3414),
        "miramar": (25.9860, -80.3032),
        "davie": (26.0765, -80.2521),
        "plantation": (26.1276, -80.2331),
        "lauderhill": (26.1669, -80.2136),
        "deerfield beach": (26.3185, -80.0998),
        "deerfield": (26.3185, -80.0998),
        "lauderdale-by-the-sea": (26.1926, -80.0956),
        "hallandale beach": (25.9812, -80.1484),
        "hallandale": (25.9812, -80.1484),
        "oakland park": (26.1722, -80.1524),
        "margate": (26.2445, -80.2069),
        "coconut creek": (26.2517, -80.1791),
        "lighthouse point": (26.2753, -80.0874),
        "wilton manors": (26.1598, -80.1378),
        "north lauderdale": (26.2175, -80.2258),
        "southwest ranches": (26.0567, -80.3484),
        "lazy lake": (26.1613, -80.2347),
        "sea ranch lakes": (26.2080, -80.0956),
        "dania beach": (26.0573, -80.1440),
        # Palm Beach County
        "boca raton": (26.3683, -80.1289),
        "boynton beach": (26.5318, -80.0905),
        "lantana": (26.5828, -80.0514),
        "palm beach gardens": (26.8234, -80.1387),
        "west palm beach": (26.7153, -80.0534),
        "delray beach": (26.4615, -80.0728),
        "lake worth": (26.6170, -80.0557),
        "palm springs": (26.6357, -80.0968),
        "greenacres": (26.6276, -80.1251),
        "jupiter": (26.9342, -80.0942),
        "royal palm beach": (26.7084, -80.2302),
        "wellington": (26.6618, -80.2414),
        "riviera beach": (26.7753, -80.0580),
        "palm beach": (26.7056, -80.0364),
        "jensen beach": (27.2547, -80.2298),
        # Miami-Dade County
        "miami beach": (25.7907, -80.1300),
        "miami": (25.7617, -80.1918),
        "north miami": (25.8901, -80.1867),
        "north miami beach": (25.9331, -80.1623),
        "key largo": (25.0865, -80.4473),
        "homestead": (25.4687, -80.4776),
        "pinecrest": (25.6651, -80.3082),
        "aventura": (25.9565, -80.1392),
        "hialeah": (25.8576, -80.2781),
        "coral gables": (25.7215, -80.2684),
        "key biscayne": (25.6938, -80.1628),
    }
    base_lat, base_lng = 26.125, -80.21
    address_lower = _s(address).lower()
    # Sort by longest city name first so "north miami beach" matches before "miami"
    for city, center in sorted(city_centers.items(), key=lambda x: -len(x[0])):
        if city in address_lower:
            base_lat, base_lng = center
            break

    digest = hashlib.md5(f"{project_type}:{address_lower}".encode("utf-8")).hexdigest()
    lat_offset = (int(digest[:4], 16) / 65535.0 - 0.5) * 0.004
    lng_offset = (int(digest[4:8], 16) / 65535.0 - 0.5) * 0.004
    return [round(base_lat + lat_offset, 6), round(base_lng + lng_offset, 6)]
_NON_PROJECT_PREFIXES = {
    "total", "sub-total", "sub total", "subtotal", "grand total",
    "projects in", "projects with", "projects delayed",
    "commercial projects", "residential projects",
}


_KNOWN_CITIES = [
    "fort lauderdale", "ft. lauderdale", "ft lauderdale", "coral springs",
    "sunrise", "hollywood", "pompano beach", "boca raton", "weston",
    "tamarac", "parkland", "cooper city", "pembroke pines", "miramar",
    "davie", "plantation", "lauderhill", "deerfield beach", "deerfield",
    "lauderdale-by-the-sea", "hallandale beach", "hallandale",
    "oakland park", "margate", "coconut creek", "lighthouse point",
    "wilton manors", "north lauderdale", "southwest ranches",
    "dania beach", "boynton beach", "lantana", "palm beach gardens",
    "west palm beach", "delray beach", "lake worth", "palm springs",
    "greenacres", "jupiter", "royal palm beach", "wellington",
    "riviera beach", "palm beach", "jensen beach", "miami beach",
    "miami", "north miami beach", "north miami", "key largo",
    "homestead", "pinecrest", "aventura", "hialeah", "coral gables",
    "key biscayne",
]
_KNOWN_CITIES_SORTED = sorted(_KNOWN_CITIES, key=len, reverse=True)


def _extract_city_from_address(address):
    """Extract a city name from an address string, trying multiple patterns."""
    # Pattern 1: "..., City, FL ..." (city between two commas before FL)
    m = re.search(r",\s*([^,]+),\s*[Ff][Ll]\b", address)
    if m:
        return m.group(1).strip()
    # Pattern 2: Match against known South Florida city names (most reliable)
    addr_lower = address.lower()
    for city in _KNOWN_CITIES_SORTED:
        if city in addr_lower:
            return city.title()
    # Pattern 3: Last resort — grab the word(s) immediately before ", FL"
    m = re.search(r"(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*,\s*[Ff][Ll]\b", address)
    if m:
        return m.group(1).strip()
    return "Florida"

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
            if not raw_job_name:
                continue

            # Skip header/subtotal/summary rows
            lower = raw_job_name.lower().strip()
            if any(lower.startswith(p) for p in _NON_PROJECT_PREFIXES):
                continue

            project_name, address = _extract_name_and_address(raw_job_name)
            if not project_name:
                project_name = raw_job_name
            if not address:
                continue

            city = _extract_city_from_address(address)
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
    return _sci_projects_cache + _load_custom_spots()


def _load_custom_spots():
    if not os.path.exists(SCI_CUSTOM_SPOTS_FILE):
        return []
    try:
        with open(SCI_CUSTOM_SPOTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        logger.debug("Failed to load custom spots", exc_info=True)
        return []


def _save_custom_spots(spots):
    os.makedirs(os.path.dirname(SCI_CUSTOM_SPOTS_FILE), exist_ok=True)
    with open(SCI_CUSTOM_SPOTS_FILE, "w") as f:
        json.dump(spots, f, indent=2)


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
    "admin":      {"password": "admin123",   "role": "admin",  "brand": "generic",    "sender_email": ""},
    "adminchan":  {"password": "icecream2",  "role": "admin",  "brand": "adminchan",  "sender_email": ""},
    "sci":        {"password": "sci123",     "role": "client", "brand": "sci",        "sender_email": "Shawn@sciroof.com"},
    "roofing123": {"password": "roofing123", "role": "client", "brand": "generic",    "sender_email": ""},
    "munsie":     {"password": "munsie123",  "role": "client", "brand": "munsie",     "sender_email": ""},
    "jobsdirect": {"password": "icecream2",  "role": "client", "brand": "jobsdirect", "sender_email": "choffman@becastaffing.com"},
}

def _get_sender_email_for_brand(brand):
    """Look up the sender email for a brand by finding the first user with that brand who has a sender_email set."""
    for uname, info in USERS.items():
        if info["brand"] == brand and info.get("sender_email"):
            return info["sender_email"]
    return SMTP_FROM_EMAIL

def _get_sender_email_for_user(username):
    """Get sender email for a specific user, falling back to their brand default, then global default."""
    info = USERS.get(username, {})
    if info.get("sender_email"):
        return info["sender_email"]
    if info.get("brand"):
        return _get_sender_email_for_brand(info["brand"])
    return SMTP_FROM_EMAIL

# ==========================================================
# EMAIL MANAGER DATA (in-memory, keyed by client username)
# ==========================================================
# Structure per client:
#   { "lists": [ {"name": str, "emails": [str], "uploaded_at": str} ],
#     "schedules": [ {"list_name": str, "scheduled_for": str, "subject": str, "status": str} ] }
EMAIL_MANAGER_DATA = {}
def _get_client_email_data(username):
    """Return email manager data for a client, initialising if needed."""
    if username not in EMAIL_MANAGER_DATA:
        EMAIL_MANAGER_DATA[username] = {"lists": [], "schedules": []}
    return EMAIL_MANAGER_DATA[username]

# ==========================================================
# EMAIL BLAST SCHEDULER (in-memory)
# ==========================================================
EMAIL_BLAST_SCHEDULES = []  # list of blast dicts
_blast_id_counter = 0

def _next_blast_id():
    global _blast_id_counter
    _blast_id_counter += 1
    return _blast_id_counter

def _get_all_email_lists():
    """Collect every uploaded email list across all clients."""
    all_lists = []
    for uname, data in EMAIL_MANAGER_DATA.items():
        sender = _get_sender_email_for_user(uname)
        for lst in data.get("lists", []):
            all_lists.append({
                "client": uname,
                "name": lst["name"],
                "emails": lst["emails"],
                "uploaded_at": lst.get("uploaded_at", ""),
                "sender_email": sender,
            })
    return all_lists

TEST_EMAIL_ADDRESS = "Chandlerhoffman497@gmail.com"

# ==========================================================
# BACKGROUND SCHEDULER: checks pending blasts every 30 seconds
# ==========================================================
_scheduler_lock = threading.Lock()

def _check_and_send_scheduled_blasts():
    """Run in a background thread. Every 30s, scan EMAIL_BLAST_SCHEDULES
    for pending blasts whose scheduled_for time has passed, then send them."""
    while True:
        time.sleep(30)
        now = datetime.now()
        with _scheduler_lock:
            for blast in EMAIL_BLAST_SCHEDULES:
                if blast["status"] != "pending" or not blast.get("scheduled_for"):
                    continue
                try:
                    sched_dt = datetime.strptime(blast["scheduled_for"], "%Y-%m-%dT%H:%M")
                except (ValueError, TypeError):
                    try:
                        sched_dt = datetime.strptime(blast["scheduled_for"], "%Y-%m-%d %H:%M:%S")
                    except (ValueError, TypeError):
                        try:
                            sched_dt = datetime.strptime(blast["scheduled_for"], "%Y-%m-%d %H:%M")
                        except (ValueError, TypeError):
                            logger.warning("Unparseable scheduled_for: %s", blast["scheduled_for"])
                            continue
                if now >= sched_dt:
                    logger.info("Scheduler: sending blast #%s (scheduled for %s)", blast["id"], blast["scheduled_for"])
                    blast["status"] = "sending"
        # Send outside the lock to avoid blocking
        blasts_to_send = [b for b in EMAIL_BLAST_SCHEDULES if b["status"] == "sending"]
        for blast in blasts_to_send:
            ok_count, fail_count = 0, 0
            for em in blast.get("recipients", []):
                try:
                    ok, err = _send_blast_email(em, blast["subject"], blast["body"],
                                                blast.get("from_name"), sender_email=blast.get("sender_email"))
                    if ok:
                        ok_count += 1
                    else:
                        fail_count += 1
                except Exception:
                    logger.exception("Scheduler: failed to send to %s", em)
                    fail_count += 1
            blast["status"] = "sent"
            blast["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            blast["send_result"] = f"{ok_count} delivered, {fail_count} failed"
            logger.info("Scheduler: blast #%s complete - %s delivered, %s failed",
                        blast["id"], ok_count, fail_count)

_scheduler_thread = threading.Thread(target=_check_and_send_scheduled_blasts, daemon=True)
_scheduler_thread.start()
logger.info("Email blast scheduler thread started.")

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
            .estimator-shell,
            .estimator-shell h1,
            .estimator-shell h2,
            .estimator-shell h3,
            .estimator-shell h4,
            .estimator-shell h5,
            .estimator-shell h6,
            .estimator-shell p,
            .estimator-shell li,
            .estimator-shell label,
            .estimator-shell span,
            .estimator-shell small,
            .estimator-shell td,
            .estimator-shell th {
                color: #0f172a;
            }
            .estimator-shell .text-muted,
            .estimator-panel .text-muted,
            .estimate-result .text-muted,
            .waste-table .text-muted,
            .estimator-shell .card-footer.text-muted {
                color: #334155 !important;
                opacity: 1;
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
                color: #000;
            }
            .estimate-kpi .text-muted {
                color: #000 !important;
                opacity: 1;
            }
            .estimate-kpi strong,
            .estimate-kpi div {
                color: #000;
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
                color: #0f172a;
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
            .map-filter-pills {
                display: flex;
                flex-wrap: wrap;
                gap: .4rem;
            }
            .map-filter-option {
                position: relative;
            }
            .map-filter-option input {
                position: absolute;
                opacity: 0;
                width: 0;
                height: 0;
                overflow: hidden;
            }
            .map-filter-option label {
                cursor: pointer;
                margin: 0;
                border: 1px solid rgba(15, 23, 42, 0.16);
                color: #334155;
                background: #fff;
                border-radius: 999px;
                padding: .35rem .75rem;
                font-size: .82rem;
                font-weight: 700;
                line-height: 1.1;
                transition: all .15s ease;
            }
            .map-filter-option input:checked + label {
                background: #1d4ed8;
                color: #fff;
                border-color: #1d4ed8;
                box-shadow: 0 10px 20px rgba(29, 78, 216, .25);
            }
            .map-filter-option input:focus-visible + label {
                outline: 2px solid rgba(37, 99, 235, .45);
                outline-offset: 2px;
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
            <a class="navbar-brand" href="{% if session.get('brand') == 'sci' %}{{ url_for('sci_landing') }}{% elif session.get('brand') == 'adminchan' %}{{ url_for('adminchan_dashboard') }}{% else %}{{ url_for('dashboard') }}{% endif %}">Florida Sales Leads</a>
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
      <style>
        .blast-header {
          background: linear-gradient(135deg, #1e293b, #334155);
          color: #fff; border-radius: 16px; padding: 1.5rem 2rem; margin-bottom: 1.5rem;
          box-shadow: 0 12px 30px rgba(15,23,42,.15);
        }
        .blast-header h4 { font-weight: 700; margin: 0; }
        .blast-header p { color: rgba(255,255,255,.65); margin: .25rem 0 0; font-size: .9rem; }
        .blast-card { background: #fff; border-radius: 14px; border: 1px solid rgba(15,23,42,.08);
          box-shadow: 0 8px 24px rgba(15,23,42,.06); margin-bottom: 1.25rem; overflow: hidden; }
        .blast-card-header { background: linear-gradient(135deg, rgba(59,130,246,.08), rgba(14,165,233,.05));
          padding: 1rem 1.25rem; border-bottom: 1px solid rgba(15,23,42,.06); font-weight: 700; color: #0f172a; }
        .blast-card-body { padding: 1.25rem; }
        .email-chip { display: inline-block; background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe;
          border-radius: 999px; padding: .2rem .6rem; font-size: .78rem; margin: .15rem; cursor: pointer; transition: all .15s; }
        .email-chip.selected { background: #2563eb; color: #fff; border-color: #2563eb; }
        .email-chip:hover { box-shadow: 0 2px 6px rgba(37,99,235,.2); }
        .sched-badge { display: inline-block; padding: .2rem .55rem; border-radius: 999px;
          font-size: .72rem; font-weight: 700; text-transform: uppercase; }
        .sched-pending { background: rgba(251,191,36,.15); color: #b45309; }
        .sched-sent { background: rgba(34,197,94,.15); color: #15803d; }
        .sched-cancelled { background: rgba(239,68,68,.15); color: #dc2626; }
        .sched-test-sent { background: rgba(59,130,246,.15); color: #1d4ed8; }
        #emailPreview { border: 1px solid #e2e8f0; border-radius: 12px; padding: 1.25rem;
          background: #fafbfc; min-height: 120px; }
      </style>

      <h3 class="mb-3">Admin Panel</h3>
      <ul class="nav nav-tabs mb-4" id="adminTabs" role="tablist">
        <li class="nav-item" role="presentation">
          <button class="nav-link active" id="logins-tab" data-bs-toggle="tab" data-bs-target="#logins-pane"
            type="button" role="tab" aria-selected="true">Manage Logins</button>
        </li>
        <li class="nav-item" role="presentation">
          <button class="nav-link" id="blast-tab" data-bs-toggle="tab" data-bs-target="#blast-pane"
            type="button" role="tab" aria-selected="false">Email Blast Scheduler</button>
        </li>
      </ul>

      <div class="tab-content" id="adminTabContent">
        <!-- ====== TAB 1: Manage Logins (existing) ====== -->
        <div class="tab-pane fade show active" id="logins-pane" role="tabpanel">
          <div class="row">
            <div class="col-lg-6">
              <div class="card mb-4"><div class="card-body">
                <h5>Add New Credential</h5>
                <form method="post" action="{{ url_for('admin_add') }}">
                  <div class="row g-2">
                    <div class="col-md-6"><label class="form-label">Username</label>
                      <input name="username" class="form-control" required></div>
                    <div class="col-md-6"><label class="form-label">Password</label>
                      <input name="password" class="form-control" required></div>
                    <div class="col-md-6"><label class="form-label">Role</label>
                      <select name="role" class="form-select">
                        <option value="client">client</option><option value="admin">admin</option>
                      </select></div>
                    <div class="col-md-6"><label class="form-label">Brand</label>
                      <select name="brand" class="form-select">
                        <option value="sci">sci</option><option value="generic">generic</option>
                        <option value="munsie">munsie</option><option value="adminchan">adminchan</option>
                        <option value="jobsdirect">jobsdirect</option>
                      </select></div>
                    <div class="col-12"><label class="form-label">Sender Email Address</label>
                      <input name="sender_email" type="email" class="form-control" placeholder="e.g. user@company.com (emails will be sent from this address)"></div>
                  </div>
                  <button class="btn btn-success mt-3">Add</button>
                </form>
              </div></div>
            </div>
            <div class="col-lg-6">
              <div class="card"><div class="card-body">
                <h5>Current Users</h5>
                <table class="table table-sm">
                  <thead><tr><th>User</th><th>Role</th><th>Brand</th><th>Sender Email</th><th class="text-end">Actions</th></tr></thead>
                  <tbody>
                    {% for u, info in users.items() %}
                    <tr>
                      <td>{{ u }}</td><td>{{ info.role }}</td><td>{{ info.brand }}</td>
                      <td>
                        <form method="post" action="{{ url_for('admin_update_sender_email') }}" class="d-flex gap-1">
                          <input type="hidden" name="username" value="{{ u }}">
                          <input type="email" name="sender_email" class="form-control form-control-sm" style="min-width:180px;"
                            value="{{ info.sender_email or '' }}" placeholder="not set">
                          <button class="btn btn-sm btn-outline-primary" title="Save">Save</button>
                        </form>
                      </td>
                      <td class="text-end">
                        {% if u != 'admin' %}
                        <form method="post" action="{{ url_for('admin_delete') }}" onsubmit="return confirm('Delete {{u}}?');" class="d-inline">
                          <input type="hidden" name="username" value="{{ u }}">
                          <button class="btn btn-sm btn-outline-danger">Delete</button>
                        </form>
                        {% else %}<span class="text-muted">protected</span>{% endif %}
                      </td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div></div>
            </div>
          </div>
        </div>

        <!-- ====== TAB 2: Email Blast Scheduler ====== -->
        <div class="tab-pane fade" id="blast-pane" role="tabpanel">
          <div class="blast-header">
            <h4>Email Blast Scheduler</h4>
            <p>Select a list, pick recipients, compose your email, and schedule or send a test</p>
          </div>

          {% if not email_lists %}
            <div style="background:rgba(241,245,249,.6); border:2px dashed #cbd5e1; border-radius:12px;
              padding:2rem; text-align:center; color:#94a3b8;">
              <h5 style="color:#64748b;">No email lists available</h5>
              <p>Upload email lists via the Email Manager (adminchan) first.</p>
            </div>
          {% else %}

          <!-- Step 1: Choose List -->
          <div class="blast-card">
            <div class="blast-card-header">Step 1 &mdash; Select an Email List</div>
            <div class="blast-card-body">
              <select id="blastListSelect" class="form-select" onchange="blastListChanged()">
                <option value="">-- choose a list --</option>
                {% for lst in email_lists %}
                  <option value="{{ loop.index0 }}"
                    data-emails="{{ lst.emails | join('||') }}"
                    data-sender="{{ lst.sender_email }}">
                    {{ lst.name }} ({{ lst.client }}) &mdash; {{ lst.emails|length }} emails &mdash; sends from {{ lst.sender_email }}
                  </option>
                {% endfor %}
              </select>
              <div id="senderEmailInfo" class="mt-2" style="display:none;">
                <span class="badge bg-info text-dark" style="font-size:.85rem;">
                  Emails will be sent from: <strong id="senderEmailAddr"></strong>
                </span>
              </div>
            </div>
          </div>

          <!-- Step 2: Pick Recipients -->
          <div class="blast-card" id="step2Card" style="display:none;">
            <div class="blast-card-header">
              Step 2 &mdash; Select Recipients
              <span class="float-end">
                <button type="button" class="btn btn-sm btn-outline-primary" onclick="toggleAllEmails(true)">Select All</button>
                <button type="button" class="btn btn-sm btn-outline-secondary ms-1" onclick="toggleAllEmails(false)">Deselect All</button>
              </span>
            </div>
            <div class="blast-card-body">
              <div id="emailChipsContainer" style="max-height:260px; overflow-y:auto;"></div>
              <div class="mt-2 text-muted" style="font-size:.82rem;">
                <span id="selectedCount">0</span> of <span id="totalCount">0</span> selected
              </div>
            </div>
          </div>

          <!-- Step 3: Compose Email -->
          <div class="blast-card" id="step3Card" style="display:none;">
            <div class="blast-card-header">Step 3 &mdash; Compose Email</div>
            <div class="blast-card-body">
              <div class="row g-3">
                <div class="col-md-6">
                  <label class="form-label fw-bold">Subject Line</label>
                  <input type="text" id="blastSubject" class="form-control" placeholder="e.g. Spring Roofing Special!">
                </div>
                <div class="col-md-6">
                  <label class="form-label fw-bold">From Name (optional)</label>
                  <input type="text" id="blastFromName" class="form-control" placeholder="e.g. SCI Roofing">
                </div>
                <div class="col-12">
                  <label class="form-label fw-bold">Email Body (HTML supported)</label>
                  <textarea id="blastBody" class="form-control" rows="10"
                    placeholder="Write your email content here. You can use HTML for formatting."></textarea>
                </div>
                <div class="col-12">
                  <button type="button" class="btn btn-outline-secondary btn-sm" onclick="previewEmail()">Preview Email</button>
                </div>
                <div class="col-12" id="previewWrap" style="display:none;">
                  <label class="form-label fw-bold">Email Preview</label>
                  <div id="emailPreview"></div>
                </div>
              </div>
            </div>
          </div>

          <!-- Step 4: Schedule / Send Test -->
          <div class="blast-card" id="step4Card" style="display:none;">
            <div class="blast-card-header">Step 4 &mdash; Schedule or Send Test</div>
            <div class="blast-card-body">
              <form method="post" action="{{ url_for('admin_blast_schedule') }}" id="blastForm">
                <input type="hidden" name="list_index" id="hListIndex">
                <input type="hidden" name="selected_emails" id="hSelectedEmails">
                <input type="hidden" name="subject" id="hSubject">
                <input type="hidden" name="from_name" id="hFromName">
                <input type="hidden" name="body" id="hBody">

                <div class="row g-3 align-items-end">
                  <div class="col-md-5">
                    <label class="form-label fw-bold">Schedule Date &amp; Time</label>
                    <input type="datetime-local" name="scheduled_for" id="blastScheduleTime" class="form-control">
                  </div>
                  <div class="col-md-7 d-flex gap-2 flex-wrap">
                    <button type="submit" name="action" value="schedule" class="btn btn-primary"
                      onclick="return prepareBlastSubmit()">Schedule Blast</button>
                    <button type="submit" name="action" value="send_now" class="btn btn-success"
                      onclick="return prepareBlastSubmit()">Send Now</button>
                    <button type="submit" name="action" value="test" class="btn btn-outline-warning"
                      onclick="return prepareTestSubmit()">Send Test Email</button>
                  </div>
                </div>
                <div class="mt-2">
                  <small class="text-muted">Test emails are sent to <strong>{{ test_email }}</strong></small>
                </div>
              </form>
            </div>
          </div>
          {% endif %}

          <!-- Scheduled Blasts -->
          {% if blast_schedules %}
          <div class="blast-card mt-4">
            <div class="blast-card-header">Scheduled &amp; Sent Blasts</div>
            <div class="blast-card-body">
              <div class="table-responsive">
                <table class="table table-sm align-middle mb-0">
                  <thead><tr>
                    <th>ID</th><th>Subject</th><th>List</th><th>Recipients</th>
                    <th>Scheduled For</th><th>Status</th><th class="text-end">Actions</th>
                  </tr></thead>
                  <tbody>
                    {% for b in blast_schedules %}
                    <tr>
                      <td>#{{ b.id }}</td>
                      <td>{{ b.subject or 'No subject' }}</td>
                      <td>{{ b.list_name }}</td>
                      <td>{{ b.recipient_count }}</td>
                      <td>{{ b.scheduled_for or 'Immediate' }}</td>
                      <td><span class="sched-badge sched-{{ b.status|lower|replace(' ','-') }}">{{ b.status }}</span></td>
                      <td class="text-end">
                        {% if b.status == 'pending' %}
                        <form method="post" action="{{ url_for('admin_blast_action') }}" class="d-inline">
                          <input type="hidden" name="blast_id" value="{{ b.id }}">
                          <button name="action" value="send" class="btn btn-sm btn-outline-success"
                            onclick="return confirm('Send this blast to {{ b.recipient_count }} recipients now?')">Send Now</button>
                          <button name="action" value="cancel" class="btn btn-sm btn-outline-danger ms-1">Cancel</button>
                        </form>
                        {% endif %}
                      </td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
          {% endif %}

        </div><!-- /blast-pane -->
      </div><!-- /tab-content -->

      <script>
        // --- Email Blast Scheduler JS ---
        var currentEmails = [];
        var selectedEmails = new Set();

        function blastListChanged() {
          var sel = document.getElementById('blastListSelect');
          var opt = sel.options[sel.selectedIndex];
          var step2 = document.getElementById('step2Card');
          var step3 = document.getElementById('step3Card');
          var step4 = document.getElementById('step4Card');
          var senderInfo = document.getElementById('senderEmailInfo');
          if (!opt.value) { step2.style.display='none'; step3.style.display='none'; step4.style.display='none'; if(senderInfo) senderInfo.style.display='none'; return; }
          var raw = opt.getAttribute('data-emails') || '';
          var senderAddr = opt.getAttribute('data-sender') || 'default';
          currentEmails = raw ? raw.split('||') : [];
          selectedEmails = new Set(currentEmails);
          renderChips();
          step2.style.display=''; step3.style.display=''; step4.style.display='';
          if(senderInfo) { senderInfo.style.display=''; document.getElementById('senderEmailAddr').textContent = senderAddr; }
        }

        function renderChips() {
          var c = document.getElementById('emailChipsContainer');
          c.innerHTML = '';
          currentEmails.forEach(function(em) {
            var chip = document.createElement('span');
            chip.className = 'email-chip' + (selectedEmails.has(em) ? ' selected' : '');
            chip.textContent = em;
            chip.onclick = function() {
              if (selectedEmails.has(em)) selectedEmails.delete(em); else selectedEmails.add(em);
              this.classList.toggle('selected');
              updateCount();
            };
            c.appendChild(chip);
          });
          updateCount();
        }

        function toggleAllEmails(selectAll) {
          if (selectAll) selectedEmails = new Set(currentEmails); else selectedEmails.clear();
          renderChips();
        }

        function updateCount() {
          document.getElementById('selectedCount').textContent = selectedEmails.size;
          document.getElementById('totalCount').textContent = currentEmails.length;
        }

        function previewEmail() {
          var subj = document.getElementById('blastSubject').value || 'No Subject';
          var body = document.getElementById('blastBody').value || '';
          var wrap = document.getElementById('previewWrap');
          var prev = document.getElementById('emailPreview');
          prev.innerHTML = '<h4 style="color:#2563eb;margin-bottom:4px;">' + subj.replace(/</g,'&lt;') + '</h4>'
            + '<hr style="border:none;border-top:2px solid #e2e8f0;margin:8px 0 16px;">'
            + '<div>' + body + '</div>'
            + '<hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0 8px;">'
            + '<p style="font-size:12px;color:#94a3b8;">Email Blast Preview</p>';
          wrap.style.display = '';
        }

        function prepareBlastSubmit() {
          if (selectedEmails.size === 0) { alert('Please select at least one recipient.'); return false; }
          var subj = document.getElementById('blastSubject').value.trim();
          var body = document.getElementById('blastBody').value.trim();
          if (!subj) { alert('Please enter a subject line.'); return false; }
          if (!body) { alert('Please enter an email body.'); return false; }
          document.getElementById('hListIndex').value = document.getElementById('blastListSelect').value;
          document.getElementById('hSelectedEmails').value = Array.from(selectedEmails).join('||');
          document.getElementById('hSubject').value = subj;
          document.getElementById('hFromName').value = document.getElementById('blastFromName').value.trim();
          document.getElementById('hBody').value = body;
          return true;
        }

        function prepareTestSubmit() {
          var subj = document.getElementById('blastSubject').value.trim();
          var body = document.getElementById('blastBody').value.trim();
          if (!subj) { alert('Please enter a subject line.'); return false; }
          if (!body) { alert('Please enter an email body.'); return false; }
          document.getElementById('hListIndex').value = document.getElementById('blastListSelect').value;
          document.getElementById('hSelectedEmails').value = '';
          document.getElementById('hSubject').value = subj;
          document.getElementById('hFromName').value = document.getElementById('blastFromName').value.trim();
          document.getElementById('hBody').value = body;
          return true;
        }
      </script>
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
                <h5 class="mb-2">Project Map</h5>
                <div class="alert alert-light border mb-3" role="alert">
                  <div class="small text-muted mb-1">Embed link (full map UI with filters + listings)</div>
                  <input type="text" class="form-control form-control-sm" value="{{ sci_embed_url }}" readonly>
                </div>
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
                      <button type="button" class="btn btn-sm btn-primary" id="add-spot-btn" style="border-radius:999px;font-size:.82rem;font-weight:700;padding:.35rem .85rem;">+ Add Spot</button>
                      <div class="map-filter-pills" role="radiogroup" aria-label="Filter projects">
                        <div class="map-filter-option">
                          <input type="radio" id="project-filter-all" name="project-filter" value="All" checked>
                          <label for="project-filter-all">All</label>
                        </div>
                        <div class="map-filter-option">
                          <input type="radio" id="project-filter-residential" name="project-filter" value="Residential">
                          <label for="project-filter-residential">Residential</label>
                        </div>
                        <div class="map-filter-option">
                          <input type="radio" id="project-filter-commercial" name="project-filter" value="Commercial">
                          <label for="project-filter-commercial">Commercial</label>
                        </div>
                        <div class="map-filter-option">
                          <input type="radio" id="project-filter-repairs" name="project-filter" value="Repairs">
                          <label for="project-filter-repairs">Repairs</label>
                        </div>
                        <div class="map-filter-option">
                          <input type="radio" id="project-filter-maintenance" name="project-filter" value="Maintenance">
                          <label for="project-filter-maintenance">Maintenance</label>
                        </div>
                      </div>
                    </div>
                    <div id="add-spot-form" class="d-none mb-3">
                      <div class="map-result-card" style="border:2px solid #2563eb;">
                        <div class="fw-semibold mb-2">Add New Spot</div>
                        <div class="mb-2">
                          <label for="spot-address" class="form-label small fw-semibold mb-1">Address</label>
                          <input type="text" class="form-control form-control-sm" id="spot-address" placeholder="e.g. 1234 Main St, Coral Springs, FL 33065" required>
                        </div>
                        <div class="mb-2">
                          <label for="spot-status" class="form-label small fw-semibold mb-1">Status</label>
                          <select class="form-select form-select-sm" id="spot-status">
                            <option value="New">New</option>
                            <option value="In Progress">In Progress</option>
                            <option value="Completed">Completed</option>
                            <option value="Pending">Pending</option>
                          </select>
                        </div>
                        <div class="mb-2">
                          <label class="form-label small fw-semibold mb-1">Type</label>
                          <div class="d-flex gap-3">
                            <div class="form-check">
                              <input class="form-check-input" type="radio" name="spot-residential" id="spot-type-yes" value="yes" checked>
                              <label class="form-check-label small" for="spot-type-yes">Residential</label>
                            </div>
                            <div class="form-check">
                              <input class="form-check-input" type="radio" name="spot-residential" id="spot-type-no" value="no">
                              <label class="form-check-label small" for="spot-type-no">Commercial</label>
                            </div>
                          </div>
                        </div>
                        <div class="d-flex gap-2 mt-3">
                          <button type="button" class="btn btn-primary btn-sm" id="spot-submit" style="border-radius:999px;font-weight:700;">Save Spot</button>
                          <button type="button" class="btn btn-outline-secondary btn-sm" id="spot-cancel" style="border-radius:999px;font-weight:700;">Cancel</button>
                        </div>
                        <div id="spot-feedback" class="small mt-2"></div>
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
          const filterInputs = document.querySelectorAll("input[name='project-filter']");
          const markerById = new Map();
          const cardById = new Map();
          const getLocationKey = (location, index) =>
            location?.id ?? `${location?.name || "project"}-${location?.address || "location"}-${index}`;
          const keyedLocations = projectLocations.map((location, index) => ({
            ...location,
            _locationKey: getLocationKey(location, index),
          }));
          let activeFilter = "all";
          let activeLocationId = null;

          const normalizeFilter = (value) => (value ?? "").toString().trim().toLowerCase();
          const isAllFilter = (value) => normalizeFilter(value) === "all";
          const locationMatchesFilter = (location, filterValue = activeFilter) => {
            if (isAllFilter(filterValue)) {
              return true;
            }
            return normalizeFilter(location?.type) === normalizeFilter(filterValue);
          };
          const getVisibleLocations = (filterValue = activeFilter) =>
            keyedLocations.filter((location) => locationMatchesFilter(location, filterValue));

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
            if (!filter) return;

            activeFilter = normalizeFilter(filter);

            filterInputs.forEach((input) => {
              input.checked = normalizeFilter(input.value) === activeFilter;
            });

            const visibleLocations = getVisibleLocations(activeFilter);
            const visibleKeys = new Set(
              visibleLocations.map((location) => location._locationKey)
            );

            if (mapInstance) {
              mapInstance.closePopup();

              keyedLocations.forEach((location) => {
                const marker = markerById.get(location._locationKey);
                if (!marker) return;

                if (visibleKeys.has(location._locationKey)) {
                  marker.addTo(mapInstance);
                } else {
                  mapInstance.removeLayer(marker);
                }
              });
            }

            renderResults();

            if (activeLocationId) {
              const activeLocation = keyedLocations.find(
                (loc) => loc._locationKey === activeLocationId
              );

              if (
                !activeLocation ||
                (!isAllFilter(activeFilter) &&
                  normalizeFilter(activeLocation.type) !== activeFilter)
              ) {
                if (cardById.has(activeLocationId)) {
                  cardById.get(activeLocationId).classList.remove("active");
                }
                activeLocationId = null;
              }
            }
          };
          const renderResults = () => {
            if (!resultsContainer) {
              return;
            }
            resultsContainer.innerHTML = "";
            cardById.clear();
            getVisibleLocations(activeFilter)
              .forEach((location) => {
                const card = document.createElement("div");
                card.className = "map-result-card";
                card.dataset.locationId = location._locationKey;
                const legendClass = `legend-${(location.type || "").toString().toLowerCase()}`;
                card.innerHTML = `
                  <div class="fw-semibold">${location.address || "No address"}</div>
                  
                  <span>
                    <span class="legend-dot ${legendClass}"></span>
                    ${location.type} · ${location.city}
                  </span>
                  <div class="text-muted small mt-1">Status: ${location.status || "Unknown"}</div>
                `;
                card.addEventListener("click", () => {
                  setActiveLocation(location._locationKey, {
                    openPopup: true,
                    scroll: false,
                  });
                  if (mapInstance) {
                    mapInstance.setView(location.coords, 12.5, { animate: true });
                  }
                });
                resultsContainer.appendChild(card);
                cardById.set(location._locationKey, card);
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

            keyedLocations.forEach((location) => {
              const color = iconColors[location.type] || "#0ea5e9";
              const marker = L.marker(location.coords, { icon: buildIcon(color) }).addTo(mapInstance);
              marker.bindPopup(
                `<strong>${location.address || "No address"}</strong><br>${location.type} · ${location.city}<br>Status: ${location.status || "Unknown"}`
              );
              marker.on("click", () => {
                setActiveLocation(location._locationKey, { scroll: true, openPopup: false });
              });
              markerById.set(location._locationKey, marker);
            });

            renderResults();
            applyFilter(activeFilter);

            document.querySelectorAll(".map-filter-option label").forEach((label) => {
              label.addEventListener("click", () => {
                const input = document.getElementById(label.getAttribute("for"));
                if (input) {
                  input.checked = true;
                  applyFilter(input.value);
                }
              });
            });
          };

          /* ---- Add New Spot ---- */
          const addSpotBtn = document.getElementById("add-spot-btn");
          const addSpotForm = document.getElementById("add-spot-form");
          const spotCancel = document.getElementById("spot-cancel");
          const spotSubmit = document.getElementById("spot-submit");
          const spotFeedback = document.getElementById("spot-feedback");

          if (addSpotBtn && addSpotForm) {
            addSpotBtn.addEventListener("click", () => {
              addSpotForm.classList.toggle("d-none");
            });
            spotCancel.addEventListener("click", () => {
              addSpotForm.classList.add("d-none");
              spotFeedback.textContent = "";
            });
            spotSubmit.addEventListener("click", async () => {
              const address = document.getElementById("spot-address").value.trim();
              const status = document.getElementById("spot-status").value;
              const isResidential = document.getElementById("spot-type-yes").checked;
              const spotType = isResidential ? "Residential" : "Commercial";

              if (!address) {
                spotFeedback.textContent = "Please enter an address.";
                spotFeedback.style.color = "#dc2626";
                return;
              }

              spotSubmit.disabled = true;
              spotFeedback.textContent = "Saving...";
              spotFeedback.style.color = "#475569";

              try {
                const resp = await fetch("/api/sci/spots", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ address, status, type: spotType, residential: isResidential }),
                });
                const result = await resp.json();
                if (!resp.ok) {
                  spotFeedback.textContent = result.error || "Failed to save.";
                  spotFeedback.style.color = "#dc2626";
                  return;
                }

                /* Add the new spot to the local data and map */
                const newLocation = { ...result, _locationKey: result.id || `custom-${keyedLocations.length}` };
                keyedLocations.push(newLocation);

                if (mapInstance) {
                  const color = iconColors[newLocation.type] || "#0ea5e9";
                  const marker = L.marker(newLocation.coords, { icon: buildIcon(color) }).addTo(mapInstance);
                  marker.bindPopup(
                    `<strong>${newLocation.address || "No address"}</strong><br>${newLocation.type} · ${newLocation.city}<br>Status: ${newLocation.status || "Unknown"}`
                  );
                  marker.on("click", () => {
                    setActiveLocation(newLocation._locationKey, { scroll: true, openPopup: false });
                  });
                  markerById.set(newLocation._locationKey, marker);
                }

                renderResults();
                applyFilter(activeFilter);

                /* Reset form */
                document.getElementById("spot-address").value = "";
                document.getElementById("spot-status").value = "New";
                document.getElementById("spot-type-yes").checked = true;
                addSpotForm.classList.add("d-none");
                spotFeedback.textContent = "";
              } catch (err) {
                spotFeedback.textContent = "Network error. Please try again.";
                spotFeedback.style.color = "#dc2626";
              } finally {
                spotSubmit.disabled = false;
              }
            });
          }

        
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

          
        })();
      </script>
    {% endblock %}
    """,

    "sci_map_embed.html": """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>SCI Project Map</title>
      <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="" />
      <style>
        html, body { margin:0; padding:0; height:100%; font-family:Arial,sans-serif; color:#0f172a; }
        *, *::before, *::after { box-sizing:border-box; }
        .embed-shell { height:100%; width:100%; display:flex; background:#f8fafc; }
        .map-column { flex:1; min-width:0; position:relative; }
        #project-map { height:100%; width:100%; }
        .sidebar {
          width:340px;
          max-width:42vw;
          background:#fff;
          border-left:1px solid #e2e8f0;
          padding:14px;
          overflow:auto;
        }
        .heading { font-size:1rem; font-weight:700; margin-bottom:8px; }
        .subtext { font-size:.78rem; color:#64748b; margin-bottom:12px; }
        .map-filter-pills { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
        .map-filter-option input { position:absolute; opacity:0; pointer-events:none; }
        .map-filter-option label {
          display:inline-block;
          border:1px solid #cbd5e1;
          border-radius:999px;
          padding:.28rem .72rem;
          font-size:.74rem;
          font-weight:700;
          color:#334155;
          cursor:pointer;
          user-select:none;
          transition:all .18s ease;
        }
        .map-filter-option input:checked + label { border-color:#2563eb; background:#eff6ff; color:#1d4ed8; }
        .map-results { display:grid; gap:8px; }
        .map-result-card { border:1px solid #e2e8f0; border-radius:12px; padding:10px; cursor:pointer; background:#fff; }
        .map-result-card.active { border-color:#2563eb; box-shadow:0 0 0 2px rgba(37,99,235,.15); }
        .map-result-card strong { display:block; font-size:.88rem; margin-bottom:3px; }
        .map-result-card span { display:block; font-size:.76rem; color:#334155; }
        .map-result-card .muted { font-size:.72rem; color:#64748b; margin-top:3px; }
        .legend-dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:6px; vertical-align:middle; }
        .legend-residential { background:#2563eb; }
        .legend-commercial { background:#f97316; }
        .legend-repairs { background:#dc2626; }
        .legend-maintenance { background:#059669; }
        .custom-map-pin { width:24px; height:32px; position:relative; }
        .custom-map-pin .pin-core {
          position:absolute;
          left:50%;
          top:2px;
          width:18px;
          height:18px;
          background:var(--pin-color);
          border-radius:50%;
          transform:translateX(-50%);
          border:2px solid #fff;
          box-shadow:0 6px 16px rgba(15,23,42,.25);
        }
        .custom-map-pin .pin-core::after {
          content:"";
          position:absolute;
          left:50%;
          bottom:-9px;
          width:14px;
          height:14px;
          background:var(--pin-color);
          transform:translateX(-50%) rotate(45deg);
          border-radius:2px;
          box-shadow:0 6px 12px rgba(15,23,42,.2);
        }
        .custom-map-pin .pin-core::before {
          content:"";
          position:absolute;
          left:50%;
          top:50%;
          width:6px;
          height:6px;
          background:#fff;
          border-radius:50%;
          transform:translate(-50%,-50%);
        }
        .legend {
          position:absolute;
          left:10px;
          bottom:10px;
          z-index:500;
          background:rgba(255,255,255,.95);
          border:1px solid #e2e8f0;
          border-radius:10px;
          padding:7px 9px;
          display:flex;
          flex-wrap:wrap;
          gap:8px;
          font-size:.7rem;
          font-weight:700;
        }
        .dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:5px; }
        @media (max-width: 900px) {
          .embed-shell { flex-direction:column; }
          .sidebar { width:100%; max-width:none; max-height:44%; border-left:none; border-top:1px solid #e2e8f0; }
        }
      </style>
    </head>
    <body>
      <div class="embed-shell">
        <div class="map-column">
          <div id="project-map" aria-label="SCI project map"></div>
          <div class="legend">
            <span><span class="dot" style="background:#2563eb"></span>Residential</span>
            <span><span class="dot" style="background:#f97316"></span>Commercial</span>
            <span><span class="dot" style="background:#dc2626"></span>Repairs</span>
            <span><span class="dot" style="background:#059669"></span>Maintenance</span>
          </div>
        </div>
        {% if embed_mode != "lite" %}
        <aside class="sidebar">
          <div class="heading">Property Listings</div>
          <div class="subtext">Filter projects and click a listing to focus the map marker.</div>
          <div class="map-filter-pills" role="radiogroup" aria-label="Filter projects">
            <div class="map-filter-option"><input type="radio" id="project-filter-all" name="project-filter" value="All" checked><label for="project-filter-all">All</label></div>
            <div class="map-filter-option"><input type="radio" id="project-filter-residential" name="project-filter" value="Residential"><label for="project-filter-residential">Residential</label></div>
            <div class="map-filter-option"><input type="radio" id="project-filter-commercial" name="project-filter" value="Commercial"><label for="project-filter-commercial">Commercial</label></div>
            <div class="map-filter-option"><input type="radio" id="project-filter-repairs" name="project-filter" value="Repairs"><label for="project-filter-repairs">Repairs</label></div>
            <div class="map-filter-option"><input type="radio" id="project-filter-maintenance" name="project-filter" value="Maintenance"><label for="project-filter-maintenance">Maintenance</label></div>
          </div>
          <div class="map-results" id="project-map-results"></div>
        </aside>
        {% endif %}
      </div>
      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
      <script>
        (function () {
          const projectLocations = {{ sci_project_locations|tojson }};
          const embedMode = {{ embed_mode|tojson }};
          const iconColors = {
            Residential: "#2563eb",
            Commercial: "#f97316",
            Repairs: "#dc2626",
            Maintenance: "#059669",
          };
          const resultsContainer = document.getElementById("project-map-results");
          const markerById = new Map();
          const cardById = new Map();
          let activeFilter = "All";
          let activeLocationId = null;

          const keyedLocations = (projectLocations || []).map((location, index) => ({
            ...location,
            _locationKey: location.id || `project-${index}`,
          }));

          const buildIcon = (color) => L.divIcon({
            className: "custom-map-pin",
            html: `<span class="pin-core" style="--pin-color:${color};"></span>`,
            iconSize: [24, 32],
            iconAnchor: [12, 30],
          });

          const map = L.map("project-map", { scrollWheelZoom: false }).setView([26.125, -80.210], 10.5);
          L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 18,
            attribution: "&copy; OpenStreetMap contributors",
          }).addTo(map);

          const bounds = [];
          keyedLocations.forEach((location) => {
            if (!Array.isArray(location.coords) || location.coords.length !== 2) {
              return;
            }
            const color = iconColors[location.type] || "#0ea5e9";
            const marker = L.marker(location.coords, { icon: buildIcon(color) }).addTo(map);
            marker.bindPopup(`<strong>${location.address || "No address"}</strong><br>${location.type || "Project"} · ${location.city || ""}<br>Status: ${location.status || "Unknown"}`);
            marker.on("click", () => {
              setActiveLocation(location._locationKey, { scroll: true, openPopup: false });
            });
            markerById.set(location._locationKey, marker);
            bounds.push(location.coords);
          });

          if (bounds.length) {
            map.fitBounds(bounds, { padding: [20, 20] });
          }

          const getVisibleLocations = () => {
            if (activeFilter === "All") return keyedLocations;
            return keyedLocations.filter((location) => location.type === activeFilter);
          };

          const setActiveLocation = (locationId, options = {}) => {
            activeLocationId = locationId;
            cardById.forEach((card, key) => card.classList.toggle("active", key === locationId));
            if (options.openPopup) {
              const marker = markerById.get(locationId);
              if (marker) marker.openPopup();
            }
            if (options.scroll) {
              cardById.get(locationId)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
            }
          };

          const applyFilter = (nextFilter) => {
            activeFilter = nextFilter;
            const visibleIds = new Set(getVisibleLocations().map((location) => location._locationKey));
            markerById.forEach((marker, key) => {
              if (visibleIds.has(key)) {
                marker.addTo(map);
              } else {
                map.removeLayer(marker);
              }
            });
            if (embedMode !== "lite") {
              renderResults();
            }
          };

          const renderResults = () => {
            if (!resultsContainer) return;
            resultsContainer.innerHTML = "";
            cardById.clear();
            getVisibleLocations().forEach((location) => {
              const legendClass = `legend-${(location.type || "").toString().toLowerCase()}`;
              const card = document.createElement("button");
              card.type = "button";
              card.className = "map-result-card";
              card.innerHTML = `<strong>${location.address || "No address"}</strong><span><span class="legend-dot ${legendClass}"></span>${location.type || "Project"} · ${location.city || "Unknown city"}</span><div class="muted">Status: ${location.status || "Unknown"}</div>`;
              card.addEventListener("click", () => {
                setActiveLocation(location._locationKey, { openPopup: true, scroll: false });
                if (Array.isArray(location.coords)) {
                  map.setView(location.coords, 12.5, { animate: true });
                }
              });
              resultsContainer.appendChild(card);
              cardById.set(location._locationKey, card);
            });
          };

          if (embedMode !== "lite") {
            document.querySelectorAll(".map-filter-option label").forEach((label) => {
              label.addEventListener("click", () => {
                const input = document.getElementById(label.getAttribute("for"));
                if (input) {
                  input.checked = true;
                  applyFilter(input.value);
                }
              });
            });
            renderResults();
          }

          applyFilter("All");
        })();
      </script>
    </body>
    </html>
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
                <p>Use standard estimating or launch the dedicated Broward & Palm Beach Estimator beta for address-driven takeoff guidance.</p>
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
                  <h5 class="mb-0">Broward & Palm Beach Estimator</h5>
                  <span class="broward-chip">Beta</span>
                </div>
                <p class="text-muted mb-3">Dedicated AI search flow for Broward + Palm Beach properties with address + city enrichment and email-ready results.</p>
                <form method="post" class="vstack gap-3 estimator-form" data-loading-message="Running Broward & Palm Beach Estimator...">
                  <input type="hidden" name="action" value="broward_ai_search">
                  <div>
                    <label class="form-label">Property Address</label>
                    <input type="text" name="search_address" class="form-control" placeholder="123 Main St" value="{{ broward_form.search_address or '' }}" required>
                  </div>
                  <div>
                    <label class="form-label">City (Broward or Palm Beach)</label>
                    <div class="input-group">
                      <input type="text" id="broward-city" name="search_city" class="form-control" placeholder="Fort Lauderdale" value="{{ broward_form.search_city or '' }}" required>
                      <button class="btn btn-outline-primary" id="add-city-btn" type="button">Add City</button>
                    </div>
                    <div class="form-text" id="city-preview">{{ broward_query or 'Address, City will appear here' }}</div>
                  </div>
                  <button class="btn btn-dark btn-lg">Run Broward & Palm Beach Estimator</button>
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
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Ground Plane Area</div>
                          <strong>{{ '{:,.0f}'.format(broward_result.ground_area) }} sqft</strong>
                        </div>
                      </div>
                
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Pitch</div>
                          <strong>{{ broward_result.pitch }}/12</strong>
                        </div>
                      </div>
                
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Complexity</div>
                          <strong>{{ broward_result.complexity|capitalize }}</strong>
                        </div>
                      </div>
                
                      <div class="col-md-6">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Adjusted Surface</div>
                          <strong>{{ '{:,.0f}'.format(broward_result.adjusted_surface) }} sqft</strong>
                        </div>
                      </div>
                
                      <div class="col-md-6">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Final Area with Waste</div>
                          <strong>{{ '{:,.0f}'.format(broward_result.final_area) }} sqft</strong>
                        </div>
                      </div>
                
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Squares</div>
                          <strong>{{ '%.1f'|format(broward_result.final_squares) }}</strong>
                        </div>
                      </div>
                    </div>
                  </div>
                
                  <div class="mb-3 small text-muted">
                    <strong>Property:</strong> {{ broward_result.address }}, {{ broward_result.city }}<br>
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
                
                  {# ----------------- NEW: IMAGE REPORT SECTION ----------------- #}
                  <div class="row g-3 mt-2 mb-3">
                    {% if broward_result.report_front_image %}
                      <div class="col-md-6">
                        <div class="card shadow-sm h-100">
                          <div class="card-header fw-semibold">Front Photo</div>
                          <div class="card-body p-2">
                            <img class="img-fluid rounded border"
                                 style="width:100%; max-height:360px; object-fit:cover;"
                                 src="{{ broward_result.report_front_image }}"
                                 alt="Front photo">
                          </div>
                          <div class="card-footer small text-muted">
                            Same image bytes sent to AI (embedded).
                          </div>
                        </div>
                      </div>
                    {% endif %}
                
                    {% if broward_result.report_sketch_image %}
                      <div class="col-md-6">
                        <div class="card shadow-sm h-100">
                          <div class="card-header fw-semibold">Property Sketch</div>
                          <div class="card-body p-2">
                            <img class="img-fluid rounded border"
                                 style="width:100%; max-height:360px; object-fit:contain; background:#f8f9fa;"
                                 src="{{ broward_result.report_sketch_image }}"
                                 alt="Sketch screenshot">
                          </div>
                          <div class="card-footer small text-muted">
                            Same image bytes sent to AI (embedded).
                          </div>
                        </div>
                      </div>
                    {% endif %}
                  </div>

                  {% if broward_result.is_palm_beach %}
                    <div class="d-flex justify-content-end mb-3">
                      <a class="btn btn-outline-dark btn-sm" href="{{ url_for('palm_beach_saved_outputs') }}" target="_blank" rel="noopener noreferrer">
                        Open Palm Beach Saved Files
                      </a>
                    </div>
                  {% endif %}
                
                  <div class="text-muted small">
                    Broward & Palm Beach Estimator is in beta. Validate on-site before ordering materials.
                  </div>

                  <hr class="my-4">

                  <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
                    <h5 class="mb-0">SCI Pricing Add-On</h5>
                    <button class="btn btn-outline-success" type="button" id="add-pricing-toggle">Add Pricing</button>
                  </div>
                  <div class="text-muted small mb-3">Add access level + material to convert AI quantity into an operations-ready contract estimate.</div>

                  <div id="add-pricing-panel" class="{% if not pricing_result %}d-none{% endif %}">
                    <form method="post" class="vstack gap-3 estimator-form" data-loading-message="Calculating SCI pricing...">
                      <input type="hidden" name="action" value="add_pricing">
                      <input type="hidden" name="search_address" value="{{ broward_result.address }}">
                      <input type="hidden" name="search_city" value="{{ broward_result.city }}">
                      <div class="row g-3">
                        <div class="col-md-6">
                          <label class="form-label">Floor Level / Access</label>
                          <select name="access_level" class="form-select" required>
                            <option value="" disabled {% if not pricing_form.access_level %}selected{% endif %}>Select access</option>
                            <option value="ground" {% if pricing_form.access_level == 'ground' %}selected{% endif %}>Ground / Single Story</option>
                            <option value="2_story" {% if pricing_form.access_level == '2_story' %}selected{% endif %}>Second Story / Two Story</option>
                            <option value="3_plus" {% if pricing_form.access_level == '3_plus' %}selected{% endif %}>Third Story+ / Difficult Access</option>
                          </select>
                        </div>
                        <div class="col-md-6">
                          <label class="form-label">Material</label>
                          <select name="pricing_material" class="form-select" required>
                            <option value="" disabled {% if not pricing_form.material %}selected{% endif %}>Select material</option>
                            <option value="shingle" {% if pricing_form.material == 'shingle' %}selected{% endif %}>Shingle</option>
                            <option value="tile" {% if pricing_form.material == 'tile' %}selected{% endif %}>Tile</option>
                            <option value="metal" {% if pricing_form.material == 'metal' %}selected{% endif %}>Metal</option>
                          </select>
                        </div>
                      </div>
                      <button class="btn btn-success">Calculate SCI Pricing Estimate</button>
                    </form>
                  </div>

                  {% if pricing_result %}
                    <div class="estimate-result mt-3">
                      <div class="row g-3 mb-3">
                        <div class="col-md-4">
                          <div class="estimate-kpi">
                            <div class="text-muted small">Material Baseline</div>
                            <strong>{{ '${:,.0f}'.format(pricing_result.baseline_material) }}</strong>
                          </div>
                        </div>
                        <div class="col-md-4">
                          <div class="estimate-kpi">
                            <div class="text-muted small">Price / Square</div>
                            <strong>{{ '${:,.2f}'.format(pricing_result.price_per_square) }}</strong>
                          </div>
                        </div>
                        <div class="col-md-4">
                          <div class="estimate-kpi">
                            <div class="text-muted small">Estimated Total</div>
                            <strong>{{ '${:,.0f}'.format(pricing_result.estimated_total) }}</strong>
                          </div>
                        </div>
                      </div>
                      <div>{{ pricing_result.summary | safe }}</div>
                    </div>
                  {% endif %}

                  <hr class="my-4">

                  <form method="post" class="vstack gap-2 estimator-form" data-loading-message="Sending estimate email...">
                    <input type="hidden" name="action" value="broward_ai_search">
                    <input type="hidden" name="search_address" value="{{ broward_result.address }}">
                    <input type="hidden" name="search_city" value="{{ broward_result.city }}">
                    {% if pricing_result %}
                      <input type="hidden" name="pricing_material" value="{{ pricing_form.material }}">
                      <input type="hidden" name="access_level" value="{{ pricing_form.access_level }}">
                      <input type="hidden" name="pricing_squares" value="{{ pricing_result.squares }}">
                      <input type="hidden" name="pricing_price_per_square" value="{{ pricing_result.price_per_square }}">
                      <input type="hidden" name="pricing_baseline_material" value="{{ pricing_result.baseline_material }}">
                      <input type="hidden" name="pricing_estimated_total" value="{{ pricing_result.estimated_total }}">
                    {% endif %}
                    <div>
                      <label class="form-label">Email This Result To</label>
                      <input type="email" name="result_email" class="form-control" placeholder="estimates@company.com" value="{{ broward_form.result_email or '' }}" required>
                    </div>
                    <button class="btn btn-outline-primary">Send Result Email</button>
                  </form>
                
                {% elif estimate %}
                  <div class="estimate-result mb-4">
                    <div class="row g-3">
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Estimated Range</div>
                          <strong>{{ estimate.range }}</strong>
                        </div>
                      </div>
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Base Cost / Sq Ft</div>
                          <strong>{{ estimate.rate }}</strong>
                        </div>
                      </div>
                      <div class="col-md-4">
                        <div class="estimate-kpi">
                          <div class="text-muted small">Confidence</div>
                          <strong>{{ estimate.confidence }}</strong>
                        </div>
                      </div>
                    </div>
                  </div>
                
                  <div class="mb-3">{{ estimate.summary | safe }}</div>
                  <div class="text-muted small">
                    This estimate is informational and should be validated with a site inspection.
                  </div>
                
                {% else %}
                  <div class="text-muted mb-4">
                    Generate a standard estimate or run Broward & Palm Beach Estimator to see polished results here.
                  </div>
                
                  <div class="estimate-result">
                    <h6 class="mb-2">What you will get</h6>
                    <ul class="mb-0 text-muted">
                      <li>Dedicated Broward & Palm Beach estimator button and loading state.</li>
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

          const addPricingBtn = document.getElementById('add-pricing-toggle');
          const addPricingPanel = document.getElementById('add-pricing-panel');
          if (addPricingBtn && addPricingPanel) {
            addPricingBtn.addEventListener('click', () => {
              addPricingPanel.classList.toggle('d-none');
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


    # ---------- ADMINCHAN EMAIL MANAGER DASHBOARD ----------
    "adminchan_dashboard.html": """
    {% extends "base.html" %}
    {% block content %}
      <style>
        .em-header {
          background: linear-gradient(135deg, #1e293b, #334155);
          color: #fff; border-radius: 20px; padding: 2rem 2.5rem; margin-bottom: 2rem;
          box-shadow: 0 20px 50px rgba(15, 23, 42, 0.2);
        }
        .em-header h2 { font-weight: 700; margin-bottom: .25rem; }
        .em-header p { color: rgba(255,255,255,.7); margin: 0; }
        .client-card, .blast-card {
          background: #fff; border-radius: 16px;
          border: 1px solid rgba(15, 23, 42, 0.08);
          box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
          margin-bottom: 1.5rem; overflow: hidden;
        }
        .client-card-header, .blast-card-header {
          background: linear-gradient(135deg, rgba(59, 130, 246, .08), rgba(14, 165, 233, .06));
          padding: 1.25rem 1.5rem;
          border-bottom: 1px solid rgba(15, 23, 42, 0.06);
          display: flex; align-items: center; justify-content: space-between;
          font-weight: 700; color: #0f172a;
        }
        .client-card-header h5 { margin: 0; font-weight: 700; color: #0f172a; }
        .client-badge {
          display: inline-flex; align-items: center; padding: .25rem .65rem;
          border-radius: 999px; font-size: .75rem; font-weight: 700;
          text-transform: uppercase; letter-spacing: .04em;
        }
        .badge-sci { background: rgba(249, 115, 22, .12); color: #c2410c; }
        .badge-munsie { background: rgba(16, 185, 129, .12); color: #047857; }
        .badge-generic { background: rgba(100, 116, 139, .12); color: #475569; }
        .badge-jobsdirect { background: rgba(124, 58, 237, .12); color: #6d28d9; }
        .client-card-body, .blast-card-body { padding: 1.25rem 1.5rem; }
        .em-section { margin-bottom: 1rem; }
        .em-section-title {
          font-size: .85rem; font-weight: 700; text-transform: uppercase;
          letter-spacing: .06em; color: #64748b; margin-bottom: .5rem;
        }
        .em-empty {
          background: rgba(241, 245, 249, .6); border: 2px dashed #cbd5e1;
          border-radius: 12px; padding: 1rem 1.25rem; color: #94a3b8;
          font-size: .9rem; text-align: center;
        }
        .em-list-item {
          background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px;
          padding: .75rem 1rem; margin-bottom: .5rem;
          display: flex; align-items: center; justify-content: space-between;
        }
        .em-list-item .list-name { font-weight: 600; color: #0f172a; }
        .em-list-item .list-meta { font-size: .82rem; color: #64748b; }
        .email-chip { display: inline-block; background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe;
          border-radius: 999px; padding: .2rem .6rem; font-size: .78rem; margin: .15rem; cursor: pointer; transition: all .15s; }
        .email-chip.selected { background: #2563eb; color: #fff; border-color: #2563eb; }
        .email-chip:hover { box-shadow: 0 2px 6px rgba(37,99,235,.2); }
        .sched-badge { display: inline-block; padding: .2rem .55rem; border-radius: 999px;
          font-size: .72rem; font-weight: 700; text-transform: uppercase; }
        .sched-pending { background: rgba(251,191,36,.15); color: #b45309; }
        .sched-sending { background: rgba(59,130,246,.15); color: #1d4ed8; }
        .sched-sent { background: rgba(34,197,94,.15); color: #15803d; }
        .sched-cancelled { background: rgba(239,68,68,.15); color: #dc2626; }
        .btn-em { border-radius: 10px; font-weight: 600; font-size: .85rem; }
        #emailPreview { border: 1px solid #e2e8f0; border-radius: 12px; padding: 1.25rem;
          background: #fafbfc; min-height: 120px; }
      </style>

      <div class="em-header">
        <h2>Email Manager</h2>
        <p>Manage email lists, compose blasts, and schedule sends for all clients</p>
      </div>

      <ul class="nav nav-tabs mb-4" id="emTabs" role="tablist">
        <li class="nav-item" role="presentation">
          <button class="nav-link active" id="lists-tab" data-bs-toggle="tab" data-bs-target="#lists-pane"
            type="button" role="tab" aria-selected="true">Client Email Lists</button>
        </li>
        <li class="nav-item" role="presentation">
          <button class="nav-link" id="compose-tab" data-bs-toggle="tab" data-bs-target="#compose-pane"
            type="button" role="tab" aria-selected="false">Compose &amp; Schedule Blast</button>
        </li>
        <li class="nav-item" role="presentation">
          <button class="nav-link" id="history-tab" data-bs-toggle="tab" data-bs-target="#history-pane"
            type="button" role="tab" aria-selected="false">Blast History <span class="badge bg-secondary ms-1">{{ blast_schedules|length }}</span></button>
        </li>
      </ul>

      <div class="tab-content" id="emTabContent">
        <!-- ====== TAB 1: Client Email Lists ====== -->
        <div class="tab-pane fade show active" id="lists-pane" role="tabpanel">
          {% if not clients %}
            <div class="em-empty" style="padding: 3rem;">
              <h5 style="color: #64748b;">No clients found</h5>
              <p>Add client accounts via the Admin panel to start managing their emails.</p>
            </div>
          {% endif %}

          {% for client in clients %}
            <div class="client-card">
              <div class="client-card-header">
                <div>
                  <h5>{{ client.username }}</h5>
                  <span class="client-badge badge-{{ client.brand }}">{{ client.brand }}</span>
                  <span style="font-size:.82rem; color:#64748b; margin-left:.5rem;">{{ client.role }}</span>
                </div>
                <div>
                  <button class="btn btn-sm btn-outline-primary btn-em" onclick="document.getElementById('upload-{{ client.username }}').click()">
                    Upload Excel List
                  </button>
                  <form method="post" action="{{ url_for('adminchan_upload_list', client_username=client.username) }}" enctype="multipart/form-data" style="display:inline;" id="form-upload-{{ client.username }}">
                    <input type="file" name="excel_file" id="upload-{{ client.username }}" accept=".xlsx,.xls,.csv"
                           onchange="document.getElementById('form-upload-{{ client.username }}').submit()">
                  </form>
                </div>
              </div>
              <div class="client-card-body">
                <div class="em-section">
                  <div class="em-section-title">Email Lists</div>
                  {% if client.email_data.lists %}
                    {% for lst in client.email_data.lists %}
                      <div class="em-list-item">
                        <div>
                          <span class="list-name">{{ lst.name }}</span>
                          <span class="list-meta">&mdash; {{ lst.emails|length }} emails</span>
                        </div>
                        <div class="list-meta">Uploaded {{ lst.uploaded_at }}</div>
                      </div>
                    {% endfor %}
                  {% else %}
                    <div class="em-empty">No email lists yet. Upload an Excel sheet to get started.</div>
                  {% endif %}
                </div>
              </div>
            </div>
          {% endfor %}
        </div>

        <!-- ====== TAB 2: Compose & Schedule Blast ====== -->
        <div class="tab-pane fade" id="compose-pane" role="tabpanel">
          {% if not email_lists %}
            <div class="em-empty" style="padding: 2rem;">
              <h5 style="color:#64748b;">No email lists available</h5>
              <p>Upload email lists in the Client Email Lists tab first.</p>
            </div>
          {% else %}

          <!-- Step 1: Choose List -->
          <div class="blast-card">
            <div class="blast-card-header">Step 1 &mdash; Select an Email List</div>
            <div class="blast-card-body">
              <select id="blastListSelect" class="form-select" onchange="blastListChanged()">
                <option value="">-- choose a list --</option>
                {% for lst in email_lists %}
                  <option value="{{ loop.index0 }}"
                    data-emails="{{ lst.emails | join('||') }}"
                    data-sender="{{ lst.sender_email }}">
                    {{ lst.name }} ({{ lst.client }}) &mdash; {{ lst.emails|length }} emails &mdash; sends from {{ lst.sender_email }}
                  </option>
                {% endfor %}
              </select>
              <div id="senderEmailInfo" class="mt-2" style="display:none;">
                <span class="badge bg-info text-dark" style="font-size:.85rem;">
                  Emails will be sent from: <strong id="senderEmailAddr"></strong>
                </span>
              </div>
            </div>
          </div>

          <!-- Step 2: Pick Recipients -->
          <div class="blast-card" id="step2Card" style="display:none;">
            <div class="blast-card-header">
              Step 2 &mdash; Select Recipients
              <span class="float-end">
                <button type="button" class="btn btn-sm btn-outline-primary" onclick="toggleAllEmails(true)">Select All</button>
                <button type="button" class="btn btn-sm btn-outline-secondary ms-1" onclick="toggleAllEmails(false)">Deselect All</button>
              </span>
            </div>
            <div class="blast-card-body">
              <div id="emailChipsContainer" style="max-height:260px; overflow-y:auto;"></div>
              <div class="mt-2 text-muted" style="font-size:.82rem;">
                <span id="selectedCount">0</span> of <span id="totalCount">0</span> selected
              </div>
            </div>
          </div>

          <!-- Step 3: Compose Email -->
          <div class="blast-card" id="step3Card" style="display:none;">
            <div class="blast-card-header">Step 3 &mdash; Compose Email</div>
            <div class="blast-card-body">
              <div class="row g-3">
                <div class="col-md-6">
                  <label class="form-label fw-bold">Subject Line</label>
                  <input type="text" id="blastSubject" class="form-control" placeholder="e.g. Spring Roofing Special!">
                </div>
                <div class="col-md-6">
                  <label class="form-label fw-bold">From Name (optional)</label>
                  <input type="text" id="blastFromName" class="form-control" placeholder="e.g. SCI Roofing">
                </div>
                <div class="col-12">
                  <label class="form-label fw-bold">Email Body (HTML supported)</label>
                  <textarea id="blastBody" class="form-control" rows="10"
                    placeholder="Write your email content here. You can use HTML for formatting."></textarea>
                </div>
                <div class="col-12">
                  <button type="button" class="btn btn-outline-secondary btn-sm" onclick="previewEmail()">Preview Email</button>
                </div>
                <div class="col-12" id="previewWrap" style="display:none;">
                  <label class="form-label fw-bold">Email Preview</label>
                  <div id="emailPreview"></div>
                </div>
              </div>
            </div>
          </div>

          <!-- Step 4: Schedule / Send -->
          <div class="blast-card" id="step4Card" style="display:none;">
            <div class="blast-card-header">Step 4 &mdash; Schedule or Send</div>
            <div class="blast-card-body">
              <form method="post" action="{{ url_for('adminchan_blast_schedule') }}" id="blastForm">
                <input type="hidden" name="list_index" id="hListIndex">
                <input type="hidden" name="selected_emails" id="hSelectedEmails">
                <input type="hidden" name="subject" id="hSubject">
                <input type="hidden" name="from_name" id="hFromName">
                <input type="hidden" name="body" id="hBody">

                <div class="row g-3 align-items-end">
                  <div class="col-md-5">
                    <label class="form-label fw-bold">Schedule Date &amp; Time</label>
                    <input type="datetime-local" name="scheduled_for" id="blastScheduleTime" class="form-control">
                  </div>
                  <div class="col-md-7 d-flex gap-2 flex-wrap">
                    <button type="submit" name="action" value="schedule" class="btn btn-primary"
                      onclick="return prepareBlastSubmit()">Schedule Blast</button>
                    <button type="submit" name="action" value="send_now" class="btn btn-success"
                      onclick="return prepareBlastSubmit()">Send Now</button>
                    <button type="submit" name="action" value="test" class="btn btn-outline-warning"
                      onclick="return prepareTestSubmit()">Send Test Email</button>
                  </div>
                </div>
                <div class="mt-2">
                  <small class="text-muted">Test emails are sent to <strong>{{ test_email }}</strong></small>
                </div>
              </form>
            </div>
          </div>
          {% endif %}
        </div>

        <!-- ====== TAB 3: Blast History ====== -->
        <div class="tab-pane fade" id="history-pane" role="tabpanel">
          {% if blast_schedules %}
          <div class="blast-card">
            <div class="blast-card-header">Scheduled &amp; Sent Blasts</div>
            <div class="blast-card-body">
              <div class="table-responsive">
                <table class="table table-sm align-middle mb-0">
                  <thead><tr>
                    <th>ID</th><th>Subject</th><th>List</th><th>Recipients</th>
                    <th>Scheduled For</th><th>Status</th><th>Result</th><th class="text-end">Actions</th>
                  </tr></thead>
                  <tbody>
                    {% for b in blast_schedules %}
                    <tr>
                      <td>#{{ b.id }}</td>
                      <td>{{ b.subject or 'No subject' }}</td>
                      <td>{{ b.list_name }}</td>
                      <td>{{ b.recipient_count }}</td>
                      <td>{{ b.scheduled_for or 'Immediate' }}</td>
                      <td><span class="sched-badge sched-{{ b.status|lower|replace(' ','-') }}">{{ b.status }}</span></td>
                      <td style="font-size:.82rem;">{{ b.send_result or b.sent_at or '' }}</td>
                      <td class="text-end">
                        {% if b.status == 'pending' %}
                        <form method="post" action="{{ url_for('adminchan_blast_action') }}" class="d-inline">
                          <input type="hidden" name="blast_id" value="{{ b.id }}">
                          <button name="action" value="send" class="btn btn-sm btn-outline-success"
                            onclick="return confirm('Send this blast to {{ b.recipient_count }} recipients now?')">Send Now</button>
                          <button name="action" value="cancel" class="btn btn-sm btn-outline-danger ms-1">Cancel</button>
                        </form>
                        {% endif %}
                      </td>
                    </tr>
                    {% endfor %}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
          {% else %}
            <div class="em-empty" style="padding: 2rem;">
              <h5 style="color:#64748b;">No blasts yet</h5>
              <p>Compose and schedule your first email blast in the Compose tab.</p>
            </div>
          {% endif %}
        </div>
      </div><!-- /tab-content -->

      <script>
        var currentEmails = [];
        var selectedEmails = new Set();

        function blastListChanged() {
          var sel = document.getElementById('blastListSelect');
          var opt = sel.options[sel.selectedIndex];
          var step2 = document.getElementById('step2Card');
          var step3 = document.getElementById('step3Card');
          var step4 = document.getElementById('step4Card');
          var senderInfo = document.getElementById('senderEmailInfo');
          if (!opt.value) { step2.style.display='none'; step3.style.display='none'; step4.style.display='none'; if(senderInfo) senderInfo.style.display='none'; return; }
          var raw = opt.getAttribute('data-emails') || '';
          var senderAddr = opt.getAttribute('data-sender') || 'default';
          currentEmails = raw ? raw.split('||') : [];
          selectedEmails = new Set(currentEmails);
          renderChips();
          step2.style.display=''; step3.style.display=''; step4.style.display='';
          if(senderInfo) { senderInfo.style.display=''; document.getElementById('senderEmailAddr').textContent = senderAddr; }
        }

        function renderChips() {
          var c = document.getElementById('emailChipsContainer');
          c.innerHTML = '';
          currentEmails.forEach(function(em) {
            var chip = document.createElement('span');
            chip.className = 'email-chip' + (selectedEmails.has(em) ? ' selected' : '');
            chip.textContent = em;
            chip.onclick = function() {
              if (selectedEmails.has(em)) selectedEmails.delete(em); else selectedEmails.add(em);
              this.classList.toggle('selected');
              updateCount();
            };
            c.appendChild(chip);
          });
          updateCount();
        }

        function toggleAllEmails(selectAll) {
          if (selectAll) selectedEmails = new Set(currentEmails); else selectedEmails.clear();
          renderChips();
        }

        function updateCount() {
          document.getElementById('selectedCount').textContent = selectedEmails.size;
          document.getElementById('totalCount').textContent = currentEmails.length;
        }

        function previewEmail() {
          var subj = document.getElementById('blastSubject').value || 'No Subject';
          var body = document.getElementById('blastBody').value || '';
          var wrap = document.getElementById('previewWrap');
          var prev = document.getElementById('emailPreview');
          prev.innerHTML = '<h4 style="color:#2563eb;margin-bottom:4px;">' + subj.replace(/</g,'&lt;') + '</h4>'
            + '<hr style="border:none;border-top:2px solid #e2e8f0;margin:8px 0 16px;">'
            + '<div>' + body + '</div>'
            + '<hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0 8px;">'
            + '<p style="font-size:12px;color:#94a3b8;">Email Blast Preview</p>';
          wrap.style.display = '';
        }

        function prepareBlastSubmit() {
          if (selectedEmails.size === 0) { alert('Please select at least one recipient.'); return false; }
          var subj = document.getElementById('blastSubject').value.trim();
          var body = document.getElementById('blastBody').value.trim();
          if (!subj) { alert('Please enter a subject line.'); return false; }
          if (!body) { alert('Please enter an email body.'); return false; }
          document.getElementById('hListIndex').value = document.getElementById('blastListSelect').value;
          document.getElementById('hSelectedEmails').value = Array.from(selectedEmails).join('||');
          document.getElementById('hSubject').value = subj;
          document.getElementById('hFromName').value = document.getElementById('blastFromName').value.trim();
          document.getElementById('hBody').value = body;
          return true;
        }

        function prepareTestSubmit() {
          var subj = document.getElementById('blastSubject').value.trim();
          var body = document.getElementById('blastBody').value.trim();
          if (!subj) { alert('Please enter a subject line.'); return false; }
          if (!body) { alert('Please enter an email body.'); return false; }
          document.getElementById('hListIndex').value = document.getElementById('blastListSelect').value;
          document.getElementById('hSelectedEmails').value = '';
          document.getElementById('hSubject').value = subj;
          document.getElementById('hFromName').value = document.getElementById('blastFromName').value.trim();
          document.getElementById('hBody').value = body;
          return true;
        }
      </script>
    {% endblock %}
    """,   


    # ---------- JOBSDIRECT DASHBOARD ----------
    "jobsdirect_dashboard.html": """
    {% extends "base.html" %}
    {% block content %}
      <style>
        .jd-header {
          background: linear-gradient(135deg, #7c3aed, #4f46e5);
          color: #fff;
          border-radius: 20px;
          padding: 2rem 2.5rem;
          margin-bottom: 2rem;
          box-shadow: 0 20px 50px rgba(79, 70, 229, 0.2);
        }
        .jd-header h2 { font-weight: 700; margin-bottom: .25rem; }
        .jd-header p { color: rgba(255,255,255,.7); margin: 0; }
        .jd-card {
          background: #fff;
          border-radius: 16px;
          border: 1px solid rgba(15, 23, 42, 0.08);
          box-shadow: 0 12px 30px rgba(15, 23, 42, 0.08);
          margin-bottom: 1.5rem;
          padding: 1.5rem 2rem;
        }
        .jd-card h5 { font-weight: 700; color: #0f172a; margin-bottom: 1rem; }
        .jd-stats { display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
        .jd-stat {
          background: linear-gradient(135deg, rgba(124, 58, 237, .06), rgba(79, 70, 229, .08));
          border-radius: 14px;
          padding: 1.25rem 1.5rem;
          flex: 1; min-width: 160px;
          text-align: center;
        }
        .jd-stat .num { font-size: 2rem; font-weight: 800; color: #4f46e5; }
        .jd-stat .label { font-size: .82rem; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
        .jd-sent-item {
          background: #f8fafc;
          border: 1px solid #e2e8f0;
          border-radius: 10px;
          padding: .75rem 1rem;
          margin-bottom: .5rem;
          display: flex;
          align-items: center;
          justify-content: space-between;
        }
        .jd-sent-item .sent-to { font-weight: 600; color: #0f172a; }
        .jd-sent-item .sent-meta { font-size: .82rem; color: #64748b; }
        .jd-status-ok { color: #15803d; font-weight: 700; font-size: .8rem; }
        .jd-status-fail { color: #dc2626; font-weight: 700; font-size: .8rem; }
        .jd-empty {
          background: rgba(241, 245, 249, .6);
          border: 2px dashed #cbd5e1;
          border-radius: 12px;
          padding: 1.25rem;
          color: #94a3b8;
          font-size: .9rem;
          text-align: center;
        }
      </style>

      <div class="jd-header">
        <h2>JobsDirect Email Dashboard</h2>
        <p>Send emails from {{ from_email }} &mdash; logged in as <strong>{{ username }}</strong></p>
      </div>

      <div class="jd-stats">
        <div class="jd-stat">
          <div class="num">{{ sent_log|length }}</div>
          <div class="label">Emails Sent</div>
        </div>
        <div class="jd-stat">
          <div class="num">{{ sent_log|selectattr('status', 'equalto', 'OK')|list|length }}</div>
          <div class="label">Delivered</div>
        </div>
        <div class="jd-stat">
          <div class="num">{{ sent_log|selectattr('status', 'equalto', 'FAILED')|list|length }}</div>
          <div class="label">Failed</div>
        </div>
      </div>

      <!-- Compose Email -->
      <div class="jd-card">
        <h5>Compose Email</h5>
        <form method="post" action="{{ url_for('jobsdirect_send') }}">
          <div class="row g-3">
            <div class="col-md-6">
              <label class="form-label">To (email address)</label>
              <input type="email" name="to_email" class="form-control" placeholder="recipient@example.com" required>
            </div>
            <div class="col-md-6">
              <label class="form-label">Subject</label>
              <input type="text" name="subject" class="form-control" placeholder="Email subject" required>
            </div>
            <div class="col-12">
              <label class="form-label">Message Body</label>
              <textarea name="body" class="form-control" rows="8" placeholder="Type your email message here..." required></textarea>
            </div>
          </div>
          <button type="submit" class="btn btn-primary mt-3" style="border-radius:10px; font-weight:600;">
            Send Email
          </button>
          <span class="text-muted ms-2" style="font-size:.85rem;">Sends from {{ from_email }}</span>
        </form>
      </div>

      <!-- Sent Log -->
      <div class="jd-card">
        <h5>Sent Emails</h5>
        {% if sent_log %}
          {% for item in sent_log|reverse %}
            <div class="jd-sent-item">
              <div>
                <span class="sent-to">{{ item.to }}</span>
                <span class="sent-meta">&mdash; {{ item.subject }}</span>
              </div>
              <div>
                {% if item.status == 'OK' %}
                  <span class="jd-status-ok">SENT</span>
                {% else %}
                  <span class="jd-status-fail">FAILED</span>
                {% endif %}
                <span class="sent-meta ms-2">{{ item.sent_at }}</span>
              </div>
            </div>
          {% endfor %}
        {% else %}
          <div class="jd-empty">No emails sent yet. Use the form above to compose and send.</div>
        {% endif %}
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


SCI_MATERIAL_PRICE_PER_SQUARE = {
    "shingle": 394.31,
    "tile": 556.94,
    "metal": 612.00,
}

SCI_ACCESS_MULTIPLIERS = {
    "ground": 1.00,
    "2_story": 1.10,
    "3_plus": 1.20,
}


def generate_sci_pricing_estimate(payload):
    material_key = payload.get("material", "").strip().lower()
    access_key = payload.get("access_level", "").strip().lower()
    squares = max(float(payload.get("squares", 0) or 0), 0.1)

    price_per_square = SCI_MATERIAL_PRICE_PER_SQUARE.get(material_key, SCI_MATERIAL_PRICE_PER_SQUARE["shingle"])
    access_multiplier = SCI_ACCESS_MULTIPLIERS.get(access_key, 1.0)

    baseline_material = squares * price_per_square
    labor_and_install = baseline_material * 0.42
    access_adjustment = (baseline_material + labor_and_install) * (access_multiplier - 1)
    overhead_and_margin = (baseline_material + labor_and_install + access_adjustment) * 0.18
    estimated_total = baseline_material + labor_and_install + access_adjustment + overhead_and_margin

    access_labels = {
        "ground": "Ground / single-story access",
        "2_story": "Second-floor / two-story access",
        "3_plus": "Third-floor+ / difficult access",
    }

    summary = (
        f"<p><strong>SCI Pricing Build:</strong> {squares:.1f} squares × ${price_per_square:,.2f}/sq "
        f"for {material_key.capitalize()} = <strong>{format_currency(baseline_material)}</strong> baseline material cost.</p>"
        "<ul>"
        f"<li>Material baseline (SCI sheet-derived): <strong>{format_currency(baseline_material)}</strong>.</li>"
        f"<li>Labor + install loading (42%): <strong>{format_currency(labor_and_install)}</strong>.</li>"
        f"<li>Access multiplier ({access_multiplier:.2f} - {access_labels.get(access_key, 'Standard access')}): <strong>{format_currency(access_adjustment)}</strong>.</li>"
        f"<li>Overhead + margin (18%): <strong>{format_currency(overhead_and_margin)}</strong>.</li>"
        "</ul>"
        f"<p><strong>Estimated Contract Price:</strong> {format_currency(estimated_total)}.</p>"
        "<p class='mb-0 text-muted small'>This is a directional estimator for operations handoff and should be field-verified before final contract execution.</p>"
    )

    return {
        "material": material_key.capitalize(),
        "access_level": access_labels.get(access_key, "Standard access"),
        "squares": round(squares, 1),
        "price_per_square": price_per_square,
        "baseline_material": round(baseline_material, 0),
        "estimated_total": round(estimated_total, 0),
        "summary": summary,
    }
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
BROWARD_ESTIMATOR_ADJUSTMENT = 1.06
BROWARD_WASTE_OPTIONS = [0, 10, 12, 15, 17, 20, 22]
BROWARD_OUTPUT_DIR = os.path.join(BASE_DIR, "bcpa_outputs")
os.makedirs(BROWARD_OUTPUT_DIR, exist_ok=True)


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


def _extract_total_adj_area(sketch_text):
    if not sketch_text:
        raise ValueError("Could not extract Total Adj Area from empty BCPA sketch text.")
    match = re.search(r"Total\s+.*?\s+([\d,]+\.\d+|[\d,]+)\s*$", sketch_text, re.MULTILINE)
    if not match:
        raise ValueError("Could not extract Total Adj Area from BCPA sketch text.")
    return float(match.group(1).replace(",", ""))

def _extract_json_object(raw_text):
    if not raw_text:
        raise ValueError("OpenAI response was empty.")
    match = re.search(r"\{.*?\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError("OpenAI response did not contain JSON.")
    return json.loads(match.group(0))

def _resolve_chrome_binary():
    candidates = [
        os.environ.get("CHROME_BIN"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def _resolve_chromedriver_binary():
    candidates = [
        os.environ.get("CHROMEDRIVER_PATH"),
        shutil.which("chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""

def create_driver():
    chrome_bin = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
    driver_bin = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

    options = Options()
    options.binary_location = chrome_bin
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    prefs = {
        "download.default_directory": BROWARD_OUTPUT_DIR,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)
    service = Service(driver_bin)
    return webdriver.Chrome(service=service, options=options)
    
def _bcpa_collect_property_data(address, city):
    driver = create_driver()


    wait = WebDriverWait(driver, 30)

    try:
        driver.get("https://web.bcpa.net/BcpaClient/#/Record-Search")
        time.sleep(4)

        addr_box = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
        addr_box.clear()
        addr_box.send_keys(f"{address}, {city}")

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#searchButton"))).click()
        time.sleep(5)

        if "Record" not in driver.current_url:
            wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a[href*='Record']"))).click()

        time.sleep(3)
        prop_img_tag = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "img[src*='/Photographs/']")))
        photo_url = prop_img_tag.get_attribute("src")

        page_text = driver.find_element(By.TAG_NAME, "body").text
        match = re.search(r"Adj\.?\s*Bldg\.?\s*S\.?F\.?\s*[:\s]*([\d,\.]+)", page_text, re.IGNORECASE)
        if match:
            ground_area = float(match.group(1).replace(",", ""))
        else:
            ground_area = 0

        sketch_file = os.path.join(BROWARD_OUTPUT_DIR, "sketch.png")
        existing_handles = driver.window_handles
        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div.btn-sketch"))).click()
        sketch_window = list(set(driver.window_handles) - set(existing_handles))[0]
        driver.switch_to.window(sketch_window)
        time.sleep(3)
        sketch_text = driver.find_element(By.TAG_NAME, "body").text
        # ---- FORCE FULL SKETCH VISIBILITY ----

        # Scroll to bottom so entire sketch renders
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        
        # Get actual rendered dimensions
        total_height = driver.execute_script("return document.body.scrollHeight")
        total_width = driver.execute_script("return document.body.scrollWidth")
        
        # Resize browser window to fit entire sketch
        driver.set_window_size(total_width + 200, total_height + 200)
        time.sleep(1)
        
        # Now take screenshot (will include bottom portion)
        driver.save_screenshot(sketch_file)
        
        # --------------------------------------
        driver.close()
        driver.switch_to.window(existing_handles[0])

        map_file = os.path.join(BROWARD_OUTPUT_DIR, "map.png")
        existing_handles = driver.window_handles
        wait.until(EC.element_to_be_clickable((
            By.XPATH,
            "//*[name()='title' and text()='Map']/ancestor::*[self::div or self::button][1]",
        ))).click()
        map_window = list(set(driver.window_handles) - set(existing_handles))[0]
        driver.switch_to.window(map_window)
        time.sleep(3)
        driver.save_screenshot(map_file)
        driver.close()
        driver.switch_to.window(existing_handles[0])

        return {
            "photo_url": photo_url,
            "ground_area": ground_area,
            "sketch_text": sketch_text,
            "sketch_file": sketch_file,
            "map_file": map_file,
        }
    finally:
        driver.quit()


def _is_palm_beach_address(address, city):
    combined = f"{address} {city}".lower()
    palm_beach_cities = {
        "boca raton", "boynton beach", "delray beach", "jupiter", "lake worth",
        "lake worth beach", "west palm beach", "wellington", "palm beach gardens",
        "riviera beach", "greenacres", "palm springs", "lantana", "north palm beach",
    }
    if "palm beach county" in combined:
        return True
    if "palm beach" in city.lower():
        return True
    return city.strip().lower() in palm_beach_cities


def _pbcpao_collect_property_data(address, city):
    driver = create_driver()
    wait = WebDriverWait(driver, 30)

    def _safe_save_screenshot(file_path, context, retries=2):
        def _capture_with_cdp_fallback():
            try:
                screenshot_data = driver.execute_cdp_cmd(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True},
                )
            except Exception:
                return False
            data_b64 = screenshot_data.get("data") if isinstance(screenshot_data, dict) else None
            if not data_b64:
                return False
            try:
                with open(file_path, "wb") as screenshot_file:
                    screenshot_file.write(base64.b64decode(data_b64))
                return True
            except Exception:
                return False

        for attempt in range(1, retries + 1):
            try:
                if driver.save_screenshot(file_path):
                    return True
                logger.warning("Palm Beach %s screenshot returned falsy on attempt %s", context, attempt)
            except Exception as exc:
                if _capture_with_cdp_fallback():
                    logger.info(
                        "Palm Beach %s screenshot recovered with CDP fallback on attempt %s",
                        context,
                        attempt,
                    )
                    return True
                logger.warning(
                    "Palm Beach %s screenshot failed on attempt %s: %s",
                    context,
                    attempt,
                    exc,
                )
            time.sleep(1)
        return False

    try:
        driver.get("https://pbcpao.gov/index.htm")
        time.sleep(4)

        search_box = wait.until(EC.presence_of_element_located((By.ID, "realsrchVal")))
        search_box.clear()
        search_box.send_keys(f"{address}, {city}")
        time.sleep(2)
        search_box.send_keys(Keys.ARROW_DOWN)
        search_box.send_keys(Keys.ENTER)
        time.sleep(6)

        ground_area = float(driver.find_element(
            By.XPATH,
            "//td[contains(text(),'Total Square Footage')]/following-sibling::td",
        ).text.replace(",", "").strip())

        sketch_file = os.path.join(BROWARD_OUTPUT_DIR, "palm_beach_sketch.png")
        sketch_text = ""
        for old_pdf in [
            os.path.join(BROWARD_OUTPUT_DIR, f)
            for f in os.listdir(BROWARD_OUTPUT_DIR)
            if f.lower().endswith(".pdf")
        ]:
            try:
                os.remove(old_pdf)
            except OSError:
                logger.debug("Unable to remove old Palm Beach sketch PDF: %s", old_pdf)

        sketch_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(@onclick,'printSketchDiv')]"))
        )
        
        def _page_text():
            try:
                return (driver.find_element(By.TAG_NAME, "body").text or "").strip()
            except Exception:
                return ""

        driver.execute_script("arguments[0].click();", sketch_button)
        time.sleep(5)

        pdf_deadline = time.time() + 20
        latest_pdf = ""
        while time.time() < pdf_deadline and not latest_pdf:
            pdf_files = [
                os.path.join(BROWARD_OUTPUT_DIR, f)
                for f in os.listdir(BROWARD_OUTPUT_DIR)
                if f.lower().endswith(".pdf")
            ]
            if pdf_files:
                latest_pdf = max(pdf_files, key=os.path.getctime)
                break
            time.sleep(0.5)

        if not latest_pdf:
            logger.warning("Palm Beach sketch PDF not saved; capturing page screenshot fallback.")
            _safe_save_screenshot(sketch_file, "sketch fallback")
            sketch_text = _page_text()


        map_file = os.path.join(BROWARD_OUTPUT_DIR, "palm_beach_map.png")

        if latest_pdf:
            logger.info("Palm Beach sketch PDF saved: %s", latest_pdf)
            latest_pdf_uri = latest_pdf.replace('\\', '/')
            driver.get(f"file:///{latest_pdf_uri}")
            time.sleep(3)
            sketch_text = _page_text()
            _safe_save_screenshot(sketch_file, "sketch PDF")
            driver.back()
            time.sleep(3)
        old_tabs = driver.window_handles[:]
        map_button = wait.until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href,'papagis') and contains(text(),'Show Full Map')]"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", map_button)
        driver.execute_script("arguments[0].click();", map_button)

        wait.until(lambda d: len(d.window_handles) > len(old_tabs))
        gis_tab = [t for t in driver.window_handles if t not in old_tabs][0]
        driver.switch_to.window(gis_tab)
        time.sleep(6)

        wait.until(EC.element_to_be_clickable((By.ID, "tools-tab"))).click()
        time.sleep(2)

        existing_tabs = driver.window_handles[:]
        print_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'Print Map')]/ancestor::*[self::a or self::div][1]"))
        )
        driver.execute_script("arguments[0].click();", print_button)
        try:
            wait.until(lambda d: len(d.window_handles) > len(existing_tabs))
            print_tab = [t for t in driver.window_handles if t not in existing_tabs][0]
            driver.switch_to.window(print_tab)
            time.sleep(4)
            if not _safe_save_screenshot(map_file, "print map tab"):
                driver.close()
                driver.switch_to.window(gis_tab)
                _safe_save_screenshot(map_file, "GIS map fallback after print tab error")
            else:
                driver.close()
                driver.switch_to.window(gis_tab)
        except TimeoutException:
            logger.warning(
                "Palm Beach print map tab did not open; using GIS view screenshot fallback."
            )
            _safe_save_screenshot(map_file, "GIS map fallback")
                
        street_file = os.path.join(BROWARD_OUTPUT_DIR, "palm_beach_street.png")
        existing_tabs = driver.window_handles[:]
        google_button = wait.until(
            EC.presence_of_element_located((By.XPATH, "//span[contains(text(),'Google Maps')]/ancestor::*[self::a or self::div][1]"))
        )
        driver.execute_script("arguments[0].click();", google_button)
        try:
            wait.until(lambda d: len(d.window_handles) > len(existing_tabs))
            google_tab = [t for t in driver.window_handles if t not in existing_tabs][0]
            driver.switch_to.window(google_tab)
            time.sleep(6)
        except TimeoutException:
            logger.warning(
                "Palm Beach Google Maps tab did not open; using GIS map screenshot fallback."
            )
            _safe_save_screenshot(street_file, "street fallback from GIS")
            return {
                "photo_url": "",
                "sketch_text": sketch_text,
                "sketch_file": sketch_file,
                "map_file": map_file,
                "street_file": street_file,
                "ground_area": ground_area,
            }
        try:
            street_tile = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.kAYW5b")))
            driver.execute_script("arguments[0].click();", street_tile)
            time.sleep(5)
        except Exception:
            logger.info("Palm Beach Street View tile not found; saving map screenshot fallback.")

        _safe_save_screenshot(street_file, "street")


        return {
            "photo_url": "",
            "sketch_text": sketch_text,
            "sketch_file": sketch_file,
            "map_file": map_file,
            "street_file": street_file,
            "ground_area": ground_area,
        }
    finally:
        driver.quit()


def _ask_openai_pitch_complexity_waste(photo_input, sketch_input, map_file):
    import os
    import json
    import base64
    import urllib.request

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is required for Broward & Palm Beach Estimator.")

    def _to_data_uri_png_from_file(path: str) -> str:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return f"data:image/png;base64,{b64}"

    def _as_image_url_obj(value: str, fallback_mime: str = "image/jpeg"):
        """
        Returns {"type":"image_url","image_url":{"url":...}}.
        If value is already a data URI, pass it through.
        If value is a URL, pass it through.
        If value is a file path, convert to a data URI (png by default).
        """
        if not value:
            return None

        v = str(value).strip()

        # data URI already
        if v.startswith("data:image/"):
            return {"type": "image_url", "image_url": {"url": v}}

        # remote URL
        if v.startswith("http://") or v.startswith("https://"):
            return {"type": "image_url", "image_url": {"url": v}}

        # local file path
        if os.path.exists(v):
            # Assume png for local screenshot files
            return {"type": "image_url", "image_url": {"url": _to_data_uri_png_from_file(v)}}

        # unknown / missing
        return None

    # Map is always a file path in your flow
    map_part = _as_image_url_obj(map_file)
    if map_part is None:
        raise RuntimeError("Map image missing/unreadable; cannot run Broward & Palm Beach Estimator.")

    # Photo can be URL or data URI (or file path if you choose later)
    photo_part = _as_image_url_obj(photo_input)
    # Sketch can be file path OR data URI
    sketch_part = _as_image_url_obj(sketch_input)

    prompt = (
        "You are estimating a residential roof.\n"
        "Use the provided images (front photo, sketch, map) to determine ONLY:\n"
        "1) pitch (integer, e.g., 2 means 2/12)\n"
        "2) complexity (one of: simple, moderate, complex)\n"
        "3) waste_percent (number)\n"
        "Respond strictly as JSON with keys: pitch, complexity, waste_percent.\n"
        "If an image is missing/unreadable, still respond with your best estimate and include a key notes explaining what was missing."
    )

    content = [{"type": "text", "text": prompt}]

    # Keep consistent ordering
    if photo_part:
        content.append(photo_part)
    if sketch_part:
        content.append(sketch_part)
    content.append(map_part)

    request_body = json.dumps({
        "model": "gpt-4.1-mini",
        "messages": [{
            "role": "user",
            "content": content,
        }],
        "max_tokens": 300,
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

    with urllib.request.urlopen(request_obj, timeout=60) as response:
        response_data = json.loads(response.read().decode("utf-8"))

    ai_text = response_data["choices"][0]["message"]["content"]
    return _extract_json_object(ai_text)


def generate_broward_estimate(address, city):
    import os
    import base64
    import requests

    cleaned_city = city.strip()
    is_palm_beach = _is_palm_beach_address(address, cleaned_city)

    if is_palm_beach:
        bcpa_data = _pbcpao_collect_property_data(address, cleaned_city)
    else:
        if "broward" not in cleaned_city.lower() and cleaned_city.lower() not in {
            "fort lauderdale", "hollywood", "pompano beach", "coral springs", "sunrise", "weston", "davie",
            "plantation", "miramar", "coconut creek", "deerfield beach", "oakland park", "lauderhill", "tamarac",
        }:
            cleaned_city = f"{cleaned_city} (Broward)" if cleaned_city else "Broward"
        bcpa_data = _bcpa_collect_property_data(address, cleaned_city)

    ground_area = _safe_float(bcpa_data.get("ground_area"), 0)
    if is_palm_beach and ground_area <= 0:
        try:
            ground_area = _extract_total_adj_area(bcpa_data.get("sketch_text", ""))
        except ValueError:
            logger.warning("Could not read sketch Total Adj Area for %s, %s; defaulting ground area to 0.", address, city)
            ground_area = 0
    # ---------------- BUILD EMBEDDED IMAGES (NO LOCAL SAVE) ----------------
    photo_url = bcpa_data.get("photo_url", "")
    photo_data_uri = ""
    sketch_data_uri = ""
    # Front photo: fetch into memory and convert to data URI
    # This makes it possible to (a) display in report and (b) send identical bytes to OpenAI
    photo_ok = False
    photo_bytes = 0
    try:
        if photo_url and (photo_url.startswith("http://") or photo_url.startswith("https://")):
            r = requests.get(photo_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            photo_bytes = len(r.content)
            b64 = base64.b64encode(r.content).decode("utf-8")
            # Usually jpeg; even if not, most browsers still render. (Optional: sniff content-type if you want.)
            photo_data_uri = f"data:image/jpeg;base64,{b64}"
            photo_ok = True
    except Exception:
        photo_ok = False
        photo_data_uri = ""

    # Sketch: you already saved this screenshot to disk, so embed it
    sketch_ok = False
    sketch_bytes = 0
    try:
        sketch_path = bcpa_data.get("sketch_file", "")
        if sketch_path and os.path.exists(sketch_path):
            with open(sketch_path, "rb") as f:
                blob = f.read()
            sketch_bytes = len(blob)
            b64 = base64.b64encode(blob).decode("utf-8")
            sketch_data_uri = f"data:image/png;base64,{b64}"
            sketch_ok = True
    except Exception:
        sketch_ok = False
        sketch_data_uri = ""

    # For Palm Beach we use street view as the front-photo input to match the county workflow.
    if not photo_data_uri and bcpa_data.get("street_file") and os.path.exists(bcpa_data["street_file"]):
        try:
            with open(bcpa_data["street_file"], "rb") as f:
                blob = f.read()
            photo_bytes = len(blob)
            photo_data_uri = f"data:image/png;base64,{base64.b64encode(blob).decode('utf-8')}"
            photo_ok = True
        except Exception:
            photo_ok = False

    # Map: keep as file for OpenAI (already used today); also track health
    map_ok = os.path.exists(bcpa_data.get("map_file", "")) if bcpa_data.get("map_file") else False
    map_bytes = os.path.getsize(bcpa_data["map_file"]) if map_ok else 0

    # ---------------- GPT (SEND SAME BYTES YOU SHOW IN REPORT) ----------------
    # IMPORTANT: update _ask_openai_pitch_complexity_waste to accept data URIs
    # If you haven't updated it yet, see note below.
    try:
        ai_guess = _ask_openai_pitch_complexity_waste(
            photo_data_uri,     # used to be bcpa_data["photo_url"]
            sketch_data_uri,    # used to be bcpa_data["sketch_file"]
            bcpa_data["map_file"],
        )
    except Exception as exc:
        logger.warning("Broward/Palm Beach AI image analysis failed; using defaults. Error: %s", exc)
        ai_guess = {"pitch": 5, "complexity": "moderate", "waste_percent": 12.0}


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

    # ---------------- DEBUG: verify image capture ----------------
    debug_images = {
        "photo_ok": photo_ok,
        "photo_url": photo_url,
        "photo_bytes": photo_bytes,
        "sketch_ok": sketch_ok,
        "sketch_file": bcpa_data.get("sketch_file", ""),
        "sketch_bytes": sketch_bytes,
        "map_ok": map_ok,
        "map_file": bcpa_data.get("map_file", ""),
        "map_bytes": map_bytes,
    }

    return {
        "address": address,
        "city": cleaned_city,
        "is_palm_beach": is_palm_beach,
        "ground_area": round(ground_area, 0),
        "pitch": pitch,
        "complexity": complexity,
        "recommended_waste": round(waste_percent, 1),
        "adjusted_surface": round(adjusted_surface, 0),
        "final_area": round(final_area, 0),
        "final_squares": round(final_squares, 1),
        "waste_breakdown": waste_breakdown,

        # For showing on the report page (aesthetic evidence they were collected)
        "report_front_image": photo_data_uri,
        "report_sketch_image": sketch_data_uri,

        "debug_images": debug_images,
    }


def normalize_broward_result(result):
    """Backfill key KPI fields so the result tiles never render empty."""
    if not isinstance(result, dict):
        return result

    normalized = dict(result)
    waste_rows = normalized.get("waste_breakdown") or []
    recommended_row = next((row for row in waste_rows if row.get("recommended")), None)
    if not recommended_row and waste_rows:
        recommended_row = waste_rows[0]

    ground_area = _safe_float(normalized.get("ground_area"), 0)
    if ground_area <= 0:
        ground_area = _safe_float(normalized.get("lot_area"), 0)

    final_area = _safe_float(normalized.get("final_area"), 0)
    if final_area <= 0 and recommended_row:
        final_area = _safe_float(recommended_row.get("area"), 0)

    final_squares = _safe_float(normalized.get("final_squares"), 0)
    if final_squares <= 0 and recommended_row:
        final_squares = _safe_float(recommended_row.get("squares"), 0)
    if final_squares <= 0 and final_area > 0:
        final_squares = final_area / 100

    adjusted_surface = _safe_float(normalized.get("adjusted_surface"), 0)
    if adjusted_surface <= 0 and final_area > 0:
        waste_percent = _safe_float(normalized.get("recommended_waste"), 0)
        divisor = 1 + (waste_percent / 100)
        adjusted_surface = final_area / divisor if divisor > 0 else final_area

    normalized["ground_area"] = round(ground_area, 0)
    normalized["final_area"] = round(final_area, 0)
    normalized["final_squares"] = round(final_squares, 1)
    normalized["adjusted_surface"] = round(adjusted_surface, 0)
    normalized["pitch"] = _safe_int(normalized.get("pitch"), 5)
    normalized["complexity"] = _s(normalized.get("complexity")) or "moderate"
    normalized["recommended_waste"] = round(_safe_float(normalized.get("recommended_waste"), 12), 1)
    return normalized

def build_broward_email_summary(result):
    lines = [
        f"Subject: Broward & Palm Beach Roof Estimate - {result['address']}, {result['city']}",
        "",
        "Team,",
        "",
        "Below is the Broward & Palm Beach estimate summary.",
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
        "- Broward & Palm Beach Estimator is currently in beta.",
        "- Figures are directional and should be field-verified.",
    ])
    return "\n".join(lines)


def build_pricing_email_summary(pricing_result):
    if not pricing_result:
        return ""

    lines = [
        "",
        "SCI Pricing Add-On:",
        f"- Material: {pricing_result.get('material', 'N/A')}",
        f"- Access Level: {pricing_result.get('access_level', 'N/A')}",
        f"- Roof Quantity: {pricing_result.get('squares', 0)} squares",
        f"- Price / Square: ${pricing_result.get('price_per_square', 0):,.2f}",
        f"- Material Baseline: ${pricing_result.get('baseline_material', 0):,.0f}",
        f"- Estimated Contract Price: ${pricing_result.get('estimated_total', 0):,.0f}",
    ]
    return "\n".join(lines)

def parse_pricing_result_from_form(form):
    material = form.get("pricing_material", "").strip().lower()
    access_level = form.get("access_level", "").strip()

    if not material or not access_level:
        return {
            "form": {
                "access_level": access_level,
                "material": material,
            },
            "result": None,
        }

    def _to_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    squares_raw = form.get("pricing_squares", "").strip()
    baseline_raw = form.get("pricing_baseline_material", "").strip()
    price_per_square_raw = form.get("pricing_price_per_square", "").strip()
    estimated_total_raw = form.get("pricing_estimated_total", "").strip()
    has_pricing_payload = any([squares_raw, baseline_raw, price_per_square_raw, estimated_total_raw])

    parsed_result = None
    if has_pricing_payload:
        parsed_result = {
            "material": material,
            "access_level": access_level,
            "squares": round(_to_float(squares_raw), 1),
            "baseline_material": _to_float(baseline_raw),
            "price_per_square": _to_float(price_per_square_raw),
            "estimated_total": _to_float(estimated_total_raw),
        }

    return {
        "form": {
            "access_level": access_level,
            "material": material,
        },
        "result": parsed_result,
    }



def _decode_data_uri_image(data_uri):
    value = (data_uri or "").strip()
    if not value.startswith("data:image/") or "," not in value:
        return None

    header, payload = value.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip().lower()
    if not mime.startswith("image/"):
        return None

    try:
        return {
            "mime": mime,
            "bytes": base64.b64decode(payload),
        }
    except Exception:
        return None


def build_broward_email_html(result, pricing_result=None):
    address = html.escape(result.get("address", ""))
    city = html.escape(result.get("city", ""))
    complexity = html.escape(str(result.get("complexity", "")).capitalize())

    rows = []
    for row in result.get("waste_breakdown", []):
        rows.append(
            "<tr>"
            f"<td style='padding:6px 10px; border:1px solid #d9d9d9;'>{row.get('waste', '')}%</td>"
            f"<td style='padding:6px 10px; border:1px solid #d9d9d9;'>{row.get('area', 0):,.0f} sq ft</td>"
            f"<td style='padding:6px 10px; border:1px solid #d9d9d9;'>{row.get('squares', 0)} squares</td>"
            "</tr>"
        )

    pricing_html = ""
    if pricing_result:
        pricing_html = f"""
    <h4 style=\"margin-bottom: 8px;\">SCI Pricing Add-On</h4>
    <table style=\"border-collapse: collapse; margin-bottom: 16px; min-width: 420px;\">
      <tbody>
        <tr>
          <th style=\"padding:6px 10px; border:1px solid #d9d9d9; text-align:left; background:#f8f9fa;\">Material</th>
          <td style=\"padding:6px 10px; border:1px solid #d9d9d9;\">{html.escape(str(pricing_result.get('material', 'N/A')))}</td>
        </tr>
        <tr>
          <th style=\"padding:6px 10px; border:1px solid #d9d9d9; text-align:left; background:#f8f9fa;\">Access Level</th>
          <td style=\"padding:6px 10px; border:1px solid #d9d9d9;\">{html.escape(str(pricing_result.get('access_level', 'N/A')))}</td>
        </tr>
        <tr>
          <th style=\"padding:6px 10px; border:1px solid #d9d9d9; text-align:left; background:#f8f9fa;\">Roof Quantity</th>
          <td style=\"padding:6px 10px; border:1px solid #d9d9d9;\">{pricing_result.get('squares', 0)} squares</td>
        </tr>
        <tr>
          <th style=\"padding:6px 10px; border:1px solid #d9d9d9; text-align:left; background:#f8f9fa;\">Price / Square</th>
          <td style=\"padding:6px 10px; border:1px solid #d9d9d9;\">${pricing_result.get('price_per_square', 0):,.2f}</td>
        </tr>
        <tr>
          <th style=\"padding:6px 10px; border:1px solid #d9d9d9; text-align:left; background:#f8f9fa;\">Material Baseline</th>
          <td style=\"padding:6px 10px; border:1px solid #d9d9d9;\">${pricing_result.get('baseline_material', 0):,.0f}</td>
        </tr>
        <tr>
          <th style=\"padding:6px 10px; border:1px solid #d9d9d9; text-align:left; background:#f8f9fa;\">Estimated Contract Price</th>
          <td style=\"padding:6px 10px; border:1px solid #d9d9d9;\"><strong>${pricing_result.get('estimated_total', 0):,.0f}</strong></td>
        </tr>
      </tbody>
    </table>
"""

    return f"""
<html>
  <body style="font-family: Arial, sans-serif; color: #222; line-height: 1.4;">
    <p>Team,</p>
    <p>Below is the Broward & Palm Beach estimate summary.</p>
    <p>
      <strong>Property:</strong> {address}, {city}<br>
      <strong>Ground Plane Area:</strong> {result.get('ground_area', 0):,.0f} sq ft<br>
      <strong>Pitch:</strong> {result.get('pitch', '')}/12<br>
      <strong>Complexity:</strong> {complexity}<br>
      <strong>Adjusted Surface:</strong> {result.get('adjusted_surface', 0):,.0f} sq ft<br>
      <strong>Recommended Waste:</strong> {result.get('recommended_waste', '')}%<br>
      <strong>Final Quantity:</strong> {result.get('final_area', 0):,.0f} sq ft ({result.get('final_squares', 0)} squares)
    </p>

    <h4 style="margin-bottom: 8px;">Waste Breakdown</h4>
    <table style="border-collapse: collapse; margin-bottom: 16px; min-width: 420px;">
      <thead>
        <tr>
          <th style="padding:6px 10px; border:1px solid #d9d9d9; text-align:left;">Waste %</th>
          <th style="padding:6px 10px; border:1px solid #d9d9d9; text-align:left;">Area</th>
          <th style="padding:6px 10px; border:1px solid #d9d9d9; text-align:left;">Squares</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>

    {pricing_html}

    <h4 style="margin-bottom: 8px;">Report Images</h4>
    <p style="margin: 0 0 8px 0;">Front Photo</p>
    <img src="cid:front-photo" alt="Front photo" style="display:block; max-width:100%; max-height:360px; border:1px solid #d9d9d9; border-radius:6px; margin-bottom:14px;">

    <p style="margin: 0 0 8px 0;">Property Sketch</p>
    <img src="cid:bcpa-sketch" alt="Property sketch" style="display:block; max-width:100%; max-height:360px; border:1px solid #d9d9d9; border-radius:6px; background:#f8f9fa;">

    <p style="margin-top: 16px; color: #666; font-size: 12px;">Broward & Palm Beach Estimator is in beta. Validate on-site before ordering materials.</p>
  </body>
</html>
"""


def send_estimate_email(recipient, subject, body, result=None, pricing_result=None):
    if not (SMTP_HOST and SMTP_FROM_EMAIL):
        return False, "SMTP is not configured. Set SMTP_HOST and SMTP_FROM_EMAIL (or SENDGRID_FROM_EMAIL) to enable outbound emails."

    from_email = _get_sender_email_for_user(session.get("username", "")) if session.get("username") else SMTP_FROM_EMAIL
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = recipient
        msg.set_content(body)

        if result:
            inline_images = []
            front_image = _decode_data_uri_image(result.get("report_front_image"))
            sketch_image = _decode_data_uri_image(result.get("report_sketch_image"))
            if front_image:
                inline_images.append(("front-photo", front_image))
            if sketch_image:
                inline_images.append(("bcpa-sketch", sketch_image))

            if inline_images:
                msg.add_alternative(build_broward_email_html(result, pricing_result), subtype="html")
                html_part = msg.get_payload()[-1]
                for cid, image_data in inline_images:
                    maintype, subtype = image_data["mime"].split("/", 1)
                    html_part.add_related(
                        image_data["bytes"],
                        maintype=maintype,
                        subtype=subtype,
                        cid=f"<{cid}>",
                        filename=f"{cid}.{subtype}",
                        disposition="inline",
                    )

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
        if session.get("brand") == "adminchan":
            return redirect(url_for("adminchan_dashboard"))
        if session.get("brand") == "jobsdirect":
            return redirect(url_for("jobsdirect_dashboard"))
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
            if info["brand"] == "adminchan":
                return redirect(url_for("adminchan_dashboard"))
            if info["brand"] == "jobsdirect":
                return redirect(url_for("jobsdirect_dashboard"))    
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
    pricing_result = None
    pricing_form = {
        "access_level": "",
        "material": "",
    }
    if request.method == "POST":
        action = request.form.get("action", "standard_estimate").strip()

        if action == "broward_ai_search":
            broward_form = {
                "search_address": request.form.get("search_address", "").strip(),
                "search_city": request.form.get("search_city", "").strip(),
                "result_email": request.form.get("result_email", "").strip(),
            }
            pricing_payload = parse_pricing_result_from_form(request.form)
            pricing_form = pricing_payload["form"]
            pricing_result = pricing_payload["result"]
            broward_query = ", ".join(part for part in [broward_form["search_address"], broward_form["search_city"]] if part)
            if not broward_form["search_address"] or not broward_form["search_city"]:
                flash("Please provide both address and city for Broward & Palm Beach Estimator.")
            else:
                try:
                    broward_result = normalize_broward_result(
                        generate_broward_estimate(broward_form["search_address"], broward_form["search_city"])
                    )
                    if pricing_form["access_level"] and pricing_form["material"] and not pricing_result:
                        pricing_result = generate_sci_pricing_estimate({
                            "squares": broward_result.get("final_squares", 0),
                            "material": pricing_form["material"],
                            "access_level": pricing_form["access_level"],
                        })
                    flash("Broward & Palm Beach Estimator complete.")
                    try:
                        dbg = broward_result.get("debug_images") or {}
                        sketch_note = "OK" if dbg.get("sketch_ok") else "MISSING"
                        map_note = "OK" if dbg.get("map_ok") else "MISSING"
                        photo_note = "OK" if dbg.get("photo_ok") else "MISSING"
                        sketch_kb = round((dbg.get("sketch_bytes", 0) or 0) / 1024, 1)
                        map_kb = round((dbg.get("map_bytes", 0) or 0) / 1024, 1)
                        flash(f"Image capture: Photo={photo_note} | Sketch={sketch_note} ({sketch_kb} KB) | Map={map_note} ({map_kb} KB)")
                    except Exception:
                        pass
                    if broward_form["result_email"]:
                        summary = build_broward_email_summary(broward_result) + build_pricing_email_summary(pricing_result)
                        subject = f"Broward & Palm Beach Roof Estimate - {broward_result['address']}, {broward_result['city']}"
                        sent, email_message = send_estimate_email(
                            broward_form["result_email"],
                            subject,
                            summary,
                            broward_result,
                            pricing_result,
                        )
                        flash(email_message)
                        if not sent:
                            flash("Tip: configure SMTP_HOST / SMTP_FROM_EMAIL and SMTP credentials (or SENDGRID_API_KEY) to enable email delivery.")
                except Exception as exc:
                    logger.exception("Broward & Palm Beach Estimator failed")
                    flash(f"Broward & Palm Beach Estimator failed: {exc}")

        elif action == "add_pricing":
            broward_form = {
                "search_address": request.form.get("search_address", "").strip(),
                "search_city": request.form.get("search_city", "").strip(),
                "result_email": "",
            }
            pricing_form = {
                "access_level": request.form.get("access_level", "").strip(),
                "material": request.form.get("pricing_material", "").strip().lower(),
            }
            broward_query = ", ".join(part for part in [broward_form["search_address"], broward_form["search_city"]] if part)

            if not broward_form["search_address"] or not broward_form["search_city"]:
                flash("Please run Broward & Palm Beach Estimator first so we can pull the roof quantity.")
            elif not pricing_form["access_level"] or not pricing_form["material"]:
                flash("Please choose both floor level/access and material.")
                broward_result = None
            else:
                try:
                    broward_result = normalize_broward_result(
                        generate_broward_estimate(broward_form["search_address"], broward_form["search_city"])
                    )
                    pricing_result = generate_sci_pricing_estimate({
                        "squares": broward_result.get("final_squares", 0),
                        "material": pricing_form["material"],
                        "access_level": pricing_form["access_level"],
                    })
                    flash("SCI pricing estimate generated.")
                except Exception as exc:
                    logger.exception("SCI pricing estimate failed")
                    flash(f"SCI pricing estimate failed: {exc}")

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
        pricing_result=pricing_result,
        pricing_form=pricing_form,
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
        sci_token = _build_sci_embed_token()
        extra_context["sci_embed_url"] = url_for("sci_map_embed", token=sci_token, _external=True)
    elif brand == "munsie":
        template = "munsie_dashboard.html"
    else:
        template = "generic_dashboard.html"

    return render_template(template, title="Permit Database", **ctx, **extra_context)

@app.route("/sci/map/embed")
def sci_map_embed():
    token = request.args.get("token", "")
    if not _is_valid_sci_embed_token(token):
        return abort(403)
    mode = (request.args.get("mode", "full") or "full").lower()
    embed_mode = mode if mode in {"full", "lite"} else "full"
    return render_template("sci_map_embed.html", sci_project_locations=get_sci_project_locations(), embed_mode=embed_mode)
    
@app.route("/api/sci/spots", methods=["POST"])
def add_sci_spot():
    if not require_login():
        return jsonify({"error": "Unauthorized"}), 401
    if current_brand() != "sci":
        return jsonify({"error": "Forbidden"}), 403
        

    data = request.get_json(silent=True) or {}
    address = (data.get("address") or "").strip()
    status = (data.get("status") or "").strip()
    spot_type = (data.get("type") or "").strip()
    residential = data.get("residential", True)

    if not address:
        return jsonify({"error": "Address is required"}), 400

    valid_types = {"Residential", "Commercial", "Repairs", "Maintenance"}
    if spot_type not in valid_types:
        spot_type = "Residential" if residential else "Commercial"

    location_id = _slugify(address)
    city = _extract_city_from_address(address)
    coords = _geocode_address(address) or _estimate_coords(address, spot_type)

    spot = {
        "id": location_id,
        "name": "",
        "type": spot_type,
        "address": address,
        "city": city,
        "status": status or "New",
        "coords": coords,
    }

    spots = _load_custom_spots()
    spots.append(spot)
    _save_custom_spots(spots)

    global _sci_projects_cache
    _sci_projects_cache = None

    return jsonify(spot), 201

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

# -------- JobsDirect Email Dashboard --------
JOBSDIRECT_SENT_LOG = []  # in-memory sent-email log

@app.route("/jobsdirect")
def jobsdirect_dashboard():
    if not require_login():
        return redirect(url_for("login"))
    if current_brand() != "jobsdirect":
        return redirect(url_for("dashboard"))

    from_email = _get_sender_email_for_user(session.get("username", ""))
    return render_template("jobsdirect_dashboard.html",
                           title="JobsDirect Dashboard",
                           from_email=from_email,
                           username=session.get("username"),
                           sent_log=JOBSDIRECT_SENT_LOG,
                           body_class="")

@app.route("/jobsdirect/send", methods=["POST"])
def jobsdirect_send():
    if not require_login() or current_brand() != "jobsdirect":
        flash("Access denied.")
        return redirect(url_for("login"))

    to_email = (request.form.get("to_email") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    body = (request.form.get("body") or "").strip()

    if not to_email or not subject or not body:
        flash("All fields (to, subject, body) are required.")
        return redirect(url_for("jobsdirect_dashboard"))

    from_email = _get_sender_email_for_user(session.get("username", ""))
    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        msg.set_content(body)

        # Also add an HTML version (simple inline formatting, no external templates)
        html_body = f"""\
<html>
<body style="font-family: Arial, Helvetica, sans-serif; color: #1e293b; line-height: 1.6; padding: 20px;">
  <div style="max-width: 600px; margin: 0 auto;">
    <h2 style="color: #4f46e5; margin-bottom: 4px;">{subject}</h2>
    <hr style="border: none; border-top: 2px solid #e2e8f0; margin: 12px 0 20px;">
    <div style="white-space: pre-wrap;">{body}</div>
    <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0 12px;">
    <p style="font-size: 12px; color: #94a3b8;">Sent by JobsDirect &mdash; {from_email}</p>
  </div>
</body>
</html>"""
        msg.add_alternative(html_body, subtype="html")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)

        JOBSDIRECT_SENT_LOG.append({
            "to": to_email, "subject": subject, "status": "OK", "sent_at": sent_at,
        })
        flash(f"Email sent to {to_email}.")
    except Exception as exc:
        logger.exception("JobsDirect email send failed")
        JOBSDIRECT_SENT_LOG.append({
            "to": to_email, "subject": subject, "status": "FAILED", "sent_at": sent_at,
        })
        flash(f"Failed to send email: {exc}")

    return redirect(url_for("jobsdirect_dashboard"))

# -------- Adminchan Email Manager --------
@app.route("/adminchan")
def adminchan_dashboard():
    if not require_login():
        return redirect(url_for("login"))
    if current_brand() != "adminchan":
        return redirect(url_for("dashboard"))

    # Build list of all non-adminchan clients with their email data
    clients = []
    for uname, info in USERS.items():
        if uname == session.get("username"):
            continue  # skip self
        clients.append({
            "username": uname,
            "brand": info["brand"],
            "role": info["role"],
            "email_data": _get_client_email_data(uname),
        })

    return render_template("adminchan_dashboard.html",
                           title="Email Manager",
                           clients=clients,
                           email_lists=_get_all_email_lists(),
                           blast_schedules=EMAIL_BLAST_SCHEDULES,
                           test_email=TEST_EMAIL_ADDRESS,
                           body_class="")

@app.route("/adminchan/upload/<client_username>", methods=["POST"])
def adminchan_upload_list(client_username):
    if not require_login() or current_brand() != "adminchan":
        flash("Access denied.")
        return redirect(url_for("login"))

    if client_username not in USERS:
        flash("Client not found.")
        return redirect(url_for("adminchan_dashboard"))

    f = request.files.get("excel_file")
    if not f or not f.filename:
        flash("No file selected.")
        return redirect(url_for("adminchan_dashboard"))

    fname = f.filename.lower()
    try:
        if fname.endswith(".csv"):
            df = pd.read_csv(f)
        else:
            df = pd.read_excel(f)

        # Try to find an email column
        email_col = None
        for col in df.columns:
            if "email" in col.lower():
                email_col = col
                break

        emails = []
        if email_col:
            emails = [str(v).strip() for v in df[email_col].dropna().tolist() if str(v).strip()]
        else:
            # Fallback: take first column as emails
            emails = [str(v).strip() for v in df.iloc[:, 0].dropna().tolist() if str(v).strip()]

        data = _get_client_email_data(client_username)
        data["lists"].append({
            "name": f.filename,
            "emails": emails,
            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        flash(f"Uploaded '{f.filename}' with {len(emails)} emails for {client_username}.")
    except Exception as exc:
        logger.exception("Excel upload failed")
        flash(f"Failed to process file: {exc}")

    return redirect(url_for("adminchan_dashboard"))


@app.route("/adminchan/blast/schedule", methods=["POST"])
def adminchan_blast_schedule():
    if not require_login() or current_brand() != "adminchan":
        flash("Access denied.")
        return redirect(url_for("login"))

    action = request.form.get("action", "")
    list_index = request.form.get("list_index", "")
    selected_raw = request.form.get("selected_emails", "")
    subject = request.form.get("subject", "").strip()
    from_name = request.form.get("from_name", "").strip()
    body = request.form.get("body", "").strip()
    scheduled_for = request.form.get("scheduled_for", "").strip()

    all_lists = _get_all_email_lists()
    list_owner = None
    try:
        idx = int(list_index) if list_index else -1
        if 0 <= idx < len(all_lists):
            list_owner = all_lists[idx].get("client")
    except (ValueError, IndexError):
        pass
    sender_email = _get_sender_email_for_user(list_owner) if list_owner else SMTP_FROM_EMAIL

    if action == "test":
        if not subject or not body:
            flash("Subject and body are required to send a test email.")
            return redirect(url_for("adminchan_dashboard"))
        ok, err = _send_blast_email(TEST_EMAIL_ADDRESS, f"[TEST] {subject}", body, from_name, sender_email=sender_email)
        if ok:
            flash(f"Test email sent to {TEST_EMAIL_ADDRESS} (from {sender_email}).")
        else:
            flash(f"Test email failed: {err}")
        return redirect(url_for("adminchan_dashboard"))

    if not selected_raw or not subject or not body:
        flash("Subject, body, and at least one recipient are required.")
        return redirect(url_for("adminchan_dashboard"))

    selected_emails = [e.strip() for e in selected_raw.split("||") if e.strip()]
    if not selected_emails:
        flash("No recipients selected.")
        return redirect(url_for("adminchan_dashboard"))

    list_name = "Unknown"
    try:
        idx = int(list_index)
        if 0 <= idx < len(all_lists):
            list_name = f"{all_lists[idx]['name']} ({all_lists[idx]['client']})"
    except (ValueError, IndexError):
        pass

    if action == "send_now":
        ok_count, fail_count = 0, 0
        for em in selected_emails:
            ok, err = _send_blast_email(em, subject, body, from_name, sender_email=sender_email)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
        blast = {
            "id": _next_blast_id(),
            "subject": subject,
            "body": body,
            "from_name": from_name,
            "sender_email": sender_email,
            "list_name": list_name,
            "recipients": selected_emails,
            "recipient_count": len(selected_emails),
            "scheduled_for": None,
            "status": "sent",
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "send_result": f"{ok_count} delivered, {fail_count} failed",
        }
        EMAIL_BLAST_SCHEDULES.insert(0, blast)
        flash(f"Blast sent from {sender_email}! {ok_count} delivered, {fail_count} failed.")
        return redirect(url_for("adminchan_dashboard"))

    if not scheduled_for:
        flash("Please select a date and time for the scheduled blast.")
        return redirect(url_for("adminchan_dashboard"))

    blast = {
        "id": _next_blast_id(),
        "subject": subject,
        "body": body,
        "from_name": from_name,
        "sender_email": sender_email,
        "list_name": list_name,
        "recipients": selected_emails,
        "recipient_count": len(selected_emails),
        "scheduled_for": scheduled_for,
        "status": "pending",
        "sent_at": None,
        "send_result": None,
    }
    EMAIL_BLAST_SCHEDULES.insert(0, blast)
    flash(f"Blast #{blast['id']} scheduled for {scheduled_for} to {len(selected_emails)} recipients.")
    return redirect(url_for("adminchan_dashboard"))


@app.route("/adminchan/blast/action", methods=["POST"])
def adminchan_blast_action():
    if not require_login() or current_brand() != "adminchan":
        flash("Access denied.")
        return redirect(url_for("login"))

    blast_id = request.form.get("blast_id", type=int)
    action = request.form.get("action", "")

    blast = next((b for b in EMAIL_BLAST_SCHEDULES if b["id"] == blast_id), None)
    if not blast:
        flash("Blast not found.")
        return redirect(url_for("adminchan_dashboard"))

    if action == "cancel":
        blast["status"] = "cancelled"
        flash(f"Blast #{blast_id} cancelled.")
    elif action == "send" and blast["status"] == "pending":
        ok_count, fail_count = 0, 0
        for em in blast["recipients"]:
            ok, _ = _send_blast_email(em, blast["subject"], blast["body"], blast.get("from_name"), sender_email=blast.get("sender_email"))
            if ok:
                ok_count += 1
            else:
                fail_count += 1
        blast["status"] = "sent"
        blast["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        blast["send_result"] = f"{ok_count} delivered, {fail_count} failed"
        flash(f"Blast #{blast_id} sent! {ok_count} delivered, {fail_count} failed.")

    return redirect(url_for("adminchan_dashboard"))


# -------- Admin area --------
@app.route("/admin")
def admin_page():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("dashboard") if session.get("username") else url_for("login"))

    # For display convenience in template
    users_view = {u: type("obj", (), info) for u, info in USERS.items()}
    return render_template("admin.html",
                           users=users_view,
                           title="Admin",
                           email_lists=_get_all_email_lists(),
                           blast_schedules=EMAIL_BLAST_SCHEDULES,
                           test_email=TEST_EMAIL_ADDRESS)

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
    if brand not in ("sci","generic","munsie","adminchan","jobsdirect"):
        flash("Invalid brand.")
        return redirect(url_for("admin_page"))

    sender_email = request.form.get("sender_email", "").strip()
    USERS[username] = {"password": password, "role": role, "brand": brand, "sender_email": sender_email}
    flash(f"User '{username}' added.")
    return redirect(url_for("admin_page"))

@app.route("/admin/update_sender_email", methods=["POST"])
def admin_update_sender_email():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("login"))

    username = request.form.get("username", "").strip()
    sender_email = request.form.get("sender_email", "").strip()

    if username not in USERS:
        flash("User not found.")
        return redirect(url_for("admin_page"))

    USERS[username]["sender_email"] = sender_email
    flash(f"Sender email for '{username}' updated to '{sender_email or '(none)' }'.")
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


# -------- Email Blast Scheduler routes --------
def _send_blast_email(to_email, subject, body_html, from_name=None, sender_email=None):
    """Send a single blast email. Returns (success: bool, error: str|None)."""
    effective_from = sender_email or SMTP_FROM_EMAIL
    from_addr = f"{from_name} <{effective_from}>" if from_name else effective_from
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email
        msg.set_content(re.sub("<[^>]+>", "", body_html))  # plain-text fallback

        html_body = f"""\
<html>
<body style="font-family:Arial,Helvetica,sans-serif;color:#1e293b;line-height:1.6;padding:20px;">
  <div style="max-width:600px;margin:0 auto;">
    <h2 style="color:#2563eb;margin-bottom:4px;">{subject}</h2>
    <hr style="border:none;border-top:2px solid #e2e8f0;margin:12px 0 20px;">
    <div>{body_html}</div>
    <hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 12px;">
    <p style="font-size:12px;color:#94a3b8;">Sent via Email Blast Scheduler</p>
  </div>
</body>
</html>"""
        msg.add_alternative(html_body, subtype="html")

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True, None
    except Exception as exc:
        logger.exception("Blast email send failed to %s", to_email)
        return False, str(exc)


@app.route("/admin/blast/schedule", methods=["POST"])
def admin_blast_schedule():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("login"))

    action = request.form.get("action", "")
    list_index = request.form.get("list_index", "")
    selected_raw = request.form.get("selected_emails", "")
    subject = request.form.get("subject", "").strip()
    from_name = request.form.get("from_name", "").strip()
    body = request.form.get("body", "").strip()
    scheduled_for = request.form.get("scheduled_for", "").strip()

    # Resolve sender email from the selected list's owner
    all_lists = _get_all_email_lists()
    list_owner = None
    try:
        idx = int(list_index) if list_index else -1
        if 0 <= idx < len(all_lists):
            list_owner = all_lists[idx].get("client")
    except (ValueError, IndexError):
        pass
    sender_email = _get_sender_email_for_user(list_owner) if list_owner else SMTP_FROM_EMAIL

    # Test emails only need subject and body - no recipients required
    if action == "test":
        if not subject or not body:
            flash("Subject and body are required to send a test email.")
            return redirect(url_for("admin_page"))
        ok, err = _send_blast_email(TEST_EMAIL_ADDRESS, f"[TEST] {subject}", body, from_name, sender_email=sender_email)
        if ok:
            flash(f"Test email sent to {TEST_EMAIL_ADDRESS} (from {sender_email}).")
        else:
            flash(f"Test email failed: {err}")
        return redirect(url_for("admin_page"))

    if not selected_raw or not subject or not body:
        flash("Subject, body, and at least one recipient are required.")
        return redirect(url_for("admin_page"))

    selected_emails = [e.strip() for e in selected_raw.split("||") if e.strip()]
    if not selected_emails:
        flash("No recipients selected.")
        return redirect(url_for("admin_page"))

    # Determine list name (reuse all_lists from above)
    list_name = "Unknown"
    try:
        idx = int(list_index)
        if 0 <= idx < len(all_lists):
            list_name = f"{all_lists[idx]['name']} ({all_lists[idx]['client']})"
    except (ValueError, IndexError):
        pass

    if action == "send_now":
        # Send immediately to all selected recipients
        ok_count, fail_count = 0, 0
        for em in selected_emails:
            ok, err = _send_blast_email(em, subject, body, from_name, sender_email=sender_email)
            if ok:
                ok_count += 1
            else:
                fail_count += 1

        blast = {
            "id": _next_blast_id(),
            "subject": subject,
            "body": body,
            "from_name": from_name,
            "sender_email": sender_email,
            "list_name": list_name,
            "recipients": selected_emails,
            "recipient_count": len(selected_emails),
            "scheduled_for": None,
            "status": "sent",
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        EMAIL_BLAST_SCHEDULES.insert(0, blast)
        flash(f"Blast sent from {sender_email}! {ok_count} delivered, {fail_count} failed.")
        return redirect(url_for("admin_page"))

    # Default: schedule for later
    if not scheduled_for:
        flash("Please select a date and time for the scheduled blast.")
        return redirect(url_for("admin_page"))

    blast = {
        "id": _next_blast_id(),
        "subject": subject,
        "body": body,
        "from_name": from_name,
        "sender_email": sender_email,
        "list_name": list_name,
        "recipients": selected_emails,
        "recipient_count": len(selected_emails),
        "scheduled_for": scheduled_for,
        "status": "pending",
        "sent_at": None,
    }
    EMAIL_BLAST_SCHEDULES.insert(0, blast)
    flash(f"Blast #{blast['id']} scheduled for {scheduled_for} to {len(selected_emails)} recipients.")
    return redirect(url_for("admin_page"))


@app.route("/admin/blast/action", methods=["POST"])
def admin_blast_action():
    if not require_login() or not is_admin():
        flash("Admin access required.")
        return redirect(url_for("login"))

    blast_id = request.form.get("blast_id", type=int)
    action = request.form.get("action", "")

    blast = next((b for b in EMAIL_BLAST_SCHEDULES if b["id"] == blast_id), None)
    if not blast:
        flash("Blast not found.")
        return redirect(url_for("admin_page"))

    if action == "cancel":
        blast["status"] = "cancelled"
        flash(f"Blast #{blast_id} cancelled.")
    elif action == "send" and blast["status"] == "pending":
        ok_count, fail_count = 0, 0
        for em in blast["recipients"]:
            ok, _ = _send_blast_email(em, blast["subject"], blast["body"], blast.get("from_name"), sender_email=blast.get("sender_email"))
            if ok:
                ok_count += 1
            else:
                fail_count += 1
        blast["status"] = "sent"
        blast["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        flash(f"Blast #{blast_id} sent! {ok_count} delivered, {fail_count} failed.")

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
@app.route("/debug/palm-beach-outputs")
def palm_beach_saved_outputs():
    if not require_login():
        return redirect(url_for("login"))

    palm_beach_files = []
    for name in sorted(os.listdir(BROWARD_OUTPUT_DIR), reverse=True):
        if not (name.startswith("palm_beach_") or name.lower().endswith(".pdf")):
            continue
        full_path = os.path.join(BROWARD_OUTPUT_DIR, name)
        if not os.path.isfile(full_path):
            continue
        palm_beach_files.append({
            "name": name,
            "size_kb": round(os.path.getsize(full_path) / 1024, 1),
            "modified": datetime.fromtimestamp(os.path.getmtime(full_path)).strftime("%Y-%m-%d %H:%M:%S"),
        })

    return render_template_string(
        """
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Palm Beach Saved Outputs</title>
            <style>
              body { font-family: Arial, sans-serif; margin: 20px; background: #f7f8fb; }
              .card { max-width: 980px; margin: 0 auto; background: #fff; border-radius: 10px; padding: 18px; box-shadow: 0 6px 24px rgba(0,0,0,.08); }
              table { width: 100%; border-collapse: collapse; margin-top: 12px; }
              th, td { text-align: left; border-bottom: 1px solid #e5e7eb; padding: 8px; }
              th { background: #f1f5f9; }
            </style>
          </head>
          <body>
            <div class="card">
              <h2 style="margin-top:0;">Palm Beach Saved Outputs</h2>
              <p style="color:#475569;">Folder: {{ output_dir }}</p>
              {% if files %}
                <table>
                  <thead>
                    <tr><th>File</th><th>Modified</th><th>Size (KB)</th><th>Open</th></tr>
                  </thead>
                  <tbody>
                    {% for file in files %}
                      <tr>
                        <td>{{ file.name }}</td>
                        <td>{{ file.modified }}</td>
                        <td>{{ file.size_kb }}</td>
                        <td><a href="{{ url_for('palm_beach_saved_output_file', filename=file.name) }}" target="_blank" rel="noopener noreferrer">Open</a></td>
                      </tr>
                    {% endfor %}
                  </tbody>
                </table>
              {% else %}
                <p>No Palm Beach output files found yet.</p>
              {% endif %}
            </div>
          </body>
        </html>
        """,
        files=palm_beach_files,
        output_dir=BROWARD_OUTPUT_DIR,
    )


@app.route("/debug/palm-beach-outputs/<path:filename>")
def palm_beach_saved_output_file(filename):
    if not require_login():
        return redirect(url_for("login"))

    safe_name = os.path.basename(filename)
    if safe_name != filename:
        abort(400)
    if not (safe_name.startswith("palm_beach_") or safe_name.lower().endswith(".pdf")):
        abort(404)
    full_path = os.path.join(BROWARD_OUTPUT_DIR, safe_name)
    if not os.path.isfile(full_path):
        abort(404)
    return send_from_directory(BROWARD_OUTPUT_DIR, safe_name)

@app.get("/health")
def health():
    return {"ok": True}, 200

@app.route("/debug-chrome")
def debug_chrome():
    import subprocess
    results = {}
    
    for cmd in [
        "which chromium",
        "which chromium-browser", 
        "which google-chrome",
        "which chromedriver",
        "ls /usr/bin/chrom*",
        "ls /usr/lib/chromium*",
        "chromium --version",
        "chromedriver --version",
    ]:
        try:
            out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT).decode()
            results[cmd] = out.strip()
        except Exception as e:
            results[cmd] = f"ERROR: {e}"
    
    return results

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




















































































































































































































































































































































