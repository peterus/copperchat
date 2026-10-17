// Applies the stored theme before first paint. An external file (not an inline <script>)
// so the Content-Security-Policy can stay at script-src 'self'.
try {
  var p = localStorage.getItem('copperchat.theme') || 'system';
  var dark = p === 'dark' || (p === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
} catch {
  // storage unavailable (private mode): keep the default theme
}
