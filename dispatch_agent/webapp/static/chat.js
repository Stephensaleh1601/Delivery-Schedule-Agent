const chat = document.getElementById("chat");
const composer = document.getElementById("composer");
const composerInput = document.getElementById("composer-input");

let state = { flow: null, step: null };

const JOB_TYPE_OPTIONS = [
  { value: "sofa", label: "Sofa" },
  { value: "bed", label: "Bed" },
  { value: "cabinet", label: "Cabinet" },
  { value: "other", label: "Other" },
];

function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

function addBubble(text, who) {
  const bubble = document.createElement("div");
  bubble.className = `wa-bubble wa-bubble--${who}`;
  bubble.textContent = text;
  chat.appendChild(bubble);
  scrollToBottom();
}

function clearInteractive() {
  chat.querySelectorAll(".wa-buttons, .wa-card-form").forEach((el) => el.remove());
}

function addButtons(buttons) {
  if (!buttons || !buttons.length) return;
  const wrap = document.createElement("div");
  wrap.className = "wa-buttons";
  buttons.forEach((b) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "wa-button";
    btn.textContent = b.label;
    btn.addEventListener("click", () => {
      clearInteractive();
      addBubble(b.label, "user");
      sendEvent({ type: "button", value: b.value });
    });
    wrap.appendChild(btn);
  });
  chat.appendChild(wrap);
  scrollToBottom();
}

function addBookingForm() {
  const form = document.createElement("form");
  form.className = "wa-card-form";
  form.innerHTML = `
    <div class="wa-card">
      <div class="wa-card-title">Delivery details</div>
      <label>Full name<input type="text" name="customer_name" required /></label>
      <label>Phone (optional)<input type="tel" name="phone" /></label>
      <label>Address<input type="text" name="address_raw" required /></label>
      <label>Postal code<input type="text" name="postal_code" inputmode="numeric" pattern="\\d{6}" maxlength="6" required /></label>
      <label>Furniture type
        <select name="job_type">
          ${JOB_TYPE_OPTIONS.map((o) => `<option value="${o.value}">${o.label}</option>`).join("")}
        </select>
      </label>
      <label>Preferred date<input type="date" name="delivery_date" required /></label>
      <div class="wa-card-row">
        <label>Earliest<input type="time" name="window_start" value="09:00" required /></label>
        <label>Latest<input type="time" name="window_end" value="18:00" required /></label>
      </div>
      <label>Notes (optional)<textarea name="notes" rows="2"></textarea></label>
      <button type="submit" class="wa-card-submit">Send details</button>
    </div>
  `;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    clearInteractive();
    addBubble("Sent my delivery details", "user");
    sendEvent({ type: "form_submit", form: "booking", data });
  });
  chat.appendChild(form);
  scrollToBottom();
}

function addRescheduleForm() {
  const form = document.createElement("form");
  form.className = "wa-card-form";
  form.innerHTML = `
    <div class="wa-card">
      <div class="wa-card-title">New delivery slot</div>
      <label>New date<input type="date" name="new_date" required /></label>
      <div class="wa-card-row">
        <label>Earliest<input type="time" name="window_start" value="09:00" required /></label>
        <label>Latest<input type="time" name="window_end" value="18:00" required /></label>
      </div>
      <button type="submit" class="wa-card-submit">Request this slot</button>
    </div>
  `;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    clearInteractive();
    addBubble(`Requested: ${data.new_date}, ${data.window_start}-${data.window_end}`, "user");
    sendEvent({ type: "form_submit", form: "reschedule_slot", data });
  });
  chat.appendChild(form);
  scrollToBottom();
}

async function sendEvent(event) {
  event.state = state;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(event),
    });
    const reply = await res.json();
    applyReply(reply);
  } catch (err) {
    addBubble("Sorry, I couldn't reach the server -- please try again.", "bot");
  }
}

function applyReply(reply) {
  state = reply.state || { flow: null, step: null };
  (reply.messages || []).forEach((m) => addBubble(m.text, "bot"));
  if (reply.form === "booking") addBookingForm();
  if (reply.form === "reschedule_slot") addRescheduleForm();
  addButtons(reply.buttons);
}

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = composerInput.value.trim();
  if (!text) return;
  clearInteractive();
  addBubble(text, "user");
  composerInput.value = "";
  sendEvent({ type: "text", value: text });
});

sendEvent({ type: "start" });
