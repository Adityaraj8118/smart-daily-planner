/* =========================================================
   Smart Daily Planner — script.js
   Handles tasks, dates, filters, statistics, validation,
   saving to the user's account and push notifications.
   No external libraries.
   ========================================================= */

// Tasks saved by the old, browser-only version; imported into the account once.
const LEGACY_STORAGE_KEY = "smartDailyPlanner.tasks";

// ----- App state -----
let tasks = [];            // [{ id, title, deadline: "YYYY-MM-DD" | null, completed, createdAt }]
let currentFilter = "all"; // "all" | "pending" | "completed"

// ----- DOM references -----
const form = document.getElementById("taskForm");
const titleInput = document.getElementById("taskTitle");
const deadlineInput = document.getElementById("taskDeadline");
const formMessage = document.getElementById("formMessage");
const taskList = document.getElementById("taskList");
const emptyState = document.getElementById("emptyState");
const filterButtons = document.querySelectorAll(".filter-btn");
const statTotal = document.getElementById("statTotal");
const statPending = document.getElementById("statPending");
const statCompleted = document.getElementById("statCompleted");
const currentDateEl = document.getElementById("currentDate");
const appEl = document.getElementById("app");
const userNameEl = document.getElementById("userName");
const userHandleEl = document.getElementById("userHandle");
const logoutBtn = document.getElementById("logoutBtn");
const notifyBtn = document.getElementById("notifyBtn");
const notifyMessage = document.getElementById("notifyMessage");

/* ---------------------------------------------------------
   Date helpers — everything uses LOCAL calendar dates
   stored as "YYYY-MM-DD" strings, so no timezone drift.
   --------------------------------------------------------- */

// Returns today's local date as "YYYY-MM-DD"
function todayKey() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

// Converts "YYYY-MM-DD" to a local Date object (midnight local time)
function parseDateKey(key) {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function isValidDateKey(key) {
  if (typeof key !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(key)) return false;
  const date = parseDateKey(key);
  const [y, m, d] = key.split("-").map(Number);
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d;
}

// Whole days from today to the given date key (negative = past)
function daysFromToday(key) {
  const msPerDay = 24 * 60 * 60 * 1000;
  return Math.round((parseDateKey(key) - parseDateKey(todayKey())) / msPerDay);
}

// Readable deadline text, e.g. "Due today", "Due tomorrow", "Due Mon, 12 Oct 2026"
function formatDeadline(key) {
  const diff = daysFromToday(key);
  const pretty = parseDateKey(key).toLocaleDateString(undefined, {
    weekday: "short", day: "numeric", month: "short", year: "numeric",
  });
  if (diff === 0) return "Due today";
  if (diff === 1) return "Due tomorrow";
  if (diff < 0) return `Was due ${pretty}`;
  return `Due ${pretty}`;
}

// Overdue = pending AND deadline earlier than today
function isOverdue(task) {
  return !task.completed && task.deadline !== null && task.deadline < todayKey();
}

/* ---------------------------------------------------------
   Server API — tasks live in the logged-in user's account
   --------------------------------------------------------- */

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.replace("login.html");
    throw new Error("Not logged in.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

// Moves tasks from the old browser-only version into the account (runs once)
async function importLegacyTasks() {
  let raw;
  try {
    raw = localStorage.getItem(LEGACY_STORAGE_KEY);
  } catch (err) {
    return;
  }
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.length > 0) {
      await api("/api/tasks/import", { method: "POST", body: { tasks: parsed } });
    }
    localStorage.removeItem(LEGACY_STORAGE_KEY);
  } catch (err) {
    console.warn("Couldn't import tasks saved in this browser.", err);
  }
}

/* ---------------------------------------------------------
   Task operations
   --------------------------------------------------------- */

async function addTask(title, deadline) {
  const { task } = await api("/api/tasks", { method: "POST", body: { title, deadline: deadline || null } });
  tasks.push(task);
  render();
}

async function toggleTask(id) {
  const task = tasks.find((t) => t.id === id);
  if (!task) return;
  try {
    const { task: updated } = await api(`/api/tasks/${encodeURIComponent(id)}`, {
      method: "PATCH", body: { completed: !task.completed },
    });
    Object.assign(task, updated);
  } catch (err) {
    showMessage(err.message, "error");
  }
  render();
}

async function deleteTask(id) {
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    tasks = tasks.filter((t) => t.id !== id);
  } catch (err) {
    showMessage(err.message, "error");
  }
  render();
}

/* ---------------------------------------------------------
   Form validation & feedback
   --------------------------------------------------------- */

function showMessage(text, type) {
  formMessage.textContent = text;
  formMessage.className = `form-message ${type}`;
}

function clearMessage() {
  formMessage.textContent = "";
  formMessage.className = "form-message";
  titleInput.classList.remove("invalid");
  titleInput.removeAttribute("aria-invalid");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = titleInput.value.trim();
  const deadline = deadlineInput.value; // "" or "YYYY-MM-DD"

  if (title === "") {
    showMessage("Please enter a task title — it can't be empty.", "error");
    titleInput.classList.add("invalid");
    titleInput.setAttribute("aria-invalid", "true");
    titleInput.focus();
    return;
  }
  if (deadline && !isValidDateKey(deadline)) {
    showMessage("Please choose a valid deadline date.", "error");
    deadlineInput.focus();
    return;
  }

  try {
    await addTask(title, deadline);
  } catch (err) {
    showMessage(err.message, "error");
    return;
  }
  form.reset();
  clearMessage();
  showMessage("Task added.", "success");
  titleInput.focus();
});

// Clear the error as soon as the user starts typing again
titleInput.addEventListener("input", () => {
  if (titleInput.classList.contains("invalid")) clearMessage();
});

/* ---------------------------------------------------------
   Filters
   --------------------------------------------------------- */

filterButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    currentFilter = btn.dataset.filter;
    filterButtons.forEach((b) => {
      const active = b === btn;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    });
    render();
  });
});

function getVisibleTasks() {
  let list = tasks;
  if (currentFilter === "pending") list = tasks.filter((t) => !t.completed);
  if (currentFilter === "completed") list = tasks.filter((t) => t.completed);

  // Sort: pending first, then by deadline (no deadline last), then newest first
  return [...list].sort((a, b) => {
    if (a.completed !== b.completed) return a.completed ? 1 : -1;
    if (a.deadline && b.deadline && a.deadline !== b.deadline) return a.deadline < b.deadline ? -1 : 1;
    if (a.deadline && !b.deadline) return -1;
    if (!a.deadline && b.deadline) return 1;
    return b.createdAt - a.createdAt;
  });
}

/* ---------------------------------------------------------
   Rendering — uses textContent only (no innerHTML with
   user data), so task titles can't inject HTML/scripts.
   --------------------------------------------------------- */

function createTaskElement(task) {
  const li = document.createElement("li");
  li.className = "task-item";
  li.dataset.id = task.id;
  if (task.completed) li.classList.add("completed");
  const overdue = isOverdue(task);
  if (overdue) li.classList.add("overdue");

  // Checkbox to complete / reopen
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "task-check";
  checkbox.checked = task.completed;
  checkbox.setAttribute(
    "aria-label",
    task.completed ? `Mark "${task.title}" as pending` : `Mark "${task.title}" as completed`
  );
  checkbox.addEventListener("change", () => toggleTask(task.id));

  // Title + meta
  const body = document.createElement("div");
  body.className = "task-body";

  const title = document.createElement("p");
  title.className = "task-title";
  title.textContent = task.title;
  body.appendChild(title);

  const meta = document.createElement("p");
  meta.className = "task-meta";
  if (overdue) {
    const badge = document.createElement("span");
    badge.className = "badge badge-overdue";
    badge.textContent = "Overdue";
    meta.appendChild(badge);
  }
  if (task.completed) {
    const badge = document.createElement("span");
    badge.className = "badge badge-done";
    badge.textContent = "Completed";
    meta.appendChild(badge);
  }
  if (task.deadline) {
    const due = document.createElement("span");
    due.className = "task-deadline";
    due.textContent = formatDeadline(task.deadline);
    meta.appendChild(due);
  } else if (!task.completed) {
    const noDue = document.createElement("span");
    noDue.textContent = "No deadline";
    meta.appendChild(noDue);
  }
  body.appendChild(meta);

  // Delete button
  const del = document.createElement("button");
  del.type = "button";
  del.className = "delete-btn";
  del.textContent = "Delete";
  del.setAttribute("aria-label", `Delete "${task.title}"`);
  del.addEventListener("click", () => deleteTask(task.id));

  li.append(checkbox, body, del);
  return li;
}

function renderStats() {
  const completed = tasks.filter((t) => t.completed).length;
  statTotal.textContent = tasks.length;
  statCompleted.textContent = completed;
  statPending.textContent = tasks.length - completed;
}

function renderEmptyState(visibleCount) {
  if (visibleCount > 0) {
    emptyState.hidden = true;
    return;
  }
  if (tasks.length === 0) {
    emptyState.textContent = "No tasks yet. Add your first task above to get started.";
  } else if (currentFilter === "pending") {
    emptyState.textContent = "No pending tasks — you're all caught up!";
  } else if (currentFilter === "completed") {
    emptyState.textContent = "No completed tasks yet. Tick a task when you finish it.";
  }
  emptyState.hidden = false;
}

function render() {
  const visible = getVisibleTasks();
  taskList.replaceChildren(...visible.map(createTaskElement));
  renderStats();
  renderEmptyState(visible.length);
}

function renderCurrentDate() {
  currentDateEl.textContent = new Date().toLocaleDateString(undefined, {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
}

/* ---------------------------------------------------------
   Account & push notifications
   --------------------------------------------------------- */

logoutBtn.addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: {} }).catch(() => {});
  window.location.replace("login.html");
});

function showNotifyMessage(text, type) {
  notifyMessage.textContent = text;
  notifyMessage.className = `form-message ${type || ""}`;
}

function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

// VAPID public key (base64url) -> Uint8Array for pushManager.subscribe
function urlBase64ToUint8Array(base64) {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

async function sendSubscription(subscription) {
  await api("/api/push/subscribe", {
    method: "POST",
    body: {
      subscription: subscription.toJSON(),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    },
  });
}

async function setupNotifications() {
  if (!pushSupported()) {
    notifyBtn.hidden = true;
    return;
  }
  const registration = await navigator.serviceWorker.register("sw.js");
  const existing = await registration.pushManager.getSubscription();
  if (existing && Notification.permission === "granted") {
    // Keep the server in sync (e.g. a different user logged in on this device)
    await sendSubscription(existing).catch(() => {});
    notifyBtn.textContent = "Send test notification";
  }
}

notifyBtn.addEventListener("click", async () => {
  notifyBtn.disabled = true;
  try {
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        showNotifyMessage("Notifications are blocked. Allow them in your browser settings to get reminders.", "error");
        return;
      }
      const { publicKey } = await api("/api/push/public-key");
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      });
    }
    await sendSubscription(subscription);
    await api("/api/push/test", { method: "POST", body: {} });
    notifyBtn.textContent = "Send test notification";
    showNotifyMessage("Notifications are on. You'll get a daily reminder for tasks due today or overdue.", "success");
  } catch (err) {
    showNotifyMessage(`Couldn't enable notifications: ${err.message}`, "error");
  } finally {
    notifyBtn.disabled = false;
  }
});

/* ---------------------------------------------------------
   Start the app
   --------------------------------------------------------- */

async function init() {
  let user;
  try {
    ({ user } = await api("/api/me"));
  } catch (err) {
    return; // api() already redirected to the login page
  }
  userNameEl.textContent = user.name;
  userHandleEl.textContent = `@${user.username}`;

  await importLegacyTasks();
  ({ tasks } = await api("/api/tasks"));
  appEl.hidden = false;
  renderCurrentDate();
  render();
  setupNotifications().catch((err) => console.warn("Push setup failed.", err));

  // If the page stays open past midnight, refresh date + overdue labels
  let lastDay = todayKey();
  setInterval(() => {
    if (todayKey() !== lastDay) {
      lastDay = todayKey();
      renderCurrentDate();
      render();
    }
  }, 60 * 1000);
}

init();
