const ordersTable = document.getElementById("orders-table");
const dateInput = document.getElementById("date-input");
const datesHint = document.getElementById("dates-hint");
const generateBtn = document.getElementById("generate-btn");
const planOutput = document.getElementById("plan-output");
const mapDiv = document.getElementById("map");

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function row(cells) {
  return `<tr>${cells.map((c) => `<td>${escapeHtml(c)}</td>`).join("")}</tr>`;
}

// -- Map -----------------------------------------------------------------

let map = null;
let depot = null;
let mapMarkers = [];
let routePolyline = null;

window.gm_authFailure = function () {
  mapDiv.innerHTML =
    "<p class='result result--error'>Google Maps failed to authenticate -- check that the " +
    "Maps JavaScript API is enabled for GOOGLE_MAPS_API_KEY in the Cloud Console.</p>";
};

window.initMap = function () {
  mapDiv.innerHTML = "";
  map = new google.maps.Map(mapDiv, {
    center: { lat: depot.lat, lng: depot.lng },
    zoom: 12,
  });
  new google.maps.Marker({
    map,
    position: { lat: depot.lat, lng: depot.lng },
    title: depot.address,
    icon: {
      path: google.maps.SymbolPath.CIRCLE,
      scale: 9,
      fillColor: "#1e7d32",
      fillOpacity: 1,
      strokeColor: "#ffffff",
      strokeWeight: 2,
    },
  });
};

async function loadMap() {
  const res = await fetch("/api/config");
  const config = await res.json();
  depot = config.depot;

  if (!config.google_maps_api_key) {
    mapDiv.innerHTML =
      "<p class='muted'>Set GOOGLE_MAPS_API_KEY in .env to show the interactive map.</p>";
    return;
  }

  const script = document.createElement("script");
  script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(config.google_maps_api_key)}&callback=initMap`;
  script.async = true;
  document.head.appendChild(script);
}

function renderRoute(stops) {
  if (!map) return;

  mapMarkers.forEach((m) => m.setMap(null));
  mapMarkers = [];
  if (routePolyline) routePolyline.setMap(null);

  const path = [{ lat: depot.lat, lng: depot.lng }];
  stops.forEach((stop) => {
    const position = { lat: stop.lat, lng: stop.lng };
    path.push(position);
    const marker = new google.maps.Marker({
      map,
      position,
      label: String(stop.sequence_index),
      title: `${stop.sequence_index}. ${stop.customer_name} (${stop.arrival}-${stop.departure})`,
    });
    mapMarkers.push(marker);
  });

  routePolyline = new google.maps.Polyline({
    map,
    path,
    strokeColor: "#2f6fed",
    strokeOpacity: 0.9,
    strokeWeight: 4,
  });

  const bounds = new google.maps.LatLngBounds();
  path.forEach((p) => bounds.extend(p));
  map.fitBounds(bounds, 60);
}

async function loadOrders() {
  const res = await fetch("/api/jobs");
  const jobs = await res.json();

  if (!jobs.length) {
    ordersTable.innerHTML = "<p class='muted'>No orders yet.</p>";
    return;
  }

  const rows = jobs
    .map((j) =>
      row([
        j.customer_name,
        `${j.address} (${j.postal_code || "-"})`,
        j.job_type,
        j.delivery_date,
        j.availability.map((w) => `${w.start}-${w.end}`).join(", "),
        j.status,
      ])
    )
    .join("");

  ordersTable.innerHTML = `
    <table>
      <thead><tr><th>Customer</th><th>Address</th><th>Type</th><th>Preferred date</th><th>Preferred window</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function loadDates() {
  const res = await fetch("/api/dates");
  const dates = await res.json();

  if (!dates.length) {
    datesHint.textContent = "No orders yet.";
    return;
  }

  if (!dateInput.value) dateInput.value = dates[0];
  datesHint.innerHTML =
    "Dates with orders: " +
    dates.map((d) => `<button type="button" class="chip" data-date="${d}">${d}</button>`).join(" ");
  datesHint.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      dateInput.value = btn.dataset.date;
    });
  });
}

generateBtn.addEventListener("click", async () => {
  if (!dateInput.value) {
    planOutput.innerHTML = "<p class='result result--error'>Pick a date first.</p>";
    return;
  }

  generateBtn.disabled = true;
  planOutput.innerHTML = "<p class='muted'>Generating route plan...</p>";

  try {
    const res = await fetch("/api/route-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date: dateInput.value }),
    });
    const plan = await res.json();

    if (plan.error) {
      planOutput.innerHTML = `<p class="result result--error">${escapeHtml(plan.error)}</p>`;
      return;
    }
    if (!plan.stops.length) {
      planOutput.innerHTML = "<p class='muted'>No orders for this date.</p>";
      return;
    }

    const rows = plan.stops
      .map((s) =>
        row([
          s.sequence_index,
          s.customer_name,
          `${s.address} (${s.postal_code || "-"})`,
          s.job_type,
          `${s.arrival} - ${s.departure}`,
          `${s.distance_from_prev_km} km`,
          `${s.drive_minutes_from_prev} min`,
        ])
      )
      .join("");

    planOutput.innerHTML = `
      <div class="metrics">
        <div><strong>${plan.stops.length}</strong><span>stops</span></div>
        <div><strong>${plan.total_drive_minutes}</strong><span>min drive</span></div>
        <div><strong>${plan.total_distance_km}</strong><span>km total</span></div>
      </div>
      <table>
        <thead>
          <tr>
            <th>#</th><th>Customer</th><th>Address</th><th>Type</th>
            <th>Scheduled window</th><th>Distance from prev</th><th>Drive time from prev</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;

    renderRoute(plan.stops);
    loadOrders();
  } catch (err) {
    planOutput.innerHTML = "<p class='result result--error'>Could not reach the server -- please try again.</p>";
  } finally {
    generateBtn.disabled = false;
  }
});

loadOrders();
loadDates();
loadMap();
