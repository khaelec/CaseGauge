/* Wallpaper renderer with four modes.
 *
 *   shader  a WebGL nebula drawn on the iPad's own GPU (the default)
 *   video   a Wallpaper Engine video loop, transcoded PC-side to iPad size
 *   web     a Wallpaper Engine web wallpaper, in an iframe behind an API shim
 *   image   a still from the local folder, served as-is
 *
 * Only one is live at a time; switching tears the previous one down so a
 * paused video or a hidden iframe is never left burning battery.
 *
 * To change the built-in shader, replace FRAGMENT_SRC. It gets u_res and
 * u_time, the same contract as a Shadertoy fragment shader.
 */
window.Wallpaper = (function () {
  'use strict';

  // Budget in backing-store pixels rather than a fixed edge, so the shader
  // renders at a sensible size on any panel and the GPU cost stays roughly
  // constant whatever the aspect ratio.
  //
  // This was 640px on the long edge, chosen when the iPad was fed a 960x1280
  // spacedesk stream. Viewed directly the panel is 2048x1536 at devicePixelRatio
  // 2, so 640px meant a 3.2x upscale - visibly soft. 700k pixels lands near
  // native CSS resolution, about 2.3x the old detail, while staying well
  // inside what an A10 will hold at 30fps without heat-soaking.
  var PIXEL_BUDGET = 700000;
  var TARGET_FPS = 30;

  var FRAGMENT_SRC = [
    'precision mediump float;',
    'uniform vec2 u_res;',
    'uniform float u_time;',

    'float hash(vec2 p){',
    '  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);',
    '}',

    'float noise(vec2 p){',
    '  vec2 i = floor(p), f = fract(p);',
    '  vec2 u = f * f * (3.0 - 2.0 * f);',
    '  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),',
    '             mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y);',
    '}',

    'float fbm(vec2 p){',
    '  float v = 0.0, a = 0.5;',
    '  for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.02; a *= 0.5; }',
    '  return v;',
    '}',

    'void main(){',
    '  vec2 uv = gl_FragCoord.xy / u_res;',
    '  vec2 p = uv * 2.4;',
    '  p.x *= u_res.x / u_res.y;',
    '  float t = u_time * 0.055;',

    '  vec2 q = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(5.2, 1.3 - t)));',
    '  vec2 r = vec2(fbm(p + 3.6 * q + vec2(1.7, 9.2) + t * 0.45),',
    '                fbm(p + 3.6 * q + vec2(8.3, 2.8) - t * 0.38));',
    '  float f = fbm(p + 3.6 * r);',

    '  vec3 base   = vec3(0.016, 0.026, 0.075);',
    '  vec3 blue   = vec3(0.090, 0.150, 0.420);',
    '  vec3 violet = vec3(0.330, 0.150, 0.520);',
    '  vec3 teal   = vec3(0.040, 0.330, 0.430);',

    '  vec3 col = mix(base, blue, clamp(f * f * 1.9, 0.0, 1.0));',
    '  col = mix(col, violet, clamp(length(q) * 0.62, 0.0, 1.0));',
    '  col = mix(col, teal,   clamp(r.x * 0.50, 0.0, 1.0));',
    '  col *= 0.72 + 0.55 * f;',

    '  col += (hash(gl_FragCoord.xy) - 0.5) / 255.0;',
    '  gl_FragColor = vec4(col, 1.0);',
    '}'
  ].join('\n');

  var VERTEX_SRC = [
    'attribute vec2 a_pos;',
    'void main(){ gl_Position = vec4(a_pos, 0.0, 1.0); }'
  ].join('\n');

  var canvas = document.getElementById('wallpaper');
  var video = document.getElementById('wallpaper-video');
  var frame = document.getElementById('wallpaper-web');
  var still = document.getElementById('wallpaper-image');

  // iOS throws the WebGL context away while the device sleeps. Without these
  // the shader silently never draws again after waking - the canvas just
  // freezes on its last frame.
  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault();          // required, or the context is never restored
    running = false;
    gl = null; prog = null; uRes = null; uTime = null;
    curW = curH = -1;              // force a full re-setup on the new context
    canvas.dataset.gl = 'lost';
  }, false);

  canvas.addEventListener('webglcontextrestored', function () {
    canvas.dataset.gl = 'restored';
    if (current.mode === 'shader') startShader();
  }, false);

  var gl = null, prog = null, uRes = null, uTime = null;
  // Tracked here rather than read back from canvas.width, because after a
  // context restore the canvas keeps its old dimensions while the new context
  // has none of the state - so comparing against the canvas would skip the
  // viewport and u_res setup and leave the shader dividing by zero.
  var curW = -1, curH = -1;
  var running = false, start = 0, last = 0;
  var minDelta = 1000 / TARGET_FPS;
  var current = { mode: 'shader' };

  // Wallpapers whose file turned out to be missing. The PC owns the choice and
  // re-asserts it on every 1 Hz poll, so `current` has to keep pointing at the
  // dead wallpaper - clearing it would make the poll re-apply it a second
  // later, forever. Instead the shader is drawn in its place.
  var missing = {};
  function keyOf(c) { return c.mode + ':' + (c.id || ''); }
  var onStatus = function () {};

  // ---- shader ------------------------------------------------------------

  function compile(type, src) {
    var sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      console.error('shader:', gl.getShaderInfoLog(sh));
      return null;
    }
    return sh;
  }

  function initGL() {
    if (gl) return true;
    gl = canvas.getContext('webgl', {
      alpha: false, antialias: false, depth: false,
      stencil: false, powerPreference: 'low-power'
    });
    if (!gl) {
      document.body.style.background =
        'radial-gradient(ellipse at 30% 20%, #1b2352, #05070d 70%)';
      return false;
    }

    var vs = compile(gl.VERTEX_SHADER, VERTEX_SRC);
    var fs = compile(gl.FRAGMENT_SHADER, FRAGMENT_SRC);
    if (!vs || !fs) return false;

    prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.error('link:', gl.getProgramInfoLog(prog));
      return false;
    }
    gl.useProgram(prog);

    // One full-screen triangle: cheaper than a quad, and no centre seam.
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, 'a_pos');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

    uRes = gl.getUniformLocation(prog, 'u_res');
    uTime = gl.getUniformLocation(prog, 'u_time');
    return true;
  }

  function resize() {
    var w = canvas.clientWidth || window.innerWidth;
    var h = canvas.clientHeight || window.innerHeight;
    // Aim for the device's real pixels, then scale back to fit the budget.
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var want = w * dpr * h * dpr;
    var scale = dpr * Math.min(1, Math.sqrt(PIXEL_BUDGET / Math.max(1, want)));
    var bw = Math.max(1, Math.round(w * scale));
    var bh = Math.max(1, Math.round(h * scale));
    if (curW !== bw || curH !== bh) {
      curW = bw; curH = bh;
      canvas.width = bw;
      canvas.height = bh;
      gl.viewport(0, 0, bw, bh);
      gl.uniform2f(uRes, bw, bh);
    }
  }

  function tick(now) {
    if (!running) return;
    requestAnimationFrame(tick);
    if (now - last < minDelta) return;   // throttle: the A10 stays cool
    last = now;
    if (!gl || gl.isContextLost()) { running = false; canvas.dataset.gl = 'stalled'; return; }
    // Checked per frame rather than on resize events, which can fire before
    // the new layout exists and leave the shader stretched after a rotation.
    resize();
    gl.uniform1f(uTime, (now - start) / 1000);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function startShader() {
    if (!initGL()) { canvas.dataset.gl = 'init-failed'; return; }
    canvas.hidden = false;
    resize();
    if (!running) {
      running = true;
      start = start || performance.now();
      last = 0;
      requestAnimationFrame(tick);
    }
    canvas.dataset.gl = 'running';
  }

  function stopShader() {
    running = false;
    canvas.hidden = true;
  }

  // ---- teardown ----------------------------------------------------------

  function clearVideo() {
    video.pause();
    video.removeAttribute('src');
    video.load();          // drops the buffered data instead of holding it
    video.hidden = true;
  }

  function clearWeb() {
    frame.hidden = true;
    frame.removeAttribute('src');
  }

  function clearImage() {
    still.hidden = true;
    still.removeAttribute('src');
  }

  function stopAll() {
    stopShader();
    clearVideo();
    clearWeb();
    clearImage();
  }

  // ---- video -------------------------------------------------------------

  function playVideo(item) {
    // Ask the server to transcode it, then poll until the file exists.
    onStatus('preparing ' + item.title + '...');

    var tries = 0;
    function poll() {
      fetch('/api/wallpaper/' + item.id + '/prepare', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (st) {
          if (current.mode !== 'video' || current.id !== item.id) return;

          if (st.state === 'done') {
            onStatus('');
            video.hidden = false;
            video.src = '/media/' + item.id + '/video';
            var p = video.play();
            if (p && p.catch) p.catch(function () {
              onStatus('tap to start ' + item.title);
            });
            return;
          }
          if (st.state === 'error') {
            onStatus('could not prepare: ' + (st.detail || 'unknown error'));
            return;
          }
          tries++;
          onStatus('transcoding ' + item.title + '... ' + tries + 's');
          setTimeout(poll, 1000);
        })
        .catch(function () {
          onStatus('server unreachable');
        });
    }
    poll();
  }

  // ---- api ---------------------------------------------------------------

  function apply(choice) {
    choice = choice || { mode: 'shader' };
    current = choice;
    stopAll();
    // Drives the card scrim: photographic wallpapers are far brighter and
    // busier than the built-in shader, and glass that reads well over the
    // nebula washes out completely over video.
    document.body.dataset.wp = choice.mode;

    if (missing[keyOf(choice)]) {
      document.body.dataset.wp = 'shader';
      startShader();
      return;
    }

    if (choice.mode === 'video') {
      playVideo(choice);
    } else if (choice.mode === 'web') {
      onStatus('');
      frame.hidden = false;
      frame.src = '/media/' + choice.id + '/web';
    } else if (choice.mode === 'image') {
      // Nothing to transcode and nothing to poll for - it is already a file
      // the browser can read.
      onStatus('');
      still.hidden = false;
      still.src = '/media/' + choice.id + '/image';
    } else {
      onStatus('');
      startShader();
    }
  }

  // Don't animate a screen nobody is looking at.
  function resume() {
    if (current.mode === 'shader') {
      // startShader re-creates the context if it was lost.
      startShader();
    } else if (current.mode === 'video') {
      if (video.error || video.readyState === 0) {
        apply(current);      // came back broken - rebuild it from scratch
        return;
      }
      var p = video.play();
      if (p && p.catch) p.catch(function () {});
    } else if (current.mode === 'web' && !frame.getAttribute('src')) {
      apply(current);
    }
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      if (current.mode === 'shader') stopShader();
      if (current.mode === 'video') video.pause();
    } else {
      resume();
    }
  });

  // Safari restores from its back/forward cache without firing
  // visibilitychange, so waking the iPad can land here instead.
  window.addEventListener('pageshow', resume);
  window.addEventListener('focus', resume);

  video.addEventListener('error', function () {
    if (current.mode === 'video') setTimeout(function () { apply(current); }, 2000);
  });

  // A still that will not load has almost always been deleted or renamed in
  // the wallpapers folder, which is a thing people do now that the folder is
  // theirs to manage. Retrying cannot bring it back, so fall back to the
  // shader rather than leaving the screen empty.
  still.addEventListener('error', function () {
    if (current.mode !== 'image') return;
    missing[keyOf(current)] = true;
    onStatus('that picture is gone - back to the nebula');
    apply(current);          // same choice, now drawn as the shader
  });

  return {
    resume: resume,
    apply: apply,
    current: function () { return current; },
    // Picking a wallpaper by hand is a fair claim that it is back.
    retry: function (choice) { delete missing[keyOf(choice)]; },
    onStatus: function (fn) { onStatus = fn; }
  };
})();
