// Reload only when the scanner has something new, and never while a box is being typed in or a form is open.
(function () {
  var v = document.querySelector("main").dataset.v, loaded = Date.now();
  function busy() {
    var a = document.activeElement;
    return (a && a.tagName === "INPUT") || document.querySelector("details.bought[open], details.skills[open]");
  }
  setInterval(function () {
    fetch("/version").then(function (r) { return r.text(); }).then(function (now) {
      if ((now !== v || Date.now() - loaded > 300000) && !busy()) location.reload();
    }).catch(function () {});
  }, 4000);
})();
// A list marked data-auto applies as soon as a choice is made; its button is only needed without scripts.
document.querySelectorAll("select[data-auto]").forEach(function (list) {
  list.form.querySelector("button").hidden = true;
  list.addEventListener("change", function () { list.form.submit(); });
});
// An item icon the image host cannot serve becomes an empty slot instead of a broken picture.
document.querySelectorAll("img.ico").forEach(function (img) {
  function blank() {
    // the Forever icon host refuses some icons that the main one serves: try that once before giving up
    if (img.src.indexOf("/forever-eu/") > 0) { img.src = img.src.replace("/forever-eu/", "/eu/"); return; }
    var slot = document.createElement("div"); slot.className = img.className; img.replaceWith(slot);
  }
  img.addEventListener("error", blank);
  if (img.complete && !img.naturalWidth) blank();
});
