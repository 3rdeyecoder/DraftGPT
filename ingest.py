"""
NFL Scout AI — Ingestion Script
Reads scraped prospect data, creates rich text documents, embeds them,
and stores in ChromaDB for retrieval.
"""

import json
import os
import sys
import chromadb
from fastembed import TextEmbedding
from typing import Dict, List


CHROMA_PATH      = "./chroma_db"
COLLECTION_NAME  = "nfl_prospects_2026"
EMBEDDING_MODEL  = "BAAI/bge-small-en-v1.5"   # 384-dim, ONNX — no torch required


def prospect_to_document(p: Dict) -> str:
    """Convert a prospect dict into a rich text document optimised for embedding."""
    lines = []

    # Header line
    name     = p.get("name", "Unknown")
    position = p.get("position", "")
    school   = p.get("school", "")
    header   = name
    if position:
        header += f" | {position}"
    if school:
        header += f" | {school}"
    lines.append(header)

    # Physical / biographical
    measurements = []
    if p.get("height"):
        measurements.append(f"Height: {p['height']}")
    if p.get("weight"):
        measurements.append(f"Weight: {p['weight']} lbs")
    if p.get("year_class"):
        measurements.append(f"Class: {p['year_class']}")
    if measurements:
        lines.append("  ".join(measurements))

    # Draft projection
    if p.get("rank"):
        rank_line = f"Overall Rank: #{p['rank']}"
        if p.get("position_rank") and p.get("position"):
            rank_line += f"  |  {p['position']} Rank: #{p['position_rank']}"
        lines.append(rank_line)
    if p.get("projected_team"):
        lines.append(f"Projected Team: {p['projected_team']}")
    if p.get("grade"):
        lines.append(f"Draft Grade: {p['grade']}")
    if p.get("projected_round"):
        lines.append(f"Projected Round: {p['projected_round']}")
    if p.get("nfl_comparison"):
        lines.append(f"NFL Comparison: {p['nfl_comparison']}")

    # NFLDraftBuzz consensus data
    nfb_parts = []
    if p.get("nfb_rating"):
        nfb_parts.append(f"NFLDraftBuzz Rating: {p['nfb_rating']}/100")
    if p.get("nfb_pos_rank"):
        nfb_parts.append(f"NFB Position Rank: #{p['nfb_pos_rank']}")
    if p.get("nfb_avg_ovr_rank"):
        nfb_parts.append(f"Consensus Overall Rank: #{p['nfb_avg_ovr_rank']}")
    if nfb_parts:
        lines.append("  |  ".join(nfb_parts))

    # Physical measurables
    phys_parts = []
    if p.get("height"):
        phys_parts.append(f"Height: {p['height']}")
    if p.get("weight"):
        phys_parts.append(f"Weight: {p['weight']}lbs")
    if p.get("forty_yard"):
        phys_parts.append(f"40yd: {p['forty_yard']}")
    if phys_parts:
        lines.append("  ".join(phys_parts))

    # NFLDraftBuzz scouting summary (consensus from multiple scouts)
    if p.get("nfb_summary"):
        lines.append(f"\nNFLDraftBuzz Scout Consensus:\n{p['nfb_summary']}")

    # Scouting strengths / weaknesses
    if p.get("strengths"):
        lines.append(f"\nStrengths: {p['strengths']}")
    if p.get("weaknesses"):
        lines.append(f"Weaknesses: {p['weaknesses']}")

    # Main scouting report (consolidated Walter Football analysis + stats)
    if p.get("scouting_report"):
        lines.append(f"\nScouting Report:\n{p['scouting_report']}")

    # Structured 2025 stats (if present as a dict)
    if isinstance(p.get("stats_2025"), dict) and p["stats_2025"]:
        stat_lines = [f"{k}: {v}" for k, v in p["stats_2025"].items() if v]
        lines.append("\n2025 Season Stats:\n" + "\n".join(stat_lines))

    return "\n".join(lines)


def build_metadata(p: Dict) -> Dict:
    """Extract flat, ChromaDB-safe metadata fields."""
    rank = p.get("rank")
    try:
        rank_int = int(rank) if rank is not None else 0
    except (ValueError, TypeError):
        rank_int = 0

    pos_rank = p.get("position_rank")
    try:
        pos_rank_int = int(pos_rank) if pos_rank is not None else 0
    except (ValueError, TypeError):
        pos_rank_int = 0

    try:
        nfb_rating_f = float(p.get("nfb_rating") or 0)
    except (ValueError, TypeError):
        nfb_rating_f = 0.0

    try:
        pick_int = int(p.get("pick_number") or 0)
    except (ValueError, TypeError):
        pick_int = 0

    return {
        "name":           str(p.get("name", "")),
        "position":       str(p.get("position", "")),
        "school":         str(p.get("school", "")),
        "projected_team": str(p.get("projected_team", "")),
        "rank":           rank_int,
        "position_rank":  pos_rank_int,
        "pick_number":    pick_int,
        "grade":          str(p.get("grade", "")),
        "projected_round":str(p.get("projected_round", "")),
        "nfl_comparison": str(p.get("nfl_comparison", "")),
        "nfb_rating":     nfb_rating_f,
        "nfb_pos_rank":   str(p.get("nfb_pos_rank", "")),
        "has_stats":      "yes" if p.get("stats_2025") else "no",
        "source":         str(p.get("source", "")),
    }


def ingest(prospects_path: str = "data/prospects.json") -> None:
    if not os.path.exists(prospects_path):
        print(f"❌  {prospects_path} not found. Run scraper.py first.")
        sys.exit(1)

    with open(prospects_path) as f:
        prospects = json.load(f)

    print(f"📥  Loaded {len(prospects)} prospects from {prospects_path}")

    # Stats summary
    with_reports = sum(1 for p in prospects if p.get("scouting_report"))
    with_stats   = sum(1 for p in prospects if p.get("stats_2025"))
    print(f"     • {with_reports} have scouting report text")
    print(f"     • {with_stats} have 2025 cfbstats enrichment")

    print(f"\n🧠  Loading fastembed model: {EMBEDDING_MODEL} ...")
    embedder = TextEmbedding(EMBEDDING_MODEL)

    print(f"💾  Connecting to ChromaDB at {CHROMA_PATH} ...")
    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # Drop existing collection for a clean rebuild
    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"    Cleared existing collection '{COLLECTION_NAME}'")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    batch_size = 32
    total = len(prospects)

    for i in range(0, total, batch_size):
        batch = prospects[i : i + batch_size]
        ids        = []
        documents  = []
        metadatas  = []

        for j, prospect in enumerate(batch):
            doc = prospect_to_document(prospect)
            ids.append(f"prospect_{i + j}")
            documents.append(doc)
            metadatas.append(build_metadata(prospect))

        embeddings = [e.tolist() for e in embedder.embed(documents)]

        collection.add(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        print(f"    Embedded {min(i + batch_size, total)}/{total} prospects...")

    print(f"\n✅  Ingested {total} prospects into ChromaDB ('{COLLECTION_NAME}')")


if __name__ == "__main__":
    ingest()
