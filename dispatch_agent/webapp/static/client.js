const form = document.getElementById("job-form");
const result = document.getElementById("result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  result.hidden = true;

  const data = Object.fromEntries(new FormData(form).entries());
  const submitButton = form.querySelector("button[type=submit]");
  submitButton.disabled = true;

  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const body = await res.json();

    result.hidden = false;
    if (res.ok) {
      result.className = "result result--ok";
      result.textContent =
        "Thanks! Your booking request has been received. We'll confirm your exact arrival time closer to the date.";
      form.reset();
    } else {
      result.className = "result result--error";
      result.textContent = body.detail || "Something went wrong -- please check your details and try again.";
    }
  } catch (err) {
    result.hidden = false;
    result.className = "result result--error";
    result.textContent = "Could not reach the server -- please try again.";
  } finally {
    submitButton.disabled = false;
  }
});
