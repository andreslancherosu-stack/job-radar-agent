// Shared helpers for the Job Radar local test application fixture.
// This is a fictional, purely local ATS simulation used to test browser
// automation. It has no backend; data is kept in localStorage only.

const STORAGE_KEY = 'jobradar_testapp_data';

function loadData() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch (e) {
    return {};
  }
}

function saveData(patch) {
  const data = loadData();
  Object.assign(data, patch);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  return data;
}

function showError(fieldId, message) {
  const el = document.getElementById(fieldId + '_error');
  if (el) {
    el.textContent = message;
    el.classList.add('visible');
  }
}

function clearError(fieldId) {
  const el = document.getElementById(fieldId + '_error');
  if (el) {
    el.textContent = '';
    el.classList.remove('visible');
  }
}

function clearAllErrors(root) {
  (root || document).querySelectorAll('.error').forEach((el) => {
    el.textContent = '';
    el.classList.remove('visible');
  });
}

function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function restoreField(id) {
  const data = loadData();
  const el = document.getElementById(id);
  if (!el || data[id] === undefined) return;
  if (el.type === 'checkbox') {
    el.checked = !!data[id];
  } else if (el.type === 'radio') {
    // handled by caller for radio groups
  } else {
    el.value = data[id];
  }
}

function restoreRadioGroup(name) {
  const data = loadData();
  const value = data[name];
  if (value === undefined) return;
  document.querySelectorAll(`input[name="${name}"]`).forEach((el) => {
    el.checked = el.value === value;
  });
}
