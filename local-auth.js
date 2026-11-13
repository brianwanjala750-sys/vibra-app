let currentUser = null;
const status = document.getElementById('accountStatus');
const message = document.getElementById('accountMessage');
const form = document.getElementById('accountForm');
const signOutButton = document.getElementById('signOutButton');
const accountButton = document.querySelector('header .secondary');

async function authRequest(path, data) {
  const options = { method: data ? 'POST' : 'GET', credentials: 'same-origin' };
  if (data) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(data);
  }
  const response = await fetch('/api/auth/' + path, options);
  const result = await response.json();
  if (!response.ok || result.status !== 'success') {
    throw new Error(result.message || `Request failed (${response.status})`);
  }
  return result.data;
}

function setCurrentUser(user) {
  currentUser = user;
  status.textContent = user ? 'Signed in as ' + user.email : 'Signed out';
  message.textContent = user
    ? 'Your library is saved on this VIBRA server.'
    : 'Create an account to save your library on this server.';
  form.style.display = user ? 'none' : 'block';
  signOutButton.style.display = user ? 'inline-block' : 'none';
  accountButton.textContent = user ? 'Account' : 'Sign in';
  window.VibraApp?.onAuthChanged(user);
}

window.VibraAuth = {
  get currentUser() {
    return currentUser;
  },
  getToken: async () => null,
  async signIn(email, password) {
    setCurrentUser(await authRequest('login', { email, password }));
  },
  async createAccount(email, password) {
    setCurrentUser(await authRequest('register', { email, password }));
  },
  async signOut() {
    await authRequest('logout', {});
    setCurrentUser(null);
  }
};

authRequest('session')
  .then(setCurrentUser)
  .catch((error) => {
    status.textContent = 'Local sign-in is unavailable';
    message.textContent = error.message;
    window.VibraAuth = null;
  });
