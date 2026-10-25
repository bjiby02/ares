/* ARES sector board. Streets around the calls, camera stills on this page. */
(function () {
  "use strict";

  var TOKEN = new URLSearchParams(location.search).get("token") || "";
  var RESPONDER = new URLSearchParams(location.search).get("as") || "dispatch";
  var $ = function (id) { return document.getElementById(id); };

  var ITEMS = [];
  var CAMS = [];
  var WIND = null;
  var FIRES = [];
  var SWARM = { source: null, rays: [], hotspots: [], satellite: null };
  var CENTER = null;
  var selectedId = null;
  var seenIds = null;
  var momentTimer = null;
  var frameGen = 0;
  var camStamp = "";
  var fittedFor = null;
  var show = { callers: true, routes: true, hazard: true, swarm: true, cameras: true };
  var map, layers, ready = false;

  var BEAR = { N: 0, NE: 45, E: 90, SE: 135, S: 180, SW: 225, W: 270, NW: 315 };
  var PRI = { 0: ["P0", "rd", "p0"], 1: ["P1", "am", "p1"], 2: ["P2", "cy", "p2"], 3: ["P3", "dim", "p3"] };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[<>&]/g, function (m) {
      return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[m];
    });
  }
  function ago(iso) {
    if (!iso) return "";
    var s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (s < 60) return Math.floor(s) + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    return Math.floor(s / 3600) + "h";
  }
  function selected() {
    for (var i = 0; i < ITEMS.length; i++) if (ITEMS[i].id === selectedId) return ITEMS[i];
    return null;
  }
  function km(aLat, aLon, bLat, bLon) {
    var r = 6371, p1 = aLat * Math.PI / 180, p2 = bLat * Math.PI / 180;
    var dp = (bLat - aLat) * Math.PI / 180, dl = (bLon - aLon) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * r * Math.asin(Math.sqrt(h));
  }
  function bearing(aLat, aLon, bLat, bLon) {
    var y = Math.sin((bLon - aLon) * Math.PI / 180) * Math.cos(bLat * Math.PI / 180);
    var x = Math.cos(aLat * Math.PI / 180) * Math.sin(bLat * Math.PI / 180) -
            Math.sin(aLat * Math.PI / 180) * Math.cos(bLat * Math.PI / 180) * Math.cos((bLon - aLon) * Math.PI / 180);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }
  function compass(deg) {
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  }
  function project(lat, lon, deg, meters) {
    var d = meters / 6371000, br = deg * Math.PI / 180;
    var p1 = lat * Math.PI / 180, l1 = lon * Math.PI / 180;
    var p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(br));
    var l2 = l1 + Math.atan2(Math.sin(br) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
    return [p2 * 180 / Math.PI, ((l2 * 180 / Math.PI + 540) % 360) - 180];
  }
  function hazardDeg(code) {
    if (!code) return null;
    var key = String(code).trim().toUpperCase();
    return Object.prototype.hasOwnProperty.call(BEAR, key) ? BEAR[key] : null;
  }
  function frameUrl(id) {
    return "/v1/cameras/frame?token=" + encodeURIComponent(TOKEN) +
      "&id=" + encodeURIComponent(id) + "&t=" + frameGen;
  }
  var HELP = ["Ambulance", "Fire engine", "Rescue team"];

  function helpName(it) {
    var note = (it.responder_note || "").trim();
    return HELP.indexOf(note) >= 0 ? note : "";
  }
  function statusOf(it) {
    var help = helpName(it);
    var rs = it.responder_state || "";
    if (it.status === "closed" || rs === "resolved") return { cls: "done", text: "Closed" };
    if (rs === "on_scene") return { cls: "go", text: (help || "Help") + " is with them" };
    if (rs === "en_route") return { cls: "go", text: (help || "Help") + " is going to them" };
    if (rs === "released") return { cls: "done", text: "Back with the automated line" };
    return { cls: "go", text: "The agent has this call" };
  }
  function problem(it) {
    if (it.status === "closed" || it.responder_state === "resolved") return "Call ended";
    if (it.status === "escalated" && it.need === "medical") return "Hurt, cannot move";
    if (it.status === "escalated") return "Cannot move";
    if (it.need === "medical") return "Hurt, can still move";
    if (it.need === "fire") return "Fire or smoke";
    if (it.need === "shelter") return "Water";
    if (it.instruction && /will not send you/.test(it.instruction)) return "Told to stay put";
    return "Can move";
  }
  function actionsHtml(it) {
    var id = it.id;
    var rs = it.responder_state || "";
    var help = helpName(it);
    if (it.status === "closed" || rs === "resolved" || rs === "released") return "";
    if (!help) {
      return '<p class="hint">The agent has this call and is asking where they are.</p>';
    }
    if (rs !== "on_scene") {
      var there = help === "Ambulance" ? "An ambulance is with you now."
        : help === "Fire engine" ? "A fire engine is with you now."
        : help === "Rescue team" ? "A rescue team is with you now."
        : "Help is with you now.";
      var other = HELP.filter(function (name) { return name !== help; });
      return '<button class="act" data-id="' + id + '" data-next="on_scene">They have arrived</button>' +
        other.map(function (name) {
          return '<button class="act quiet" data-id="' + id + '" data-next="en_route" data-help="' +
            name + '">Send ' + name.toLowerCase() + " instead</button>";
        }).join("") +
        '<p class="hint">They will hear: “' + there + '”</p>';
    }
    if (rs === "on_scene") {
      return '<button class="act" data-id="' + id + '" data-next="resolved">End this call</button>' +
        '<p class="hint">They will hear that the call is closed.</p>';
    }
    return "";
  }

  function initMap() {
    if (!window.L) {
      $("mapstat").textContent = "map library did not load";
      return;
    }
    map = L.map("tmap", { zoomControl: false, attributionControl: true, minZoom: 3 });
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 16,
      attribution: "Tiles &copy; Esri"
    }).addTo(map);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    L.control.scale({ imperial: false, position: "bottomleft" }).addTo(map);
    layers = {
      callers: L.layerGroup().addTo(map),
      routes: L.layerGroup().addTo(map),
      hazard: L.layerGroup().addTo(map),
      swarm: L.layerGroup().addTo(map),
      cameras: L.layerGroup().addTo(map)
    };
    ready = true;
    window.addEventListener("resize", function () { map.invalidateSize(); });
    setTimeout(function () {
      map.invalidateSize();
      fittedFor = null;
      renderMap(true);
    }, 250);
  }

  function syncLayers() {
    if (!ready) return;
    Object.keys(layers).forEach(function (name) {
      if (!map.hasLayer(layers[name])) map.addLayer(layers[name]);
    });
  }

  function marker(latlng, kind, color, text) {
    var icon = L.divIcon({
      className: "pin " + kind,
      html: '<i style="background:' + color + '"></i>' + (text ? "<b>" + esc(text) + "</b>" : ""),
      iconSize: [12, 12],
      iconAnchor: [6, 6]
    });
    return L.marker(latlng, { icon: icon, keyboard: false });
  }

  function renderMap(fly) {
    if (!ready) return;
    Object.keys(layers).forEach(function (name) { layers[name].clearLayers(); });
    var focus = selected();
    ITEMS.forEach(function (it) {
      if (it.lat == null || it.lon == null) return;
      var here = [it.lat, it.lon];
      var hot = focus && focus.id === it.id;
      var color = it.status === "escalated" ? "#ff5d4e" : "#f0b429";
      marker(here, "caller", color, hot ? (it.place_name || ("#" + it.id)) : "").addTo(layers.callers);
      if (it.destination_lat != null && it.destination_lon != null) {
        var there = [it.destination_lat, it.destination_lon];
        L.polyline([here, there], {
          color: hot ? "#7ee0c0" : "#2f6d5a", weight: hot ? 3 : 1.5, opacity: hot ? 0.95 : 0.55
        }).addTo(layers.routes);
        if (hot) marker(there, "dest", "#7ee0c0", it.destination_name || "assembly").addTo(layers.routes);
      }
      var deg = hazardDeg(it.hazard);
      if (hot && deg != null) {
        var tip = project(it.lat, it.lon, deg, 1800);
        L.polyline([here, tip], { color: "#ff5d4e", weight: 2, dashArray: "5 6", opacity: 0.9 }).addTo(layers.hazard);
      }
    });
    renderSwarm();
    CAMS.forEach(function (c) {
      if (c.lat == null) return;
      var m = marker([c.lat, c.lon], "cam", "#b7a6ff", "");
      m.on("click", function () { openCam(c); });
      m.addTo(layers.cameras);
    });
    syncLayers();
    // A fire the callers just located must never sit off the edge of the map.
    var src = SWARM.source;
    if (!focus && src) {
      var swarmKey = "swarm:" + src.support + ":" + src.lat.toFixed(3) + ":" + src.lon.toFixed(3);
      if (fittedFor !== swarmKey || fly) {
        fittedFor = swarmKey;
        var pts = [[src.lat, src.lon]];
        (SWARM.rays || []).forEach(function (r) { pts.push([r.lat, r.lon]); });
        map.fitBounds(L.latLngBounds(pts).pad(0.35), { maxZoom: 14, animate: true });
      }
      return;
    }
    var key = focus && focus.lat != null ? focus.id + ":" + focus.lat.toFixed(3) : (CENTER ? "center" : "");
    if (!key || (fittedFor === key && !fly)) return;
    fittedFor = key;
    if (focus && focus.lat != null) {
      var pts = [[focus.lat, focus.lon]];
      if (focus.destination_lat != null) pts.push([focus.destination_lat, focus.destination_lon]);
      map.fitBounds(L.latLngBounds(pts).pad(0.6), { maxZoom: 15, animate: true });
    } else if (CENTER) {
      map.setView([CENTER.lat, CENTER.lon], 12);
    }
  }

  function renderSwarm() {
    // Satellite detections sit under everything: they are context, not a route.
    (SWARM.hotspots || []).forEach(function (h) {
      L.rectangle([[h.lat - 0.004, h.lon - 0.004], [h.lat + 0.004, h.lon + 0.004]], {
        color: "#ffb45d", weight: 1, fillColor: "#ffb45d", fillOpacity: 0.16
      }).bindTooltip("VIIRS " + h.sat + " &middot; " + h.kelvin + "K &middot; " + h.at)
        .addTo(layers.swarm);
    });
    var src = SWARM.source;
    var rays = SWARM.rays || [];
    if (!src) {
      rays.forEach(function (r) {
        var deg = hazardDeg(r.hazard);
        if (deg == null) return;
        L.polyline([[r.lat, r.lon], project(r.lat, r.lon, deg, 1400)], {
          color: "#ff8a5d", weight: 1.5, dashArray: "4 7", opacity: 0.45
        }).addTo(layers.swarm);
      });
      return;
    }
    var at = [src.lat, src.lon];
    // Each caller's own bearing, carried all the way to the point they agree on.
    rays.forEach(function (r) {
      L.polyline([[r.lat, r.lon], at], {
        color: "#ff5d4e", weight: 1.6, dashArray: "3 6", opacity: 0.75
      }).addTo(layers.swarm);
      L.circleMarker([r.lat, r.lon], {
        radius: 4, color: "#ff8a5d", weight: 2, fillColor: "#ff8a5d", fillOpacity: 0.9
      }).addTo(layers.swarm);
    });
    // How tightly they agreed. The circle is the error, drawn to scale.
    L.circle(at, {
      radius: Math.max(src.spread_m, 60), color: "#ff5d4e", weight: 1,
      dashArray: "2 6", fillColor: "#ff5d4e", fillOpacity: 0.09
    }).addTo(layers.swarm);
    var sat = SWARM.satellite;
    if (sat && sat.confirms) {
      L.circle(at, { radius: Math.max(src.spread_m, 60) + 900, color: "#ffb45d",
        weight: 1, dashArray: "6 8", fill: false, opacity: 0.6 }).addTo(layers.swarm);
    }
    var noun = src.kind === "shelter" ? "RISING WATER" : "FIRE";
    L.marker(at, {
      keyboard: false,
      icon: L.divIcon({
        className: "swarmsrc",
        html: '<span class="pulse"></span><b>' + noun + '</b>'
          + '<i>' + (sat && sat.confirms
              ? 'confirmed by VIIRS ' + sat.sat + ', ' + sat.km + ' km &middot; '
              : '')
          + 'located by ' + src.support + ' callers &middot; &plusmn;'
          + (src.spread_m >= 1000
              ? (src.spread_m / 1000).toFixed(1) + " km"
              : src.spread_m + " m") + '</i>',
        iconSize: [0, 0], iconAnchor: [0, 0]
      })
    }).addTo(layers.swarm);
  }

  function assetLine(it) {
    if (!it) return "";
    var bits = [];
    if (it.unit) bits.push(esc(it.unit) + (it.eta_min ? " &middot; ETA " + it.eta_min + " min" : ""));
    if (it.walk_min != null) bits.push("caller walks " + it.walk_min + " min");
    if (it.camera) bits.push("camera " + esc(it.camera.name) + " at " + it.camera.km + " km");
    return bits.join("  &middot;  ");
  }

  function renderWind() {
    var box = $("wind");
    if (!WIND) { box.hidden = true; return; }
    box.hidden = false;
    var toward = { N: "north", NE: "northeast", E: "east", SE: "southeast",
      S: "south", SW: "southwest", W: "west", NW: "northwest" }[compass(WIND.to_deg)] || "";
    $("windline").textContent = "Smoke drifts " + toward;
    $("windsub").textContent = WIND.kph != null ? "Wind " + WIND.kph + " kph" : "";
    $("windarrow").style.transform = "rotate(" + WIND.to_deg + "deg)";
  }

  function renderQueue() {
    var open = 0;
    ITEMS.forEach(function (it) {
      if (it.status !== "closed" && it.responder_state !== "resolved") open++;
    });
    $("summary").textContent = open ? "The agent has every call" : "No one is waiting";
    $("qcount").textContent = String(open);
    $("mapstat").textContent = open ? open + (open === 1 ? " person" : " people") : "No one on the map";
    if (!ITEMS.length) {
      $("queue").innerHTML = '<div class="empty">No calls. The line is idle.</div>';
      return;
    }
    var shown = ITEMS.slice().sort(function (a, b) {
      var ac = (a.status === "closed" || a.responder_state === "resolved") ? 1 : 0;
      var bc = (b.status === "closed" || b.responder_state === "resolved") ? 1 : 0;
      if (ac !== bc) return ac - bc;
      return (a.priority || 2) - (b.priority || 2);
    });
    $("queue").innerHTML = shown.map(function (it) {
      var urgent = it.status === "escalated";
      var st = statusOf(it);
      return '<div class="card' + (it.id === selectedId ? " on" : "") + '" data-id="' + it.id + '">' +
        '<div class="place">' + (urgent ? '<i class="mark"></i>' : "") + esc(it.place_name || "Place unknown") + "</div>" +
        '<div class="sub">' + esc(problem(it)) + "</div>" +
        '<div class="status ' + st.cls + '">' + esc(st.text) + "</div>" +
        "</div>";
    }).join("");
  }

  function renderDetail() {
    var it = selected();
    $("calltag").textContent = "";
    if (!it) {
      $("detail").innerHTML = '<div class="empty">Choose a person. Their status and the next step appear here.</div>';
      return;
    }
    $("calltag").textContent = problem(it);
    var st = statusOf(it);
    $("detail").innerHTML =
      "<h2>" + esc(it.place_name || "Place unknown") + "</h2>" +
      '<div class="status big ' + st.cls + '">' + esc(st.text) + "</div>" +
      '<div class="decision">' + actionsHtml(it) + "</div>" +
      '<div class="say">' + esc(it.instruction || "Nothing has been said yet.") + "</div>" +
      (assetLine(it) ? '<p class="assets">' + assetLine(it) + "</p>" : "") +
      (it.summary ? '<p class="hint">They said: ' + esc(it.summary) + "</p>" : "");
  }

  function feedBlock() {
    var rows = [
      ["NWS", WIND ? "live" : "down", WIND ? "cy" : "rd"],
      ["CAMERAS", CAMS.length ? CAMS.length + " in sector" : "down", CAMS.length ? "cy" : "rd"],
      ["NIFC", FIRES.length + " perimeters", FIRES.length ? "am" : "dim"]
    ];
    var fireLines = FIRES.slice(0, 3).map(function (f) {
      return '<div><span class="am">' + esc(f.name) + "</span><span>" + f.acres + " ac</span></div>";
    }).join("");
    return '<div class="feeds"><div class="k" style="margin:0 0 6px">FEEDS</div>' +
      rows.map(function (r) {
        return "<div><span>" + r[0] + '</span><span class="' + r[2] + '">' + r[1] + "</span></div>";
      }).join("") + fireLines + "</div>";
  }

  function nearest() {
    var it = selected();
    var list = CAMS.map(function (c) {
      var copy = Object.assign({}, c);
      if (it && it.lat != null && c.lat != null) {
        copy.dkm = km(it.lat, it.lon, c.lat, c.lon);
        copy.dir = compass(bearing(it.lat, it.lon, c.lat, c.lon));
      } else {
        copy.dkm = c.km;
        copy.dir = "";
      }
      return copy;
    });
    list.sort(function (a, b) { return a.dkm - b.dkm; });
    return list.slice(0, 6);
  }
  function distLabel(c) {
    if (c.dkm == null) return "";
    var n = c.dkm < 10 ? c.dkm.toFixed(1) : String(Math.round(c.dkm));
    return n + " km" + (c.dir ? " " + c.dir : "");
  }

  function renderCams() {
    var list = nearest();
    var it = selected();
    $("camstat").textContent = list.length ? "Near this person" : "None nearby";
    var stamp = list.map(function (c) { return c.id; }).join(",") + "|" + (selectedId || "");
    if (stamp === camStamp && $("cams").querySelector(".cam")) {
      list.forEach(function (c) {
        var meta = $("cams").querySelector('.cam[data-id="' + CSS.escape(c.id) + '"] .meta');
        if (meta) meta.textContent = distLabel(c);
      });
      return;
    }
    camStamp = stamp;
    $("cams").innerHTML = "";
    if (!list.length) {
      $("cams").innerHTML = '<div class="empty">No camera stills in this sector yet.</div>';
      return;
    }
    list.forEach(function (c) {
      var tile = document.createElement("button");
      tile.type = "button";
      tile.className = "cam";
      tile.dataset.id = c.id;
      var img = document.createElement("img");
      img.className = "frame";
      img.alt = "";
      img.dataset.id = c.id;
      img.referrerPolicy = "no-referrer";
      img.src = frameUrl(c.id);
      img.addEventListener("error", function () { tile.classList.add("badpic"); });
      var live = document.createElement("s");
      live.textContent = "LIVE";
      var cap = document.createElement("div");
      cap.className = "cap";
      cap.innerHTML = "<b>" + esc(c.name) + '</b><span class="meta">' + esc(distLabel(c)) + "</span>";
      tile.appendChild(img);
      tile.appendChild(live);
      tile.appendChild(cap);
      tile.addEventListener("click", function () { openCam(c); });
      $("cams").appendChild(tile);
    });
  }

  function openCam(c) {
    var img = $("vimg");
    img.dataset.id = c.id;
    img.src = frameUrl(c.id);
    $("vname").textContent = c.name || "camera";
    $("vmeta").textContent = distLabel(c) + (c.online ? " · " + c.online : "");
    $("viewer").hidden = false;
  }
  function closeCam() { $("viewer").hidden = true; }
  $("vclose").addEventListener("click", closeCam);
  $("viewer").addEventListener("click", function (e) { if (e.target === $("viewer")) closeCam(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeCam(); });

  function render(fly) {
    if (selectedId == null || !ITEMS.some(function (it) { return it.id === selectedId; })) {
      var open = ITEMS.filter(function (it) { return it.status !== "closed"; })[0];
      selectedId = open ? open.id : (ITEMS[0] ? ITEMS[0].id : null);
      fittedFor = null;
    }
    renderQueue();
    renderDetail();
    renderMap(fly);
    renderCams();
    renderWind();
  }

  $("queue").addEventListener("click", function (e) {
    if (e.target.closest("button.act")) return;
    var card = e.target.closest(".card");
    if (!card) return;
    selectedId = Number(card.dataset.id);
    fittedFor = null;
    render(true);
  });

  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("button.act") : null;
    if (!b) return;
    var person = null;
    for (var i = 0; i < ITEMS.length; i++) {
      if (String(ITEMS[i].id) === String(b.dataset.id)) person = ITEMS[i];
    }
    if (person) {
      person.responder_state = b.dataset.next;
      if (b.dataset.help) person.responder_note = b.dataset.help;
      if (b.dataset.next === "resolved") person.status = "closed";
      selectedId = person.id;
      render(false);
    }
    fetch("/v1/incidents/" + b.dataset.id + "/respond?token=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        state: b.dataset.next,
        who: RESPONDER,
        note: b.dataset.help || ""
      })
    }).then(function (r) { return r.json(); })
      .then(function () { tick(); })
      .catch(function () { tick(); });
  });

  function tick() {
    var d = new Date();
    $("clock").textContent = d.toTimeString().slice(0, 8);
    fetch("/v1/incidents?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (j) {
        ITEMS = j.incidents || j || [];
        showHearing(j.hearing);
        noticeNew();
        render(false);
      })
      .catch(function () { $("mapstat").textContent = "The list did not load"; });
    fetch("/v1/swarm?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (j) {
        var had = SWARM.source ? SWARM.source.support : 0;
        SWARM = { source: j.source || null, rays: j.rays || [], hotspots: j.hotspots || [], satellite: j.satellite || null };
        if (SWARM.source && SWARM.source.support > had) announceSwarm();
        renderMap(false);
      })
      .catch(function () {});
  }

  function announceSwarm() {
    var src = SWARM.source;
    var box = $("mapstat");
    if (!box || !src) return;
    box.textContent = src.support + " callers just fixed the "
      + (src.kind === "shelter" ? "water" : "fire")
      + " to \u00b1" + (src.spread_m >= 1000
          ? (src.spread_m / 1000).toFixed(1) + " km" : src.spread_m + " m");
    box.classList.add("hit");
    setTimeout(function () { box.classList.remove("hit"); }, 6000);
  }

  function situation() {
    fetch("/v1/situation?token=" + encodeURIComponent(TOKEN))
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (s) {
        WIND = s.wind || null;
        FIRES = s.fires || [];
        CAMS = s.cameras || [];
        CENTER = s.center || null;
        if (CENTER && CENTER.label) $("area").textContent = CENTER.label;
        frameGen++;
        camStamp = "";
        renderWind();
        render(false);
      })
      .catch(function () {});
  }

  function liveStream() {
    var es = new EventSource("/v1/stream?token=" + encodeURIComponent(TOKEN));
    es.addEventListener("queue", function (e) {
      try {
        var j = JSON.parse(e.data);
        ITEMS = j.incidents || [];
        noticeNew();
        render(false);
        var dot = $("livedot");
        if (dot) { dot.classList.add("beat"); setTimeout(function () { dot.classList.remove("beat"); }, 700); }
      } catch (err) {}
    });
    es.onerror = function () {
      es.close();
      setTimeout(liveStream, 2500);   // the console must survive a dropped tunnel
    };
  }
  liveStream();

  setInterval(function () {
    frameGen++;
    document.querySelectorAll("img.frame").forEach(function (img) {
      if (img.dataset.id) img.src = frameUrl(img.dataset.id);
    });
  }, 20000);

  initMap();
  situation();
  tick();
  function showHearing(heard) {
    var box = $("hear");
    if (!heard || !heard.text) { box.hidden = true; return; }
    box.hidden = false;
    $("hearline").textContent = heard.text;
  }
  function showMoment(it) {
    var line = it.instruction || it.summary || "";
    if (!line) return;
    $("momentline").textContent = line;
    var where = it.place_name || "Place still unknown";
    var help = helpName(it);
    $("momentwhere").textContent = help ? where + " · " + help : where;
    $("moment").hidden = false;
    clearTimeout(momentTimer);
    momentTimer = setTimeout(function () { $("moment").hidden = true; }, 9000);
  }
  function noticeNew() {
    if (seenIds === null) {
      seenIds = {};
      ITEMS.forEach(function (it) { seenIds[it.id] = true; });
      return;
    }
    var fresh = null;
    ITEMS.forEach(function (it) {
      if (seenIds[it.id]) return;
      seenIds[it.id] = true;
      if (it.status !== "closed" && it.responder_state !== "resolved") fresh = it;
    });
    if (!fresh) return;
    selectedId = fresh.id;
    fittedFor = null;
    showMoment(fresh);
  }

  setInterval(situation, 30000);
  setInterval(tick, 6000);
})();
