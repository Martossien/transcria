/* Minutages SRT — les quatre fonctions pures extraites de l'éditeur.
 *
 * Ce sont les premiers tests JavaScript du projet. Ils portent sur ce qui casse en silence :
 * un minutage mal relu ne provoque aucune erreur, il DÉCALE des sous-titres, et cela se
 * découvre à la lecture du livrable — longtemps après.
 */
import { beforeAll, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

let T;

beforeAll(() => {
  // Script CLASSIQUE (pas un module) : on l'évalue comme le navigateur le ferait, puis on
  // lit ce qu'il a posé sur `globalThis`. Le convertir en module pour les tests seuls
  // ferait diverger ce qui est testé de ce qui est servi.
  const code = readFileSync("transcria/web/static/js/srt_time.js", "utf8");
  new Function(code)();
  T = globalThis.TranscrIATime;
});

describe("parseTs", () => {
  it("relit les trois formes que l'éditeur reçoit", () => {
    expect(T.parseTs("01:02:03,450")).toBe(3723450);
    expect(T.parseTs("02:03")).toBe(123000);
    expect(T.parseTs("00:00:01.500")).toBe(1500); // le point vaut la virgule
  });

  it("complète les millisecondes à DROITE", () => {
    // Convention SRT : `,5` vaut 500 ms. Compléter à gauche décalerait d'une demi-seconde.
    expect(T.parseTs("00:00:00,5")).toBe(500);
    expect(T.parseTs("00:00:00,05")).toBe(50);
    expect(T.parseTs("00:00:00,005")).toBe(5);
  });

  it("rend null sur une saisie fautive, jamais zéro", () => {
    // Un zéro silencieux placerait le sous-titre au DÉBUT du fichier au lieu de signaler
    // l'erreur de saisie — le pire des deux comportements.
    for (const mauvais of ["", "abc", "1:2:3:4", "00:00:00,1234", "-1:00", "12"]) {
      expect(T.parseTs(mauvais), `« ${mauvais} » devrait être refusé`).toBeNull();
    }
  });

  it("tolère les espaces autour", () => {
    expect(T.parseTs("  00:01:00  ")).toBe(60000);
  });
});

describe("fmt / fmtMs", () => {
  it("produit la forme SRT", () => {
    expect(T.fmtMs(3723450)).toBe("01:02:03,450");
    expect(T.fmt(3723450)).toBe("01:02:03");
  });

  it("remplit toujours les zéros", () => {
    expect(T.fmtMs(0)).toBe("00:00:00,000");
    expect(T.fmtMs(5)).toBe("00:00:00,005");
  });

  it("ne recule pas sous zéro", () => {
    // Un glissement peut produire un minutage négatif ; l'écrire tel quel donnerait un SRT
    // illisible par les lecteurs vidéo.
    expect(T.fmtMs(-1000)).toBe("00:00:00,000");
    expect(T.fmt(-1)).toBe("00:00:00");
  });

  it("passe les heures", () => {
    expect(T.fmt(36000000)).toBe("10:00:00");
  });
});

describe("aller-retour", () => {
  it("relit ce qu'il a écrit", () => {
    // La propriété qui compte : sauvegarder puis rouvrir l'éditeur ne doit RIEN décaler.
    for (const ms of [0, 1, 999, 1000, 61234, 3723450, 35999999]) {
      expect(T.parseTs(T.fmtMs(ms)), `aller-retour sur ${ms}`).toBe(ms);
    }
  });
});

describe("esc", () => {
  it("neutralise ce qui pourrait s'exécuter", () => {
    expect(T.esc("<script>alert(1)</script>")).toBe(
      "&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(T.esc(`a"b'c&d`)).toBe("a&quot;b&#39;c&amp;d");
  });

  it("rend une chaîne vide pour null et undefined", () => {
    // La transcription peut porter des champs absents ; afficher « null » serait un défaut
    // visible dans le livrable.
    expect(T.esc(null)).toBe("");
    expect(T.esc(undefined)).toBe("");
  });
});
