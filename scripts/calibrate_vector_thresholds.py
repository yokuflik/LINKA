"""Read-only calibration of VECTOR_SEARCH_MAX_DISTANCE / _EXPANDED
(VECTOR_EMBEDDING_RETRY_AND_TASKTYPE_PLAN.md Part 3, ADR 0107).

Never touches Postgres or Redis. Embeds the synthetic multilingual corpus in
scripts/calibration_corpus/ with real Gemini calls (docs = RETRIEVAL_DOCUMENT,
queries = RETRIEVAL_QUERY; `--baseline` also runs without taskType), caches
vectors in a local JSON file, and writes a Markdown + JSON report.

    python3 -m scripts.calibrate_vector_thresholds --out-dir <dir>
    python3 -m scripts.calibrate_vector_thresholds --out-dir <dir> --baseline
"""

import argparse
import asyncio
import hashlib
import importlib
import json
import os

from dotenv import load_dotenv

load_dotenv()

import numpy as np

from config import settings
from modules.vector_search import gemini_client
from modules.vector_search.errors import EmbeddingProviderQuotaExceededError
from scripts.calibration_corpus import LANGS, TOPIC_GROUP

CACHE_PATH = ".dev_cache/calibration_embeddings.json"
BATCH = 90
GRID = np.round(np.arange(0.10, 0.80, 0.005), 3)
STRICT_PRECISION = 0.9
EXPANDED_RECALL = 0.9


def load_corpus():
    docs, queries = [], []  # docs: (text, lang, topic|None); queries: (text, lang, topic, kind)
    confuser_against = {}
    for lang in LANGS:
        mod = importlib.import_module(f"scripts.calibration_corpus.{lang}")
        for topic, (ds, kw, q) in mod.TOPICS.items():
            docs += [(d, lang, topic) for d in ds]
            queries += [(kw, lang, topic, "kw"), (q, lang, topic, "q")]
        for text, against in mod.CONFUSERS:
            docs.append((text, lang, None))
            confuser_against[text] = against
    return docs, queries


def _key(task_type, text):
    raw = f"{settings.GEMINI_EMBED_MODEL}:{settings.VECTOR_EMBEDDING_DIM}:{task_type}:{text}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def embed_all(texts, task_type, cache):
    todo = [t for t in dict.fromkeys(texts) if _key(task_type, t) not in cache]
    for i in range(0, len(todo), BATCH):
        chunk = todo[i : i + BATCH]
        for attempt in range(5):
            try:
                vecs = await gemini_client.embed_batch(chunk, task_type=task_type)
                break
            except EmbeddingProviderQuotaExceededError:
                print("429 - sleeping 30s")
                await asyncio.sleep(30)
        else:
            raise SystemExit("giving up after repeated 429s")
        for t, v in zip(chunk, vecs):
            cache[_key(task_type, t)] = v
        print(f"  embedded {min(i + BATCH, len(todo))}/{len(todo)} ({task_type})")
        await asyncio.sleep(1)
    return np.array([cache[_key(task_type, t)] for t in texts], dtype=np.float32)


def _norm(m):
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def build_pairs(docs, queries, qvecs, dvecs):
    dist = 1.0 - _norm(qvecs) @ _norm(dvecs).T  # (Q, D), pgvector `<=>` semantics
    rows = []
    for qi, (_qt, ql, qtopic, kind) in enumerate(queries):
        for di, (_dt, dl, dtopic) in enumerate(docs):
            if dtopic is None:
                label = 0
            elif dtopic == qtopic:
                label = 2
            elif TOPIC_GROUP[dtopic] == TOPIC_GROUP[qtopic]:
                label = 1
            else:
                label = 0
            rows.append((qi, di, ql, dl, kind, label))
    meta = np.array([(r[5], r[4] == "kw", r[2] == r[3], (r[2], r[3]) in (("he", "en"), ("en", "he")))
                     for r in rows], dtype=object)
    d = np.array([dist[r[0], r[1]] for r in rows])
    return d, meta


def sweep(d, labels, positive):
    pos = np.isin(labels, positive)
    p_total = pos.sum()
    out = []
    for t in GRID:
        pred = d < t
        tp = int((pred & pos).sum())
        fp = int((pred & ~pos).sum())
        out.append((float(t), tp / (tp + fp) if tp + fp else 1.0, tp / p_total if p_total else 0.0))
    return out


def pick_strict(curve):
    ok = [c for c in curve if c[1] >= STRICT_PRECISION and c[2] > 0]
    return max(ok, key=lambda c: (c[2], -c[0])) if ok else None


def pick_expanded(curve):
    ok = [c for c in curve if c[2] >= EXPANDED_RECALL]
    return max(ok, key=lambda c: (c[1], -c[0])) if ok else None


def at(curve, t):
    return min(curve, key=lambda c: abs(c[0] - t))


def analyse(d, meta):
    labels = meta[:, 0].astype(int)
    kw, same, cross_he_en = meta[:, 1].astype(bool), meta[:, 2].astype(bool), meta[:, 3].astype(bool)
    scopes = {
        "same-language": same,
        "cross he<->en": cross_he_en,
        "cross-language (all)": ~same,
        "same-language keyword queries": same & kw,
        "same-language question queries": same & ~kw,
        "all pairs": np.ones(len(d), dtype=bool),
    }
    res = {}
    for name, mask in scopes.items():
        dd, ll = d[mask], labels[mask]
        strict = sweep(dd, ll, [2])
        expanded = sweep(dd, ll, [1, 2])
        res[name] = {
            "n_pairs": int(mask.sum()),
            "percentiles": {
                str(lab): [round(float(np.percentile(dd[ll == lab], p)), 3) for p in (5, 25, 50, 75, 95)]
                for lab in (2, 1, 0) if (ll == lab).any()
            },
            "strict_best": pick_strict(strict),
            "expanded_best": pick_expanded(expanded),
            "strict_at_current": at(strict, settings.VECTOR_SEARCH_MAX_DISTANCE),
            "expanded_at_current": at(expanded, settings.VECTOR_SEARCH_MAX_DISTANCE_EXPANDED),
        }
    return res


def per_language(docs, queries, qvecs, dvecs):
    d, meta = build_pairs(docs, queries, qvecs, dvecs)
    nq, nd = len(queries), len(docs)
    qlang = np.repeat([q[1] for q in queries], nd)
    dlang = np.tile([x[1] for x in docs], nq)
    out = {}
    labels = meta[:, 0].astype(int)
    for lang in LANGS:
        m = (qlang == lang) & (dlang == lang)
        out[lang] = {
            "strict_best": pick_strict(sweep(d[m], labels[m], [2])),
            "expanded_best": pick_expanded(sweep(d[m], labels[m], [1, 2])),
        }
    return out


def fmt(c):
    return "n/a" if c is None else f"t={c[0]:.3f} (precision {c[1]:.2f}, recall {c[2]:.2f})"


def render(title, res, perlang):
    lines = [f"## {title}", ""]
    for name, r in res.items():
        lines += [f"### {name} ({r['n_pairs']} pairs)", "",
                  "| label | p5 | p25 | p50 | p75 | p95 |", "|---|---|---|---|---|---|"]
        for lab, ps in r["percentiles"].items():
            lines.append(f"| {lab} | " + " | ".join(f"{p:.3f}" for p in ps) + " |")
        lines += ["",
                  f"- strict (label 2 vs rest, precision>={STRICT_PRECISION}): **{fmt(r['strict_best'])}**; "
                  f"current {settings.VECTOR_SEARCH_MAX_DISTANCE}: {fmt(r['strict_at_current'])}",
                  f"- expanded (label>=1 vs 0, recall>={EXPANDED_RECALL}): **{fmt(r['expanded_best'])}**; "
                  f"current {settings.VECTOR_SEARCH_MAX_DISTANCE_EXPANDED}: {fmt(r['expanded_at_current'])}", ""]
    lines += ["### Per language (same-language pairs)", "", "| lang | strict | expanded |", "|---|---|---|"]
    for lang, r in perlang.items():
        lines.append(f"| {lang} | {fmt(r['strict_best'])} | {fmt(r['expanded_best'])} |")
    return "\n".join(lines) + "\n"


async def run(out_dir, baseline):
    docs, queries = load_corpus()
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    cache = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {}
    modes = [("taskType (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY)", gemini_client.TASK_TYPE_DOCUMENT, gemini_client.TASK_TYPE_QUERY)]
    if baseline:
        modes.append(("baseline (no taskType)", None, None))
    report, raw = ["# Vector threshold calibration", "",
                   f"Corpus: {len(docs)} docs, {len(queries)} queries, langs {LANGS}. "
                   f"Model {settings.GEMINI_EMBED_MODEL}, dim {settings.VECTOR_EMBEDDING_DIM}.", ""], {}
    for title, dtask, qtask in modes:
        dvecs = await embed_all([x[0] for x in docs], dtask, cache)
        qvecs = await embed_all([q[0] for q in queries], qtask, cache)
        json.dump(cache, open(CACHE_PATH, "w"))
        d, meta = build_pairs(docs, queries, qvecs, dvecs)
        res, perlang = analyse(d, meta), per_language(docs, queries, qvecs, dvecs)
        raw[title] = {"scopes": res, "per_language": perlang}
        report.append(render(title, res, perlang))
    os.makedirs(out_dir, exist_ok=True)
    open(os.path.join(out_dir, "calibration_report.md"), "w").write("\n".join(report))
    json.dump(raw, open(os.path.join(out_dir, "calibration_report.json"), "w"), indent=1, ensure_ascii=False)
    print(f"report written to {out_dir}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--baseline", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.out_dir, a.baseline))
