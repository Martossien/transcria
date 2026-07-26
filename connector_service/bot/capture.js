// Payload de capture in-page du bot de réunion (code TranscrIA, écrit de zéro).
//
// Injecté dans la page de la réunion AVANT chargement. Intercepte les pistes audio distantes
// au niveau WebRTC, les décode en PCM par piste via WebCodecs, et pousse le résultat sur le
// PONT PCM neutre (WebSocket locale vers connector_service.bot.bridge_server). Aucune capture
// de périphérique système : tout est résident dans la page (marche derrière proxy).
//
// Confirmé au gate manuel (banc d'essai Jitsi). Format poussé = contrat de bridge_source :
//   { participant_id, participant_name, pcm (base64 s16le), sample_rate_hz, channels }
(function () {
  "use strict";
  const BRIDGE_URL = window.__TRANSCRIA_BRIDGE_URL__ || "ws://127.0.0.1:8791";
  const TARGET_RATE = 16000;

  let socket = null;
  function connect() {
    socket = new WebSocket(BRIDGE_URL);
    socket.binaryType = "arraybuffer";
    socket.onclose = () => setTimeout(connect, 1000); // reconnexion simple
  }
  connect();

  function floatToPcm16Base64(float32) {
    const pcm = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    let bin = "";
    const bytes = new Uint8Array(pcm.buffer);
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  // Chromium ne fait COULER une piste audio distante que si elle est branchée à un
  // « puits » audio : sans ça, MediaStreamTrackProcessor ne rend JAMAIS de frame (vérifié).
  // Un élément <audio> MUET suffit (le bot n'émet aucun son). On garde la référence pour
  // que le ramasse-miettes ne coupe pas le puits.
  const sinks = [];
  function attachSink(track) {
    const el = new Audio();
    el.srcObject = new MediaStream([track]);
    el.muted = true;
    el.play().catch(() => {});
    sinks.push(el);
  }

  // Consomme une piste audio distante → frames AudioData → push PCM sur le pont.
  function pipeTrack(track, participantId) {
    if (!("MediaStreamTrackProcessor" in window)) return; // WebCodecs requis
    attachSink(track);                                    // OBLIGATOIRE (voir ci-dessus)
    const processor = new window.MediaStreamTrackProcessor({ track });
    const reader = processor.readable.getReader();
    (async function pump() {
      for (;;) {
        const { value: frame, done } = await reader.read();
        if (done) return;
        try {
          // AudioData est f32-PLANAIRE : le plan 0 = canal 0 (gauche), numberOfFrames
          // échantillons. On force le MONO (suffisant pour le STT) — lire channels*frames
          // et annoncer stéréo corromprait le PCM (interprété interleaved en aval).
          const buf = new Float32Array(frame.numberOfFrames);
          frame.copyTo(buf, { planeIndex: 0 });
          if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
              participant_id: participantId,
              pcm: floatToPcm16Base64(buf),
              sample_rate_hz: frame.sampleRate || TARGET_RATE,
              channels: 1,
            }));
          }
        } finally {
          frame.close();
        }
      }
    })();
  }

  // Interception WebRTC : chaque piste audio reçue est routée vers pipeTrack.
  const NativeRTCPeerConnection = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new NativeRTCPeerConnection(...args);
    pc.addEventListener("track", (event) => {
      if (event.track && event.track.kind === "audio") {
        // Attribution fine (endpoint id / DOM) affinée au gate ; par défaut = id de piste.
        const pid = (event.streams[0] && event.streams[0].id) || event.track.id;
        pipeTrack(event.track, pid);
      }
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = NativeRTCPeerConnection.prototype;
  // Préserve les méthodes statiques (generateCertificate…) que certains clients appellent.
  Object.setPrototypeOf(window.RTCPeerConnection, NativeRTCPeerConnection);
})();
