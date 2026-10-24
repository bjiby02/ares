/* ARES talk. GPT-Live-1 over WebRTC.
 *
 * Live path  : the peer connection. Audio never stops, barge-in is native.
 * Async path : tool calls relayed to the decision core while the line keeps talking.
 * The model composes nothing — it speaks the `say` string a tool returned.
 */
(function () {
  "use strict";

  var TOKEN = new URLSearchParams(location.search).get("token") || "";
  var PHONE = new URLSearchParams(location.search).get("phone") || "+1619BROWSER";
  var $ = function (id) { return document.getElementById(id); };

  var pc = null, dc = null, mic = null, turns = 0;
  var marks = [];                       // {kind, t0, t1, label}
  var t0 = 0;

  function now() { return Date.now() - t0; }
  function setState(s, cls) { var e = $("state"); e.textContent = s; e.className = cls || ""; }

  function log(kind, who, text) {
    var d = document.createElement("div");
    d.className = kind;
    d.innerHTML = '<span class="who">' + who + "</span>" + esc(text);
    $("log").appendChild(d);
    $("log").scrollTop = $("log").scrollHeight;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[<>&]/g, function (m) {
      return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[m];
    });
  }

  // ---- lanes ---------------------------------------------------------------
  function mark(kind, label) {
    var m = { kind: kind, t0: now(), t1: null, label: label };
    marks.push(m); drawLanes(); return m;
  }
  function close(m) { if (m) { m.t1 = now(); drawLanes(); } }

  function drawLanes() {
    var span = Math.max(6000, now());
    function lane(kind, name) {
      var segs = marks.filter(function (m) { return m.kind === kind; }).map(function (m) {
        var a = 100 * m.t0 / span, b = 100 * ((m.t1 == null ? now() : m.t1)) / span;
        return '<div class="seg ' + kind + '" style="left:' + a + '%;width:' +
               Math.max(b - a, 0.8) + '%">' + esc(m.label) + "</div>";
      }).join("");
      return '<div class="lane"><div class="nm">' + name + '</div><div class="track">' +
             segs + "</div></div>";
    }
    $("lanes").innerHTML = lane("caller", "CALLER") + lane("live", "LIVE") +
                           lane("async", "ASYNC");
  }
  setInterval(function () { if (pc) drawLanes(); }, 400);

  // ---- the async path ------------------------------------------------------
  function runTool(name, args, callId) {
    var m = mark("async", name);
    log("tool", "ASYNC PATH", name + "(" + (args.spoken || "").slice(0, 46) + ")");
    return fetch("/v1/tools/" + name + "?token=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone: PHONE, spoken: args.spoken || "" })
    }).then(function (r) { return r.json(); }).then(function (out) {
      close(m);
      log("sys", "CORE", name + " → " + (out.action || "?") + "  " + (out.ms || 0) + "ms");
      decision(out);
      // hand the result back; the model speaks `say` and nothing else
      send({
        type: "conversation.item.create",
        item: { type: "function_call_output", call_id: callId,
                output: JSON.stringify({ say: out.say, action: out.action,
                                         choices: out.choices || null }) }
      });
      send({ type: "response.create" });
      return out;
    }).catch(function (e) {
      close(m);
      log("sys", "CORE", "tool failed: " + e);
      send({ type: "conversation.item.create",
             item: { type: "function_call_output", call_id: callId,
                     output: JSON.stringify({ say: "I did not catch that. Tell me what is happening, and where you are." }) } });
      send({ type: "response.create" });
    });
  }

  function decision(out) {
    $("inc").textContent = out.incident ? "incident " + out.incident : "—";
    var rows = [["action", out.action], ["place", out.place || "—"],
                ["hazard", out.hazard || "—"], ["need", out.need || "—"],
                ["mobility", out.mobility || "—"], ["core", (out.ms || 0) + " ms"]];
    $("dec").innerHTML = rows.map(function (r) {
      return '<div class="kv"><span>' + r[0] + "</span><b>" + esc(r[1]) + "</b></div>";
    }).join("");
  }

  function send(obj) { if (dc && dc.readyState === "open") dc.send(JSON.stringify(obj)); }

  // ---- events from the model ----------------------------------------------
  var liveSeg = null, speaking = "";
  function onEvent(ev) {
    switch (ev.type) {
      case "session.created":
      case "session.updated":
        log("sys", "SESSION", ev.type); break;

      case "input_audio_buffer.speech_started":
        mark("caller", "speaking"); break;
      case "input_audio_buffer.speech_stopped": {
        var last = marks.filter(function (m) { return m.kind === "caller"; }).pop();
        close(last); break;
      }
      case "conversation.item.input_audio_transcription.completed":
        turns++; $("turns").textContent = turns + " turns";
        log("caller", "CALLER", ev.transcript || ""); break;

      case "response.created":
        liveSeg = mark("live", "speaking"); speaking = ""; break;
      case "response.output_audio_transcript.delta":
      case "response.audio_transcript.delta":
        speaking += ev.delta || ""; break;
      case "response.output_audio_transcript.done":
      case "response.audio_transcript.done":
        if (speaking.trim()) log("ares", "ARES", speaking.trim());
        speaking = ""; break;
      case "response.done":
        close(liveSeg); liveSeg = null;
        var out = ((ev.response || {}).output) || [];
        out.forEach(function (item) {
          if (item.type === "function_call") {
            var args = {};
            try { args = JSON.parse(item.arguments || "{}"); } catch (e) {}
            runTool(item.name, args, item.call_id);
          }
        });
        break;

      case "error":
        log("sys", "ERROR", JSON.stringify(ev.error || ev).slice(0, 200));
        setState("ERROR", "err"); break;
    }
  }

  // ---- connect -------------------------------------------------------------
  async function start() {
    $("go").disabled = true;
    setState("CONNECTING");
    t0 = Date.now(); marks = []; turns = 0;
    try {
      mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      setState("NO MIC", "err"); log("sys", "ERROR", "microphone denied: " + e);
      $("go").disabled = false; return;
    }
    pc = new RTCPeerConnection();
    pc.ontrack = function (e) { $("sink").srcObject = e.streams[0]; };
    mic.getTracks().forEach(function (t) { pc.addTrack(t, mic); });

    dc = pc.createDataChannel("oai-events");
    dc.onopen = async function () {
      setState("LIVE", "on");
      log("sys", "SESSION", "data channel open — arming tools");
      // /v1/live/sessions only accepts model+instructions. Tools, voice and turn
      // detection are applied here, over the channel, before the first word.
      try {
        var sc = await fetch("/v1/tools/schema?token=" + encodeURIComponent(TOKEN))
                        .then(function (r) { return r.json(); });
        send(sc.session_update);
        log("sys", "SESSION", sc.tools.length + " tools armed: " +
            sc.tools.map(function (t) { return t.name; }).join(", "));
      } catch (e) {
        log("sys", "ERROR", "could not arm tools: " + e);
      }
      setTimeout(function () { send({ type: "response.create" }); }, 300);
    };
    dc.onmessage = function (e) {
      try { onEvent(JSON.parse(e.data)); } catch (err) {}
    };

    var offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await new Promise(function (res) {            // let ICE settle
      if (pc.iceGatheringState === "complete") return res();
      pc.onicegatheringstatechange = function () {
        if (pc.iceGatheringState === "complete") res();
      };
      setTimeout(res, 1500);
    });

    var r = await fetch("/v1/live/offer?token=" + encodeURIComponent(TOKEN), {
      method: "POST", headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription.sdp
    });
    var j = await r.json();
    if (!j.ok || !j.answer) {
      setState("REJECTED", "err");
      log("sys", "ERROR", (j.error || "no answer") + " " + (j.detail || "").slice(0, 160));
      $("go").disabled = false; return;
    }
    await pc.setRemoteDescription({ type: "answer", sdp: j.answer });
    $("stop").disabled = false;
    $("xport").textContent = "gpt-live-1 / webrtc";
  }

  function stop() {
    if (pc) pc.close();
    if (mic) mic.getTracks().forEach(function (t) { t.stop(); });
    pc = null; dc = null; mic = null;
    setState("IDLE"); $("go").disabled = false; $("stop").disabled = true;
    log("sys", "SESSION", "call ended");
  }

  $("go").onclick = start;
  $("stop").onclick = stop;
  drawLanes();
})();
