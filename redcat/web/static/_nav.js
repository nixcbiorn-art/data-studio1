/* _nav.js — единая навигация mail-страниц */
(function(){
  "use strict";
  if (document.getElementById("rc-nav")) return;
  var path = location.pathname.replace(/\/+$/, "") || "/";
  var pages = {
    "chat":      ["/mail_chat.html", "/mail_view.html"],
    "tasks":     ["/mail_tasks.html"],
    "templates": ["/mail_templates.html"],
    "saved":     ["/mail_saved.html"],
    "settings":  ["/mail_settings.html"]
  };
  var active = "";
  for (var key in pages) {
    if (pages[key].indexOf(path) >= 0) { active = key; break; }
  }
  function link(href, label, key) {
    var cls = (key && key === active) ? ' class="active"' : "";
    return '<a href="' + href + '"' + cls + '>' + label + '</a>';
  }
  var nav = document.createElement("nav");
  nav.id = "rc-nav";
  nav.innerHTML =
    '<a class="brand" href="/"><span class="logo">🐱</span>'
    + '<span>RedCat Studio</span></a>'
    + '<div class="links">'
    +   link("/mail_chat.html",      "📬 Почта",      "chat")
    +   link("/mail_tasks.html",     "📋 Задачи",     "tasks")
    +   link("/hub.html",            "📡 Хаб",        "hub")
    +   link("/mail_saved.html",     "💾 Сохранённые", "saved")
    +   link("/mail_templates.html", "📝 Шаблоны",    "templates")
    +   link("/mail_settings.html",  "⚙️ Настройки", "settings")
    + '</div>'
    + '<div class="spacer"></div>'
    + '<a class="back" href="/">← В Studio</a>';
  function insert() {
    if (!document.body) return;
    if (document.getElementById("rc-nav")) return;
    document.body.insertBefore(nav, document.body.firstChild);
    document.body.classList.add("rc-has-nav");
    try {
      var cs = getComputedStyle(document.body);
      if (cs.display === "flex" && cs.flexDirection === "column") {
        document.body.classList.add("rc-flex-column");
      }
    } catch (_) {}
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", insert);
  } else {
    insert();
  }
})();
