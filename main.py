"""
NFL Scout AI - FastAPI Backend
RAG chatbot for 2026 NFL draft prospect scouting analysis.
"""

import os
import re
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Tuple

import chromadb
import anthropic
from fastembed import TextEmbedding
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

CHROMA_PATH      = "./chroma_db"
COLLECTION_NAME  = "nfl_prospects_2026"
EMBEDDING_MODEL  = "BAAI/bge-small-en-v1.5"
N_RESULTS_GENERAL = 32   # top prospects returned for general queries
MIN_RESULTS       = 10   # floor — never send fewer than this to Claude

# Global state initialized at startup
_embedder:  TextEmbedding        = None
_collection                      = None
_anthropic: anthropic.Anthropic  = None


# ── Position keyword → ChromaDB position codes ─────────────────────────────

# Each entry: (list-of-query-keywords, list-of-db-position-codes)
# Each entry: (list-of-regex-patterns, list-of-db-position-codes)
# Patterns are matched as whole words/phrases (word boundaries via \b or explicit spacing).
_POSITION_RULES: List[Tuple[List[str], List[str]]] = [
    ([r"quarter\s*back", r"\bqbs?\b", r"signal caller", r"under center"],
     ["QB"]),
    ([r"wide\s*receiver", r"\bwrs?\b", r"wideout", r"\breceiver", r"pass catcher"],
     ["WR"]),
    ([r"running\s*back", r"\brbs?\b", r"tailback", r"halfback", r"ball carrier"],
     ["RB"]),
    ([r"tight\s*end", r"\btes?\b"],
     ["TE"]),
    ([r"offensive\s*tackle", r"\bots?\b", r"blindside", r"left tackle", r"right tackle",
      r"tackle prospect"],
     ["OT", "OT/G"]),
    ([r"offensive\s*guard", r"\bogs?\b", r"interior\s*lineman", r"\bguards?\b",
      r"interior\s*offensive\s*line", r"\biols?\b"],
     ["G", "OG", "OT/G", "C"]),
    ([r"\bcenters?\b"],
     ["C"]),
    ([r"edge\s*rusher", r"edge\s*defender", r"pass\s*rusher", r"\bedge\b",
      r"outside\s*linebacker", r"\bolbs?\b", r"speed\s*rusher"],
     ["DE", "OLB"]),
    ([r"defensive\s*end", r"\bdes?\b"],
     ["DE", "OLB"]),
    ([r"defensive\s*tackle", r"\bdts?\b", r"nose\s*tackle", r"interior\s*defender",
      r"defensive\s*interior", r"d-lineman"],
     ["DT"]),
    ([r"\blinebackers?\b", r"\blbs?\b", r"inside\s*linebacker", r"\bilbs?\b"],
     ["LB", "OLB"]),
    ([r"cornerbacks?\b", r"\bcbs?\b", r"\bcorners?\b"],
     ["CB", "CB/S"]),
    ([r"\bsafeties\b", r"\bsafety\b", r"\bss\b", r"\bfs\b"],
     ["S", "CB/S"]),
    ([r"defensive\s*back", r"\bdbs?\b"],
     ["CB", "S", "CB/S"]),
    ([r"\bkickers?\b"],
     ["K"]),
    ([r"\bpunters?\b"],
     ["P"]),
    ([r"offensive\s*lineman", r"offensive\s*line", r"\bols?\b"],
     ["OT", "G", "OG", "C", "OT/G"]),
    ([r"defensive\s*lineman", r"defensive\s*line", r"\bdls?\b"],
     ["DE", "DT", "OLB"]),
]


def detect_position_filter(query: str) -> Optional[List[str]]:
    """
    Return a list of ChromaDB position codes to filter on, or None if the
    query doesn't clearly target a specific position group.
    Uses word-boundary regex so 'rbs', 'qbs', 'wrs' etc. all match correctly.
    """
    q = query.lower()
    for patterns, codes in _POSITION_RULES:
        if any(re.search(pat, q) for pat in patterns):
            return codes
    return None


# ── Context builder ─────────────────────────────────────────────────────────

def build_context(docs: List[str], metas: List[Dict]) -> str:
    """
    Build the context string sent to Claude, with rank info prominently at
    the top of each prospect block.
    """
    blocks = []
    for doc, meta in zip(docs, metas):
        overall_rank  = meta.get("rank") or "?"
        pos_rank      = meta.get("position_rank") or "?"
        position      = meta.get("position", "")
        name          = meta.get("name", "Unknown")
        school        = meta.get("school", "")
        proj_team     = meta.get("projected_team", "")
        proj_round    = meta.get("projected_round", "")
        pick_number   = meta.get("pick_number") or ""

        header_parts = [f"── {name} | {position} | {school}"]
        nfb_rating   = meta.get("nfb_rating") or ""
        nfb_pos_rank = meta.get("nfb_pos_rank") or ""

        rank_line    = f"   Overall rank: #{overall_rank}"
        if pos_rank != "?" and position:
            rank_line += f"  |  {position} rank: #{pos_rank}"
        if nfb_pos_rank:
            rank_line += f"  |  NFB {position} rank: #{nfb_pos_rank}"
        if nfb_rating:
            rank_line += f"  |  NFB rating: {nfb_rating}/100"
        if proj_team:
            pick_str = f" (pick #{pick_number})" if pick_number else ""
            rank_line += f"  |  Projected: {proj_team}{pick_str}"
        if proj_round and not proj_team:
            rank_line += f"  |  Projected round: {proj_round}"

        block = "\n".join([header_parts[0], rank_line, "", doc])
        blocks.append(block)

    return ("\n" + "=" * 64 + "\n").join(blocks)


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are NFL Scout AI, an elite draft analyst specializing exclusively in the **2026 NFL Draft class**.

CRITICAL RULES — follow without exception:
1. ONLY discuss players present in the RETRIEVED PROSPECT DATA provided with each question. Never mention or invent players outside that data.
2. Every player in the database is a confirmed **2026 draft prospect** who played college football in the 2025 season. Do not reference the 2025 NFL Draft (already held in April 2025) or any prior class.
3. If a player is not in the retrieved data, say: "I don't have that player in my 2026 database."
4. NEVER fill gaps with general training knowledge about players. Use only the provided scouting documents.

RANKING RULES — always reference numbers:
- Each prospect has an **Overall rank** (their position in the full 2026 big board) and a **Position rank** (their rank among players at the same position).
- When answering ranking questions ("best", "top", "who should go first"), lead with these numbers: e.g. "David Bailey is the #1 DE in this class (overall #6)."
- When listing multiple prospects, present them in rank order and include both rank numbers for each.

ANALYSIS FORMAT:
- Lead with rank, then the key scouting insight
- Use professional scouting language (burst, bend, hip flexibility, processing speed, hand technique, leverage, motor, etc.)
- Reference specific traits and stats from the provided data
- Discuss scheme fit (4-3, 3-4, zone, man, gap vs. zone run schemes, etc.)
- Address strengths AND weaknesses honestly
- Give a draft value assessment (round projection, best-case/worst-case)
- For comparisons, give a concrete recommendation with reasoning

Be direct and specific — like a scout presenting at a draft board meeting."""


# ── FastAPI setup ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _embedder, _collection, _anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set.")

    print("Loading fastembed model...")
    _embedder = TextEmbedding(EMBEDDING_MODEL)

    print("Connecting to ChromaDB...")
    if not os.path.exists(CHROMA_PATH) or not os.listdir(CHROMA_PATH):
        print("chroma_db missing — running ingest...")
        from ingest import ingest
        ingest()
    chroma = chromadb.PersistentClient(path=CHROMA_PATH)
    _collection = chroma.get_collection(COLLECTION_NAME)
    print(f"Loaded collection with {_collection.count()} prospects.")

    _anthropic = anthropic.Anthropic(api_key=api_key)
    print("NFL Scout AI is ready.\n")
    yield


app = FastAPI(title="NFL Scout AI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []


class ChatResponse(BaseModel):
    response: str
    sources: List[str]


# ── Chat endpoint ────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    pos_filter = detect_position_filter(req.message)

    docs: List[str]  = []
    metas: List[Dict] = []

    if pos_filter:
        # Fetch ALL prospects at that position via metadata filter (no semantic limit)
        try:
            where_clause = (
                {"position": pos_filter[0]}
                if len(pos_filter) == 1
                else {"position": {"$in": pos_filter}}
            )
            results = _collection.get(
                where=where_clause,
                include=["documents", "metadatas"],
            )
            docs  = results["documents"]
            metas = results["metadatas"]
        except Exception:
            pass  # fall through to general query if filter fails

    if not docs:
        # General query — semantic search via fastembed
        query_embedding = next(_embedder.query_embed(req.message)).tolist()
        results = _collection.query(
            query_embeddings=[query_embedding],
            n_results=N_RESULTS_GENERAL,
            include=["documents", "metadatas"],
        )
        docs  = results["documents"][0]
        metas = results["metadatas"][0]

    if not docs:
        raise HTTPException(status_code=404, detail="No prospects found in database.")

    # Sort by position_rank for position queries, overall rank for general
    if pos_filter:
        paired = sorted(
            zip(docs, metas),
            key=lambda x: x[1].get("position_rank") or 9999,
        )
    else:
        paired = sorted(
            zip(docs, metas),
            key=lambda x: x[1].get("rank") or 9999,
        )

    docs  = [d for d, _ in paired]
    metas = [m for _, m in paired]

    # Enforce minimum result floor
    if len(docs) < MIN_RESULTS:
        extra_results = _collection.get(include=["documents", "metadatas"])
        seen = {m.get("name") for m in metas}
        extra_paired = sorted(
            zip(extra_results["documents"], extra_results["metadatas"]),
            key=lambda x: x[1].get("rank") or 9999,
        )
        for d, m in extra_paired:
            if m.get("name") not in seen:
                docs.append(d)
                metas.append(m)
            if len(docs) >= MIN_RESULTS:
                break

    context = build_context(docs, metas)

    # Build conversation history
    messages = []
    for msg in req.history[-16:]:
        messages.append({"role": msg.role, "content": msg.content})

    user_content = (
        "Use ONLY the following 2026 NFL draft prospect scouting data to answer the question. "
        "All players listed are confirmed 2026 draft prospects. "
        "Always reference their Overall rank and Position rank numbers in your answer.\n\n"
        f"RETRIEVED 2026 PROSPECT DATA:\n{context}\n\n"
        f"QUESTION: {req.message}"
    )
    messages.append({"role": "user", "content": user_content})

    response = _anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=messages,
    )

    answer  = response.content[0].text
    sources = sorted({m.get("name", "") for m in metas if m.get("name")})

    return ChatResponse(response=answer, sources=sources)


# ── Other endpoints ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html") as f:
        return HTMLResponse(f.read())


@app.get("/prospects")
async def list_prospects():
    results = _collection.get(include=["metadatas"])
    prospects = sorted(
        results["metadatas"],
        key=lambda x: x.get("rank") or 9999,
    )
    return {"count": len(prospects), "prospects": prospects}


@app.get("/health")
async def health():
    return {"status": "ok", "prospects_in_db": _collection.count()}
