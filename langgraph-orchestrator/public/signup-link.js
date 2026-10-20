// Adds a "Request access" link under the Chainlit login form, pointing to the
// /signup page served by signup.py. Enable in .chainlit/config.toml:
//   [UI]
//   custom_js = "/public/signup-link.js"
(function () {
  function addLink() {
    if (!location.pathname.startsWith("/login")) return;
    if (document.getElementById("ngpt-signup-link")) return;
    var form = document.querySelector("form");
    if (!form) return;
    var p = document.createElement("p");
    p.id = "ngpt-signup-link";
    p.style.textAlign = "center";
    p.style.marginTop = "16px";
    p.style.fontSize = "14px";
    var a = document.createElement("a");
    a.href = "/signup";
    a.textContent = "No account? Request access";
    a.style.textDecoration = "underline";
    p.appendChild(a);
    form.parentNode.insertBefore(p, form.nextSibling);
  }
  new MutationObserver(addLink).observe(document.documentElement, { childList: true, subtree: true });
  addLink();
})();
