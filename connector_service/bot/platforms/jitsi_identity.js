// Résolveur d'identité JITSI — branche le point d'extension de `capture.js`.
//
// `capture.js` est générique : il sait capter des pistes, pas qui parle. Chaque plateforme
// fournit ici sa propre traduction « piste WebRTC → participant », en interrogeant l'état de
// l'application (source de vérité) plutôt qu'en devinant depuis des identifiants de flux.
//
// Jitsi tient son état dans un store Redux : `features/base/tracks` associe chaque piste à un
// `participantId`, et `features/base/participants` donne le nom affiché.
(function () {
  "use strict";

  function state() {
    try {
      return window.APP && window.APP.store && window.APP.store.getState();
    } catch (e) {
      return null;
    }
  }

  // Retrouve le participant propriétaire d'une MediaStreamTrack reçue.
  function participantOfTrack(st, track) {
    const tracks = (st && st["features/base/tracks"]) || [];
    for (const entry of tracks) {
      if (entry.local || entry.mediaType !== "audio") continue;
      const jitsiTrack = entry.jitsiTrack;
      const media = jitsiTrack && jitsiTrack.getTrack && jitsiTrack.getTrack();
      if (media && media.id === track.id) return entry.participantId;
    }
    return null;
  }

  // Repli : les flux Jitsi sont nommés `<endpointId>-audio-0-1` — l'endpoint y est lisible.
  // Exception : `remote-audio-N` est un nom d'EMPLACEMENT récepteur, pas un endpoint ; en
  // extraire « remote » fabriquerait un participant fantôme.
  function participantOfStreamId(streamId) {
    const s = String(streamId || "");
    if (/^remote-(audio|video)/.test(s)) return null;
    const m = /^([0-9a-zA-Z]+)-audio-/.exec(s);
    return m ? m[1] : null;
  }

  function displayName(st, participantId) {
    try {
      const p = st && st["features/base/participants"];
      const remote = p && p.remote;
      const entry = remote && remote.get && remote.get(participantId);
      return (entry && entry.name) || "";
    } catch (e) {
      return "";
    }
  }

  window.__TRANSCRIA_RESOLVE_IDENTITY__ = function (track, streamId) {
    const st = state();
    if (!st) return null;                       // application pas encore prête
    const id = participantOfTrack(st, track) || participantOfStreamId(streamId);
    if (!id) return null;
    return { id: id, name: displayName(st, id) };
  };
})();
