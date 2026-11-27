// Two small jobs, nothing that collects anything.
(function () {
  // Today's hours: mark the row and repeat it in the hero. Without this the
  // table still says everything; the hero line stays hidden.
  var day = new Date().getDay();
  var row = document.querySelector('.hours tr[data-day="' + day + '"]');
  if (row) {
    row.classList.add('is-today');
    var hours = row.querySelector('td');
    var slot = document.querySelector('[data-today]');
    if (hours && slot) {
      var text = hours.textContent.trim();
      slot.textContent = /closed/i.test(text) ? 'closed' : text;
      if (/closed/i.test(text)) slot.parentNode.firstChild.nodeValue = 'Today: ';
      slot.parentNode.hidden = false;
    }
  }

  // The arrows beside a sideways row move it one card at a time.
  document.querySelectorAll('[data-scroll]').forEach(function (b) {
    b.addEventListener('click', function () {
      var r = document.getElementById(b.getAttribute('data-scroll'));
      if (!r) return;
      var card = r.firstElementChild;
      var step = card ? card.getBoundingClientRect().width + 20 : 300;
      r.scrollBy({ left: step * Number(b.getAttribute('data-dir')), behavior: 'smooth' });
    });
  });
})();
