// The contact address is stored reversed and split (see templates/landing/index.html.j2)
// so scrapers reading the HTML don't find it; rebuild it for people.
document.querySelectorAll(".js-mail").forEach(function (a) {
  var parts = a.dataset.m.split("|").map(function (x) { return x.split("").reverse().join(""); });
  var addr = parts[1] + "@" + parts[0];
  a.href = "mailto:" + addr;
  a.querySelector(".handle").textContent = addr;
});
