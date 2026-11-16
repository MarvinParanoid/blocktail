const filters = document.getElementById('filters');

// The top bar and the filter bar are both sticky, and the table head sticks
// below them. Their heights depend on font metrics and on whether the bars
// wrap, so they are measured rather than assumed — a stale guess leaves a gap
// that feed rows scroll through, and buries the table head.
(function trackChromeHeight() {
  const topbar = document.querySelector('.topbar');
  const root = document.documentElement;
  if (!topbar || !filters) return;
  const measure = () => {
    const top = topbar.getBoundingClientRect().height;
    root.style.setProperty('--topbar-h', top + 'px');
    root.style.setProperty('--chrome-h', (top + filters.getBoundingClientRect().height) + 'px');
  };
  measure();
  if (window.ResizeObserver) {
    const observer = new ResizeObserver(measure);
    observer.observe(topbar);
    observer.observe(filters);
  }
  window.addEventListener('resize', measure);
})();

// Keep the address bar in step with the filters without spamming history.
function syncUrl() {
  const params = new URLSearchParams(new FormData(filters));
  for (const [key, value] of [...params]) if (!value || value === 'all') params.delete(key);
  const query = params.toString();
  history.replaceState(null, '', query ? '?' + query : location.pathname);
}

// Mark the controls that are currently narrowing the feed, so a filter set
// from the summary or the sidebar is visible where it is cleared.
function syncActiveFilters() {
  for (const field of filters.querySelectorAll('.field')) {
    const control = field.querySelector('select, input[type="search"]');
    field.classList.toggle('is-active', !!(control && control.value));
  }
  const direction = filters.querySelector('input[name="direction"]:checked');
  const segment = filters.querySelector('.field--dir');
  if (segment) segment.classList.toggle('is-active', !!direction && direction.value !== 'all');

  // A search in force is never collapsed behind the magnifier: a filter you
  // cannot see is a filter you will not think to clear.
  filters.classList.toggle('has-search', !!document.getElementById('search').value);
}

// Which columns a row needs depends on the scope, and the scope changes
// without the page being rendered again: htmx swaps the feed and leaves the
// container's server-rendered class behind. A stale `one-wallet` hides a
// cell that spans two columns on transfers between your own wallets, which
// takes the rest of that row's layout with it.
function syncScopeClass() {
  const layout = document.querySelector('.layout');
  if (!layout) return;
  const wallet = document.getElementById('wallet-value');
  layout.classList.toggle('one-wallet', !!(wallet && wallet.value));
}

// --- how the page updates --------------------------------------------
//
// One path, in one order, for every change of what the feed shows:
//
//   1. state     — the form's fields (#wallet-value, #scope-value, asset,
//                  direction, search) are set, synchronously;
//   2. the feed  — the form asks for /activity (queued, never dropped; an
//                  answer to an older question is not swapped in);
//   3. after it  — onFiltered() puts the filters in the address bar, then
//                  fires `filtersApplied` (the hidden-transfers note) and,
//                  unless this was the timed refresh, `refreshWallets`
//                  (sidebar, summary, status bar).
//
// Who starts it:
//   * the filter controls themselves (change; search on input, debounced);
//   * pick() — a link with data-pick: a wallet, a scope, an asset, an
//     address. Its href stays for a new tab and for no script;
//   * the server's `wallet-changed` {action, id} — one event, whose
//     handler closes the dialog, drops a removed wallet from the scope,
//     then applies. Nothing depends on the order of keys in a header;
//   * `refreshFeed` — the 15-second refresh, and nothing else.
//
// `history-loaded` is fired when older history arrives in the feed.
function applyFilters(reason) {
  document.body.dataset.paged = '';
  syncActiveFilters();
  syncScopeClass();
  htmx.trigger(filters, 'filters-changed', { reason: reason || 'filter' });
}

syncActiveFilters();
syncScopeClass();

// A narrow screen is not a small desktop: it has no sidebar, its feed rows
// are a log rather than a table, and rows open their technical detail.
const NARROW = window.matchMedia('(max-width: 860px)');
function isNarrow() { return NARROW.matches; }

// --- modals ----------------------------------------------------------
//
// On a phone the scope sheet and the transaction sheet are modal: focus goes
// in, Tab cannot leave, everything behind is inert (unreachable by Tab and
// hidden from assistive technology), Escape closes, and focus goes back to
// what opened it. On a wide screen the sidebar and the inspector are panels
// beside the feed and none of this applies. The wallet/holdings dialog is a
// native <dialog> opened with showModal(), which does all of it itself.
const html = document.documentElement;
const scrim = document.querySelector('.scrim');
const inspector = document.getElementById('inspector');
const sidebar = document.getElementById('sidebar');
const modal = { el: null, inerted: [] };

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select,'
  + ' textarea, [tabindex]:not([tabindex="-1"])';
function focusables(root) {
  return [...root.querySelectorAll(FOCUSABLE)]
    .filter((e) => e.offsetParent !== null && !e.closest('[inert]'));
}

function makeModal(el, label) {
  releaseModal();
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  if (label) el.setAttribute('aria-label', label);
  // Everything that is not on the way from the modal up to <body> goes
  // inert. The scrim stays live (a tap on it closes), and so does the
  // native dialog, which manages itself.
  for (let node = el; node && node !== document.body; node = node.parentElement) {
    for (const sibling of node.parentElement.children) {
      if (sibling === node || sibling.inert || sibling === scrim
          || sibling.tagName === 'SCRIPT' || sibling.tagName === 'DIALOG') continue;
      sibling.inert = true;
      modal.inerted.push(sibling);
    }
  }
  modal.el = el;
}

function releaseModal() {
  modal.inerted.forEach((e) => { e.inert = false; });
  modal.inerted = [];
  if (modal.el) {
    modal.el.removeAttribute('role');
    modal.el.removeAttribute('aria-modal');
  }
  modal.el = null;
}

// Tab and Shift+Tab wrap inside an open modal.
// The native dialog makes the page behind it inert, but Chromium still lets
// Tab leave it for the browser's own UI, so it is trapped here as well.
document.addEventListener('keydown', (evt) => {
  const dialog = document.getElementById('wallet-dialog');
  const box = dialog && dialog.open ? dialog : modal.el;
  if (evt.key !== 'Tab' || !box) return;
  const items = focusables(box);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  const inside = box.contains(document.activeElement);
  if (evt.shiftKey && (document.activeElement === first || !inside)) {
    evt.preventDefault(); last.focus();
  } else if (!evt.shiftKey && (document.activeElement === last || !inside)) {
    evt.preventDefault(); first.focus();
  }
});

// --- scope sheet (a phone's sidebar) -----------------------------------

function openScopeSheet() {
  html.classList.add('scope-open');
  scrim.hidden = false;
  applyLayout();
  const target = sidebar.querySelector('.is-selected .wallet__hit, .wallets__group.is-selected')
    || focusables(sidebar)[0];
  if (target) target.focus();
}
function closeScopeSheet() {
  const wasOpen = html.classList.contains('scope-open');
  html.classList.remove('scope-open');
  if (!html.classList.contains('inspector-open')) scrim.hidden = true;
  applyLayout();
  // The opener lives in the status bar, which is re-rendered every poll:
  // find it again rather than keeping the element.
  if (wasOpen && isNarrow()) {
    const opener = document.querySelector('.topline__scope');
    if (opener) opener.focus();
  }
}

// --- transaction inspector ---------------------------------------------

// Docked beside the feed on a wide screen and docked to the bottom on a
// narrow one — one component either way. On a wide screen it is a panel,
// not a modal: the point of an inspector is that you can keep reading what
// it inspects. On a phone it covers the feed, so it is a modal there.
let inspectorOpener = null;   // the activity id whose row opened it

function openInspector(row) {
  html.classList.add('inspector-open');
  document.querySelectorAll('.row.is-open').forEach((r) => r.classList.remove('is-open'));
  if (row) {
    row.classList.add('is-open');
    inspectorOpener = row.dataset.activity || null;
  }
  if (isNarrow()) scrim.hidden = false;
  inspector.inert = false;
  if (document.getElementById('insp-title')) {
    inspector.setAttribute('aria-labelledby', 'insp-title');
  }
  applyLayout();
  const title = document.getElementById('insp-title');
  if (title) title.focus();
}

function closeInspector() {
  const wasOpen = html.classList.contains('inspector-open');
  html.classList.remove('inspector-open');
  document.querySelectorAll('.row.is-open').forEach((r) => r.classList.remove('is-open'));
  if (!html.classList.contains('scope-open')) scrim.hidden = true;
  inspector.inert = true;
  inspector.removeAttribute('aria-labelledby');
  applyLayout();
  if (wasOpen && inspectorOpener) focusRow(inspectorOpener);
}

function closeSheets() { closeScopeSheet(); closeInspector(); }

// Which of the two is a modal depends on the width, and the width can
// change with either open (a rotated phone, a resized window).
function applyLayout() {
  if (isNarrow() && html.classList.contains('inspector-open')) {
    makeModal(inspector);
  } else if (isNarrow() && html.classList.contains('scope-open')) {
    makeModal(sidebar, 'Choose what the feed shows');
  } else {
    releaseModal();
    sidebar.removeAttribute('aria-label');
  }
  // On a phone the sidebar is an off-screen sheet while closed: inert, so
  // Tab does not wander through links nobody can see.
  sidebar.inert = isNarrow() && !html.classList.contains('scope-open');
  scrim.hidden = !(isNarrow() && (html.classList.contains('inspector-open')
                                  || html.classList.contains('scope-open')));
}
NARROW.addEventListener('change', applyLayout);
applyLayout();

document.addEventListener('keydown', (evt) => {
  if (evt.key !== 'Escape') return;
  if (walletDialog.open) return;   // the dialog handles its own Escape
  // Innermost first: Escape should not dismiss everything at once.
  if (html.classList.contains('inspector-open')) closeInspector();
  else closeScopeSheet();
});

// --- the feed, by keyboard ---------------------------------------------
//
// One Tab stop for the whole feed, not one per row: with hundreds of rows a
// stop each would make everything after the feed unreachable. Each row has
// a button; exactly one of them is tabbable (tabindex 0, the rest -1), and
// the arrows, Home and End move it. Enter and Space press it — a native
// button, so nothing reimplements them.
const feedBody = document.getElementById('feed-body');

function rowButtons() {
  return [...feedBody.querySelectorAll('tr[data-activity] .row__open')]
    .filter((b) => b.offsetParent !== null);
}
// Only the current row is in the Tab order — its button and its own links
// (the counterparty, the explorer, the hash, the token), and the fold-out
// under it. Every other row's controls are tabindex -1. Tab walks the one
// row and leaves the feed; the arrows choose which row that is.
const ROW_CONTROLS = 'tr[data-activity] a[href], tr[data-activity] button,'
  + ' tr.row--more-of button';
function setStop(button) {
  feedBody.querySelectorAll(ROW_CONTROLS).forEach((el) => { el.tabIndex = -1; });
  const row = button && button.closest('tr[data-activity]');
  if (!row) return;
  row.querySelectorAll('a[href], button').forEach((el) => { el.tabIndex = 0; });
  const fold = row.nextElementSibling;
  if (fold && fold.classList.contains('row--more-of')) {
    fold.querySelectorAll('button').forEach((el) => { el.tabIndex = 0; });
  }
}
function ensureStop() {
  const current = feedBody.querySelector('.row__open[tabindex="0"]');
  // New rows arrive with their links tabbable; the stop is re-applied so
  // they fall in line.
  setStop(current && current.offsetParent !== null ? current : rowButtons()[0] || null);
}
function focusRow(activityId) {
  const button = feedBody.querySelector(
    'tr[data-activity="' + activityId + '"] .row__open');
  const target = button && button.offsetParent !== null ? button : rowButtons()[0];
  if (target) { setStop(target); target.focus(); }
}

feedBody.addEventListener('keydown', (evt) => {
  const current = evt.target.closest && evt.target.closest('.row__open');
  if (!current) return;
  const buttons = rowButtons();
  const at = buttons.indexOf(current);
  const step = { ArrowDown: 1, ArrowUp: -1, PageDown: 10, PageUp: -10 }[evt.key];
  let next = null;
  if (step) next = buttons[Math.max(0, Math.min(buttons.length - 1, at + step))];
  else if (evt.key === 'Home') next = buttons[0];
  else if (evt.key === 'End') next = buttons[buttons.length - 1];
  if (!next) return;
  evt.preventDefault();
  setStop(next);
  next.focus();
});
feedBody.addEventListener('focusin', (evt) => {
  const button = evt.target.closest && evt.target.closest('.row__open');
  if (button) setStop(button);
});

// A swap replaces the rows. The row that had focus (or the tab stop) is the
// same activity afterwards, and gets it back — a refresh every fifteen
// seconds must not throw a keyboard user out of the feed.
// An answer to a question that is no longer asked is not shown. The form
// queues its requests (hx-sync "queue last": nothing is aborted, so htmx
// logs nothing), and a response for the feed is swapped in only if the
// filters it answers — read off its own URL — are still the form's. A page
// or older history carries the filters too, plus where it continues from.
function feedQuestion(search) {
  const out = [];
  for (const [key, value] of new URLSearchParams(search)) {
    if (!value || key === 'before' || key === 'after_day') continue;
    if ((key === 'scope' || key === 'direction') && value === 'all') continue;
    out.push(key + '=' + value);
  }
  return out.sort().join('&');
}
document.body.addEventListener('htmx:beforeSwap', (evt) => {
  const target = evt.detail.target;
  const config = evt.detail.requestConfig || {};
  if (!target || !(target === feedBody || feedBody.contains(target))) return;
  if ((config.verb || '').toLowerCase() !== 'get' || !evt.detail.xhr) return;
  const asked = feedQuestion(new URL(evt.detail.xhr.responseURL, location.href).search);
  const now = feedQuestion(new URLSearchParams(new FormData(filters)).toString());
  if (asked !== now) evt.detail.shouldSwap = false;
});

let feedFocus = null;
document.body.addEventListener('htmx:beforeSwap', (evt) => {
  const target = evt.detail.target;
  if (!target || !(target === feedBody || feedBody.contains(target))) return;
  const focused = document.activeElement && document.activeElement.closest
    && document.activeElement.closest('#feed-body tr[data-activity]');
  const stop = feedBody.querySelector('.row__open[tabindex="0"]');
  feedFocus = {
    row: focused ? focused.dataset.activity
      : (stop ? stop.closest('tr[data-activity]').dataset.activity : null),
    hadFocus: !!focused,
  };
});
document.body.addEventListener('htmx:afterSettle', () => {
  if (feedFocus) {
    const { row, hadFocus } = feedFocus;
    feedFocus = null;
    // Only when the swap took the focused element away. A page appended
    // below (infinite scroll) replaces nothing the reader is on, and must
    // not pull focus back to a row they have since Tabbed past.
    const focused = document.activeElement;
    const lost = !focused || focused === document.body || !focused.isConnected;
    if (lost && hadFocus) {
      focusRow(row);
    } else if (!feedBody.contains(focused)) {
      const button = row && feedBody.querySelector(
        'tr[data-activity="' + row + '"] .row__open');
      if (button && button.offsetParent !== null) setStop(button);
    }
  }
});
// New rows arrive with their links tabbable — a refresh, a filter, a page
// appended by scrolling. Whenever rows are added, however they came, the
// stop is re-applied to the row the reader is on (or the remembered one)
// so the new rows fall in line. setStop only changes attributes, so this
// does not observe itself.
new MutationObserver((records) => {
  if (!records.some((r) => r.addedNodes.length)) return;
  const here = document.activeElement;
  const onRow = feedBody.contains(here) && here.closest('tr[data-activity]');
  if (onRow) setStop(onRow.querySelector('.row__open'));
  else ensureStop();
}).observe(feedBody, { childList: true, subtree: true });
ensureStop();

// "Skip to activity": the current row of the feed (its Tab stop), so the
// arrows work at once. With no rows — a filter that matches nothing, a
// removed wallet — the first control in the feed, or the feed itself.
// Looked up on every use: the feed is replaced all the time.
document.querySelector('.skip-link').addEventListener('click', (evt) => {
  evt.preventDefault();
  ensureStop();
  const stop = feedBody.querySelector('.row__open[tabindex="0"]');
  const target = stop && stop.offsetParent !== null ? stop
    : feedBody.querySelector('a[href], button');
  if (target) {
    target.focus();
  } else {
    feedBody.tabIndex = -1;
    feedBody.focus();
  }
});

// The status bar, the sidebar and the summary are replaced whole — on a
// timer, and after every filter or wallet change — and a keyboard user may
// be on a control inside one: focus goes back to the same control in the
// new one. "The same" is what it picks (data-pick) where it says, else its
// first class.
const REPLACED_WHOLE = ['topline', 'wallets', 'summary'];
document.body.addEventListener('htmx:beforeSwap', (evt) => {
  const target = evt.detail.target;
  if (!target || !REPLACED_WHOLE.includes(target.id)) return;
  const focused = document.activeElement;
  if (!focused || !target.contains(focused)) return;
  const selector = focused.dataset && focused.dataset.pick
    ? '[data-pick="' + focused.dataset.pick + '"]'
    : (focused.classList.length ? '.' + focused.classList[0] : null);
  if (!selector) return;
  const id = target.id;
  setTimeout(() => {
    const again = document.querySelector('#' + id + ' ' + selector);
    if (again && document.activeElement === document.body) again.focus();
  }, 0);
});

// --- wallet dialog -----------------------------------------------------
const walletDialog = document.getElementById('wallet-dialog');

// The dialog is a container; what goes in it is fetched by whoever opens it.
// What opened it, by class: the summary and the sidebar it lives in are
// re-rendered on a timer, so the element itself may be gone by close.
let dialogOpener = null;

function showWalletDialog() {
  document.querySelectorAll('.wallet__menu[open]').forEach((m) => (m.open = false));
  const focused = document.activeElement;
  if (!walletDialog.open) {
    dialogOpener = focused && focused !== document.body && focused.classList.length
      ? '.' + focused.classList[0] : null;
    walletDialog.showModal();
    const close = walletDialog.querySelector('.insp__close');
    const first = walletDialog.querySelector('[autofocus]')
      || walletDialog.querySelector('input, select, textarea') || close;
    if (first) first.focus();
  }
}

// A refused form comes back with the field at fault marked [autofocus]
// (U-11). htmx does not focus it in a swapped fragment, so this does —
// otherwise focus falls to <body> and the keyboard user is lost.
document.body.addEventListener('htmx:afterSwap', (evt) => {
  if (!walletDialog.open || !walletDialog.contains(evt.detail.target)) return;
  const wanted = walletDialog.querySelector('[aria-invalid="true"], [autofocus]');
  if (wanted) wanted.focus();
});

walletDialog.addEventListener('close', () => {
  const opener = dialogOpener && document.querySelector(dialogOpener);
  dialogOpener = null;
  if (opener && opener.offsetParent !== null) opener.focus();
});

function openWalletDialog(id) {
  // The form is rendered server-side so the dialog itself holds no state.
  htmx.ajax('GET', '/wallets/form' + (id ? '?id=' + id : ''),
            { target: '#wallet-dialog-body', swap: 'innerHTML' })
    .then(showWalletDialog);
}

function closeWalletDialog() {
  walletDialog.close();
}

// The server says what changed — {action: added | updated | removed, id} —
// and this decides the order: the dialog and menus that made the change
// close; a removed wallet stops being the scope, before anything asks the
// server about it; then the feed, and after it everything else (see
// applyFilters). A removed wallet's rows leave the feed now, and a feed
// scoped to it goes back to everything.
document.body.addEventListener('wallet-changed', (evt) => {
  const { action, id } = evt.detail || {};
  if (walletDialog.open) walletDialog.close();
  document.querySelectorAll('.wallet__menu[open]').forEach((m) => (m.open = false));
  const wallet = document.getElementById('wallet-value');
  if (action === 'removed' && wallet.value === String(id)) {
    wallet.value = '';
    document.getElementById('scope-value').value = 'all';
  }
  applyFilters('wallet');
});

// navigator.clipboard needs a secure context. Over plain HTTP it simply is
// not there, so the fallback puts the whole address on screen to be selected
// by hand rather than failing silently.
async function copyAddress(address, button) {
  const original = button.textContent;
  let copied = false;
  try {
    await navigator.clipboard.writeText(address);
    copied = true;
  } catch (e) {
    copied = false;
  }
  button.textContent = copied ? 'Copied' : address;
  button.classList.toggle('is-copied', copied);
  setTimeout(() => {
    button.textContent = original;
    button.classList.remove('is-copied');
  }, copied ? 1200 : 4000);
}

// Close an open actions menu when the click lands elsewhere.
document.addEventListener('click', (evt) => {
  for (const menu of document.querySelectorAll('.wallet__menu[open]')) {
    if (!menu.contains(evt.target)) menu.open = false;
  }
});

// A timed sidebar refresh must not yank an open menu away mid-click. One
// that follows a wallet change carries {force: true} and always goes.
document.body.addEventListener('htmx:beforeRequest', (evt) => {
  const trigger = evt.detail.requestConfig && evt.detail.requestConfig.triggeringEvent;
  if (evt.detail.elt && evt.detail.elt.id === 'wallets'
      && (document.querySelector('.wallet__menu[open]')
          || document.documentElement.classList.contains('scope-open'))
      && !(trigger && trigger.detail && trigger.detail.force)) {
    evt.preventDefault();
  }
});

// Holdings panel: collapsed state lives on <html> so it survives every
// fragment swap, and in localStorage so it survives a reload.
// The same button, two jobs. On a narrow screen the head names the scope and
// opens the sheet; on a wide one the sidebar does that and the head folds the
// holdings away instead.
// The rest of a transaction's transfers are already in the page; this only
// decides whether they are on screen.
function toggleFold(button) {
  const open = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', open ? 'true' : 'false');
  const tx = button.closest('tr').dataset.tx;
  for (const row of document.querySelectorAll('.row--folded[data-tx="' + tx + '"]')) {
    row.classList.toggle('is-shown', open);
  }
}

// Search is a mode on a phone, not a third row that is always there.
function toggleSearch(open) {
  const bar = document.getElementById('filters');
  bar.classList.toggle('is-searching', open);
  if (open) document.getElementById('search').focus();
}

// Closing the box clears the search: leaving a filter in force behind a
// button the reader just dismissed is how a feed silently lies about itself.
function closeSearch() {
  const box = document.getElementById('search');
  const had = !!box.value;
  box.value = '';
  toggleSearch(false);
  if (had) applyFilters('filter');
}

function toggleSummary() {
  const collapsed = document.documentElement.classList.toggle('summary-collapsed');
  try { localStorage.setItem('blocktail.summary.collapsed', collapsed ? '1' : '0'); } catch (e) {}
}

// Revealing dust is a view preference, so it lives on <html> and survives
// every fragment swap, like the summary's collapsed state.
function toggleHiddenTokens() {
  const shown = document.documentElement.classList.toggle('show-hidden-tokens');
  try { localStorage.setItem('blocktail.tokens.showHidden', shown ? '1' : '0'); } catch (e) {}
}

// Dust is a real filter, not a view preference: it changes what the server
// sends, so it lives in the form like the others.
function toggleDust(show) {
  let field = filters.querySelector('input[name="dust"]');
  if (!field) {
    field = document.createElement('input');
    field.type = 'hidden';
    field.name = 'dust';
    filters.appendChild(field);
  }
  field.value = show ? 'show' : '';
  applyFilters('filter');
}

// A link that narrows the feed — a wallet or a group in the sidebar, an
// asset in the summary or the holdings sheet, a counterparty in a row —
// says what it chooses in data-pick ("wallet:3", "scope:mine",
// "asset:12", "address:0x…"). Its choice goes into the form and through
// applyFilters like any other filter. Wired once, on the document, so it
// holds for every fragment however often it is replaced. A modified click
// or a middle click keeps the href and opens a new tab.
function pick(kind, value) {
  const wallet = document.getElementById('wallet-value');
  const scope = document.getElementById('scope-value');
  const asset = filters.querySelector('select[name="asset"]');
  if (kind === 'wallet') {
    wallet.value = value;
    scope.value = 'all';
  } else if (kind === 'scope') {
    wallet.value = '';
    scope.value = value;
  } else if (kind === 'asset') {
    if (asset) asset.value = value;
  } else if (kind === 'address') {
    // "What have my wallets ever done with this address?" — every other
    // filter gets out of the way.
    wallet.value = '';
    scope.value = 'all';
    document.getElementById('search').value = value;
    if (asset) asset.value = '';
    const all = filters.querySelector('.segment--all input');
    if (all) all.checked = true;
  } else {
    return;
  }
  closeScopeSheet();
  if (walletDialog.open) walletDialog.close();
  applyFilters('pick');
}

document.addEventListener('click', (evt) => {
  const link = evt.target.closest && evt.target.closest('a[data-pick]');
  if (!link || evt.button !== 0 || evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) {
    return;
  }
  evt.preventDefault();
  const spec = link.dataset.pick;
  const at = spec.indexOf(':');
  pick(spec.slice(0, at), spec.slice(at + 1));
});

// A swap replaces every <tr>, so the marker on the open row has to be put
// back — otherwise the inspector points at a row that no longer looks open.
document.body.addEventListener('htmx:afterSwap', (evt) => {
  if (evt.target.id !== 'feed-body') return;
  const open = document.getElementById('inspector-body').querySelector('.xfer.is-current');
  if (!open) return;
  const id = open.closest('.insp')?.dataset.selected;
  if (id) document.querySelector('.row[data-activity="' + id + '"]')?.classList.add('is-open');
});

function onFiltered(evt) {
  // An aborted request (a newer one replaced it) or a failed one changed
  // nothing on the page, so nothing follows from it.
  if (!evt.detail.successful) return;
  syncUrl();
  syncActiveFilters();
  // Anything outside the swapped feed that depends on the filters — the line
  // saying what is hidden — refreshes off this.
  document.body.dispatchEvent(new Event('filtersApplied'));
  const trigger = evt.detail.requestConfig && evt.detail.requestConfig.triggeringEvent;
  if (!trigger || trigger.type !== 'refreshFeed') {
    const force = !!(trigger && trigger.detail && trigger.detail.reason === 'wallet');
    document.body.dispatchEvent(new CustomEvent('refreshWallets', { detail: { force } }));
  }
}

syncActiveFilters();

// --- new-row highlight -------------------------------------------------
// Rows that appear on an automatic refresh glow briefly and settle, the way a
// new line lands in `tail -f`. Only auto-refresh qualifies: paging back and
// changing a filter also bring in unseen rows, but those are not news.
//
// Every decision here is read off the event being handled. A shared "is this
// an auto-refresh?" flag looks simpler and is wrong: the status bar polls on
// its own timer, and its request lands between this one's send and swap,
// resetting the flag before the rows arrive.
const rowsBeforeSwap = new WeakMap();

function isAutoRefresh(config) {
  const trigger = config && config.triggeringEvent;
  return !!(trigger && trigger.type === 'refreshFeed');
}

// Rows loaded below the first page — by scrolling, or older history once
// it arrives — are kept from the timed refresh (U-12). Read off the
// request that brought them: the sentinel that asked is gone by now, and
// a status poll in between says nothing about the feed. The flag is
// cleared only by applyFilters, when the question itself changes.
document.body.addEventListener('htmx:afterSwap', (evt) => {
  const config = evt.detail.requestConfig || {};
  const path = config.path || '';
  if (path.startsWith('/activity') && /[?&]before=/.test(path)) {
    document.body.dataset.paged = '1';
  } else if (path.startsWith('/history/older') && evt.detail.xhr
             && !/row--older is-busy/.test(evt.detail.xhr.responseText)) {
    document.body.dataset.paged = '1';
    document.body.dispatchEvent(new Event('history-loaded'));
  }
});

// htmx discards 4xx responses by default. A rejected wallet form *is* the
// response we want to render — that is where the reason for the rejection is.
document.body.addEventListener('htmx:beforeSwap', (evt) => {
  if (evt.detail.xhr && [403, 404, 422].includes(evt.detail.xhr.status)) {
    evt.detail.shouldSwap = true;
    evt.detail.isError = false;
  }
});

document.body.addEventListener('htmx:beforeSwap', (evt) => {
  const target = evt.detail.target;
  if (!target || target.id !== 'feed-body') return;
  rowsBeforeSwap.set(target, new Set([...target.querySelectorAll('tr[data-activity]')]
    .map((row) => row.dataset.activity)));
});

document.body.addEventListener('htmx:afterSwap', (evt) => {
  const target = evt.detail.target;
  if (!target || target.id !== 'feed-body') return;
  const seen = rowsBeforeSwap.get(target);
  rowsBeforeSwap.delete(target);
  if (!seen || !isAutoRefresh(evt.detail.requestConfig)) return;
  for (const row of target.querySelectorAll('tr[data-activity]')) {
    if (seen.has(row.dataset.activity)) continue;
    row.classList.add('row--new');
    setTimeout(() => row.classList.remove('row--new'), 4500);
  }
});

// Refresh the newest page periodically, but never yank the view out from
// under someone who has scrolled into history or paged further back.
setInterval(() => {
  if (document.hidden) return;
  if (window.blocktail && window.blocktail.connection.lost) return;
  if (document.body.dataset.paged === '1') return;
  const scroller = document.querySelector('.table-scroll');
  const offset = scroller && scroller.scrollTop ? scroller.scrollTop : window.scrollY;
  if (offset > 300) return;
  document.body.dispatchEvent(new Event('refreshFeed'));
}, 15000);

// --- connection -------------------------------------------------------
//
// The status bar is a fragment the server re-renders every few seconds.
// When the server stops answering, nothing replaces it, so on its own it
// would keep saying "synced, 2s ago" for ever. This is what notices.
//
// * Every request for the status ends in htmx:afterRequest. A failed one —
//   no response at all (status 0), or a 5xx — means the page has lost the
//   server; a successful one means it has it again. A provider failure is
//   not this: the server answers, and says so in the fragment itself.
// * The age is counted here, every second, from the time the server says
//   the last sync succeeded, corrected by the server's clock — so it keeps
//   growing while the server is gone, and is right after it comes back.
// * Nothing is replaced or zeroed while the connection is lost: the page
//   keeps what it last had, and says how old that is.
//
// Wired once, on the page — never on the fragment, which is replaced every
// poll. The interval is the only timer; the polling itself is htmx's own.
(function watchConnection() {
  if (window.blocktail && window.blocktail.connection) return;
  const html = document.documentElement;
  const notice = document.getElementById('connection');
  const live = document.getElementById('status-live');
  const conn = {
    lost: false,
    lastReply: Date.now(),   // the browser's clock: when the server last answered
    offset: 0,               // server clock minus browser clock, in ms
    announced: null,
    stats: { handled: 0, ticks: 0 },
  };
  window.blocktail = Object.assign(window.blocktail || {}, { connection: conn });

  // The same wording the server uses, so a reload does not change the text.
  function ago(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function clock(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  function status() { return document.querySelector('#topline .status'); }

  function readServer() {
    const s = status();
    if (s && s.dataset.now) conn.offset = Number(s.dataset.now) * 1000 - Date.now();
  }

  function tick() {
    const s = status();
    if (s && s.dataset.updated) {
      const text = ago((Date.now() + conn.offset) / 1000 - Number(s.dataset.updated));
      const age = s.querySelector('.status__age');
      if (age && age.textContent !== text) age.textContent = text;
      if (s.dataset.age !== text) s.dataset.age = text;
    }
    if (conn.lost) {
      notice.querySelector('.connection__age').textContent =
        '· last reply ' + ago((Date.now() - conn.lastReply) / 1000);
    }
  }

  function say(text) { if (live.textContent !== text) live.textContent = text; }

  function lose(code) {
    const message = code ? 'Server error (HTTP ' + code + ')' : 'Connection lost';
    notice.querySelector('.connection__msg').textContent = message;
    if (conn.lost) return;
    conn.lost = true;
    html.classList.add('conn-lost');
    notice.hidden = false;
    say(message + '. The page shows what it had at ' + clock(conn.lastReply) + '.');
    tick();
  }

  function regain() {
    conn.lastReply = Date.now();
    readServer();
    if (conn.lost) {
      conn.lost = false;
      html.classList.remove('conn-lost');
      notice.hidden = true;
      say('Connection restored.');
    }
    // Announce a change of sync state — not every poll.
    const s = status();
    const state = s ? s.dataset.state : null;
    if (state && conn.announced !== null && state !== conn.announced) {
      const detail = s.querySelector('.status__detail');
      say(state.startsWith('ok') && !state.includes('+') ? 'Synced.'
          : (detail ? detail.textContent.trim() : 'Sync state: ' + state + '.'));
    }
    conn.announced = state;
    tick();
  }

  // A good reply replaces #topline (outerHTML), and htmx:afterRequest then
  // fires on the detached old element, where it never reaches the body. So
  // a reply is seen before the swap, while the element is still in place,
  // and handled once the new one is.
  document.body.addEventListener('htmx:beforeSwap', (evt) => {
    const target = evt.detail.target;
    const code = evt.detail.xhr ? evt.detail.xhr.status : 0;
    if (!target || target.id !== 'topline' || code >= 400) return;
    conn.stats.handled += 1;
    setTimeout(regain, 0);
  });

  // A failed request swaps nothing, so this one does reach the body. No
  // answer at all (status 0), or a server error, is a lost server — from
  // the status poll or from any other request on the page.
  document.body.addEventListener('htmx:afterRequest', (evt) => {
    if (evt.detail.successful) return;
    const code = evt.detail.xhr ? evt.detail.xhr.status : 0;
    const elt = evt.detail.elt;
    if (elt && elt.id === 'topline') conn.stats.handled += 1;
    if (code === 0 || code >= 500) lose(code || null);
  });

  readServer();
  conn.announced = status() ? status().dataset.state : null;
  tick();
  // The one timer. Counted, so a test can tell one from several.
  setInterval(() => { conn.stats.ticks += 1; tick(); }, 1000);
})();

// --- wiring -------------------------------------------------------------
// The CSP allows no inline handler and no eval (app/security.py), so what the
// markup used to say in onclick=, hx-on and hx-trigger filters is said here,
// once. A control names what it does — data-action="…" — and carries its
// arguments as data-*; the functions are the same ones as before.
const ACTIONS = {
  'toggle-dust': (el) => toggleDust(el.dataset.show === 'true'),
  'open-scope-sheet': () => openScopeSheet(),
  'close-sheets': () => closeSheets(),
  'open-search': () => toggleSearch(true),
  'close-search': () => closeSearch(),
  'close-inspector': () => closeInspector(),
  'toggle-summary': () => toggleSummary(),
  'copy-address': (el) => copyAddress(el.dataset.address, el),
  'toggle-hidden-tokens': () => toggleHiddenTokens(),
  'close-wallet-dialog': () => closeWalletDialog(),
  'toggle-fold': (el) => toggleFold(el),
  'open-wallet-dialog': (el) => openWalletDialog(el.dataset.wallet),
};

document.addEventListener('click', (evt) => {
  const el = evt.target.closest && evt.target.closest('[data-action]');
  const action = el && ACTIONS[el.dataset.action];
  if (action) action(el, evt);
});

// A filter control changing is a new question, asked the one way (see
// applyFilters). The search box's own change, on blur, is not: it has
// already asked, on input.
filters.addEventListener('change', (evt) => {
  if (evt.target.id !== 'search') applyFilters('filter');
});
filters.addEventListener('submit', (evt) => evt.preventDefault());
filters.addEventListener('htmx:afterRequest', onFiltered);

// A row opens the transaction behind it, unless the click was on a link or a
// control of its own (a filter link, the explorer, the fold-out): one rule
// here rather than a stopPropagation on each. Its own button, .row__open,
// is how the keyboard does the same.
feedBody.addEventListener('click', (evt) => {
  if (evt.target.closest('a, button:not(.row__open)')) return;
  const row = evt.target.closest('tr[data-activity][hx-get]');
  if (row) htmx.trigger(row, 'open-row');
});

document.body.addEventListener('htmx:afterRequest', (evt) => {
  const elt = evt.detail.elt;
  if (!elt || !elt.matches) return;
  if (elt.matches('tr[data-activity]')) openInspector(elt);
  else if (elt.matches('[data-opens-dialog]')) showWalletDialog();
});
