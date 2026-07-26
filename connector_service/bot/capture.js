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
  // Seuil (échelle s16) au-dessus duquel une piste est jugée PORTEUSE de voix.
  const VOICE_THRESHOLD = 300;

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
  //
  // Une plateforme expose souvent PLUSIEURS pistes pour le même locuteur, dont des muettes,
  // et l'ordre d'arrivée varie d'une session à l'autre. Choisir « la première identifiée »
  // menait donc à capter du silence une fois sur deux. Règle retenue, objective : une piste
  // doit d'abord PROUVER qu'elle porte du son ; la première qui le prouve prend la place du
  // participant, les autres sont abandonnées. Rien n'est émis tant que la preuve n'est pas
  // faite (le silence de tête n'a de toute façon aucune valeur pour la transcription).
  function pipeTrack(track, trackId, participantId, participantName, onEnded) {
    if (!("MediaStreamTrackProcessor" in window)) return; // WebCodecs requis
    attachSink(track);                                    // OBLIGATOIRE (voir ci-dessus)
    const processor = new window.MediaStreamTrackProcessor({ track });
    const reader = processor.readable.getReader();
    let owns = false;
    (async function pump() {
      for (;;) {
        const { value: frame, done } = await reader.read();
        if (done) {
          if (owns && onEnded) onEnded();                 // libère le participant
          return;
        }
        try {
          // AudioData est f32-PLANAIRE : le plan 0 = canal 0 (gauche), numberOfFrames
          // échantillons. On force le MONO (suffisant pour le STT) — lire channels*frames
          // et annoncer stéréo corromprait le PCM (interprété interleaved en aval).
          const buf = new Float32Array(frame.numberOfFrames);
          frame.copyTo(buf, { planeIndex: 0 });

          if (!owns) {
            let peak = 0;
            for (let i = 0; i < buf.length; i++) {
              const a = Math.abs(buf[i]);
              if (a > peak) peak = a;
            }
            // Pas encore de son audible : on n'émet rien et on ne réserve rien.
            if (peak * 32767 < VOICE_THRESHOLD) continue;
            // Une autre piste a déjà prouvé sa voix pour ce participant → celle-ci est un
            // doublon, on l'abandonne.
            if (activeByParticipant.has(participantId)) return;
            activeByParticipant.set(participantId, trackId);
            owns = true;
          }

          if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({
              participant_id: participantId,
              participant_name: participantName || "",
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

  // On écarte UNIQUEMENT le flux MIXÉ de la salle (le capter transcrirait tout en double).
  // ⚠️ Ne PAS écarter les pistes `remote-audio-N` : ce sont des transceivers pré-alloués sur
  // lesquels Jitsi MAPPE les vraies sources — c'est par elles que la voix arrive réellement
  // (vérifié par getStats : l'énergie audio est sur `remote-audio-1`). Les écarter revenait à
  // ne capturer que du silence.
  const MIXED_MARKERS = ["mixedmslabel", "mixedlabelaudio"];
  function isMixedStream(id) {
    const s = String(id);
    return MIXED_MARKERS.some((m) => s.indexOf(m) !== -1);
  }

  // Une même piste peut être signalée plusieurs fois (renégociations) : on ne la branche
  // qu'une seule fois, sinon l'audio serait dupliqué.
  const piped = new Set();

  // UNE seule piste active PAR PARTICIPANT. Indispensable : une plateforme peut exposer
  // plusieurs pistes pour le même locuteur (transceiver mappé + piste nommée par endpoint),
  // dont certaines SILENCIEUSES. Les brancher toutes entrelacerait du silence dans sa parole.
  const activeByParticipant = new Map();

  // Identité : `capture.js` reste GÉNÉRIQUE — chaque plateforme fournit son résolveur via
  // `window.__TRANSCRIA_RESOLVE_IDENTITY__` (cf. platforms/*_identity.js). L'état de
  // l'application arrive parfois APRÈS la piste : on lui laisse un court délai, puis on
  // retombe sur l'identifiant de flux (dégradé mais jamais bloquant).
  const IDENTITY_TIMEOUT_MS = 2500;
  const IDENTITY_POLL_MS = 250;
  // Noms d'emplacements récepteurs génériques (`remote-audio-1`…) : ils ne désignent aucun
  // locuteur en propre. Cf. la politique de repli plus bas.
  const PLACEHOLDER_ID = /^remote-(audio|video)(-\d+)*$/;
  let anyIdentityResolved = false;

  function resolveIdentity(track, streamId) {
    return new Promise((resolve) => {
      let waited = 0;
      (function attempt() {
        let found = null;
        try {
          if (typeof window.__TRANSCRIA_RESOLVE_IDENTITY__ === "function") {
            found = window.__TRANSCRIA_RESOLVE_IDENTITY__(track, streamId);
          }
        } catch (e) {
          found = null;
        }
        if (found && found.id) return resolve(found);
        if (waited >= IDENTITY_TIMEOUT_MS) return resolve(null);
        waited += IDENTITY_POLL_MS;
        setTimeout(attempt, IDENTITY_POLL_MS);
      })();
    });
  }

  // Interception WebRTC : chaque piste audio reçue est routée vers pipeTrack.
  const NativeRTCPeerConnection = window.RTCPeerConnection;
  window.RTCPeerConnection = function (...args) {
    const pc = new NativeRTCPeerConnection(...args);
    pc.addEventListener("track", (event) => {
      if (event.track && event.track.kind === "audio") {
        // Attribution fine (endpoint id / DOM) affinée au gate ; par défaut = id de piste.
        const streamId = (event.streams[0] && event.streams[0].id) || "";
        const track = event.track;
        const trackId = track.id;
        if (isMixedStream(streamId) || isMixedStream(trackId)) return;  // mixage global
        if (piped.has(trackId)) return;                                  // déjà branchée
        piped.add(trackId);

        resolveIdentity(track, streamId).then((identity) => {
          let pid;
          let name = "";
          if (identity && identity.id) {
            pid = identity.id;
            name = identity.name || "";
            anyIdentityResolved = true;
          } else {
            const fallback = streamId || trackId;
            // Piste NON identifiable et au nom d'emplacement générique : c'est un récepteur
            // pré-alloué, doublon SILENCIEUX d'un locuteur déjà capté (mesuré : crête 0).
            // Garde-fou : on ne l'écarte que si la plateforme a su identifier au moins une
            // piste — sinon (aucun résolveur) mieux vaut capter en dégradé que rien du tout.
            if (anyIdentityResolved && PLACEHOLDER_ID.test(fallback)) return;
            pid = fallback;
          }
          // La place du participant est prise à la PREUVE de voix (cf. pipeTrack), pas ici :
          // l'ordre d'arrivée des pistes n'est pas fiable.
          pipeTrack(track, trackId, pid, name, () => {
            if (activeByParticipant.get(pid) === trackId) activeByParticipant.delete(pid);
          });
        });
      }
    });
    return pc;
  };
  window.RTCPeerConnection.prototype = NativeRTCPeerConnection.prototype;
  // Préserve les méthodes statiques (generateCertificate…) que certains clients appellent.
  Object.setPrototypeOf(window.RTCPeerConnection, NativeRTCPeerConnection);
})();
