// Colour mode for the guestbook. The choice (auto / light / dark) is kept in this browser only.
// Loaded in <head> without defer, so the right colours are set before the page paints.
(function () {
  var KEY = 'gb-theme', root = document.documentElement, fadeTimer;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function saved() {
    try { var v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : 'auto'; } catch (e) { return 'auto'; }
  }
  function apply(pref) {
    var dark = pref === 'dark' || (pref === 'auto' && mq && mq.matches);
    root.dataset.theme = dark ? 'dark' : 'light';
    root.style.colorScheme = dark ? 'dark' : 'light';
    document.querySelectorAll('.theme button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.mode === pref));
    });
  }
  apply(saved());
  if (mq) mq.addEventListener('change', function () { if (saved() === 'auto') apply('auto'); });

  document.addEventListener('DOMContentLoaded', function () {
    apply(saved());
    document.querySelectorAll('.theme button').forEach(function (b) {
      b.addEventListener('click', function () {
        try { localStorage.setItem(KEY, b.dataset.mode); } catch (e) {}
        root.classList.add('theme-fade');
        apply(b.dataset.mode);
        clearTimeout(fadeTimer);
        fadeTimer = setTimeout(function () { root.classList.remove('theme-fade'); }, 400);
      });
    });
  });
})();
