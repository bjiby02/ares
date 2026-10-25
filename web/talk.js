/* ARES talk. Every call is GPT-Live-1 over WebRTC.
 *
 * Live path  : the peer connection. Audio never stops, barge-in is native.
 * Async path : the decision core, while GPT-Live keeps the line.
 * The model composes nothing — it speaks the `say` string the core returned.
 */
(function () {
  "use strict";

  var TOKEN = new URLSearchParams(location.search).get("token") || "";
  var PHONE = new URLSearchParams(location.search).get("phone") || "";
  if (!PHONE) {
    PHONE = sessionStorage.getItem("ares-phone") || "";
    if (!PHONE) {
      PHONE = "+1619555" + String(Math.floor(1000 + Math.random() * 9000));
      sessionStorage.setItem("ares-phone", PHONE);
    }
  }
  var $ = function (id) { return document.getElementById(id); };

  var pc = null, dc = null, mic = null, turns = 0;
  var marks = [];
  var t0 = 0;
  var alive = false, callSid = "";

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
  setInterval(function () { if (pc || alive) drawLanes(); }, 400);

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

  var liveSeg = null, speaking = "", callerBuf = "", hearTimer = null;
  var lastLine = "", greeted = false, answerTimer = null, pendingDelegation = null;

  function words(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  }
  function isEcho(said) {
    var a = words(said), b = words(lastLine);
    if (!a || !b) return false;
    if (a === b || a.indexOf(b) >= 0 || b.indexOf(a) >= 0) return true;
    var aw = a.split(" "), hit = 0, i;
    for (i = 0; i < aw.length; i++) if (b.split(" ").indexOf(aw[i]) >= 0) hit++;
    return aw.length >= 5 && hit / aw.length >= 0.7;
  }

  function speakLine(line) {
    line = String(line || "").replace(/\s+/g, " ").trim();
    if (!line || line === lastLine) return;
    lastLine = line;
    send({
      type: "session.instructions.append",
      event_id: "say-" + Date.now(),
      delegation_id: null,
      content: "Say exactly these words, once, then stop and listen. Do not add a name or any other words: " + line
    });
  }

  function scheduleAnswer(delegationId) {
    pendingDelegation = delegationId;
    clearTimeout(answerTimer);
    answerTimer = setTimeout(function () { answerCaller(pendingDelegation); }, 500);
  }

  function answerCaller(delegationId) {
    answerTimer = null;
    var said = callerBuf.replace(/\s+/g, " ").trim();
    callerBuf = "";
    if (!said || isEcho(said)) return;
    turns++;
    $("turns").textContent = turns + " turns";
    log("caller", "CALLER", said);
    var waiting = mark("async", "line");
    fetch("/v1/live/turn?token=" + encodeURIComponent(TOKEN), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone: PHONE, said: said, call_sid: callSid })
    }).then(function (r) { return r.json(); }).then(function (out) {
      close(waiting);
      var line = (out && out.say) || "I did not catch that. Tell me what is happening, and where you are.";
      if (out && out.incident) decision(out);
      log("ares", "ARES", line);
      speakLine(line);
    }).catch(function (e) {
      close(waiting);
      log("sys", "ERROR", String(e));
      speakLine("I did not catch that. Tell me what is happening, and where you are.");
    });
  }

  function onEvent(ev) {
    if (ev.type === "session.input_transcript.delta") {
      callerBuf += ev.delta || "";
      if (answerTimer) scheduleAnswer(pendingDelegation);
      clearTimeout(hearTimer);
      hearTimer = setTimeout(function () {
        var text = callerBuf.replace(/\s+/g, " ").trim();
        if (!text) return;
        fetch("/v1/live/hearing?token=" + encodeURIComponent(TOKEN), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ phone: PHONE, text: text })
        }).catch(function () {});
      }, 280);
      return;
    }
    if (ev.type === "session.output_transcript.delta") {
      if (!liveSeg) liveSeg = mark("live", "speaking");
      speaking += ev.delta || "";
      return;
    }
    if (ev.type === "session.delegation.created") {
      scheduleAnswer((ev.delegation && ev.delegation.id) || null);
      return;
    }
    switch (ev.type) {
      case "session.started":
        setState("LIVE", "on");
        $("xport").textContent = "gpt-live-1 / webrtc";
        $("stop").disabled = false;
        log("sys", "SESSION", "gpt-live is on");
        if (!greeted) {
          greeted = true;
          speakLine("Operator. What is happening, and where are you?");
        }
        break;
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

  function failMic(micPromise, state, message) {
    alive = false;
    setState(state, "err");
    log("sys", "ERROR", message);
    $("go").disabled = false;
    micPromise.then(function (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
    }).catch(function () {});
  }

  function start() {
    $("go").disabled = true;
    setState("CONNECTING");
    t0 = Date.now();
    marks = [];
    turns = 0;
    greeted = false;
    callerBuf = "";
    alive = true;
    callSid = "CA" + String(Date.now());
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alive = false;
      setState("NO MIC", "err");
      log("sys", "ERROR", "This browser has no microphone.");
      $("go").disabled = false;
      return;
    }
    var micPromise = navigator.mediaDevices.getUserMedia({ audio: true });
    fetch("/v1/voice?token=" + encodeURIComponent(TOKEN)).then(function (r) { return r.json(); }).then(function (voice) {
      if (!voice || !voice.configured) {
        failMic(micPromise, "NO KEY", (voice && voice.reason) || "GPT Live is not configured.");
        return;
      }
      liveStart(micPromise);
    }).catch(function (e) { failMic(micPromise, "ERROR", String(e)); });
  }

  async function liveStart(micPromise) {
    try {
      mic = await micPromise;
    } catch (e) {
      alive = false;
      setState("NO MIC", "err");
      log("sys", "ERROR", "microphone denied: " + e);
      $("go").disabled = false;
      return;
    }
    try {
      pc = new RTCPeerConnection();
      pc.ontrack = function (e) {
        $("sink").srcObject = e.streams[0];
        var play = $("sink").play();
        if (play && play.catch) play.catch(function () {});
      };
      mic.getTracks().forEach(function (t) { pc.addTrack(t, mic); });
      dc = pc.createDataChannel("oai-events");
      dc.onopen = function () { log("sys", "SESSION", "data channel open"); };
      dc.onmessage = function (e) {
        try { onEvent(JSON.parse(e.data)); } catch (err) {}
      };
      var offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise(function (res) {
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
        alive = false;
        setState("REJECTED", "err");
        log("sys", "ERROR", (j.error || "no answer") + " " + (j.detail || "").slice(0, 160));
        $("go").disabled = false;
        pc.close();
        mic.getTracks().forEach(function (t) { t.stop(); });
        pc = null;
        return;
      }
      await pc.setRemoteDescription({ type: "answer", sdp: j.answer });
      $("stop").disabled = false;
      $("xport").textContent = "gpt-live-1 / webrtc";
    } catch (e) {
      alive = false;
      setState("ERROR", "err");
      log("sys", "ERROR", String(e));
      $("go").disabled = false;
      if (pc) pc.close();
      if (mic) mic.getTracks().forEach(function (t) { t.stop(); });
      pc = null;
    }
  }

  function stop() {
    alive = false;
    if (pc) pc.close();
    if (mic) mic.getTracks().forEach(function (t) { t.stop(); });
    pc = null; dc = null; mic = null;
    setState("IDLE");
    $("go").disabled = false;
    $("stop").disabled = true;
    log("sys", "SESSION", "call ended");
  }

  $("go").onclick = start;
  $("typed").onsubmit = function (ev) {
    ev.preventDefault();
    var text = ($("said").value || "").trim();
    $("said").value = "";
    if (!text) return;
    if (!dc || dc.readyState !== "open") {
      log("sys", "ERROR", "Start the call first. Typed lines go through GPT Live.");
      return;
    }
    callerBuf = text;
    answerCaller(null);
  };
  $("stop").onclick = stop;
  drawLanes();
})();
