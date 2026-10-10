/* =========================================================
   Smart Daily Planner — auth.js
   Login, sign-up and email OTP verification.
   ========================================================= */

const tabLogin = document.getElementById("tabLogin");
const tabSignup = document.getElementById("tabSignup");
const loginForm = document.getElementById("loginForm");
const signupForm = document.getElementById("signupForm");
const otpForm = document.getElementById("otpForm");
const otpEmailEl = document.getElementById("otpEmail");
const otpCode = document.getElementById("otpCode");
const resendBtn = document.getElementById("resendBtn");
const changeEmailBtn = document.getElementById("changeEmailBtn");
const authMessage = document.getElementById("authMessage");

let pendingEmail = "";
let resendTimer = null;
let cooldownLeft = 0;

const RESENT_MESSAGE = "A new code has been sent to your email.";
const DEV_MODE_MESSAGE = "Dev mode: email isn't configured, so the code is printed in the server console.";

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

function showMessage(text, type) {
  authMessage.textContent = text;
  authMessage.className = `form-message ${type || ""}`;
}

function setBusy(form, busy) {
  form.querySelectorAll("button, input").forEach((el) => { el.disabled = busy; });
}

function showTab(which) {
  const isLogin = which === "login";
  tabLogin.classList.toggle("active", isLogin);
  tabSignup.classList.toggle("active", !isLogin);
  tabLogin.setAttribute("aria-selected", String(isLogin));
  tabSignup.setAttribute("aria-selected", String(!isLogin));
  loginForm.hidden = !isLogin;
  signupForm.hidden = isLogin;
  otpForm.hidden = true;
  showMessage("");
}

function showOtpStep(email, devMode) {
  pendingEmail = email;
  otpEmailEl.textContent = email;
  signupForm.hidden = true;
  otpForm.hidden = false;
  otpCode.value = "";
  otpCode.focus();
  showMessage(devMode ? DEV_MODE_MESSAGE : "", "success");
  startResendCooldown(60);
}

function startResendCooldown(seconds) {
  clearInterval(resendTimer);
  cooldownLeft = seconds;
  resendBtn.disabled = true;
  resendBtn.textContent = `Resend code (${cooldownLeft}s)`;
  resendTimer = setInterval(() => {
    cooldownLeft -= 1;
    if (cooldownLeft <= 0) {
      clearInterval(resendTimer);
      resendBtn.disabled = false;
      resendBtn.textContent = "Resend code";
      // The "new code sent" notice only lasts until the user can resend again
      if (authMessage.textContent === RESENT_MESSAGE) showMessage("");
    } else {
      resendBtn.textContent = `Resend code (${cooldownLeft}s)`;
    }
  }, 1000);
}

function goToApp() {
  window.location.replace("/");
}

tabLogin.addEventListener("click", () => showTab("login"));
tabSignup.addEventListener("click", () => showTab("signup"));

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const identifier = document.getElementById("loginId").value.trim();
  const password = document.getElementById("loginPassword").value;
  if (!identifier || !password) return showMessage("Please enter your email/username and password.", "error");

  setBusy(loginForm, true);
  try {
    await api("/api/login", { identifier, password });
    goToApp();
  } catch (err) {
    showMessage(err.message, "error");
    setBusy(loginForm, false);
  }
});

signupForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = document.getElementById("signupName").value.trim();
  const email = document.getElementById("signupEmail").value.trim().toLowerCase();
  const password = document.getElementById("signupPassword").value;

  if (!name) return showMessage("Please enter your name.", "error");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return showMessage("Please enter a valid email address.", "error");
  if (password.length < 8) return showMessage("Password must be at least 8 characters.", "error");

  setBusy(signupForm, true);
  showMessage("Sending code…");
  try {
    const data = await api("/api/signup", { name, email, password });
    showOtpStep(email, data.devMode);
  } catch (err) {
    showMessage(err.message, "error");
  } finally {
    setBusy(signupForm, false);
  }
});

otpForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const otp = otpCode.value.trim();
  if (!/^\d{6}$/.test(otp)) return showMessage("Enter the 6-digit code from your email.", "error");

  setBusy(otpForm, true);
  try {
    const data = await api("/api/verify-otp", { email: pendingEmail, otp });
    showMessage(`Account created! Your username is ${data.user.username}. Opening your planner…`, "success");
    setTimeout(goToApp, 1500);
  } catch (err) {
    showMessage(err.message, "error");
    setBusy(otpForm, false);
    resendBtn.disabled = cooldownLeft > 0;
  }
});

resendBtn.addEventListener("click", async () => {
  resendBtn.disabled = true;
  try {
    const data = await api("/api/resend-otp", { email: pendingEmail });
    showMessage(data.devMode ? DEV_MODE_MESSAGE : RESENT_MESSAGE, "success");
    startResendCooldown(60);
  } catch (err) {
    showMessage(err.message, "error");
    resendBtn.disabled = false;
  }
});

changeEmailBtn.addEventListener("click", () => {
  clearInterval(resendTimer);
  cooldownLeft = 0;
  showTab("signup");
});

// Already logged in? Go straight to the planner.
fetch("/api/me").then((res) => { if (res.ok) goToApp(); }).catch(() => {});
