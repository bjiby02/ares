/* ARES console. Polls /v1/incidents and draws the global situation. */
(function () {
  "use strict";

  var TOKEN = new URLSearchParams(location.search).get("token") || "";
  var RESPONDER = new URLSearchParams(location.search).get("as") || "dispatch";
  var $ = function (id) { return document.getElementById(id); };

  var LAYERS = [
    ["CALLER REPORTS", "am", 1], ["SWARM IGNITION", "rd", 1], ["NO-GO CONES", "rd", 1],
    ["ROUTED CALLERS", "cy", 1], ["ALERTCA CAMERAS", "vi", 1], ["VIIRS THERMAL", "dm", 0],
    ["NWS WIND", "cy", 1], ["NIFC PERIMETERS", "am", 1], ["EMERGENCY BCAST", "dm", 0],
    ["ROAD CLOSURES", "dm", 0], ["HOSPITAL CAPACITY", "dm", 0]
  ];

  var SITU = [];        // alert rows derived from the live situation feed

  // ---- static rails -------------------------------------------------------
  $("layers").innerHTML = LAYERS.map(function (l) {
    return '<label class="row ' + (l[2] ? "on" : "") + '"><input type="checkbox" class="' +
      l[1] + '"' + (l[2] ? " checked" : "") + '><span>' + l[0] + "</span></label>";
  }).join("");
  $("sats").innerHTML = '<div class="mono dm" style="font-size:8px">awaiting feed</div>';
  $("feeds").innerHTML = '<div class="mono dm" style="font-size:8px">awaiting feed</div>';
  function drawCams(list) {
    var cams = (list && list.length) ? list.slice(0, 10) : [];
    if (!cams.length) {
      $("cams").innerHTML = '<div class="mono dm" style="font-size:8px;padding:10px">' +
        "no camera feed configured</div>";
      return;
    }
    $("cams").innerHTML = cams.map(function (c, i) {
      var hot = i < 2;
      var href = c.url ? ' onclick="window.open(\'' + c.url + '\')" style="cursor:pointer"' : "";
      return '<div class="cam' + (hot ? " hot" : "") + '"' + href + '><i>LIVE</i><b>' +
        esc(c.name) + (c.km != null ? " &middot; " + c.km + "km" : "") + "</b>" +
        (hot ? "<s></s>" : "") + "</div>";
    }).join("");
  }
  drawCams(null);

  var g = $("grat"), gr = "";
  for (var lon = 0; lon <= 360; lon += 30) gr += '<line class="grat" x1="' + lon + '" y1="0" x2="' + lon + '" y2="180"/>';
  for (var lat = 30; lat <= 150; lat += 30) gr += '<line class="grat" x1="0" y1="' + lat + '" x2="360" y2="' + lat + '"/>';
  g.innerHTML = gr;

  // ---- helpers ------------------------------------------------------------
  function proj(lon, lat) { return [lon + 180, 90 - lat]; }
  function esc(s) { return String(s == null ? "" : s).replace(/[<>&]/g, function (m) {
    return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[m]; }); }
  function ago(iso) {
    if (!iso) return "-";
    var s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    return s < 60 ? Math.floor(s) + "s" : Math.floor(s / 60) + ":" + ("0" + Math.floor(s % 60)).slice(-2);
  }
  var PRI = { 0: ["P0", "rd"], 1: ["P1", "am"], 2: ["P2", "cy"], 3: ["P3", "dm"] };

  // ---- render -------------------------------------------------------------
  function draw(items) {
    var pins = "", alerts = [], open = 0, esc_ = 0, trapped = 0, refused = 0;

    items.forEach(function (it) {
      var pr = PRI[it.priority] || PRI[2];
      if (it.status !== "closed") open++;
      if (it.status === "escalated") { esc_++; trapped++; }
      if (it.instruction && /will not send you/.test(it.instruction)) refused++;

      if (it.lat != null && it.lon != null) {
        var p = proj(it.lon, it.lat);
        var col = it.status === "escalated" ? "#ff4d3d"
                : it.hazard ? "#f2a33c" : "#5ce65c";
        if (it.status === "escalated") {
          pins += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="4" fill="#ff4d3d" opacity=".16"/>' +
                  '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="2.4" fill="#ff4d3d" opacity=".3"/>';
        }
        pins += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="1.1" fill="' + col + '"/>';
      }
      alerts.push({
        t: ago(it.updated_at), src: "ARES", col: pr[1],
        m: "#" + it.id + " " + (it.status || "") + " · " +
           (it.place_name || "place unknown") + (it.hazard ? " · hazard " + it.hazard : "")
      });
    });

    // Feed rows come from real sources only. SITU is filled by the situation poll.
    alerts = alerts.concat(SITU);

    $("pins").innerHTML = pins;
    $("alerts").innerHTML = alerts.slice(0, 9).map(function (a) {
      return '<div class="f" style="border-left-color:var(--' + (a.col === "dm" ? "edge" : a.col) + ')">' +
        '<span class="t">' + esc(a.t) + '</span><span class="s ' + a.col + '">' + esc(a.src) +
        '</span><span class="m">' + esc(a.m) + "</span></div>";
    }).join("");

    var handled = items.length, absorbed = handled ? (100 * (handled - esc_) / handled) : 100;
    $("nHandled").textContent = handled;
    $("nEsc").textContent = esc_;
    $("nAbs").textContent = absorbed.toFixed(1) + "%";
    $("mapstat").textContent = open + " OPEN · " + trapped + " TRAPPED · " + refused + " REFUSED";

    var active = items.filter(function (i) { return i.status !== "closed"; })[0];
    $("brief").innerHTML =
      (active
        ? '<p><b class="cy">INCIDENT ' + active.id + " · " + esc(active.status).toUpperCase() + "</b></p>" +
          "<p>" + esc(active.summary || "no summary yet") + "</p>" +
          "<p><b>" + esc(active.instruction || "awaiting instruction") + "</b></p>" +
          '<p class="dm mono" style="font-size:8.5px">' +
          (active.lat != null ? active.lat.toFixed(4) + " " + active.lon.toFixed(4) : "position unknown") +
          (active.destination_name ? " → " + esc(active.destination_name) : "") + "</p>"
        : "<p>No open incident. The line is idle.</p>") +
      '<div class="lbl">QUEUE</div>' +
      items.slice(0, 5).map(function (i) {
        var pr = PRI[i.priority] || PRI[2];
        var rs = i.responder_state || "";
        var next = rs === "" ? "claimed" : rs === "claimed" ? "en_route"
                 : rs === "en_route" ? "on_scene" : "resolved";
        var label = next === "claimed" ? "CLAIM" : next === "en_route" ? "ROLL"
                  : next === "on_scene" ? "ARRIVE" : "CLOSE";
        return '<div class="row on"><span class="pill ' + pr[1] + '">' + pr[0] +
          '</span><span>#' + i.id + "</span>" +
          (rs ? '<span class="' + (rs === "resolved" ? "dm" : "cy") + '" ' +
                'style="font-size:8px;margin-left:5px">' + esc(rs) + "</span>" : "") +
          '<span class="r">' +
          (i.status === "closed" ? '<span class="dm">closed</span>'
            : '<button class="act" data-id="' + i.id + '" data-next="' + next + '">' +
              label + "</button>") +
          (i.supervised ? ' <span class="rd" style="font-size:8px">HUMAN</span>' : "") +
          "</span></div>";
      }).join("") +
      '<div class="lbl">ABSORPTION</div><div class="meter"><i style="width:' + absorbed.toFixed(1) +
      '%"></i></div><div class="big">' + absorbed.toFixed(1) +
      '%</div><div class="mono dm" style="font-size:8px">handled without a human</div>';
  }

  function situation() {
    fetch("/v1/situation?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (s) {
        drawCams(s.cameras);
        var w = s.wind;
        SITU = [];
        if (w) SITU.push({ t: "now", src: "NWS", col: "am",
               m: "wind " + w.from_deg + " deg at " + (w.kph != null ? w.kph + " kph" : "?") +
                  ", smoke drifts toward " + w.to_deg + " deg" });
        (s.fires || []).forEach(function (f) {
          SITU.push({ t: "now", src: "NIFC", col: "am",
                      m: f.name + " perimeter, " + f.acres + " acres within range" });
        });
        if (s.camera_count) SITU.push({ t: "now", src: "ALERTCA", col: "vi",
               m: s.camera_count + " cameras in range, nearest " + s.cameras[0].name +
                  " at " + s.cameras[0].km + " km" });
        $("feeds").innerHTML = [
          ["CALLERS", "live", "cy"],
          ["OSM", "cached", "cy"],
          ["NWS", w ? (s.wind_age_s < 60 ? "just now" : Math.round(s.wind_age_s / 60) + "m") : "down", w ? "cy" : "rd"],
          ["ALERTCA", s.camera_count ? "live" : "down", s.camera_count ? "cy" : "rd"],
          ["NIFC", (s.fires || []).length + " fires", "cy"]
        ].map(function (f) {
          return '<div class="row"><span class="' + f[2] + '">&bull;</span><span>' + f[0] +
            '</span><span class="r">' + f[1] + "</span></div>";
        }).join("");
        $("sats").innerHTML =
          (w ? '<div class="row on"><span class="cy">&bull;</span><span>NWS WIND</span>' +
               '<span class="r cy">' + w.from_deg + '&deg; ' + (w.kph != null ? w.kph + " kph" : "") +
               "</span></div>" +
               '<div class="row"><span class="am">&bull;</span><span>SMOKE DRIFT</span>' +
               '<span class="r am">toward ' + w.to_deg + "&deg;</span></div>"
             : '<div class="row"><span class="dm">&bull;</span><span>NWS WIND</span><span class="r">offline</span></div>') +
          '<div class="row"><span class="vi">&bull;</span><span>ALERTCA CAMS</span><span class="r vi">' +
          s.camera_count + " in 40km</span></div>" +
          s.fires.slice(0, 3).map(function (f) {
            return '<div class="row"><span class="am">&bull;</span><span>' + esc(f.name) +
              '</span><span class="r am">' + f.acres + " ac</span></div>";
          }).join("") +
          "";
      })
      .catch(function () { /* cached console keeps its last good render */ });
  }
  situation();
  setInterval(situation, 30000);

  function lanes(tr, voice) {
    $("vxport").textContent = voice ? voice.transport : "-";
    $("vxtools").textContent = (voice ? voice.tool_count : 4) + " TOOLS" +
      (voice && !voice.configured ? " · FALLBACK" : "");
    var last = tr.slice(-6);
    if (!last.length) {
      $("lanes").innerHTML = '<div class="mono dm" style="font-size:8px;padding:8px">' +
        "no tool calls yet — run a call and both lanes fill</div>";
      return;
    }
    var span = Math.max(1200, last.reduce(function (a, t) { return a + t.ms + 900; }, 0));
    var cur = 0, caller = "", live = "", async_ = "", weave = "";
    last.forEach(function (t) {
      var talk = 700;                                  // caller speaking
      var cw = 100 * talk / span, cx = 100 * cur / span;
      caller += '<div class="seg caller" style="left:' + cx + '%;width:' + cw + '%">' +
                esc(t.spoken.slice(0, 22)) + "</div>";
      cur += talk;
      var ax = 100 * cur / span, aw = 100 * Math.max(t.ms, 90) / span;
      async_ += '<div class="seg async" style="left:' + ax + '%;width:' + aw + '%">' +
                t.tool + " " + t.ms + "ms</div>";
      // the live path covers the whole async window and keeps going
      var lw = aw + 100 * 200 / span;
      live += '<div class="seg live" style="left:' + ax + '%;width:' + lw + '%">' +
              (t.swarm ? "swarm weave-in" : "stay with me") + "</div>";
      weave += '<div class="weave" style="left:' + (ax + aw) + '%"></div>';
      cur += Math.max(t.ms, 90) + 200;
    });
    $("lanes").innerHTML =
      lane("CALLER", "speaking", caller) +
      lane("LIVE PATH", voice && voice.configured ? "gpt-live full duplex" : "never silent", live + weave) +
      lane("ASYNC PATH", "tools + swarm", async_) +
      lane("SUPERVISOR", "human override", '<div class="seg sup" style="left:0;width:100%">watching · may seize the call</div>');
  }
  function lane(n, sub, inner) {
    return '<div class="lane"><div class="nm">' + n + "<i>" + sub + "</i></div>" +
           '<div class="track">' + inner + "</div></div>";
  }

  function trace() {
    fetch("/v1/trace?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (d) { lanes(d.trace || [], d.voice); })
      .catch(function () {});
  }
  trace();
  setInterval(trace, 2000);

  // Responder actions. One click moves the incident and the caller is told on
  // their next turn (or by text when a number is configured).
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("button.act") : null;
    if (!b) return;
    b.disabled = true; b.textContent = "...";
    fetch("/v1/incidents/" + b.dataset.id + "/respond?token=" + encodeURIComponent(TOKEN), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: b.dataset.next, who: RESPONDER })
    }).then(function (r) { return r.json(); })
      .then(function () { tick(); })
      .catch(function () { b.disabled = false; b.textContent = "RETRY"; });
  });

  function tick() {
    var d = new Date();
    $("clock").textContent = d.toTimeString().slice(0, 8);
    fetch("/v1/incidents?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (j) { draw(j.incidents || j || []); })
      .catch(function (e) {
        $("mapstat").textContent = "FEED ERROR " + e;
      });
  }
  tick();
  setInterval(tick, 2000);
})();
