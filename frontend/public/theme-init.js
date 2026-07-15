// Set the theme before first paint so light-mode users don't flash dark. Default = dark.
// Kept as an external file (not inline in index.html) so the CSP can be script-src 'self'
// with no 'unsafe-inline' exception.
(function () {
  try {
    document.documentElement.dataset.theme = localStorage.getItem("foray-theme") === "light" ? "light" : "dark";
    document.documentElement.dataset.textSize =
      localStorage.getItem("foray-text-size") === "large" ? "large" : "normal";
  } catch (e) {
    document.documentElement.dataset.theme = "dark";
    document.documentElement.dataset.textSize = "normal";
  }
})();
