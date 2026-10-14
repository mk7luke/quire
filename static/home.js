(function () {
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var text = form.getAttribute("data-confirm") || "Continue?";
      if (!window.confirm(text)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var node = document.getElementById(btn.getAttribute("data-copy"));
      var text = node ? (node.textContent || "") : "";
      var done = function () { btn.textContent = "Copied"; };
      var fail = function () { btn.textContent = "Select the link"; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fail);
      } else {
        fail();
      }
    });
  });

  document.querySelectorAll("textarea[data-count]").forEach(function (box) {
    var out = document.getElementById(box.getAttribute("data-count"));
    if (!out) return;
    var tick = function () {
      out.textContent = String(box.value.length) + " / 900";
    };
    box.addEventListener("input", tick);
    tick();
  });

  var quire = document.getElementById("quire-select");
  var waste = document.getElementById("waste-line");
  var written = parseInt(document.body.getAttribute("data-written") || "0", 10);
  if (quire && waste && written > 0) {
    var sizes = [4, 8, 12, 16];
    var pad = function (n, q) {
      var target = Math.max(n, q);
      var rem = target % q;
      if (rem) target += q - rem;
      return target;
    };
    var paint = function () {
      var q = parseInt(quire.value, 10);
      var padded = pad(written, q);
      var blanks = padded - written;
      var better = null;
      sizes.forEach(function (alt) {
        if (alt === q) return;
        var w = pad(written, alt) - written;
        if (w < blanks && (better === null || w < better.waste)) better = { waste: w, quire: alt };
      });
      var line = written + " written. " + padded + " on the sheet.";
      if (blanks === 0) line += " No blanks.";
      else if (blanks === 1) line += " 1 blank pad.";
      else line += " " + blanks + " blank pads.";
      if (better) line += " A quire of " + better.quire + " adds " + better.waste + " instead.";
      waste.textContent = line;
    };
    quire.addEventListener("change", paint);
  }
})();
