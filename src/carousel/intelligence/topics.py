"""Topics v1 (plan task 24): versioned topic models per radar, with identities that survive refits.

A **topic model** is fitted on a radar's papers (those its scans observed),
using the embeddings the store already holds:

1. **Clustering**: spherical k-means on L2-normalized vectors, seeded
   k-means++ (``SEED``), papers in ID order, so the same papers and embedding
   model always give the same topics. ``k`` is chosen by the mean silhouette
   (cosine distance) over ``2..K_MAX``, on a fixed sample; clusters smaller than
   ``MIN_TOPIC_SIZE`` are dropped and their papers left unassigned. When even the
   best silhouette is below ``MIN_SILHOUETTE`` the papers don't separate into
   topics, and no model is fitted rather than an arbitrary one.
2. **Labels**: distinctive words and word pairs per topic (c-TF-IDF over titles
   and abstracts, kept only when several members use them; generic scientific
   words are left out). The label is the top
   three; ``key_terms`` (top eight) are what topic counts query later (task 25).
3. **Assignment**: a paper joins the nearest topic when its similarity to that
   centroid reaches the topic's threshold (the 5th percentile of its members'
   similarities, a little relaxed); otherwise it stays **unassigned**. Every
   assignment records the model's ``classifier_version``.
4. **Versions and identity**: ``classifier_version`` hashes the algorithm, the
   embedding model, the parameters, and the fit corpus. A refit is a new version
   with an explicit mapping to the previous one, built from shared members and
   centroid similarity: a topic that is the **same** keeps its ``topic_id`` (its
   lineage), and **splits**, **merges**, **new** topics, and **retired** ones are
   recorded, never silently relabelled. Counts and trends (tasks 25-27) compare
   a topic only within one version or across a ``same`` mapping.

Pure Python: the fit corpus is capped (``MAX_FIT_PAPERS``, the most recently
observed) so a fit takes seconds, and assigning a new paper is ``k`` dot
products.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import random
from collections import Counter, defaultdict
from operator import mul
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from carousel.core.text import search_tokens

TOPICS_VERSION = "topics-1"
SEED = 20261124
MIN_TOPIC_SIZE = 5
MIN_FIT_PAPERS = 15
MAX_FIT_PAPERS = 600
K_MAX = 12
MAX_ITER = 40
N_INIT = 6            # seeded restarts per k; the best (highest total similarity) wins
SILHOUETTE_SAMPLE = 300
THRESHOLD_PERCENTILE = 0.05
THRESHOLD_MARGIN = 0.02
SAME_SHARE = 0.5      # a mapping is "same" when most shared members stay together...
LINK_SHARE = 0.3      # ...and a split or merge part must carry at least this share
CENTROID_SAME = 0.85
PHRASE_SHARE = 0.6
MIN_SILHOUETTE = 0.05  # below this, the papers don't separate into topics: no model is fitted (unless k is given)
GENERIC = frozenset(
    "can may might also however thus here well found show shown showed shows study studies result results data "
    "analysis analyses based new novel one two three first using use used including include across within among "
    "different many several high higher low lower increase increased important significant significantly approach "
    "method methods model models effect effects level levels associated role potential suggest suggests provide "
    "provides present report reported observed identify identified overall large small type types case cases "
    "number total use further whether while could would should these this those such other".split())    # a label word becomes its phrase when the phrase carries this share of the word's uses  # without shared members, centroids this close (and mutually nearest) are the same topic

Vector = List[float]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(map(mul, a, b))


def _normalize(v: Sequence[float]) -> Vector:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _mean(vectors: Sequence[Sequence[float]]) -> Vector:
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    return _normalize(acc)


# -------------------------
# Clustering
# -------------------------

def kmeans(vectors: Sequence[Vector], k: int, seed: int = SEED, max_iter: int = MAX_ITER,
           n_init: int = N_INIT) -> List[int]:
    """Spherical k-means, best of ``n_init`` seeded k-means++ starts; returns a cluster index per vector."""
    best: Optional[Tuple[float, List[int]]] = None
    for r in range(n_init):
        labels, centers = _kmeans_once(vectors, k, seed * 1000 + k * 10 + r, max_iter)
        score = sum(_dot(v, centers[c]) for v, c in zip(vectors, labels))
        if best is None or score > best[0] + 1e-9:
            best = (score, labels)
    return best[1]  # type: ignore[index]


def _kmeans_once(vectors: Sequence[Vector], k: int, seed: int, max_iter: int) -> Tuple[List[int], List[Vector]]:
    n = len(vectors)
    rng = random.Random(seed)
    centers = [vectors[rng.randrange(n)]]
    best = [1.0 - _dot(v, centers[0]) for v in vectors]
    while len(centers) < k:
        weights = [max(d, 0.0) ** 2 for d in best]
        total = sum(weights)
        if total <= 0:
            break
        r, acc, pick = rng.random() * total, 0.0, n - 1
        for i, w in enumerate(weights):
            acc += w
            if acc >= r:
                pick = i
                break
        centers.append(vectors[pick])
        best = [min(b, 1.0 - _dot(v, vectors[pick])) for b, v in zip(best, vectors)]
    labels = [-1] * n
    for _ in range(max_iter):
        new = [max(range(len(centers)), key=lambda c: (_dot(v, centers[c]), -c)) for v in vectors]
        if new == labels:
            break
        labels = new
        groups: Dict[int, List[Vector]] = defaultdict(list)
        for v, c in zip(vectors, labels):
            groups[c].append(v)
        for c in range(len(centers)):
            if groups.get(c):
                centers[c] = _mean(groups[c])
            else:  # an empty cluster takes the point farthest from its own centre
                far = min(range(n), key=lambda i: (_dot(vectors[i], centers[labels[i]]), i))
                centers[c] = vectors[far]
    return labels, centers


def silhouette(vectors: Sequence[Vector], labels: Sequence[int], sample: int = SILHOUETTE_SAMPLE) -> float:
    """Mean silhouette (cosine distance) over a fixed, evenly spaced sample."""
    n = len(vectors)
    idx = list(range(n)) if n <= sample else [round(i * (n - 1) / (sample - 1)) for i in range(sample)]
    if len(set(labels[i] for i in idx)) < 2:
        return -1.0
    scores = []
    for i in idx:
        by: Dict[int, List[float]] = defaultdict(list)
        for j in idx:
            if j != i:
                by[labels[j]].append(1.0 - _dot(vectors[i], vectors[j]))
        own = by.get(labels[i])
        others = [sum(d) / len(d) for c, d in by.items() if c != labels[i] and d]
        if not own or not others:
            continue
        a, b = sum(own) / len(own), min(others)
        scores.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return sum(scores) / len(scores) if scores else -1.0


def choose_k(vectors: Sequence[Vector], k_max: int = K_MAX) -> Tuple[int, List[int], Dict[int, float]]:
    """The k (2..k_max, at least MIN_TOPIC_SIZE papers per cluster on average) with the best silhouette."""
    hi = min(k_max, len(vectors) // MIN_TOPIC_SIZE)
    if hi < 2:
        return 1, [0] * len(vectors), {}
    scores: Dict[int, float] = {}
    best: Tuple[float, int, List[int]] = (-2.0, 1, [0] * len(vectors))
    for k in range(2, hi + 1):
        labels = kmeans(vectors, k)
        s = round(silhouette(vectors, labels), 6)
        scores[k] = s
        if s > best[0]:  # ties keep the smaller k
            best = (s, k, labels)
    return best[1], best[2], scores


# -------------------------
# Labels (c-TF-IDF)
# -------------------------

def _terms(text: str) -> List[str]:
    toks = [t for t in search_tokens(text) if len(t) > 2 and not t.isdigit() and t not in GENERIC]
    return toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]


def key_terms(texts_by_topic: Mapping[Any, Sequence[str]], n: int = 8) -> Dict[Any, List[str]]:
    """Distinctive terms per topic: class-based TF-IDF, each term used by at least two members (or 20%)."""
    tf: Dict[Any, Counter] = {}
    df: Dict[Any, Counter] = {}
    for c, texts in texts_by_topic.items():
        tf[c], df[c] = Counter(), Counter()
        for t in texts:
            terms = _terms(t)
            tf[c].update(terms)
            df[c].update(set(terms))
    total = Counter()
    for c in tf:
        total.update(tf[c])
    avg = sum(sum(v.values()) for v in tf.values()) / max(1, len(tf))
    out: Dict[Any, List[str]] = {}
    for c, counts in tf.items():
        size = max(1, sum(counts.values()))
        need = max(2, math.ceil(0.2 * len(texts_by_topic[c])))
        scored = sorted(((counts[t] / size) * math.log(1 + avg / total[t]), t) for t in counts if df[c][t] >= need)
        eligible = {t for _, t in scored}
        chosen: List[str] = []
        for _, t in sorted(scored, key=lambda x: (-x[0], x[1])):
            if " " not in t:  # a word that mostly occurs in one phrase is shown as that phrase ("coral reef")
                phrases = sorted((counts[b], b) for b in eligible if " " in b and t in b.split()
                                 and counts[b] >= PHRASE_SHARE * counts[t])
                if phrases and phrases[-1][1] not in chosen:
                    t = phrases[-1][1]
            if t in chosen:
                continue
            words = t.split()
            if len(words) == 1 and any(t in c2.split() for c2 in chosen):
                continue  # a word already covered by a chosen pair
            if len(words) == 2 and all(w in chosen for w in words):
                continue  # a pair of two words already chosen
            chosen.append(t)
            if len(chosen) == n:
                break
        out[c] = chosen
    return out


# -------------------------
# Models
# -------------------------

@dataclasses.dataclass
class Topic:
    topic_id: str          # lineage: kept across refits while the topic is the "same"
    label: str
    key_terms: List[str]
    centroid: Vector
    size: int
    threshold: float


@dataclasses.dataclass
class TopicModel:
    classifier_version: str
    radar_id: str
    embedder: str
    params: Dict[str, Any]
    topics: List[Topic]
    members: Dict[str, Optional[str]]  # paper_id -> topic_id (None: unassigned) for the fit corpus
    fitted_at: str
    previous_version: Optional[str] = None
    mapping: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    confidence: Dict[str, float] = dataclasses.field(default_factory=dict)  # fit member -> similarity to its topic

    def topic(self, topic_id: str) -> Optional[Topic]:
        return next((t for t in self.topics if t.topic_id == topic_id), None)

    def assign(self, vector: Sequence[float]) -> Tuple[Optional[str], float]:
        """(topic_id, similarity) of the nearest topic, or (None, similarity) when below its threshold."""
        if not self.topics:
            return None, 0.0
        sims = [(_dot(vector, t.centroid), t) for t in self.topics]
        sim, t = max(sims, key=lambda x: (x[0], x[1].topic_id))
        return (t.topic_id if sim >= t.threshold else None), round(sim, 6)


def _version(radar_id: str, embedder: str, params: Mapping[str, Any], corpus: Sequence[str]) -> str:
    blob = json.dumps({"algorithm": TOPICS_VERSION, "radar": radar_id, "embedder": embedder, "params": params,
                       "corpus": sorted(corpus)}, sort_keys=True)
    return f"{TOPICS_VERSION}:{hashlib.sha256(blob.encode()).hexdigest()[:12]}"


class NoTopicStructure(ValueError):
    """The papers don't separate into distinct topics; no model was fitted."""


def fit(radar_id: str, corpus: Sequence[Tuple[str, Vector, str]], embedder: str, fitted_at: str,
        previous: Optional[TopicModel] = None, k: Optional[int] = None, k_max: int = K_MAX) -> TopicModel:
    """Fit a topic model on ``corpus`` = [(paper_id, normalized vector, text)], mapped to ``previous`` if given."""
    if len(corpus) < MIN_FIT_PAPERS:
        raise ValueError(f"a topic model needs at least {MIN_FIT_PAPERS} papers with embeddings (got {len(corpus)})")
    corpus = sorted(corpus, key=lambda x: x[0])
    ids = [c[0] for c in corpus]
    vectors = [_normalize(c[1]) for c in corpus]
    if k is None:
        k, labels, scores = choose_k(vectors, k_max)
        best = max(scores.values(), default=-1.0)
        if best < MIN_SILHOUETTE:
            raise NoTopicStructure(f"these {len(ids)} papers don't separate into topics (best silhouette {best:.3f} "
                                   f"< {MIN_SILHOUETTE}); with the hashing embedder, semantic embeddings usually help")
    else:
        labels, scores = kmeans(vectors, k), {}
    params = {"k": k, "seed": SEED, "n_init": N_INIT, "min_topic_size": MIN_TOPIC_SIZE, "k_max": k_max, "max_iter": MAX_ITER,
              "threshold_percentile": THRESHOLD_PERCENTILE, "silhouette": scores}
    version = _version(radar_id, embedder, {kk: v for kk, v in params.items() if kk != "silhouette"}, ids)

    groups: Dict[int, List[int]] = defaultdict(list)
    for i, c in enumerate(labels):
        groups[c].append(i)
    kept = sorted((g for g in groups.values() if len(g) >= MIN_TOPIC_SIZE), key=lambda g: (-len(g), ids[g[0]]))
    texts = {n: [corpus[i][2] for i in g] for n, g in enumerate(kept)}
    terms = key_terms(texts)
    topics: List[Topic] = []
    members: Dict[str, Optional[str]] = {pid: None for pid in ids}
    confidence: Dict[str, float] = {}
    for n, g in enumerate(kept):
        centroid = _mean([vectors[i] for i in g])
        sims = sorted(_dot(vectors[i], centroid) for i in g)
        threshold = max(0.0, sims[int(THRESHOLD_PERCENTILE * (len(sims) - 1))] - THRESHOLD_MARGIN)
        tid = "tp_" + hashlib.sha256(f"{version}:{n}".encode()).hexdigest()[:10]
        topics.append(Topic(topic_id=tid, label=" · ".join(terms[n][:3]) or f"topic {n + 1}", key_terms=terms[n],
                            centroid=centroid, size=len(g), threshold=round(threshold, 6)))
        for i in g:
            members[ids[i]] = tid
            confidence[ids[i]] = round(_dot(vectors[i], centroid), 6)
    for i, pid in enumerate(ids):  # dropped small clusters' papers: their nearest topic's similarity, unassigned
        if members[pid] is None and topics:
            confidence[pid] = round(max(_dot(vectors[i], t.centroid) for t in topics), 6)
    model = TopicModel(classifier_version=version, radar_id=radar_id, embedder=embedder, params=params,
                       topics=topics, members=members, fitted_at=fitted_at, confidence=confidence)
    if previous is not None:
        relink(previous, model)
    return model


# -------------------------
# Refits: mapping versions and keeping identities
# -------------------------

def relink(old: TopicModel, new: TopicModel) -> None:
    """Map ``new``'s topics to ``old``'s, renaming a topic that is the *same* to its old lineage ID.

    Relations (one row each in ``new.mapping``): ``same`` (old -> new), ``split`` (old -> each part),
    ``merge`` (each old -> merged), ``new`` (-> new), ``retired`` (old ->).
    """
    shared = [p for p in new.members if p in old.members and new.members[p] and old.members[p]]
    pairs = Counter((old.members[p], new.members[p]) for p in shared)
    old_n = Counter(old.members[p] for p in shared)
    new_n = Counter(new.members[p] for p in shared)
    cos = {(o.topic_id, n.topic_id): round(_dot(o.centroid, n.centroid), 6) for o in old.topics for n in new.topics}
    receivers = {o.topic_id: [n for (oo, n), c in pairs.items() if oo == o.topic_id and c / old_n[o.topic_id] >= LINK_SHARE]
                 for o in old.topics}
    contributors = {n.topic_id: [o for (o, nn), c in pairs.items() if nn == n.topic_id and c / new_n[n.topic_id] >= LINK_SHARE]
                    for n in new.topics}
    rename: Dict[str, str] = {}
    rows: List[Dict[str, Any]] = []

    def row(rel: str, o: Optional[str], n: Optional[str]) -> None:
        rows.append({"relation": rel, "from_topic": o, "to_topic": n,
                     "shared_members": pairs.get((o, n), 0) if o and n else 0,
                     "centroid_similarity": cos.get((o, n)) if o and n else None})

    for n in new.topics:
        cs = contributors[n.topic_id]
        if not shared:  # nothing in common to compare: fall back to mutually nearest centroids
            o = max(old.topics, key=lambda t: cos[(t.topic_id, n.topic_id)], default=None)
            if o and cos[(o.topic_id, n.topic_id)] >= CENTROID_SAME and \
                    max(new.topics, key=lambda t: cos[(o.topic_id, t.topic_id)]).topic_id == n.topic_id:
                rename[n.topic_id] = o.topic_id
                row("same", o.topic_id, n.topic_id)
            else:
                row("new", None, n.topic_id)
            continue
        if len(cs) == 1 and receivers[cs[0]] == [n.topic_id] and \
                pairs[(cs[0], n.topic_id)] / old_n[cs[0]] >= SAME_SHARE:
            rename[n.topic_id] = cs[0]
            row("same", cs[0], n.topic_id)
        elif len(cs) >= 2:
            for o in sorted(cs):
                row("merge", o, n.topic_id)
        elif len(cs) == 1:
            row("split", cs[0], n.topic_id)
        else:
            row("new", None, n.topic_id)
    mapped_old = {r["from_topic"] for r in rows if r["from_topic"]}
    for o in old.topics:
        if o.topic_id not in mapped_old:
            row("retired", o.topic_id, None)
    for t in new.topics:  # the same topic keeps its identity
        t.topic_id = rename.get(t.topic_id, t.topic_id)
    for r in rows:
        r["to_topic"] = rename.get(r["to_topic"], r["to_topic"]) if r["to_topic"] else None
    new.members = {p: rename.get(t, t) if t else None for p, t in new.members.items()}
    new.previous_version = old.classifier_version
    new.mapping = sorted(rows, key=lambda r: (r["relation"], r["from_topic"] or "", r["to_topic"] or ""))


# -------------------------
# With a store: fitting a radar, assigning papers, and the scan hook
# -------------------------

def corpus_for(store: Any, radar_id: str, embedder: Any, limit: int = MAX_FIT_PAPERS) -> List[Tuple[str, Vector, str]]:
    """The radar's most recently observed papers (up to ``limit``) with vectors under ``embedder`` and their text."""
    from carousel.search.embeddings import ensure_embeddings
    from carousel.search.lexical import paper_text

    papers = [p for p in (store.get(pid) for pid in store.radar_paper_ids(radar_id)[:limit]) if p is not None]
    vectors, _ = ensure_embeddings(store, papers, embedder)
    return [(p.paper_id, vectors[p.paper_id], paper_text(p)) for p in papers if p.paper_id in vectors]


def fit_radar(store: Any, radar_id: str, embedder: Any, fitted_at: str,
              k: Optional[int] = None) -> Tuple[TopicModel, bool]:
    """Fit a new topic model for the radar, mapped to its active one (if any), and make it active.

    Returns ``(model, changed)``: refitting the same papers with the same embedder and settings gives the same
    version, so the stored model is kept and ``changed`` is False.
    """
    previous = store.active_topic_model(radar_id)
    model = fit(radar_id, corpus_for(store, radar_id, embedder), embedder.model_version, fitted_at,
                previous=previous if previous and previous.embedder == embedder.model_version else None, k=k)
    if previous is not None and model.classifier_version == previous.classifier_version:
        return previous, False  # same papers, embedder, and settings: nothing changed
    if previous is not None and model.previous_version is None:
        model.previous_version = previous.classifier_version  # another embedder: not comparable, so no mapping
    store.save_topic_model(model)
    return model, True


def assign_papers(store: Any, model: TopicModel, embedder: Any, paper_ids: Iterable[str], at: Any = None) -> Dict[str, int]:
    """Assign papers the model hasn't placed yet; returns ``{assigned, unassigned, already}``."""
    from carousel.search.embeddings import ensure_embeddings

    placed = store.paper_topics(model.classifier_version)
    todo = sorted({store.resolve(p) for p in paper_ids} - set(placed))
    papers = [p for p in (store.get(pid) for pid in todo) if p is not None]
    vectors, _ = ensure_embeddings(store, papers, embedder)
    rows = [(pid, *model.assign(_normalize(vectors[pid]))) for pid in sorted(vectors)]
    store.assign_topics(model.classifier_version, rows, at)
    return {"assigned": sum(1 for r in rows if r[1]), "unassigned": sum(1 for r in rows if not r[1]),
            "already": len(set(placed) & {store.resolve(p) for p in paper_ids})}


def update_topics(store: Any, radar_id: str, embedder: Any, paper_ids: Iterable[str], at: Any) -> Dict[str, Any]:
    """After a scan: fit the radar's first model once it has enough papers, else assign the scan's papers.

    Refits are never automatic (``carousel topics fit``): a refit is a new version, and versions should change
    when someone decides, not every scan. A model fitted with another embedder is left alone.
    """
    if embedder is None:
        return {"status": "skipped", "reason": "no embeddings in this scan"}
    model = store.active_topic_model(radar_id)
    stamp = at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(at, "astimezone") else str(at)
    if model is None:
        n = len(store.radar_paper_ids(radar_id))
        if n < MIN_FIT_PAPERS:
            return {"status": "waiting", "reason": f"{n} papers so far; the first topic model needs {MIN_FIT_PAPERS}"}
        try:
            model, _ = fit_radar(store, radar_id, embedder, stamp)
        except NoTopicStructure as exc:
            return {"status": "no_structure", "reason": str(exc)}
        return {"status": "fitted", "classifier_version": model.classifier_version, "topics": len(model.topics)}
    if model.embedder != embedder.model_version:
        return {"status": "skipped", "classifier_version": model.classifier_version,
                "reason": f"the topic model uses {model.embedder}; this scan used {embedder.model_version}"}
    return {"status": "assigned", "classifier_version": model.classifier_version,
            **assign_papers(store, model, embedder, paper_ids, at)}


__all__ = ["MIN_SILHOUETTE", "NoTopicStructure", "MIN_FIT_PAPERS", "MIN_TOPIC_SIZE", "TOPICS_VERSION", "Topic", "TopicModel", "assign_papers", "choose_k",
           "corpus_for", "fit", "fit_radar", "key_terms", "kmeans", "relink", "silhouette", "update_topics"]
