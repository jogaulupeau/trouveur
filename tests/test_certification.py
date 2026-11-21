"""Le filtre de public (classement d'age) : ce qui part vers TMDB, et ce qui n'y part pas.

Lancement :  python tests/test_certification.py

« Au plus -12 » se demande a TMDB avec le classement de la **region** configuree.
Deux choses doivent tenir : la valeur ne part jamais en l'etat si elle est
louche (elle finit dans une URL), et une region sans classement connu n'offre
pas de filtre plutot qu'un filtre qui ne filtre rien.
"""

from __future__ import annotations

import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

import server  # noqa: E402
from trouveur.tmdb import Tmdb  # noqa: E402

ok: list[bool] = []


def check(label: str, condition: object, detail: str = "") -> None:
    ok.append(bool(condition))
    print("%-62s %s%s" % (label, "OK" if condition else "ECHEC",
                          "  " + detail if detail and not condition else ""))


def tmdb(region: str = "FR") -> Tmdb:
    return Tmdb({"tmdb": {"api_key": "0123456789abcdef0123456789abcdef",
                          "language": "fr-FR", "region": region}})


# -- ce qui part vers TMDB ---------------------------------------------------
avec = tmdb()._discover_params({"certification_max": "12"})
check("le plafond part avec le pays de la region",
      avec.get("certification.lte") == "12" and avec.get("certification_country") == "FR",
      str(avec))

sans = tmdb()._discover_params({})
check("sans plafond, aucun critere de classement n'est envoye",
      "certification.lte" not in sans and "certification_country" not in sans, str(sans))

us = tmdb("US")._discover_params({"certification_max": "PG-13"})
check("le pays suit la region configuree",
      us.get("certification_country") == "US" and us.get("certification.lte") == "PG-13", str(us))

# -- la liste des classements de la region -----------------------------------
REPONSE = {"certifications": {
    "FR": [{"certification": "16", "order": 4}, {"certification": "U", "order": 1},
           {"certification": "12", "order": 3}, {"certification": "10", "order": 2},
           {"certification": "18", "order": 5}],
    "US": [{"certification": "G", "order": 1}],
}}


class FauxTmdb(Tmdb):
    def _call(self, path, params=None):  # noqa: D401 - aucun reseau
        return REPONSE


fr = FauxTmdb({"tmdb": {"api_key": "0123456789abcdef0123456789abcdef", "region": "FR"}})
check("les classements sont rendus du plus ouvert au plus strict",
      [c["code"] for c in fr.certifications()] == ["U", "10", "12", "16", "18"],
      str(fr.certifications()))

inconnue = FauxTmdb({"tmdb": {"api_key": "0123456789abcdef0123456789abcdef", "region": "JP"}})
check("une region sans classement connu n'en propose aucun",
      inconnue.certifications() == [], str(inconnue.certifications()))

# -- la valeur recue de la page ---------------------------------------------
def lire(valeur):
    return server._certification({"certification_max": [valeur]} if valeur is not None else {})


check("« 12 », « U » et « PG-13 » passent", [lire("12"), lire("U"), lire("PG-13")] == ["12", "U", "PG-13"])
check("l'absence de valeur donne None", lire(None) is None and lire("") is None)
check("une valeur louche est ecartee, pas transmise",
      lire("12&with_genres=28") is None and lire(";drop") is None and lire("A" * 20) is None)

print("\n%d/%d verifications passees" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
