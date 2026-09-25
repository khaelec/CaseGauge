/* Dashboard logic: poll /api/stats, paint the numbers, keep the screen awake.
 *
 * Polling (not WebSocket) is deliberate - at 1 Hz the payload is ~200 bytes
 * and plain fetch has far fewer failure modes than a socket that has to
 * survive WiFi sleep, backgrounding and reconnects.
 */
(function () {
  'use strict';

  var POLL_MS = 1000;
  var STALE_MS = 3500;   // no good reply for this long -> warn
  var DOWN_MS = 8000;    // ...and this long -> mark it down

  // Heat thresholds in Celsius: [warm, hot].
  // The 5700X3D runs hot by design and only throttles in the 80s.
  var CPU_HEAT = [65, 82];
  var GPU_HEAT = [60, 78];
  // Inside the case runs far cooler than any chip in it: 45 is warm for a
  // board sensor and 55 means the airflow has stopped.
  var CASE_HEAT = [45, 55];

  var $ = function (id) { return document.getElementById(id); };

  var lastGood = 0;
  var everConnected = false;
  var myBuild = (document.getElementById('build') || {}).textContent || '';
  var wasDownLong = false;

  // The hint line has two writers. Wallpaper progress is transient and wins
  // while present; the sensor notice is the steady state underneath it.
  var sensorHint = '';
  var wallpaperHint = '';

  function showHint() {
    setText($('hint'), wallpaperHint || sensorHint);
  }

  function heat(value, limits) {
    if (value === null || value === undefined) return '';
    if (value >= limits[1]) return 'hot';
    if (value >= limits[0]) return 'warm';
    return 'cool';
  }

  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    return value.toFixed(digits === undefined ? 0 : digits);
  }

  function setBar(el, percent, limits) {
    if (!el) return;
    var p = (percent === null || percent === undefined) ? 0 : percent;
    el.style.width = Math.max(0, Math.min(100, p)) + '%';
    if (limits) el.dataset.heat = heat(p, limits);
  }

  // The headline numbers get one fixed-width box per character.
  //
  // font-variant-numeric: tabular-nums does nothing here - the rounded system
  // font has no tabular figures, so "4" renders 3px wider than "5" and the
  // degree sign visibly hops every time a temperature ticks. Measured widths
  // at 140px: "44" 161.2 vs "45" 158.2, identical with and without tnum.
  // Boxing each glyph makes it stable on any font.
  function charKind(ch) {
    if (ch >= '0' && ch <= '9') return 'd';
    if (ch === '.') return 'p';
    return 'o';
  }

  var KIND_CLASS = { d: 'dig', p: 'pt', o: 'ch' };

  // Zero-width padding, so a value's character COUNT never changes and the
  // spans never have to be rebuilt. U+200B renders at zero width, so this is
  // invisible - it exists purely to keep the DOM structure stable.
  function padTo(text, len) {
    while (text.length < len) text = '​' + text;
    return text;
  }

  // Every character gets its own span and its own text node. Updating a value
  // then only writes nodeValue (and, rarely, a className) - no nodes are added
  // or removed, so no line box is ever torn down and rebuilt.
  //
  // This matters: the earlier version rebuilt the children whenever the string
  // changed length, and measurement showed #cpu-load-text doing exactly that
  // ~10 times in 14s as CPU load crossed between "3%" and "12%". That rebuild
  // is what made the row twitch down and settle on each update.
  function setNum(el, text) {
    if (!el || el._num === text) return;
    var prev = el._num;
    var i, ch, kind;

    if (prev !== undefined && el._parts && prev.length === text.length) {
      for (i = 0; i < text.length; i++) {
        ch = text.charAt(i);
        if (prev.charAt(i) === ch) continue;
        el._parts[i].nodeValue = ch;
        var span = el._parts[i].parentNode;
        if (span && span.nodeType === 1) {
          var want = KIND_CLASS[charKind(ch)];
          if (span.className !== want) span.className = want;
        }
      }
      el._num = text;
      return;
    }

    el.textContent = '';
    var parts = [];
    for (i = 0; i < text.length; i++) {
      ch = text.charAt(i);
      kind = charKind(ch);
      var node = document.createTextNode(ch);
      var wrap = document.createElement('span');
      wrap.className = KIND_CLASS[kind];
      wrap.appendChild(node);
      el.appendChild(wrap);
      parts.push(node);
    }
    el._parts = parts;
    el._num = text;
  }

  function setValue(el, value, limits, digits, width) {
    if (!el) return;
    setNum(el, padTo(fmt(value, digits), width || 0));
    if (limits) {
      var h = heat(value, limits);
      if (h) el.dataset.heat = h; else delete el.dataset.heat;
    }
  }

  // ---- per-core load square ----------------------------------------------

  // One fixed track per logical processor (16 on this CPU), 2 columns x 8 rows.
  // The tracks are built once and only the fill widths change afterwards, for
  // the same reason setNum writes nodeValue instead of rebuilding: adding or
  // removing nodes reflows the card. Never touches `hidden` - the layout owns
  // that; .cores:empty hides it when there is no per-core data at all.
  var CORE_HEAT = [60, 85];

  function setCores(cores) {
    var el = $('cpu-cores');
    if (!el || !cores || !cores.length) return;

    if (el._n !== cores.length) {
      el.textContent = '';
      el._bars = [];
      for (var i = 0; i < cores.length; i++) {
        var track = document.createElement('span');
        track.className = 'core';
        var fill = document.createElement('i');
        track.appendChild(fill);
        el.appendChild(track);
        el._bars.push(fill);
      }
      el._n = cores.length;
    }

    for (var j = 0; j < cores.length; j++) {
      var bar = el._bars[j];
      var v = cores[j];
      bar.style.width = Math.max(0, Math.min(100, v)) + '%';
      var h = heat(v, CORE_HEAT);
      if (h) bar.dataset.heat = h; else delete bar.dataset.heat;
    }
  }

  // ---- clock --------------------------------------------------------------

  function tickClock() {
    var now = new Date();
    setNum($('time'), now.toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', hour12: false
    }));
    setText($('date'), now.toLocaleDateString([], {
      weekday: 'short', day: 'numeric', month: 'short'
    }));
  }

  // ---- connection state ---------------------------------------------------

  function setStatus(state, text) {
    var el = $('status');
    if (el) el.dataset.state = state;
    setText($('status-text'), text);
  }

  function refreshStatus() {
    if (!everConnected) return;
    var age = Date.now() - lastGood;
    if (age > DOWN_MS) {
      setStatus('down', 'no signal');
      // Deliberately NOT reloading here. Reloading while the server is
      // unreachable lands on a connection-error page with nothing to poll and
      // no way back - and this iPad is mounted inside the PC case, so nobody
      // can rescue it. Recovery is handled on the first successful poll
      // instead (see paint), which is safe because the server is by then
      // demonstrably answering.
      if (age > 120000) wasDownLong = true;
    } else if (age > STALE_MS) {
      setStatus('stale', 'reconnecting');
    }
  }

  // ---- GPU cards ----------------------------------------------------------

  // A machine can hold more than one GPU, so the page holds as many GPU cards
  // as the server reports GPUs. The markup ships with one; the others are
  // clones of it with every id inside suffixed by the card id, so each card's
  // rows can still be found by id and card 1 keeps the ids it always had.
  var MAX_GPU_CARDS = 4;
  var gpuIds = ['gpu'];

  function isGpu(id) { return /^gpu[2-9]?$/.test(id); }

  function kindOf(id) { return isGpu(id) ? 'gpu' : id; }

  function gel(cardId, base) {
    return $(cardId === 'gpu' ? base : base + '-' + cardId);
  }

  // Two labels, deliberately. The Layout editor has to tell the cards apart
  // even when only one of them is shown, so it numbers them as soon as there
  // are two GPUs. The heading on the card numbers itself only when two cards
  // are actually on screen - with one card there is nothing to distinguish it
  // from, and "GPU 1" beside no GPU 2 just raises a question.
  function gpuLabel(cardId) {
    if (gpuIds.length < 2) return 'GPU';
    return 'GPU ' + (gpuIds.indexOf(cardId) + 1);
  }

  function gpuHeading(cardId) {
    var shown = layoutState.filter(function (c) { return isGpu(c.id); });
    if (shown.length < 2) return 'GPU';
    return 'GPU ' + (gpuIds.indexOf(cardId) + 1);
  }

  function refreshGpuHeadings() {
    gpuIds.forEach(function (id) {
      var card = $('card-' + id);
      var head = card && card.querySelector('h2');
      if (head) setText(head, gpuHeading(id));
    });
  }

  function ensureGpuCards(count) {
    count = Math.max(1, Math.min(count || 1, MAX_GPU_CARDS));
    if (count === gpuIds.length) return;

    var first = $('card-gpu');
    if (!first) return;

    while (gpuIds.length > count) {
      var dead = $('card-' + gpuIds.pop());
      if (dead && dead.parentNode) dead.parentNode.removeChild(dead);
    }
    while (gpuIds.length < count) {
      var id = 'gpu' + (gpuIds.length + 1);
      var clone = first.cloneNode(true);
      clone.id = 'card-' + id;
      clone.setAttribute('data-card', id);
      // Suffix every id inside, or the clone would answer to card 1's ids and
      // both cards would paint the same numbers.
      var kids = clone.querySelectorAll('[id]');
      for (var i = 0; i < kids.length; i++) {
        kids[i].id = kids[i].id + '-' + id;
      }
      first.parentNode.appendChild(clone);
      gpuIds.push(id);
    }

    // The card set changed without the layout changing, so the grid template
    // and the headings have to be rebuilt by hand - applyLayout() alone only
    // runs when the key changes.
    applyLayout(layoutState);
  }

  function paintGpu(cardId, gpu, missing) {
    if (!$('card-' + cardId)) return;
    if (!gpu) {
      setText(gel(cardId, 'gpu-name'), missing);
      return;
    }
    setText(gel(cardId, 'gpu-name'), gpu.name || ' ');
    setValue(gel(cardId, 'gpu-temp'), gpu.temp_c, GPU_HEAT, 0, 3);
    setNum(gel(cardId, 'gpu-load-text'), padTo(fmt(gpu.load, 0) + '%', 4));
    setBar(gel(cardId, 'gpu-load-bar'), gpu.load);

    var vramPct = null;
    if (gpu.vram_used_gb !== null && gpu.vram_total_gb) {
      vramPct = 100 * gpu.vram_used_gb / gpu.vram_total_gb;
    }
    setNum(gel(cardId, 'vram-text'), padTo(fmt(gpu.vram_used_gb, 1), 4) +
           ' / ' + fmt(gpu.vram_total_gb, 1) + ' GB');
    setBar(gel(cardId, 'vram-bar'), vramPct);
    setNum(gel(cardId, 'gpu-power'), padTo(fmt(gpu.power_w, 0), 3));
    setNum(gel(cardId, 'gpu-fan'), padTo(fmt(gpu.fan, 0), 3));
    var fanUnit = (gpu.fan_unit === 'RPM' ? 'RPM' : '% fan');
    if (gpu.fan_count > 1) fanUnit += ' ×' + gpu.fan_count;
    setText(gel(cardId, 'gpu-fan-unit'), fanUnit);

    // AMD reports a GPU hot spot where NVIDIA reports a memory junction;
    // show whichever exists and label it accordingly.
    var junction = (gpu.junction_c === null || gpu.junction_c === undefined)
      ? null : gpu.junction_c;
    var hotspot = (gpu.hotspot_c === null || gpu.hotspot_c === undefined)
      ? null : gpu.hotspot_c;
    setNum(gel(cardId, 'gpu-junction'),
           padTo(fmt(junction !== null ? junction : hotspot, 0), 3));
    setText(gel(cardId, 'gpu-junction-unit'),
            (junction === null && hotspot !== null) ? 'hot spot' : 'junction');

    setNum(gel(cardId, 'gpu-core-mhz'), padTo(fmt(gpu.core_mhz, 0), 4));
    setNum(gel(cardId, 'gpu-mem-mhz'), padTo(fmt(gpu.mem_mhz, 0), 5));
  }

  // ---- cooling ------------------------------------------------------------

  // Chips whose SET can change - a fan spinning up, a drive arriving - are
  // rebuilt only when the set changes and updated in place otherwise, so
  // nothing churns at 1 Hz. Same rule the drive chips have always followed.
  function chipRow(rowId, items, label, keyOf) {
    var row = $(rowId);
    if (!row) return null;
    items = items || [];
    var key = items.map(keyOf).join(',');
    if (row._key !== key) {
      row.innerHTML = '';
      row._parts = {};
      items.forEach(function (item) {
        var chip = document.createElement('span');
        chip.className = 'chip';
        var name = document.createElement('span');
        name.textContent = label(item) + ' ';
        var value = document.createElement('b');
        var unit = document.createElement('span');
        chip.appendChild(name);
        chip.appendChild(value);
        chip.appendChild(unit);
        row.appendChild(chip);
        row._parts[keyOf(item)] = { value: value, unit: unit };
      });
      row._key = key;
    }
    return row._parts || {};
  }

  function fanName(f) { return f.name; }

  function paintCool(data) {
    var board = data.board || [];
    var fans = data.fans || [];

    // The headline is the board's own System temperature - the one number that
    // describes the inside of the case rather than one component in it.
    var system = null;
    for (var i = 0; i < board.length; i++) {
      if (board[i].name === 'System') { system = board[i].temp_c; break; }
    }
    if (system === null && board.length) system = board[0].temp_c;
    setValue($('cool-temp'), system, CASE_HEAT, 0, 3);

    setText($('cool-sub'), fans.length
      ? (fans.length === 1 ? '1 fan turning' : fans.length + ' fans turning')
      : 'no fan readings - needs LibreHardwareMonitor');

    // RPM, not the control percentage: a header commanded to 60% that reads 0
    // is a dead fan, and that is the whole reason to show this row.
    var fanParts = chipRow('fan-chips', fans, fanName, fanName);
    if (fanParts) {
      fans.forEach(function (f) {
        var part = fanParts[f.name];
        if (!part) return;
        setNum(part.value, padTo(fmt(f.rpm, 0), 4));
        setText(part.unit, ' rpm');
      });
    }

    var boardParts = chipRow('board-chips', board, fanName, fanName);
    if (boardParts) {
      board.forEach(function (b) {
        var part = boardParts[b.name];
        if (!part) return;
        setNum(part.value, padTo(fmt(b.temp_c, 0), 3));
        setText(part.unit, '°C');
      });
    }
  }

  // ---- network ------------------------------------------------------------

  // A rate is shown in whichever unit keeps it to a readable number of digits,
  // so an idle link reads 1.1 KB/s and a busy one 118 MB/s rather than 120832.
  function rateParts(bps) {
    if (bps === null || bps === undefined) return { v: null, u: 'KB/s' };
    if (bps >= 1048576) return { v: bps / 1048576, u: 'MB/s' };
    return { v: bps / 1024, u: 'KB/s' };
  }

  function paintNet(data) {
    var net = data.net;
    if (!net) {
      setText($('net-name'), 'no adapter readings - needs LibreHardwareMonitor');
      return;
    }
    setText($('net-name'), net.name || ' ');

    var down = rateParts(net.down_bps);
    setValue($('net-down'), down.v, null, 1, 4);
    setText($('net-down-unit'), down.u);

    var up = rateParts(net.up_bps);
    setNum($('net-up'), padTo(fmt(up.v, 1), 4));
    setText($('net-up-unit'), up.u + ' up');

    setNum($('net-util-text'), padTo(fmt(net.util, 0) + '%', 4));
    setBar($('net-util-bar'), net.util);
    setNum($('net-total-down'), padTo(fmt(net.down_gb, 1), 5));
    setNum($('net-total-up'), padTo(fmt(net.up_gb, 1), 5));
  }

  // ---- painting -----------------------------------------------------------

  function paint(data) {
    if (!data || !data.ok) {
      setStatus('down', (data && data.error) ? data.error : 'sensor error');
      return;
    }

    lastGood = Date.now();
    everConnected = true;
    setStatus('live', 'live');

    // Self-update, and recovery after a long outage. Both reload, and both do
    // it here - on a successful poll - because that proves the server is up
    // and the reload will actually land on a page.
    //
    // The iPad is mounted inside the PC case and cannot be touched, so a
    // deliberate server outage of a few minutes is the only remote lever for
    // forcing stale code to refresh.
    if (data.build && myBuild && data.build !== myBuild) {
      setText($('hint'), 'updating…');
      setTimeout(function () { location.reload(); }, 400);
      return;
    }
    if (wasDownLong) {
      wasDownLong = false;
      setText($('hint'), 'reconnected, reloading…');
      setTimeout(function () { location.reload(); }, 600);
      return;
    }

    // The PC owns the zoom as well; adopt it when it changes - but not while
    // one of our own writes is still settling, or an in-flight poll would
    // undo the click that caused it.
    if (typeof data.zoom === 'number' && data.zoom !== zoomWanted &&
        Date.now() - zoomWroteAt > ZOOM_SETTLE_MS) {
      zoomWanted = data.zoom;
      applyZoom(zoomWanted, false);
    }

    // The PC owns the wallpaper; adopt its choice whenever it differs.
    if (data.wallpaper && !sameChoice(data.wallpaper, Wallpaper.current())) {
      Wallpaper.apply(data.wallpaper);
      markActive(data.wallpaper);
    }

    // Before the layout, which addresses the GPU cards by id and therefore
    // needs them to exist. An older server sends one GPU and no list.
    var gpus = data.gpus;
    if (!gpus) gpus = data.gpu ? [data.gpu] : [];
    ensureGpuCards(gpus.length);

    // ...and the layout. Only re-applied when it actually changed, so the 1 Hz
    // poll does not rebuild the grid every second.
    if (Array.isArray(data.layout)) {
      var lk = layoutKey(data.layout);
      if (lk !== lastLayoutKey) applyLayout(data.layout);
    }

    var cpu = data.cpu || {};
    setText($('cpu-name'), cpu.name || ' ');
    setValue($('cpu-temp'), cpu.temp_c, CPU_HEAT, 0, 3);
    setNum($('cpu-load-text'), padTo(fmt(cpu.load, 0) + '%', 4));
    setBar($('cpu-load-bar'), cpu.load);
    setNum($('cpu-threads'), fmt(cpu.threads, 0));
    setCores(cpu.cores);

    sensorHint = (cpu.temp_c === null || cpu.temp_c === undefined)
      ? 'CPU temperature needs LibreHardwareMonitor running as administrator.'
      : '';
    showHint();

    // One card per GPU. A card with nothing behind it says so rather than
    // holding the last card's numbers.
    gpuIds.forEach(function (id, i) {
      paintGpu(id, gpus[i],
               gpus.length ? 'not detected' : 'needs nvidia-smi or LHM');
    });

    var ram = data.ram;
    if (ram) {
      setValue($('ram-used'), ram.used_gb, null, 1, 4);
      setNum($('ram-sub'), fmt(ram.total_gb, 0) + ' GB total');
      setNum($('ram-pct-text'), padTo(fmt(ram.percent, 0) + '%', 4));
      setBar($('ram-bar'), ram.percent, [75, 90]);
      setNum($('ram-free'), padTo(fmt(ram.total_gb - ram.used_gb, 1), 4));
    }

    setNum($('cpu-clock'), padTo(fmt(cpu.clock_mhz, 0), 4));
    setNum($('cpu-power'), padTo(fmt(cpu.power_w, 0), 3));

    paintCool(data);
    paintNet(data);
    renderDisks(data.disks);
  }

  // ---- drives -------------------------------------------------------------

  // One chip per drive. The row is only rebuilt when the SET of drives
  // changes (a share appearing or dropping); otherwise the numbers are
  // updated in place like every other value, so no nodes churn.
  var diskParts = {};

  function renderDisks(disks) {
    var row = $('disk-chips');
    if (!row || !disks) return;

    var key = disks.map(function (d) { return d.drive; }).join(',');
    if (row._key !== key) {
      row.innerHTML = '';
      diskParts = {};
      disks.forEach(function (d) {
        var chip = document.createElement('span');
        chip.className = 'chip' + (d.network ? ' chip-net' : '');
        var name = document.createElement('b');
        name.textContent = d.drive;
        var used = document.createElement('b');
        var total = document.createElement('span');
        // Temperature and remaining life, when LHM could match this letter to
        // a physical drive. Empty nodes otherwise, so the chip does not change
        // width when a reading appears.
        var temp = document.createElement('b');
        var life = document.createElement('span');
        chip.appendChild(name);
        chip.appendChild(document.createTextNode(' '));
        chip.appendChild(used);
        chip.appendChild(total);
        chip.appendChild(temp);
        chip.appendChild(life);
        row.appendChild(chip);
        diskParts[d.drive] = { used: used, total: total,
                               temp: temp, life: life };
      });
      row._key = key;
    }

    disks.forEach(function (d) {
      var p = diskParts[d.drive];
      if (!p) return;
      setNum(p.used, padTo(fmt(d.used_gb, 0), 4));
      setNum(p.total, '/' + fmt(d.total_gb, 0));
      // Only drives LHM can see have these, and a network share never will.
      setNum(p.temp, (d.temp_c === null || d.temp_c === undefined)
             ? '' : '  ' + fmt(d.temp_c, 0) + '°');
      setText(p.life, (d.life === null || d.life === undefined || d.life >= 100)
              ? '' : ' ' + fmt(d.life, 0) + '%');
    });
  }

  // ---- polling ------------------------------------------------------------

  function poll() {
    fetch('/api/stats', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(paint)
      .catch(function () { /* refreshStatus reports it from the timestamp */ });
  }

  // ---- screen wake lock ---------------------------------------------------

  // Safari 16.4+ supports this, so iPadOS 17 is fine. Without it the iPad
  // dims on its Auto-Lock timer and the dashboard goes dark.
  var wakeLock = null;

  function requestWakeLock() {
    if (!('wakeLock' in navigator)) return;
    navigator.wakeLock.request('screen').then(function (lock) {
      wakeLock = lock;
      lock.addEventListener('release', function () { wakeLock = null; });
    }).catch(function () {
      // Denied (usually because the page is not visible) - retried on focus.
    });
  }

  // ---- waking from sleep --------------------------------------------------

  // After the iPad sleeps the page comes back in a variety of states: the wake
  // lock is gone, the poll timer may have been throttled or stopped, the WebGL
  // context may have been discarded and the video may be stalled. Rather than
  // try to repair each one, anything gone quiet for long enough gets a clean
  // reload - it is a dashboard, there is no state worth preserving.
  var RELOAD_AFTER_MS = 90000;

  function wakeUp() {
    if (!wakeLock) requestWakeLock();

    // Same reasoning as refreshStatus: do not reload blind. Flag it and let
    // the next successful poll do the reload.
    if (everConnected && Date.now() - lastGood > RELOAD_AFTER_MS) {
      wasDownLong = true;
    }

    if (window.Wallpaper && Wallpaper.resume) Wallpaper.resume();
    poll();
    // The network usually needs a moment to come back with the WiFi radio.
    setTimeout(poll, 1200);
    setTimeout(poll, 4000);
  }

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) wakeUp();
  });

  // Safari can restore from its back/forward cache without firing
  // visibilitychange, so waking the iPad sometimes only lands here.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) wakeUp();
  });
  window.addEventListener('focus', wakeUp);
  window.addEventListener('online', wakeUp);

  // ---- wallpaper picker ---------------------------------------------------

  // The choice lives on the PC, not in this browser: the server owns it, and
  // every viewer follows whatever the PC last set. That is what lets the
  // wallpaper be changed from the tray menu without touching the iPad.

  function sameChoice(a, b) {
    if (!a || !b) return false;
    if (a.mode !== b.mode) return false;
    if (a.mode === 'shader') return true;
    return String(a.id) === String(b.id);
  }

  function markActive(choice) {
    var grid = $('pick-grid');
    if (!grid) return;
    Array.prototype.forEach.call(grid.children, function (tile) {
      var c = tile._choice;
      tile.classList.toggle('is-active', sameChoice(c, choice));
    });
  }

  function selectWallpaper(choice) {
    // Applied here straight away so the tap feels instant, then told to the
    // server, which is what other viewers pick up on their next poll.
    Wallpaper.retry(choice);
    Wallpaper.apply(choice);
    markActive(choice);
    var q = '?mode=' + encodeURIComponent(choice.mode) +
            '&id=' + encodeURIComponent(choice.id || '') +
            '&title=' + encodeURIComponent(choice.title || '');
    fetch('/api/wallpaper/select' + q, { cache: 'no-store' }).catch(function () {});
  }

  function tile(choice, label, badge, imgSrc, active, reason, full) {
    var el = document.createElement('button');
    el.type = 'button';
    if (full && full !== label) el.title = full;
    el.className = 'tile' + (active ? ' is-active' : '') +
                   (imgSrc ? '' : ' tile-shader') +
                   (reason ? ' is-blocked' : '');
    if (imgSrc) {
      var img = document.createElement('img');
      img.loading = 'lazy';
      img.alt = '';
      // No thumbnail could be made: fall back to the plain lettered tile
      // rather than showing a broken image.
      img.addEventListener('error', function () {
        el.classList.add('tile-shader');
        img.remove();
        if (!el.querySelector('.tile-fallback')) {
          var t = document.createElement('span');
          t.className = 'tile-fallback';
          t.textContent = label;
          el.insertBefore(t, el.firstChild);
        }
      });
      img.src = imgSrc;
      el.appendChild(img);
    } else {
      el.appendChild(document.createTextNode(label));
    }
    if (badge) {
      var b = document.createElement('span');
      b.className = 'tile-badge';
      b.textContent = badge;
      el.appendChild(b);
    }
    if (imgSrc) {
      var n = document.createElement('span');
      n.className = 'tile-name';
      n.textContent = label;
      el.appendChild(n);
    }
    if (reason) {
      el.disabled = true;
      el.title = reason;
      var r = document.createElement('span');
      r.className = 'tile-reason';
      r.textContent = reason;
      el.appendChild(r);
      return el;
    }
    el._choice = choice;
    el.addEventListener('click', function () { selectWallpaper(choice); });
    return el;
  }

  // Whether to draw the wallpapers that cannot be used. On by default: a
  // workshop library is mostly scene wallpapers, and fifty identical grey
  // tiles bury the handful that actually work. Kept per device rather than on
  // the server, because it changes what you look at, not what the PC shows.
  var hideUnusable = true;
  try {
    hideUnusable = localStorage.getItem('wpHideUnusable') !== '0';
  } catch (e) {}

  function heading(text, count) {
    var h = document.createElement('div');
    h.className = 'pick-heading';
    h.textContent = text;
    if (count) {
      var c = document.createElement('span');
      c.textContent = count;
      h.appendChild(c);
    }
    return h;
  }

  function renderSources(data) {
    var box = $('pick-sources');
    box.innerHTML = '';
    var list = [data.local_dir].concat(data.sources || []);
    list.forEach(function (path, idx) {
      if (!path) return;
      var chip = document.createElement('span');
      chip.className = 'source-chip';
      var label = document.createElement('span');
      label.textContent = path;
      chip.appendChild(label);
      // The folder beside the app is not removable; it is where the app
      // itself puts things, and losing it would leave no default at all.
      if (idx > 0) {
        var x = document.createElement('button');
        x.type = 'button';
        x.className = 'source-drop';
        x.textContent = '×';
        x.title = 'Stop scanning this folder';
        x.addEventListener('click', function () {
          fetch('/api/wallpaper/source/remove?path=' + encodeURIComponent(path),
                { cache: 'no-store' })
            .then(function () { buildPicker(); })
            .catch(function () {});
        });
        chip.appendChild(x);
      }
      box.appendChild(chip);
    });
  }

  function buildPicker() {
    fetch('/api/wallpapers', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var grid = $('pick-grid');
        grid.innerHTML = '';
        var active = Wallpaper.current();

        renderSources(data);

        var all = data.items || [];
        var usable = 0;
        all.forEach(function (i) { if (i.supported) usable++; });
        var shown = hideUnusable
          ? all.filter(function (i) { return i.supported; })
          : all;

        grid.appendChild(heading('Built-in', ''));
        grid.appendChild(tile(
          { mode: 'shader' }, 'Built-in nebula', 'shader', null,
          active.mode === 'shader'
        ));

        // Grouped under the folder each one came out of, so a drawer of six
        // Frieren loops reads as one section rather than six loose tiles.
        var order = [];
        var groups = {};
        shown.forEach(function (i) {
          var key = i.group || (i.source === 'local' ? 'Your folder'
                                                     : 'Wallpaper Engine');
          if (!groups[key]) { groups[key] = []; order.push(key); }
          groups[key].push(i);
        });
        // Your own folders first; the workshop is the long tail.
        order.sort(function (a, b) {
          var la = groups[a][0].source === 'local' ? 0 : 1;
          var lb = groups[b][0].source === 'local' ? 0 : 1;
          return la - lb || a.localeCompare(b);
        });

        order.forEach(function (key) {
          var list = groups[key];
          grid.appendChild(heading(key, list.length));
          list.forEach(function (i) {
            var choice = { mode: i.type, id: i.id, title: i.title };
            // A video with no artwork beside it still gets a thumbnail: the
            // server pulls a frame out of the file itself.
            var img = (i.preview || i.type === 'video')
              ? '/media/' + i.id + '/preview' : null;
            grid.appendChild(tile(
              choice, i.label || i.title, i.type || 'unknown', img,
              i.supported && active.mode === i.type && active.id === i.id,
              i.supported ? '' : (i.reason || 'Cannot be used here'),
              i.title
            ));
          });
        });

        var build = $('build') ? $('build').textContent : '?';
        var hidden = all.length - shown.length;
        setText($('pick-count'),
                usable + ' of ' + all.length + ' usable' +
                (hidden ? '  ·  ' + hidden + ' hidden' : '') +
                '  ·  build ' + build);

        var note = 'Drop pictures and videos into a folder above, or add '
                 + 'another - .jpg .png .gif .webp .mp4 .webm all work.';
        if (!data.ffmpeg) {
          note += ' ffmpeg was not found, so videos cannot be prepared; '
                + 'pictures still work.';
        }
        setText($('pick-note'), note);
      })
      .catch(function () {
        setText($('pick-note'), 'Could not read the wallpaper library.');
      });
  }

  $('pick-hide').checked = hideUnusable;
  $('pick-hide').addEventListener('change', function () {
    hideUnusable = this.checked;
    try { localStorage.setItem('wpHideUnusable', hideUnusable ? '1' : '0'); }
    catch (e) {}
    buildPicker();
  });

  function addSource() {
    var input = $('pick-path');
    var path = (input.value || '').trim();
    if (!path) return;
    setText($('pick-note'), 'adding ' + path + '...');
    fetch('/api/wallpaper/source/add?path=' + encodeURIComponent(path),
          { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.ok) { input.value = ''; buildPicker(); }
        else setText($('pick-note'), res.detail || 'could not add that folder');
      })
      .catch(function () { setText($('pick-note'), 'server unreachable'); });
  }

  $('pick-add').addEventListener('click', addSource);
  $('pick-path').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') addSource();
  });

  $('pick-open').addEventListener('click', function () {
    $('picker').hidden = false;
    buildPicker();
  });

  $('pick-close').addEventListener('click', function () {
    $('picker').hidden = true;
  });

  $('picker').addEventListener('click', function (e) {
    if (e.target === $('picker')) $('picker').hidden = true;   // tap outside
  });

  // ---- zoom ---------------------------------------------------------------

  // Drives the --s multiplier in style.css rather than browser zoom or a
  // transform: every size is recomputed, so the layout reflows and the text
  // is re-rasterised crisp instead of being scaled as pixels.
  // Zoom is owned by the server too, so it can be set from the PC's tray menu.
  // Two numbers matter: what was ASKED for, and what actually fitted. Only the
  // asked-for value is sent back, or the fit clamp would ratchet it down.
  var ZOOM_MIN = 0.6, ZOOM_MAX = 2.4, ZOOM_STEP = 0.1;
  var zoom = 1;          // applied, after the fit clamp
  var zoomWanted = 1;    // requested, what the server holds
  // A local change and the 1 Hz poll race: the poll can return the previous
  // server value after a click has already moved on, which drags the page
  // backwards and makes the next click compute from a stale base. Ignore the
  // server's value briefly after we write one.
  var zoomWroteAt = 0;
  var ZOOM_SETTLE_MS = 2500;

  function overflows() {
    var d = $('dash');
    // Reading scrollHeight forces a reflow, so this sees the new layout.
    return d.scrollHeight > d.clientHeight + 1;
  }

  function applyZoom(value, persist) {
    zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(value * 100) / 100));
    document.documentElement.style.setProperty('--s', String(zoom));

    // The dashboard never scrolls, so a zoom that does not fit would silently
    // clip the bottom card. Step back down until it does fit - the ceiling
    // depends on the display, so it cannot be a fixed number.
    var guard = 0;
    while (zoom > ZOOM_MIN && overflows() && guard++ < 40) {
      zoom = Math.round((zoom - ZOOM_STEP) * 100) / 100;
      document.documentElement.style.setProperty('--s', String(zoom));
    }

    setText($('zoom-reset'), Math.round(zoom * 100) + '%');
    if (persist) {
      zoomWroteAt = Date.now();
      fetch('/api/zoom/select?value=' + encodeURIComponent(zoomWanted),
            { cache: 'no-store' }).catch(function () {});
    }
  }

  function requestZoom(value) {
    zoomWanted = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(value * 100) / 100));
    applyZoom(zoomWanted, true);
  }

  // A rotation or a resolution change moves the ceiling, so re-fit. The grid
  // also switches between columns (landscape) and weighted rows (portrait).
  window.addEventListener('resize', function () { applyZoom(zoom, false); fitGrid(); });

  applyZoom(1, false);   // corrected by the first poll from the server

  $('zoom-in').addEventListener('click', function () { requestZoom(zoomWanted + ZOOM_STEP); });
  $('zoom-out').addEventListener('click', function () { requestZoom(zoomWanted - ZOOM_STEP); });
  $('zoom-reset').addEventListener('click', function () { requestZoom(1); });

  // ---- fullscreen ---------------------------------------------------------

  var root = document.documentElement;
  var canFullscreen = !!(root.requestFullscreen || root.webkitRequestFullscreen);

  function isFullscreen() {
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
  }

  function toggleFullscreen() {
    if (!canFullscreen) return;
    try {
      if (isFullscreen()) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
      } else {
        (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
      }
    } catch (e) { /* refused without a user gesture; the button retries */ }
  }

  // iPhone Safari has no element fullscreen, and an Add-to-Home-Screen launch
  // is already full screen - so only offer the button where it does something.
  if (canFullscreen && !window.navigator.standalone) {
    $('fs-toggle').hidden = false;
    $('fs-toggle').addEventListener('click', toggleFullscreen);
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'f' || e.key === 'F') toggleFullscreen();
    if (e.key === 'w' || e.key === 'W') $('pick-open').click();
    if (e.key === '+' || e.key === '=') requestZoom(zoomWanted + ZOOM_STEP);
    if (e.key === '-' || e.key === '_') requestZoom(zoomWanted - ZOOM_STEP);
    if (e.key === '0') requestZoom(1);
  });

  // ---- idle chrome --------------------------------------------------------

  var idleTimer = null;

  function wake() {
    document.body.classList.remove('is-idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(function () {
      if ($('picker').hidden && $('layout').hidden) {
        document.body.classList.add('is-idle');
      }
    }, 5000);
  }

  ['mousemove', 'touchstart', 'keydown', 'click'].forEach(function (evt) {
    document.addEventListener(evt, wake, { passive: true });
  });
  wake();

  // ---- debug overlay ------------------------------------------------------

  // Open with ?debug=1 (or press D) to measure layout movement ON THE DEVICE.
  // Three rounds of fixes measured clean in headless Chromium while the user
  // still saw shifting on the iPad, so the measurement has to run where the
  // problem is. Reports any element whose box moves, and by how much.
  function startDebug() {
    var watch = [
      ['time', '#time'], ['date', '.date'],
      ['bar', '.bar'], ['cards', '.cards'],
      ['cpuRead', '#card-cpu .readout'], ['cpuTemp', '#cpu-temp'],
      ['cpuUnit', '#card-cpu .unit'], ['cpuLoad', '#cpu-load-text'],
      ['gpuRead', '#card-gpu .readout'], ['gpuTemp', '#gpu-temp'],
      ['gpuUnit', '#card-gpu .unit'], ['ramUsed', '#ram-used'],
      ['chips', '#card-gpu .chips'], ['hint', '#hint']
    ];

    var box = document.createElement('div');
    box.id = 'debug-box';
    document.body.appendChild(box);

    var base = {}, worst = {}, samples = 0;
    var settleUntil = Date.now() + 3000;   // ignore initial layout settling
    var lastS = '', lastW = 0, lastH = 0;

    function reset() {
      base = {}; worst = {}; samples = 0;
      settleUntil = Date.now() + 1200;
    }

    // Tap the readout to re-baseline after deliberately changing something.
    box.style.pointerEvents = 'auto';
    box.addEventListener('click', reset);

    function rect(sel) {
      var el = document.querySelector(sel);
      if (!el) return null;
      var r = el.getBoundingClientRect();
      return [r.left, r.top, r.width, r.height];
    }

    function sample() {
      // Zoom changes and rotations legitimately move everything, so they must
      // re-baseline rather than be reported as drift.
      var sNow = getComputedStyle(document.documentElement)
                   .getPropertyValue('--s').trim();
      if (sNow !== lastS || window.innerWidth !== lastW ||
          window.innerHeight !== lastH) {
        lastS = sNow; lastW = window.innerWidth; lastH = window.innerHeight;
        reset();
      }
      if (Date.now() < settleUntil) { base = {}; return; }
      samples++;
      var lines = [];
      lines.push('build ' + (($('build') || {}).textContent || '?') +
                 '  dpr ' + (window.devicePixelRatio || 1) +
                 '  ' + window.innerWidth + 'x' + window.innerHeight);
      lines.push('--s ' + getComputedStyle(document.documentElement)
                   .getPropertyValue('--s').trim() +
                 '  fs ' + Math.round(parseFloat(
                   getComputedStyle(document.querySelector('.value')).fontSize)) + 'px' +
                 '  n=' + samples);

      var any = false;
      for (var i = 0; i < watch.length; i++) {
        var name = watch[i][0], r = rect(watch[i][1]);
        if (!r) continue;
        if (!base[name]) { base[name] = r; worst[name] = [0, 0, 0, 0]; continue; }
        for (var k = 0; k < 4; k++) {
          var d = Math.abs(r[k] - base[name][k]);
          if (d > worst[name][k]) worst[name][k] = d;
        }
        var w = worst[name];
        var m = Math.max(w[0], w[1], w[2], w[3]);
        if (m > 0.5) {
          any = true;
          lines.push(name + '  L' + w[0].toFixed(1) + ' T' + w[1].toFixed(1) +
                     ' W' + w[2].toFixed(1) + ' H' + w[3].toFixed(1));
        }
      }
      if (!any) lines.push('NOTHING HAS MOVED  (tap to reset)');
      else lines.push('(tap to reset)');
      box.textContent = lines.join(String.fromCharCode(10));
    }

    sample();
    setInterval(sample, 250);
  }

  if (/[?&]debug=1/.test(location.search)) startDebug();
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'd' || e.key === 'D') && !$('debug-box')) startDebug();
  });

  // ---- layout-shift watchdog ----------------------------------------------

  // Samples a few key rows every animation frame and posts anything that moves
  // back to the server. The display cannot be reached or filmed, and the shift
  // does not reproduce in headless Chromium, so the page has to be the one
  // that measures it. Cheap: four rects per frame, and it only reports when
  // something actually moved, at most once a minute.
  (function watchShifts() {
    // Track HEIGHTS too: the tops told us everything shifts by a multiple of
    // ~24.9px, which means something is changing height and the effect
    // accumulates down the page. This finds which element it is.
    var TARGETS = [
      ['dash',    '#dash'],
      ['cards',   '.cards'],
      ['hint',    '#hint'],
      ['bar',     '.bar'],
      ['cpuCard', '#card-cpu'],
      ['gpuCard', '#card-gpu'],
      ['ramCard', '#card-ram'],
      ['cpuHead', '#card-cpu .card-head'],
      ['cpuRead', '#card-cpu .readout'],
      ['cpuSide', '#card-cpu .side'],
      ['cpuSub',  '#cpu-name'],
      ['gpuHead', '#card-gpu .card-head'],
      ['gpuRead', '#card-gpu .readout'],
      ['gpuSide', '#card-gpu .side'],
      ['gpuMet',  '#card-gpu .meters'],
      ['gpuChip', '#card-gpu .chips'],
      ['ramHead', '#card-ram .card-head'],
      ['ramRead', '#card-ram .readout'],
      ['ramSide', '#card-ram .side'],
      ['ramChip', '#card-ram .chips']
    ];
    var base = {}, worst = {}, frames = 0, reportedAt = 0;
    var loadedAt = Date.now();
    var snapshot = null;
    var SETTLE_MS = 8000;

    function flush() {
      var bad = [];
      for (var k in worst) {
        var w = worst[k];
        if (w.hd > 0.5) {
          bad.push(k + ' HEIGHT ' + w.hlo.toFixed(2) + '..' + w.hhi.toFixed(2) +
                   ' (d' + w.hd.toFixed(2) + ')');
        } else if (w.d > 0.5) {
          bad.push(k + ' top d' + w.d.toFixed(2));
        }
      }
      if (bad.length && Date.now() - reportedAt > 20000) {
        reportedAt = Date.now();
        try {
          fetch('/api/diag', {
            method: 'POST',
            body: JSON.stringify({
              ua: navigator.userAgent,
              dpr: window.devicePixelRatio,
              vw: window.innerWidth, vh: window.innerHeight,
              s: getComputedStyle(document.documentElement).getPropertyValue('--s').trim(),
              build: myBuild, frames: frames,
              aliveSec: Math.round((Date.now() - loadedAt) / 1000),
              moved: bad, snap: snapshot
            })
          }).catch(function () {});
        } catch (e) {}
      }
      base = {}; worst = {}; frames = 0; snapshot = null;
    }

    var windowStart = Date.now();
    var lastW = window.innerWidth, lastH = window.innerHeight;
    function tick() {
      requestAnimationFrame(tick);
      if (Date.now() - loadedAt < SETTLE_MS) { base = {}; worst = {}; return; }
      // A resize or a rotation legitimately moves everything - including a
      // headless test harness changing the viewport. Re-baseline rather than
      // report it, exactly as the debug overlay does. Without this, rotating
      // the iPad would post a full-screen "movement" report and look like the
      // jitter bug returned.
      if (window.innerWidth !== lastW || window.innerHeight !== lastH) {
        lastW = window.innerWidth;
        lastH = window.innerHeight;
        base = {}; worst = {}; frames = 0; snapshot = null;
        return;
      }
      frames++;
      for (var i = 0; i < TARGETS.length; i++) {
        var el = document.querySelector(TARGETS[i][1]);
        if (!el) continue;
        var r = el.getBoundingClientRect();
        var k = TARGETS[i][0];
        if (!base[k]) {
          base[k] = { t: r.top, h: r.height };
          worst[k] = { d: 0, hd: 0, hlo: r.height, hhi: r.height };
          continue;
        }
        var d = Math.abs(r.top - base[k].t);
        if (d > worst[k].d) worst[k].d = d;
        var hd = Math.abs(r.height - base[k].h);
        if (hd > worst[k].hd) worst[k].hd = hd;
        if (r.height < worst[k].hlo) worst[k].hlo = r.height;
        if (r.height > worst[k].hhi) worst[k].hhi = r.height;

        // When the CPU card reaches a new maximum height, photograph the whole
        // layout in that same frame. Independent min/max per element loses the
        // correlation, and the correlation is the whole question: which
        // element is actually tall at the moment the card is tall?
        if (k === 'cpuCard' && r.height > base[k].h + 1 && !snapshot) {
          snapshot = {};
          for (var j = 0; j < TARGETS.length; j++) {
            var e2 = document.querySelector(TARGETS[j][1]);
            if (!e2) continue;
            var r2 = e2.getBoundingClientRect();
            snapshot[TARGETS[j][0]] = +r2.height.toFixed(1);
          }
          snapshot.__text = {
            cpuSub: (document.getElementById('cpu-name') || {}).textContent,
            hint: (document.getElementById('hint') || {}).textContent
          };
          snapshot.__baseline = {};
          for (var b in base) snapshot.__baseline[b] = +base[b].h.toFixed(1);
        }
      }
      if (Date.now() - windowStart > 15000) { windowStart = Date.now(); flush(); }
    }
    requestAnimationFrame(tick);
  })();

  // ---- layout -------------------------------------------------------------

  // Which cards and rows are shown, and in what order. Owned by the PC like
  // the wallpaper and zoom: the page adopts whatever /api/stats carries and
  // writes edits back to /api/layout/select.
  //
  // The grid is the one place this can regress the "numbers jump" fix, so the
  // template is always regenerated from the VISIBLE cards as explicit fr
  // fractions - never auto rows. A hidden card is display:none and therefore
  // not a grid item at all; that is why the column count and the portrait row
  // fractions are rebuilt here rather than left to CSS.
  // Every GPU card shares the GPU's rows, weight and labels, so these stay
  // keyed by KIND while the ids themselves depend on how many GPUs there are.
  var CARD_LABEL = { cpu: 'CPU', gpu: 'GPU', ram: 'Memory',
                     cool: 'Cooling', net: 'Network' };
  // Portrait row weights: the GPU card carries more rows, so it gets more room.
  var CARD_WEIGHT = { cpu: 0.78, gpu: 1.15, ram: 1.07, cool: 0.95, net: 0.95 };

  var ROW_IDS = {
    cpu: ['temp', 'load', 'cores', 'clocks'],
    gpu: ['temp', 'load', 'vram', 'chips', 'clocks'],
    ram: ['temp', 'load', 'free', 'drives'],
    cool: ['temp', 'fans', 'board'],
    net: ['rate', 'load', 'totals']
  };
  var ROW_LABEL = {
    cpu: { temp: 'Temperature', load: 'Load meter', cores: 'Per-core square',
           clocks: 'Clock / Package power' },
    gpu: { temp: 'Temperature', load: 'Load meter', vram: 'VRAM meter',
           chips: 'Power / Fan / Junction', clocks: 'Core / Memory clock' },
    ram: { temp: 'Temperature', load: 'In-use meter', free: 'Free GB',
           drives: 'Drive chips' },
    cool: { temp: 'System temperature', fans: 'Fan speeds',
            board: 'Board temperature chips' },
    net: { rate: 'Download speed', load: 'Utilisation meter',
           totals: 'Upload and session totals' }
  };

  // What a fresh dashboard shows, and what a card gets when it is ticked back
  // on. ROW_IDS is everything available; this is everything on by default.
  // Mirrors DEFAULT_CARDS / DEFAULT_ROWS on the server - the page only uses
  // these before the first poll and for the Reset button, but the two must
  // agree or Reset would flip rows the server would then flip back.
  var DEFAULT_CARDS = ['cpu', 'gpu', 'ram'];
  var DEFAULT_ROWS = {
    cpu: ['temp', 'load', 'cores'],
    gpu: ['temp', 'load', 'vram', 'chips'],
    ram: ['temp', 'load', 'free', 'drives'],
    cool: ['temp', 'fans', 'board'],
    net: ['rate', 'load', 'totals']
  };

  function cardIds() {
    return ['cpu'].concat(gpuIds, ['ram', 'cool', 'net']);
  }

  function cardLabel(id) {
    return isGpu(id) ? gpuLabel(id) : (CARD_LABEL[id] || id);
  }

  function rowIds(id) {
    return ROW_IDS[kindOf(id)] || [];
  }

  function defaultRows(id) {
    return DEFAULT_ROWS[kindOf(id)] || [];
  }
  function defaultLayout() {
    return cardIds().filter(function (id) {
      // Only the first GPU, and none of the cards that are offered rather
      // than shown.
      if (isGpu(id)) return id === 'gpu';
      return DEFAULT_CARDS.indexOf(id) !== -1;
    }).map(function (id) {
      return { id: id, rows: defaultRows(id).slice() };
    });
  }

  var layoutState = defaultLayout();
  var lastLayoutKey = '';

  function layoutKey(layout) {
    return layout.map(function (c) {
      return c.id + ':' + ((c.rows || []).join('.'));
    }).join('|');
  }

  function indexOfCard(id) {
    for (var i = 0; i < layoutState.length; i++) {
      if (layoutState[i].id === id) return i;
    }
    return -1;
  }

  function fitGrid() {
    var cards = document.querySelector('.cards');
    if (!cards) return;
    var visible = layoutState.map(function (c) { return c.id; });
    if (!visible.length) return;
    // Same breakpoint as the CSS media query, so the two never disagree.
    // minmax(0, Xfr) everywhere: a bare fr is minmax(auto, fr) and a wide
    // min-content would break the equal columns / fixed rows.
    if (window.matchMedia('(max-aspect-ratio: 1/1)').matches) {
      if (visible.length > 4) {
        // Six cards stacked in portrait leave each about 150px, which clips the
        // meters and chips. Two columns instead - the card puts its number
        // beside its meters, so it reads fine at half width and would rather
        // have the height.
        cards.style.gridTemplateColumns = 'repeat(2, minmax(0, 1fr))';
        cards.style.gridTemplateRows =
          'repeat(' + Math.ceil(visible.length / 2) + ', minmax(0, 1fr))';
      } else {
        cards.style.gridTemplateColumns = 'minmax(0, 1fr)';
        cards.style.gridTemplateRows = visible.map(function (id) {
          return 'minmax(0, ' + (CARD_WEIGHT[kindOf(id)] || 1) + 'fr)';
        }).join(' ');
      }
      cards.style.gridAutoRows = '0';
    } else if (visible.length > 4) {
      // Five or six cards in one row gives each about 170px on the iPad, which
      // is narrower than the headline number. Two rows instead: still explicit
      // fractions, so the columns stay equal and the numbers still cannot jump.
      var cols = Math.ceil(visible.length / 2);
      cards.style.gridTemplateColumns =
        'repeat(' + cols + ', minmax(0, 1fr))';
      cards.style.gridTemplateRows = 'repeat(2, minmax(0, 1fr))';
      cards.style.gridAutoRows = '0';
    } else {
      cards.style.gridTemplateColumns =
        'repeat(' + visible.length + ', minmax(0, 1fr))';
      cards.style.gridTemplateRows = 'minmax(0, 1fr)';
      cards.style.gridAutoRows = '0';
    }
  }

  function applyLayout(layout) {
    if (!Array.isArray(layout) || !layout.length) return;
    layoutState = layout.map(function (c) {
      return { id: c.id, rows: (c.rows || []).slice() };
    });
    lastLayoutKey = layoutKey(layoutState);

    var present = {};
    layoutState.forEach(function (c, i) {
      present[c.id] = true;
      var card = $('card-' + c.id);
      if (!card) return;
      card.hidden = false;
      card.style.order = String(i);
      rowIds(c.id).forEach(function (row) {
        var el = card.querySelector('[data-row="' + row + '"]');
        if (el) el.hidden = c.rows.indexOf(row) === -1;
      });
    });
    cardIds().forEach(function (id) {
      if (present[id]) return;
      var card = $('card-' + id);
      if (card) card.hidden = true;
    });

    refreshGpuHeadings();
    fitGrid();
  }

  function saveLayout() {
    applyLayout(layoutState);
    buildLayoutEditor();
    try {
      fetch('/api/layout/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(layoutState)
      }).catch(function () {});
    } catch (e) { /* older Safari without fetch body support: keep local */ }
  }

  // ---- layout editor ------------------------------------------------------

  function moveCard(id, dir) {
    var i = indexOfCard(id), j = i + dir;
    if (i < 0 || j < 0 || j >= layoutState.length) return;
    var tmp = layoutState[i];
    layoutState[i] = layoutState[j];
    layoutState[j] = tmp;
    saveLayout();
  }

  function hideCard(id) {
    if (layoutState.length <= 1) return;   // never leave the dashboard empty
    var i = indexOfCard(id);
    if (i < 0) return;
    layoutState.splice(i, 1);
    saveLayout();
  }

  function showCard(id) {
    if (indexOfCard(id) >= 0) return;
    // A GPU card comes back beside the other GPU cards rather than after the
    // memory card: that is where it was, and where it is expected.
    var at = -1;
    if (isGpu(id)) {
      layoutState.forEach(function (c, i) {
        if (isGpu(c.id)) at = i + 1;
      });
    }
    var entry = { id: id, rows: defaultRows(id).slice() };
    if (at < 0) layoutState.push(entry);
    else layoutState.splice(at, 0, entry);
    saveLayout();
  }

  function toggleRow(id, row) {
    var i = indexOfCard(id);
    if (i < 0) return;
    var rows = layoutState[i].rows;
    var k = rows.indexOf(row);
    if (k >= 0) rows.splice(k, 1); else rows.push(row);
    // Keep the canonical order so the page matches what the server stores.
    layoutState[i].rows = rowIds(id).filter(function (r) {
      return rows.indexOf(r) !== -1;
    });
    saveLayout();
  }

  function lbtn(label, title, handler) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'lbtn';
    b.textContent = label;
    if (title) b.title = title;
    b.addEventListener('click', handler);
    return b;
  }

  function buildLayoutEditor() {
    var list = $('layout-list');
    if (!list) return;
    list.innerHTML = '';
    var present = {};

    layoutState.forEach(function (card, idx) {
      present[card.id] = true;
      var group = document.createElement('div');
      group.className = 'lgroup';

      var head = document.createElement('div');
      head.className = 'lgroup-head';
      var up = lbtn('\u2191', 'Move up', function () { moveCard(card.id, -1); });
      var down = lbtn('\u2193', 'Move down', function () { moveCard(card.id, 1); });
      up.disabled = idx === 0;
      down.disabled = idx === layoutState.length - 1;
      var name = document.createElement('span');
      name.className = 'lname';
      name.textContent = cardLabel(card.id);
      var vis = lbtn('\u2713', 'Hide this card', function () { hideCard(card.id); });
      vis.setAttribute('aria-pressed', 'true');
      vis.disabled = layoutState.length <= 1;
      head.appendChild(up);
      head.appendChild(down);
      head.appendChild(name);
      head.appendChild(vis);
      group.appendChild(head);

      rowIds(card.id).forEach(function (row) {
        var on = card.rows.indexOf(row) !== -1;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'lrow';
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        var box = document.createElement('span');
        box.className = 'box';
        box.textContent = '\u2713';
        var label = document.createElement('span');
        label.textContent = (ROW_LABEL[kindOf(card.id)] || {})[row] || row;
        btn.appendChild(box);
        btn.appendChild(label);
        btn.addEventListener('click', function () { toggleRow(card.id, row); });
        group.appendChild(btn);
      });

      list.appendChild(group);
    });

    // Cards that are currently hidden get their own group, so they can be
    // brought back without touching the tray.
    var hidden = cardIds().filter(function (id) { return !present[id]; });
    if (hidden.length) {
      var group = document.createElement('div');
      group.className = 'lgroup';
      var head = document.createElement('div');
      head.className = 'lgroup-head';
      var name = document.createElement('span');
      name.className = 'lname';
      name.textContent = 'Hidden cards';
      head.appendChild(name);
      group.appendChild(head);
      hidden.forEach(function (id) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'lrow';
        btn.setAttribute('aria-pressed', 'false');
        var box = document.createElement('span');
        box.className = 'box';
        var label = document.createElement('span');
        label.textContent = cardLabel(id);
        btn.appendChild(box);
        btn.appendChild(label);
        btn.addEventListener('click', function () { showCard(id); });
        group.appendChild(btn);
      });
      list.appendChild(group);
    }
  }

  $('layout-open').addEventListener('click', function () {
    $('layout').hidden = false;
    buildLayoutEditor();
  });

  $('layout-close').addEventListener('click', function () {
    $('layout').hidden = true;
  });

  $('layout').addEventListener('click', function (e) {
    if (e.target === $('layout')) $('layout').hidden = true;   // tap outside
  });

  $('layout-reset').addEventListener('click', function () {
    layoutState = defaultLayout();
    saveLayout();
  });

  applyLayout(layoutState);   // corrected by the first poll from the server

  // ---- go -----------------------------------------------------------------

  tickClock();
  setInterval(tickClock, 1000);

  // Wallpaper progress shares the hint line under the cards.
  Wallpaper.onStatus(function (msg) {
    wallpaperHint = msg || '';
    showHint();
  });
  Wallpaper.apply({ mode: 'shader' });

  poll();
  setInterval(poll, POLL_MS);
  setInterval(refreshStatus, 1000);

  requestWakeLock();

  // A tap anywhere re-arms the wake lock if iOS dropped it.
  document.addEventListener('touchend', function () {
    if (!wakeLock) requestWakeLock();
  }, { passive: true });
})();
