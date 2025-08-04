from flask import Flask, render_template, request, redirect, url_for
import random
from faker import Faker
from datetime import datetime
from jinja2 import DictLoader

app = Flask(__name__)
fake = Faker()

# Generate fake data
properties = [{
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
    "contact_info": [],
    "notes": []
} for i in range(1, 51)]

# HTML Templates in DictLoader
app.jinja_loader = DictLoader({
    'index.html': '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>SCI Roofing Permits</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
        <style>
            body { margin: 20px; }
            table { width: 100%; }
            th, td { text-align: center; vertical-align: middle; }
            th input, th select { width: 90%; margin: auto; }
            img { max-width: 250px; margin-bottom: 20px; }
        </style>
        <script>
            function toggleDateInputs() {
                const filter = document.getElementById("date_filter").value;
                const toField = document.getElementById("date_to");
                if (filter === "between") {
                    toField.style.display = "block";
                } else {
                    toField.style.display = "none";
                    toField.value = "";  // clear stale value
                }
            }
            window.addEventListener("DOMContentLoaded", toggleDateInputs);
        </script>
    </head>
    <body onload="toggleDateInputs()">
        <div class="container">
            <img src="{{ url_for('static', filename='SCILOGO.png') }}">
            <h2 class="mb-4">Permit Database</h2>
            <form method="get" class="row g-2 align-items-end mb-4">
                <div class="col-md-2">
                    <input type="text" class="form-control" name="address" placeholder="Address or City" value="{{ address }}">
                </div>
                <div class="col-md-2">
                    <input type="text" class="form-control" name="roof_material" placeholder="Roof Material" value="{{ roof_material }}">
                </div>
                <div class="col-md-2">
                    <input type="text" class="form-control" name="owner" placeholder="Owner" value="{{ owner }}">
                </div>
                <div class="col-md-2">
                    <input type="text" class="form-control" name="property_use" placeholder="Property Use" value="{{ property_use }}">
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
                    <input type="date" class="form-control mb-1" name="date_from" value="{{ date_from }}">
                    <input type="date" class="form-control" name="date_to" id="date_to" value="{{ date_to }}"
                        {% if date_filter != 'between' %}style="display:none;"{% endif %}>
                </div>

                <div class="col-md-1 text-end">
                    <button class="btn btn-primary w-100">Search</button>
                </div>
            </form>
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
        </div>
    </body>
    </html>
    ''',

    'edit_property.html': '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Edit Property</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <script>
            function addContact(){
                document.getElementById('contacts').insertAdjacentHTML('beforeend', '<div class="input-group mb-2"><input type="email" name="email" placeholder="Email" class="form-control"><input type="text" name="phone" placeholder="Phone" class="form-control"><input type="text" name="job_title" placeholder="Job Title" class="form-control"></div>');
            }
            function confirmReturn() {
                if (confirm('Do you want to save your changes before returning to the main page?')) {
                    const form = document.querySelector('form');
                    const saveInput = document.createElement('input');
                    saveInput.type = 'hidden';
                    saveInput.name = 'redirect_to_main';
                    saveInput.value = '1';
                    form.appendChild(saveInput);
                    form.submit();
                } else {
                    window.location.href = "/";
                }
            }
        </script>
        <style>
            #note-box {
                max-height: 200px;
                overflow-y: auto;
                border: 1px solid #ddd;
                padding: 10px;
                background-color: #f8f9fa;
                margin-bottom: 1rem;
            }
        </style>
    </head>
    <body>
        <div class="container py-4">
            <h2>Edit Property Details</h2>

            {% if request.args.get('saved') == 'true' %}
                <div class="alert alert-success">Changes saved successfully!</div>
            {% endif %}

            <button onclick="confirmReturn()" class="btn btn-outline-primary mb-3">Return to Main Page</button>

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
                <div id="note-box">
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
                            <input class="form-control" name="email" value="{{ c.email }}">
                            <input class="form-control" name="phone" value="{{ c.phone }}">
                            <input class="form-control" name="job_title" value="{{ c.job_title }}">
                        </div>
                    {% endfor %}
                </div>
                <button type="button" class="btn btn-info mb-3" onclick="addContact()">Add Contact</button><br>
                <button class="btn btn-success" name="save" value="1">Save Changes</button>
            </form>
        </div>
    </body>
    </html>
    '''
})



# Routes and logic
@app.route('/')
def index():
    address = request.args.get('address', '').lower()
    roof_material = request.args.get('roof_material', '').lower()
    owner = request.args.get('owner', '').lower()
    property_use = request.args.get('property_use', '').lower()
    date_filter = request.args.get('date_filter', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    filtered_properties = properties

    if address:
        filtered_properties = [prop for prop in filtered_properties if address in prop['address'].lower() or address in prop['city'].lower()]
    if roof_material:
        filtered_properties = [prop for prop in filtered_properties if roof_material in prop['roof_material'].lower()]
    if owner:
        filtered_properties = [prop for prop in filtered_properties if owner in prop['owner'].lower()]
    if property_use:
        filtered_properties = [prop for prop in filtered_properties if property_use in prop['property_use'].lower()]

    try:
        if date_filter and date_from:
            date_from_obj = datetime.strptime(date_from, '%Y-%m-%d')
            if date_filter == 'before':
                filtered_properties = [prop for prop in filtered_properties if datetime.strptime(prop['last_roof_date'], '%Y-%m-%d') < date_from_obj]
            elif date_filter == 'after':
                filtered_properties = [prop for prop in filtered_properties if datetime.strptime(prop['last_roof_date'], '%Y-%m-%d') > date_from_obj]
            elif date_filter == 'between' and date_to:
                if date_to:
                    date_to_obj = datetime.strptime(date_to, '%Y-%m-%d')
                    filtered_properties = [prop for prop in filtered_properties if date_from_obj <= datetime.strptime(prop['last_roof_date'], '%Y-%m-%d') <= date_to_obj]
    except ValueError:
        pass  # ignore invalid date inputs silently

    return render_template('index.html', properties=filtered_properties,
                           address=address,
                           roof_material=roof_material,
                           owner=owner,
                           property_use=property_use,
                           date_filter=date_filter,
                           date_from=date_from,
                           date_to=date_to)




@app.route('/property/<int:prop_id>', methods=['GET', 'POST'])
def edit_property(prop_id):
    prop = next((p for p in properties if p['id'] == prop_id), None)
    if request.method == 'POST':
        prop['address'] = request.form['address']
        prop['roof_type'] = request.form['roof_type']
        prop['last_roof_date'] = request.form['last_roof_date']
        prop['owner'] = request.form['owner']
        prop['parcel_name'] = request.form['parcel_name']
        prop['llc_mailing_address'] = request.form['llc_mailing_address']

        emails = request.form.getlist('email')
        phones = request.form.getlist('phone')
        job_titles = request.form.getlist('job_title')
        prop['contact_info'] = [
            {"email": e, "phone": p, "job_title": j}
            for e, p, j in zip(emails, phones, job_titles)
            if e or p or j
        ]

        if 'notes' in request.form and request.form['notes'].strip():
            note_text = request.form['notes'].strip()
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            prop['notes'].append({"content": note_text, "timestamp": timestamp})

        if request.form.get('redirect_to_main') == '1':
            return redirect(url_for('index'))
        return redirect(url_for('edit_property', prop_id=prop_id, saved='true'))

    return render_template('edit_property.html', prop=prop)
if __name__ == '__main__':
    app.run(debug=False, use_reloader=False)
