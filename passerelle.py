#!/usr/bin/env python3
"""
Passerelle Assemblée nationale -> outil « Suivi PLF ».

Récupère, depuis l'open data officiel de l'Assemblée nationale (licence ouverte) :
  - les amendements des textes listés dans config.json, au fil des publications ;
  - les scrutins publics correspondants (une fois par jour) ;
et écrit un fichier unique  sortie/suivi-plf-an.json  à importer dans l'outil.

Aucune dépendance : Python 3.9+ suffit.
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
LEG = str(CFG.get("legislature", "17"))
OUT = os.path.join(HERE, CFG.get("dossier_sortie", "sortie"))
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "suivi-plf-passerelle/1.0 (usage parlementaire; open data AN)"}
WORKERS = int(CFG.get("requetes_simultanees", 6))
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


# ---------------------------------------------------------------- réseau
def get(url, tries=4, timeout=60):
    for t in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                return None
        except Exception:
            pass
        time.sleep(2 * (t + 1))
    return None


# ---------------------------------------------------------------- utilitaires JSON AN
def nil(v):
    if isinstance(v, dict) and any(k.endswith("nil") for k in v):
        return None
    return v


def g(d, *path):
    for p in path:
        d = nil(d)
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return nil(d)


def txt(v):
    v = nil(v)
    if v is None:
        return ""
    if isinstance(v, dict):
        v = v.get("#text", "")
    if isinstance(v, list):
        return ", ".join(txt(x) for x in v)
    return str(v)


def as_list(v):
    v = nil(v)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def find_key(obj, pred, depth=0):
    """Premier (clé, valeur) dont la clé satisfait pred, en profondeur."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if pred(k) and nil(v) is not None:
                return v
        for v in obj.values():
            r = find_key(v, pred, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, pred, depth + 1)
            if r is not None:
                return r
    return None


def clean_html(s):
    s = re.sub(r"<\s*br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"</(p|div|li|tr|h\d)>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(html.unescape(s))
    s = re.sub(r"[ \t\u00a0\u202f]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().replace("’", "'")


def clip(s, n):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def load(name, default):
    p = os.path.join(OUT, name)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return default


def save(name, data):
    p = os.path.join(OUT, name)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(p + ".tmp", p)


# ---------------------------------------------------------------- référentiel députés / groupes
def referentiel():
    ref = load("referentiel.json", {})
    if ref and time.time() - ref.get("_t", 0) < 86400:
        return ref
    log("Téléchargement du référentiel des députés et des groupes…")
    url = f"https://data.assemblee-nationale.fr/static/openData/repository/{LEG}/amo/deputes_actifs_mandats_actifs_organes/AMO10_deputes_actifs_mandats_actifs_organes.json.zip"
    data = get(url, timeout=240)
    if not data:
        log("  référentiel indisponible, on garde l'ancien")
        return ref or {"organes": {}, "acteurs": {}}
    new = {"organes": {}, "acteurs": {}, "acteur_groupe": {}}
    z = zipfile.ZipFile(io.BytesIO(data))
    for n in z.namelist():
        if not n.endswith(".json"):
            continue
        try:
            j = json.loads(z.read(n))
        except Exception:
            continue
        if "organe" in j:
            o = j["organe"]
            new["organes"][txt(o.get("uid"))] = {
                "type": txt(o.get("codeType")),
                "abrev": txt(o.get("libelleAbrev")) or txt(o.get("libelleAbrege")),
                "libelle": txt(o.get("libelle")),
            }
        elif "acteur" in j:
            a = j["acteur"]
            uid = txt(a.get("uid"))
            ident = g(a, "etatCivil", "ident") or {}
            new["acteurs"][uid] = " ".join(x for x in (txt(ident.get("civ")), txt(ident.get("prenom")), txt(ident.get("nom"))) if x)
            for m in as_list(g(a, "mandats", "mandat")):
                if txt(g(m, "typeOrgane")) == "GP" and not txt(g(m, "dateFin")):
                    new["acteur_groupe"][uid] = txt(g(m, "organes", "organeRef"))
    new["_t"] = time.time()
    # conserver les anciens noms (députés sortis depuis)
    for k in ("organes", "acteurs"):
        for uid, v in (ref.get(k) or {}).items():
            new[k].setdefault(uid, v)
    save("referentiel.json", new)
    log(f"  {len(new['acteurs'])} députés, {len(new['organes'])} organes")
    return new


def groupe_abrev(ref, po):
    o = ref["organes"].get(po) or {}
    return o.get("abrev") or o.get("libelle") or ""


# ---------------------------------------------------------------- amendements
UID_RE = re.compile(r"AMANR5L(\d+)PO(\d+)B(\w+?)P(\d+)D(\d+)N(\d+)")
TEXTES = {str(t["numero"]): t for t in CFG["textes"] if t.get("actif", True)}
TOUS = ("seance", "commission", "avis")
# Codes d'organe connus (appris automatiquement ensuite à partir des amendements lus)
ORG = {"seance": set(CFG.get("organes_seance", ["717460"])), "commission": set(CFG.get("organes_commission", ["59048"]))}


def texte_of(uid):
    m = UID_RE.search(uid)
    if not m or m.group(1) != LEG:
        return None, None
    return TEXTES.get(m.group(3)), m


def kind_of(org):
    for k, codes in ORG.items():
        if org in codes:
            return k
    return None  # inconnu : on le lit pour l'apprendre


def suivi(t, kind):
    return kind is None or kind in t.get("suivre", TOUS)


def publications(day):
    s = day.strftime("%Y-%m-%d")
    data = get(f"https://www.assemblee-nationale.fr/dyn/opendata/list-publication/publication_{s}.csv", timeout=90)
    out = []
    if not data:
        return out
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.strip().split(";")
        if len(parts) >= 2 and "/AMAN" in parts[1] and parts[1].endswith(".xml"):
            uid = parts[1].rsplit("/", 1)[-1][:-4]
            t, m = texte_of(uid)
            if t and suivi(t, kind_of(m.group(2))):
                out.append((parts[0].strip(), uid))
    return out


def parse(uid, j, ref):
    a = j.get("amendement", j)
    t, m = texte_of(uid)
    ident = g(a, "identification") or {}
    num = txt(ident.get("numeroLong")) or str(int(m.group(6)))
    prefix = txt(ident.get("prefixeOrganeExamen")).upper()
    sig = g(a, "signataires") or {}
    aut = g(sig, "auteur") or {}
    typ = norm(txt(aut.get("typeAuteur")))
    libelle = clean_html(txt(sig.get("libelle")))
    if typ.startswith("gouvernement"):
        auteur, groupe = "Gouvernement", "Gouvernement"
    else:
        acteur = txt(aut.get("acteurRef"))
        auteur = ref["acteurs"].get(acteur) or re.split(r",| et ", libelle)[0].strip()
        groupe = groupe_abrev(ref, txt(aut.get("groupePolitiqueRef"))) or groupe_abrev(ref, ref.get("acteur_groupe", {}).get(acteur, ""))
        if "rapporteur" in typ or "rapporteur" in norm(libelle):
            groupe = groupe or "Commission"
    div = g(a, "pointeurFragmentTexte", "division") or {}
    titre = txt(div.get("titre")) or txt(div.get("articleDesignation"))
    aa = norm(txt(div.get("avant_A_Apres")))
    article = (("Après " if aa.startswith("apr") else "Avant " if aa.startswith("av") else "") + titre).strip()
    corps = g(a, "corps") or {}
    ca = g(corps, "contenuAuteur") or {}
    dispo = clean_html(txt(ca.get("dispositif")))
    expo = clean_html(txt(ca.get("exposeSommaire")))
    cart = clean_html(txt(corps.get("cartoucheInformatif")))
    cdv = g(a, "cycleDeVie") or {}
    sort = txt(cdv.get("sort"))
    etat = txt(g(cdv, "etatDesTraitements", "etat", "libelle"))
    sous = txt(g(cdv, "etatDesTraitements", "sousEtat", "libelle"))
    if not sort:
        sort = sous if (sous and norm(etat).startswith("discut")) else (etat or sous)
    if not dispo and not norm(etat).startswith("irrecevable") and not norm(sort).startswith("irrecevable"):
        frag = get(f"https://www.assemblee-nationale.fr/dyn/{LEG}/amendements/dispositif/{uid}.fragmenthtml", tries=2, timeout=40)
        if frag:
            dispo = clean_html(frag.decode("utf-8", "replace"))
    mission = ""
    mv = find_key(a, lambda k: "mission" in k.lower())
    if mv is not None:
        mission = txt(mv) if not isinstance(mv, dict) else txt(mv.get("libelle") or mv.get("libelleMission") or find_key(mv, lambda k: "libelle" in k.lower()))
    if not mission:
        mm = re.search(r"Mission\s*«\s*([^»]{3,140}?)\s*»", dispo)
        mission = mm.group(1).strip() if mm else ""
    org = m.group(2)
    if prefix == "AN":
        lecture, kind = t.get("seance", "Séance AN"), "seance"
    elif "FIN" in prefix:
        lecture, kind = t.get("commission", "Commission des finances AN"), "commission"
    else:
        lecture, kind = t.get("avis", "Commissions saisies pour avis"), "avis"
    if kind != "avis":
        ORG[kind].add(org)
    dc = find_key(g(a, "discussionCommune") or {}, lambda k: k.lower().startswith("iddiscussion"))
    di = find_key(g(a, "discussionIdentique") or {}, lambda k: k.lower().startswith("iddiscussion"))
    return {
        "uid": uid,
        "num": num,
        "lecture": lecture,
        "_kind": kind,
        "_texte": m.group(3),
        "partie": int(m.group(4)) if m else None,
        "article": article,
        "auteur": auteur,
        "cosignataires": libelle,
        "groupe": groupe,
        "sort": sort,
        "etat": etat,
        "dispositif": clip(dispo, int(CFG.get("longueur_dispositif", 2500))),
        "expose": clip(expo or cart, int(CFG.get("longueur_expose", 1500))),
        "mission": mission,
        "ordre": txt(a.get("triAmendement")),
        "discussionCommune": txt(dc),
        "identique": txt(di),
        "parent": txt(a.get("amendementParentRef")),
        "dateDepot": txt(cdv.get("dateDepot")),
        "dateSort": txt(cdv.get("dateSort")),
        "url": f"https://www.assemblee-nationale.fr/dyn/{LEG}/amendements/{uid}",
        "chronotag": txt(a.get("chronotag")),
    }


def fetch_amdt(uid, ref):
    data = get(f"https://www.assemblee-nationale.fr/dyn/opendata/{uid}.json")
    if not data:
        return uid, None
    try:
        return uid, parse(uid, json.loads(data), ref)
    except Exception as e:
        log(f"  ! {uid} illisible : {e}")
        return uid, None


def maj_amendements(ref):
    state = load("etat.json", {})
    for k, v in (state.get("organes") or {}).items():
        ORG.setdefault(k, set()).update(v)
    amdts = load("amendements.json", {})
    seen = state.get("vus", {})
    today = dt.date.today()
    start = dt.date.fromisoformat(state.get("dernier_jour") or CFG.get("depuis", str(today)))
    start = min(start, today) - dt.timedelta(days=1)
    days = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
    log(f"Lecture des publications du {days[0]} au {days[-1]} ({len(days)} jour(s))…")
    todo = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for pubs in ex.map(publications, days):
            for ts, uid in pubs:
                if ts > seen.get(uid, "") and ts >= todo.get(uid, ""):
                    todo[uid] = ts
    log(f"  {len(todo)} amendement(s) nouveaux ou modifiés")
    ok = 0
    budget = float(CFG.get("budget_minutes", 40)) * 60
    uids = list(todo)
    complet = True
    with ThreadPoolExecutor(WORKERS) as ex:
        for start_i in range(0, len(uids), 300):
            if time.time() - T0 > budget:
                complet = False
                log(f"  budget de temps atteint : {len(uids) - start_i} amendement(s) reportés à la prochaine exécution")
                break
            for uid, rec in ex.map(lambda u: fetch_amdt(u, ref), uids[start_i:start_i + 300]):
                if rec:
                    amdts[uid] = rec
                    seen[uid] = todo[uid]
                    ok += 1
            log(f"  {min(start_i + 300, len(uids))}/{len(uids)}")
            save("amendements.json", amdts)
            state.update(vus=seen)
            save("etat.json", state)
    state.update(vus=seen, organes={k: sorted(v) for k, v in ORG.items()})
    if complet:
        state["dernier_jour"] = str(today)
    save("amendements.json", amdts)
    save("etat.json", state)
    log(f"  {ok} récupéré(s), {len(amdts)} au total")
    return amdts, ok


# ---------------------------------------------------------------- scrutins
AMDT_RE = re.compile(r"amendements?\s+(?:de suppression\s+)?n[°o]s?\s*((?:[IV]+-)?[A-Z]*\d+)", re.I)
ART_RE = re.compile(r"(?:à|après|avant)\s+l'article\s+(liminaire|premier|\d+(?:\s*(?:bis|ter|quater|quinquies|sexies))?)", re.I)


def maj_scrutins(ref):
    kw = [norm(k) for k in CFG.get("scrutins_mots_cles", [])]
    state = load("etat.json", {})
    cache = load("scrutins.json", None)
    if not kw:
        return []
    if cache is not None and time.time() - state.get("scrutins_t", 0) < float(CFG.get("scrutins_toutes_les_heures", 12)) * 3600:
        return cache
    log("Téléchargement des scrutins publics…")
    data = get(f"https://data.assemblee-nationale.fr/static/openData/repository/{LEG}/loi/scrutins/Scrutins.json.zip", timeout=600)
    if not data:
        log("  scrutins indisponibles, on garde les précédents")
        return cache or []
    nous = norm(CFG.get("groupe", ""))
    out = []
    z = zipfile.ZipFile(io.BytesIO(data))
    for n in z.namelist():
        if not n.endswith(".json"):
            continue
        try:
            s = json.loads(z.read(n)).get("scrutin", {})
        except Exception:
            continue
        titre = txt(s.get("titre")) or txt(g(s, "objet", "libelle"))
        if not any(k in norm(titre) for k in kw):
            continue
        dec = g(s, "syntheseVote", "decompte") or {}
        rec = {
            "numero": txt(s.get("numero")),
            "date": txt(s.get("dateScrutin")),
            "titre": titre,
            "sort": txt(g(s, "sort", "code")),
            "type": txt(g(s, "typeVote", "libelleTypeVote")),
            "pour": int(txt(dec.get("pour")) or 0),
            "contre": int(txt(dec.get("contre")) or 0),
            "abst": int(txt(dec.get("abstentions")) or 0),
            "amendements": sorted(set(x.upper() for x in AMDT_RE.findall(titre))),
            "article": (ART_RE.search(titre).group(1) if ART_RE.search(titre) else ""),
            "groupes": {},
            "nous": None,
        }
        for gr in as_list(g(s, "ventilationVotes", "organe", "groupes", "groupe")):
            ab = groupe_abrev(ref, txt(gr.get("organeRef"))) or txt(gr.get("organeRef"))
            v = g(gr, "vote") or {}
            dv = g(v, "decompteVoix") or {}
            rec["groupes"][ab] = {
                "pos": txt(v.get("positionMajoritaire")),
                "p": int(txt(dv.get("pour")) or 0),
                "c": int(txt(dv.get("contre")) or 0),
                "a": int(txt(dv.get("abstentions")) or 0),
                "nv": int(txt(dv.get("nonVotants")) or 0),
                "membres": int(txt(gr.get("nombreMembresGroupe")) or 0),
            }
            if nous and norm(ab) == nous:
                dn = g(v, "decompteNominatif") or {}
                noms = {}
                for key, lab in (("pours", "pour"), ("contres", "contre"), ("abstentions", "abstention"), ("nonVotants", "nonVotant")):
                    noms[lab] = [ref["acteurs"].get(txt(x.get("acteurRef")), txt(x.get("acteurRef"))) for x in as_list(g(dn, key, "votant"))]
                rec["nous"] = {"pos": txt(v.get("positionMajoritaire")), "votes": noms}
        out.append(rec)
    out.sort(key=lambda r: int(r["numero"] or 0))
    state["scrutins_t"] = time.time()
    save("etat.json", state)
    save("scrutins.json", out)
    log(f"  {len(out)} scrutin(s) retenus")
    return out


# ---------------------------------------------------------------- sortie
def main():
    ref = referentiel()
    amdts, n_new = maj_amendements(ref)
    scr = maj_scrutins(ref)
    ignorer_irr = CFG.get("ignorer_irrecevables_des_autres", True)
    nous = norm(CFG.get("groupe", ""))
    items = []
    for r in amdts.values():
        t = TEXTES.get(r.get("_texte") or (UID_RE.search(r["uid"]).group(3) if UID_RE.search(r["uid"]) else ""))
        if not t or (r.get("_kind") and r["_kind"] not in t.get("suivre", TOUS)):
            continue
        if ignorer_irr and norm(r.get("sort", "")).startswith("irrecevable") and norm(r.get("groupe", "")) != nous:
            continue
        r = {k: v for k, v in r.items() if k not in ("chronotag", "cosignataires") and not k.startswith("_") and v not in ("", None)}
        items.append(r)
    items.sort(key=lambda r: (r.get("lecture", ""), r.get("ordre", ""), r.get("num", "")))
    out = {
        "format": "suivi-plf-an",
        "version": 1,
        "legislature": LEG,
        "genere": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "Assemblée nationale, open data (licence ouverte)",
        "textes": list(TEXTES.values()),
        "groupe": CFG.get("groupe", ""),
        "amendements": items,
        "scrutins": scr,
    }
    save(CFG.get("fichier", "suivi-plf-an.json"), out)
    size = os.path.getsize(os.path.join(OUT, CFG.get("fichier", "suivi-plf-an.json"))) / 1e6
    log(f"Fichier écrit : {len(items)} amendements, {len(scr)} scrutins, {size:.1f} Mo")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
