// Runs in <head>, before first paint (blocking on purpose): the classes it
// sets decide what the first frame looks like.
// Applied before first paint so a collapsed panel never flashes open.
// A narrow screen should open on activity, not on a portfolio panel.
try {
  const stored = localStorage.getItem('blocktail.summary.collapsed');
  const collapsed = stored === null
    ? window.matchMedia('(max-width: 860px)').matches
    : stored === '1';
  if (collapsed) document.documentElement.classList.add('summary-collapsed');
  if (localStorage.getItem('blocktail.tokens.showHidden') === '1') {
    document.documentElement.classList.add('show-hidden-tokens');
  }
} catch (e) {}
