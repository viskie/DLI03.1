(function () {
  const storageKey = 'dliSandboxProfile';
  const fallback = { firstName: 'Alex', lastName: 'Morgan', email: 'alex@protocol.xyz', organization: 'Personal evaluation', role: 'Protocol engineer' };

  function loadProfile() {
    try { return Object.assign({}, fallback, JSON.parse(localStorage.getItem(storageKey) || '{}')); }
    catch (_) { return Object.assign({}, fallback); }
  }

  function saveProfile(profile) {
    try { localStorage.setItem(storageKey, JSON.stringify(profile)); } catch (_) {}
  }

  document.querySelectorAll('[data-demo-auth]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!form.checkValidity()) { form.reportValidity(); return; }
      const data = new FormData(form);
      if (form.dataset.demoAuth === 'register') {
        saveProfile({ firstName: data.get('firstName'), lastName: data.get('lastName'), email: data.get('email'), organization: data.get('organization') || 'Personal evaluation', role: 'Protocol engineer' });
      } else {
        const profile = loadProfile();
        profile.email = data.get('email');
        saveProfile(profile);
      }
      window.location.href = 'sandbox-dashboard.html';
    });
  });

  document.querySelectorAll('[data-demo-sso]').forEach(function (button) {
    button.addEventListener('click', function () {
      const provider = button.dataset.demoSso;
      saveProfile({
        firstName: provider,
        lastName: 'Evaluator',
        email: 'sandbox@' + provider.toLowerCase() + '.example',
        organization: provider + ' SSO demo',
        role: 'Protocol engineer'
      });
      window.location.href = 'sandbox-dashboard.html';
    });
  });

  const views = Array.from(document.querySelectorAll('[data-view]'));
  if (!views.length) return;
  const links = Array.from(document.querySelectorAll('[data-view-link]'));
  const breadcrumb = document.getElementById('view-breadcrumb');
  const sidebar = document.getElementById('dashboard-sidebar');
  const menuButton = document.getElementById('menu-button');

  function showView(name) {
    if (!views.some(function (view) { return view.dataset.view === name; })) name = 'overview';
    views.forEach(function (view) { view.classList.toggle('active', view.dataset.view === name); });
    links.forEach(function (link) { link.classList.toggle('active', link.dataset.viewLink === name); });
    if (breadcrumb) breadcrumb.textContent = name.toUpperCase();
    if (sidebar) sidebar.classList.remove('open');
    if (menuButton) menuButton.setAttribute('aria-expanded', 'false');
  }

  links.forEach(function (link) { link.addEventListener('click', function () { showView(link.dataset.viewLink); }); });
  document.querySelectorAll('[data-go-profile]').forEach(function (button) { button.addEventListener('click', function () { window.location.hash = 'profile'; showView('profile'); }); });
  window.addEventListener('hashchange', function () { showView(window.location.hash.slice(1)); });
  if (menuButton) menuButton.addEventListener('click', function () { const open = sidebar.classList.toggle('open'); menuButton.setAttribute('aria-expanded', String(open)); });

  const profile = loadProfile();
  const profileForm = document.getElementById('profile-form');
  function renderProfile(data) {
    const name = ((data.firstName || '') + ' ' + (data.lastName || '')).trim() || 'Sandbox user';
    const initials = ((data.firstName || 'S')[0] + (data.lastName || 'U')[0]).toUpperCase();
    document.querySelectorAll('.avatar').forEach(function (el) { el.textContent = initials; });
    const largeAvatar = document.getElementById('profile-avatar'); if (largeAvatar) largeAvatar.textContent = initials;
    const displayName = document.getElementById('profile-display-name'); if (displayName) displayName.textContent = name;
    const workspace = document.getElementById('workspace-name'); if (workspace) workspace.textContent = data.organization || 'Personal evaluation';
    const accountOrg = document.getElementById('account-organization'); if (accountOrg) accountOrg.textContent = data.organization || 'Personal evaluation';
  }
  if (profileForm) {
    Object.keys(profile).forEach(function (key) { if (profileForm.elements[key]) profileForm.elements[key].value = profile[key]; });
    profileForm.addEventListener('submit', function (event) {
      event.preventDefault();
      if (!profileForm.checkValidity()) { profileForm.reportValidity(); return; }
      const updated = Object.fromEntries(new FormData(profileForm).entries());
      saveProfile(updated); renderProfile(updated);
      const feedback = document.getElementById('profile-feedback'); feedback.textContent = 'Profile saved in this browser.';
      setTimeout(function () { feedback.textContent = ''; }, 3000);
    });
  }
  renderProfile(profile);
  const updated = document.getElementById('last-updated');
  if (updated) updated.textContent = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date());
  const copyButton = document.getElementById('copy-key');
  if (copyButton) copyButton.addEventListener('click', async function () {
    const feedback = document.getElementById('copy-feedback');
    try { await navigator.clipboard.writeText('dli_sandbox_demo_8f2a'); feedback.textContent = 'Sample key copied.'; }
    catch (_) { feedback.textContent = 'Copy is unavailable in this browser.'; }
  });
  showView(window.location.hash.slice(1) || 'overview');
}());
