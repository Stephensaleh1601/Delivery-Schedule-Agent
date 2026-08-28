const ordersTable = document.getElementById("orders-table");
const dateInput = document.getElementById("date-input");
const datesHint = document.getElementById("dates-hint");
const generateBtn = document.getElementById("generate-btn");
const planOutput = document.getElementById("plan-output");
const mapDiv = document.getElementById("map");
const mapNote = document.getElementById("map-note");
const notificationsDiv = document.getElementById("notifications");
const editSection = document.getElementById("edit-order-section");
const editForm = document.getElementById("edit-order-form");
const editError = document.getElementById("edit-order-error");
const cancelEditBtn = document.getElementById("cancel-edit-btn");

const NOTIFICATION_POLL_MS = 15000;

let currentJobs = [];

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function row(cells) {
  return `<tr>${cells.map((c) => `<td>${escapeHtml(c)}</td>`).join("")}</tr>`;
}

// -- Notifications ---------------------------------------------------------

async function loadNotifications() {
  const res = await fetch("/api/notifications");
  const notifications = await res.json();

  if (!notifications.length) {
    notificationsDiv.innerHTML = "";
    return;
  }

  notificationsDiv.innerHTML = notifications
    .map((n) => {
      const dateButtons = n.dates
        .map((d) => `<button type="button" class="chip" data-regenerate="${d}">Regenerate ${d}</button>`)
        .join(" ");
      return `
        <div class="notification" data-id="${n.id}">
          <div class="notification-text">${escapeHtml(n.message)}</div>
          <div class="notification-actions">
            ${dateButtons}
            <button type="button" class="chip" data-dismiss="${n.id}">Dismiss</button>
          </div>
        </div>`;
    })
    .join("");

  notificationsDiv.querySelectorAll("[data-regenerate]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const date = btn.dataset.regenerate;
      dateInput.value = date;
      await generatePlan(date);
      const notifId = btn.closest(".notification").dataset.id;
      await dismissNotification(notifId);
    });
  });
  notificationsDiv.querySelectorAll("[data-dismiss]").forEach((btn) => {
    btn.addEventListener("click", () => dismissNotification(btn.dataset.dismiss));
  });
}

async function dismissNotification(id) {
  await fetch(`/api/notifications/${id}/dismiss`, { method: "POST" });
  loadNotifications();
}

// -- Map ---------------------------------------------------------------------

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

// Requests the actual road-following path (depot -> stop 1 -> ... -> last stop) from the
// Directions API. Returns null on any failure (API not enabled, no route found, etc) so the
// caller can fall back to a straight-line connector instead of leaving the map blank.
function requestDirections(depotPoint, stops) {
  return new Promise((resolve) => {
    if (!stops.length) {
      resolve(null);
      return;
    }
    const directionsService = new google.maps.DirectionsService();
    const last = stops[stops.length - 1];
    const waypoints = stops.slice(0, -1).map((s) => ({ location: { lat: s.lat, lng: s.lng }, stopover: true }));
    directionsService.route(
      {
        origin: { lat: depotPoint.lat, lng: depotPoint.lng },
        destination: { lat: last.lat, lng: last.lng },
        waypoints,
        travelMode: google.maps.TravelMode.DRIVING,
      },
      (result, status) => {
        if (status !== "OK") console.error("DirectionsService failed:", status, result);
        resolve({ ok: status === "OK", result, status });
      }
    );
  });
}

async function renderRoute(stops) {
  if (!map) return;

  mapMarkers.forEach((m) => m.setMap(null));
  mapMarkers = [];
  if (routePolyline) routePolyline.setMap(null);
  mapNote.textContent = "";

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

  const bounds = new google.maps.LatLngBounds();
  path.forEach((p) => bounds.extend(p));

  const directions = await requestDirections(depot, stops);
  if (directions.ok) {
    routePolyline = new google.maps.Polyline({
      map,
      path: directions.result.routes[0].overview_path,
      strokeColor: "#2f6fed",
      strokeOpacity: 0.9,
      strokeWeight: 4,
    });
  } else {
    routePolyline = new google.maps.Polyline({
      map,
      path,
      strokeColor: "#2f6fed",
      strokeOpacity: 0.6,
      strokeWeight: 4,
      icons: [{ icon: { path: "M 0,-1 0,1", strokeOpacity: 1 }, offset: "0", repeat: "12px" }],
    });
    mapNote.textContent = `Showing a straight-line estimate between stops -- Directions API returned "${directions.status}" (see browser console for details).`;
  }

  map.fitBounds(bounds, 60);
}

// -- Orders + route plan -------------------------------------------------------

async function loadOrders() {
  const res = await fetch("/api/jobs");
  const jobs = await res.json();
  currentJobs = jobs;

  if (!jobs.length) {
    ordersTable.innerHTML = "<p class='muted'>No orders yet.</p>";
    return;
  }

  const rows = jobs
    .map(
      (j) => `
      <tr>
        <td>${escapeHtml(j.customer_name)}</td>
        <td>${escapeHtml(j.address)} (${escapeHtml(j.postal_code || "-")})</td>
        <td>${escapeHtml(j.job_type)}</td>
        <td>${escapeHtml(j.delivery_date)}</td>
        <td>${escapeHtml(j.availability.map((w) => `${w.start}-${w.end}`).join(", "))}</td>
        <td>${escapeHtml(j.status)}</td>
        <td>
          <button type="button" class="chip" data-edit="${j.id}">Edit</button>
          <button type="button" class="chip" data-delete="${j.id}">Delete</button>
        </td>
      </tr>`
    )
    .join("");

  ordersTable.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Customer</th><th>Address</th><th>Type</th><th>Preferred date</th>
          <th>Preferred window</th><th>Status</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  ordersTable.querySelectorAll("[data-edit]").forEach((btn) => {
    btn.addEventListener("click", () => openEditOrder(btn.dataset.edit));
  });
  ordersTable.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", () => deleteOrder(btn.dataset.delete));
  });
}

// -- Edit / delete orders -----------------------------------------------------

function openEditOrder(jobId) {
  const job = currentJobs.find((j) => j.id === jobId);
  if (!job) return;

  editError.hidden = true;
  editForm.job_id.value = job.id;
  editForm.customer_name.value = job.customer_name;
  editForm.phone.value = job.phone || "";
  editForm.address_raw.value = job.address;
  editForm.postal_code.value = job.postal_code || "";
  editForm.job_type.value = job.job_type;
  editForm.delivery_date.value = job.delivery_date;
  editForm.window_start.value = job.availability[0] ? job.availability[0].start : "09:00";
  editForm.window_end.value = job.availability[0] ? job.availability[0].end : "18:00";
  editForm.duration_minutes.value = job.duration_minutes;
  editForm.notes.value = job.notes || "";

  editSection.hidden = false;
  editSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

cancelEditBtn.addEventListener("click", () => {
  editSection.hidden = true;
});

editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  editError.hidden = true;
  const jobId = editForm.job_id.value;
  const data = Object.fromEntries(new FormData(editForm).entries());
  delete data.job_id;

  try {
    const res = await fetch(`/api/jobs/${jobId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      editError.textContent = body.detail || "Could not save changes -- please check the fields and try again.";
      editError.hidden = false;
      return;
    }
    editSection.hidden = true;
    loadOrders();
    loadDates();
    loadNotifications();
  } catch (err) {
    editError.textContent = "Could not reach the server -- please try again.";
    editError.hidden = false;
  }
});

async function deleteOrder(jobId) {
  const job = currentJobs.find((j) => j.id === jobId);
  const label = job ? `${job.customer_name}'s order` : "this order";
  if (!confirm(`Delete ${label}? This cannot be undone.`)) return;

  try {
    const res = await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
    if (!res.ok) {
      alert("Could not delete this order.");
      return;
    }
    if (!editSection.hidden && editForm.job_id.value === jobId) {
      editSection.hidden = true;
    }
    loadOrders();
    loadDates();
    loadNotifications();
  } catch (err) {
    alert("Could not reach the server -- please try again.");
  }
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

// Builds a Google Maps "share via link" URL -- no API key or OAuth needed, this is Google's
// public deep-link scheme (docs: developers.google.com/maps/documentation/urls). Opening it on
// a phone with the Google Maps app installed launches turn-by-turn navigation through the
// stops in this exact order (Google does NOT re-optimize order for a plain dir link), using
// whichever Google account the driver is already signed into on their own device.
function buildGoogleMapsTripUrl(depotPoint, stops) {
  if (!stops.length) return null;
  const last = stops[stops.length - 1];
  const waypoints = stops
    .slice(0, -1)
    .map((s) => `${s.lat},${s.lng}`)
    .join("|");
  const params = new URLSearchParams({
    api: "1",
    origin: `${depotPoint.lat},${depotPoint.lng}`,
    destination: `${last.lat},${last.lng}`,
    travelmode: "driving",
  });
  let url = `https://www.google.com/maps/dir/?${params.toString()}`;
  if (waypoints) url += `&waypoints=${encodeURIComponent(waypoints)}`;
  return url;
}

function renderTripShare(stops) {
  if (!depot) return; // /api/config hasn't resolved yet -- rare, just skip this render
  const url = buildGoogleMapsTripUrl(depot, stops);
  const container = document.createElement("div");
  container.className = "trip-share";
  container.innerHTML = `
    <button type="button" id="share-trip-btn">Share Trip Link</button>
    <div id="trip-link-box" hidden>
      <input type="text" id="trip-link-input" readonly value="${escapeHtml(url)}" />
      <button type="button" id="copy-trip-btn">Copy</button>
      <a href="${escapeHtml(url)}" target="_blank" rel="noopener">Open in Google Maps</a>
    </div>
    <p class="muted">Send this link to the driver's phone -- it opens turn-by-turn navigation
      through every stop in this exact order in their own Google Maps app.</p>`;
  planOutput.appendChild(container);

  const box = container.querySelector("#trip-link-box");
  container.querySelector("#share-trip-btn").addEventListener("click", () => {
    box.hidden = !box.hidden;
  });
  container.querySelector("#copy-trip-btn").addEventListener("click", async () => {
    const input = container.querySelector("#trip-link-input");
    try {
      await navigator.clipboard.writeText(url);
    } catch (err) {
      input.select();
      document.execCommand("copy");
    }
  });
}

async function generatePlan(date) {
  if (!date) {
    planOutput.innerHTML = "<p class='result result--error'>Pick a date first.</p>";
    return;
  }

  generateBtn.disabled = true;
  planOutput.innerHTML = "<p class='muted'>Generating route plan...</p>";

  try {
    const res = await fetch("/api/route-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ date }),
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

    renderTripShare(plan.stops);
    await renderRoute(plan.stops);
    loadOrders();
  } catch (err) {
    planOutput.innerHTML = "<p class='result result--error'>Could not reach the server -- please try again.</p>";
  } finally {
    generateBtn.disabled = false;
  }
}

generateBtn.addEventListener("click", () => generatePlan(dateInput.value));

loadOrders();
loadDates();
loadMap();
loadNotifications();
setInterval(loadNotifications, NOTIFICATION_POLL_MS);
